"""Vectorised Pine interpreter: AST -> aligned ``pandas.Series``.

Evaluation model
----------------
Each statement/expression is evaluated once over the *entire* bar series.
``close`` is a ``Series``; ``ta.sma(close, 20)`` is a ``Series``; ``x := y``
rebinds ``x`` to a new ``Series``. ``if`` is executed by evaluating the body and
merging each assignment through the condition mask, i.e. ``x = x.where(~cond, new)``
which matches Pine's "value carries over from the previous bar" semantics for
same-bar branches.

Signals
-------
``strategy.entry`` / ``strategy.close`` calls record boolean masks and prices.
The evaluator returns a :class:`PineResult` containing:

* ``plots``  — every ``plot()`` series, keyed by title
* ``signals``— aggregated entry/exit masks with direction
* ``logs``   — everything the script tried to do that we do not render
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from nanobot.trading.pine import indicators as ind
from nanobot.trading.pine.parser import (
    Assign,
    Binary,
    Call,
    ExprStmt,
    For,
    FuncDef,
    History,
    If,
    Literal,
    Member,
    Name,
    Node,
    Program,
    Ternary,
    TupleAssign,
    TupleLit,
    Unary,
    parse,
)

MAX_LOOP_ITERATIONS = 10_000

# Calls whose *value* is not used by the script; their series is captured by
# the side-effect handler instead.
_PLOT_CALLS = frozenset({
    "plot", "plotshape", "plotchar", "plotarrow", "plotcandle", "plotbar",
})


class PineRuntimeError(Exception):
    """Raised when evaluation fails (unknown identifier, bad arity, ...)."""

    def __init__(self, message: str, line: int = 0) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


# --------------------------------------------------------------------------
# result containers
# --------------------------------------------------------------------------


@dataclass
class PineResult:
    plots: dict[str, pd.Series] = field(default_factory=dict)
    """Series produced by ``plot()`` / ``plotshape()``, keyed by title."""

    plot_colors: dict[str, str] = field(default_factory=dict)
    plot_styles: dict[str, str] = field(default_factory=dict)

    entries_long: pd.Series | None = None
    entries_short: pd.Series | None = None
    exits: pd.Series | None = None

    entry_prices: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    variables: dict[str, Any] = field(default_factory=dict)

    @property
    def any_signal(self) -> pd.Series:
        idx = None
        for series in (self.entries_long, self.entries_short, self.exits):
            if series is not None:
                idx = series.index
                break
        if idx is None:
            return pd.Series(dtype=bool)
        base = pd.Series(False, index=idx)
        for series in (self.entries_long, self.entries_short, self.exits):
            if series is not None:
                base = base | series.fillna(False).astype(bool)
        return base

    def signal_count(self) -> int:
        return int(self.any_signal.sum())

    def signal_indices(self, limit: int = 0) -> list[int]:
        mask = self.any_signal.to_numpy()
        positions = np.flatnonzero(mask)
        if limit:
            positions = positions[-limit:]
        return [int(p) for p in positions]


# --------------------------------------------------------------------------
# evaluation context
# --------------------------------------------------------------------------


class _Scope:
    def __init__(self, parent: "_Scope | None" = None) -> None:
        self.parent = parent
        self.vars: dict[str, Any] = {}
        self.types: dict[str, str] = {}

    def get(self, name: str) -> Any:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.vars:
                return scope.vars[name]
            scope = scope.parent
        raise KeyError(name)

    def has(self, name: str) -> bool:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.vars:
                return True
            scope = scope.parent
        return False

    def set(self, name: str, value: Any, is_new: bool = False) -> None:
        """Assign in the nearest scope that already defines *name* (Pine semantics)."""
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.vars:
                scope.vars[name] = value
                return
            scope = scope.parent
        self.vars[name] = value
        if is_new:
            return


# --------------------------------------------------------------------------
# evaluator
# --------------------------------------------------------------------------


class PineEvaluator:
    def __init__(self, data: pd.DataFrame, *, symbol: str = "", timeframe: str = "1d") -> None:
        self.data = data
        self.index = data.index
        self.symbol = symbol
        self.timeframe = timeframe
        self.result = PineResult()
        self.functions: dict[str, FuncDef] = {}
        self.global_scope = _Scope()
        self._install_builtins()
        self._install_series(data)
        self._loop_depth = 0
        self._warned: set[str] = set()
        # Enclosing `if` condition masks; side effects AND against these so a
        # `strategy.entry` inside `if cond` only fires where cond is true.
        self._cond_stack: list[pd.Series] = []

    # -- setup ------------------------------------------------------------
    def _install_series(self, data: pd.DataFrame) -> None:
        g = self.global_scope
        close = data["close"].astype("float64")
        g.vars["close"] = close
        g.vars["open"] = data["open"].astype("float64")
        g.vars["high"] = data["high"].astype("float64")
        g.vars["low"] = data["low"].astype("float64")
        g.vars["volume"] = data["volume"].astype("float64") if "volume" in data else pd.Series(0.0, index=self.index)
        g.vars["hl2"] = (g.vars["high"] + g.vars["low"]) / 2
        g.vars["hlc3"] = (g.vars["high"] + g.vars["low"] + close) / 3
        g.vars["ohlc4"] = (g.vars["open"] + g.vars["high"] + g.vars["low"] + close) / 4
        g.vars["hlcc4"] = (g.vars["high"] + g.vars["low"] + 2 * close) / 4
        g.vars["bar_index"] = pd.Series(np.arange(len(self.index), dtype="float64"), index=self.index)
        g.vars["last_bar_index"] = float(max(0, len(self.index) - 1))
        g.vars["time"] = pd.Series(self.index.view("int64") // 10**9, index=self.index, dtype="float64")
        g.vars["time_close"] = g.vars["time"]
        g.vars["na"] = float("nan")
        g.vars["true"] = True
        g.vars["false"] = False
        g.vars["strategy.long"] = 1
        g.vars["strategy.short"] = -1
        g.vars["strategy.equity"] = 1.0

    def _install_builtins(self) -> None:
        g = self.global_scope
        # ta.*
        g.vars["ta.sma"] = ind.sma
        g.vars["ta.ema"] = ind.ema
        g.vars["ta.rma"] = ind.rma
        g.vars["ta.wma"] = ind.wma
        g.vars["ta.hma"] = ind.hma
        g.vars["ta.vwma"] = ind.vwma
        g.vars["ta.swma"] = ind.swma
        g.vars["ta.tr"] = ind.true_range
        g.vars["ta.atr"] = ind.atr
        g.vars["ta.rsi"] = ind.rsi
        g.vars["ta.macd"] = ind.macd
        g.vars["ta.bb"] = ind.bb
        g.vars["ta.bbw"] = _bbw
        g.vars["ta.stoch"] = ind.stoch
        g.vars["ta.cci"] = ind.cci
        g.vars["ta.mfi"] = ind.mfi
        g.vars["ta.roc"] = ind.roc
        g.vars["ta.mom"] = ind.mom
        g.vars["ta.change"] = ind.change
        g.vars["ta.highest"] = ind.highest
        g.vars["ta.lowest"] = ind.lowest
        g.vars["ta.highestbars"] = ind.highestbars
        g.vars["ta.lowestbars"] = ind.lowestbars
        g.vars["ta.sum"] = ind.sum_
        g.vars["ta.median"] = ind.median
        g.vars["ta.percentile_linear_interpolation"] = ind.percentile
        g.vars["ta.percentile_nearest_rank"] = ind.percentile
        g.vars["ta.linreg"] = ind.linreg
        g.vars["ta.correlation"] = ind.correlation
        g.vars["ta.barssince"] = ind.barssince
        g.vars["ta.valuewhen"] = ind.valuewhen
        g.vars["ta.crossover"] = ind.crossover
        g.vars["ta.crossunder"] = ind.crossunder
        g.vars["ta.cross"] = ind.cross
        g.vars["ta.rising"] = ind.rising
        g.vars["ta.falling"] = ind.falling
        g.vars["ta.cum"] = ind.cum
        g.vars["ta.vwap"] = ind.vwap
        g.vars["ta.stdev"] = ind.stdev
        g.vars["ta.dev"] = ind.dev
        g.vars["ta.variance"] = ind.variance
        g.vars["ta.cmo"] = _cmo
        g.vars["ta.roc_percent"] = ind.roc

        # math.*
        g.vars["math.abs"] = ind.math_abs
        g.vars["math.max"] = ind.math_max
        g.vars["math.min"] = ind.math_min
        g.vars["math.round"] = ind.math_round
        g.vars["math.floor"] = ind.math_floor
        g.vars["math.ceil"] = ind.math_ceil
        g.vars["math.sqrt"] = ind.math_sqrt
        g.vars["math.pow"] = ind.math_pow
        g.vars["math.log"] = ind.math_log
        g.vars["math.log10"] = lambda x: ind.math_log(x) / np.log(10) if isinstance(x, pd.Series) else np.log10(x)
        g.vars["math.avg"] = ind.math_avg
        g.vars["math.sign"] = ind.math_sign
        g.vars["math.exp"] = ind.math_exp
        g.vars["math.isna"] = ind.math_na
        g.vars["math.na"] = ind.math_na
        g.vars["math.isfinite"] = ind.math_isfinite
        g.vars["math.todegrees"] = lambda x: np.degrees(x) if isinstance(x, pd.Series) else float(np.degrees(x))
        g.vars["math.toradians"] = lambda x: np.radians(x) if isinstance(x, pd.Series) else float(np.radians(x))
        g.vars["math.pi"] = float(np.pi)
        g.vars["math.e"] = float(np.e)
        g.vars["nz"] = ind.nz
        g.vars["na"] = ind.math_na

        # barstate.* — we always evaluate on the last (closed) bar of a series,
        # so these are series-aware scalar flags.
        ones = pd.Series(1.0, index=self.index)
        zeros = pd.Series(0.0, index=self.index)
        last = zeros.copy()
        if len(last):
            last.iloc[-1] = 1.0
        g.vars["barstate.isfirst"] = _flag(self.index, 0)
        g.vars["barstate.islast"] = last.astype(bool)
        g.vars["barstate.isnew"] = _flag(self.index, -1)
        g.vars["barstate.isconfirmed"] = _flag(self.index, -1)
        g.vars["barstate.isrealtime"] = zeros.astype(bool)
        g.vars["barstate.ishistory"] = ones.astype(bool)
        _ = (ones, zeros)

        # color.* / shape.* / location.* / size.* enums are string constants
        for ns, members in (
            ("color", ("yellow", "blue", "purple", "green", "red", "orange", "white",
                       "black", "gray", "silver", "lime", "aqua", "fuchsia", "maroon",
                       "navy", "olive", "teal", "new", "na")),
            ("shape", ("circle", "triangleup", "triangledown", "square", "diamond",
                       "xcross", "cross", "flag", "arrowup", "arrowdown", "labelup",
                       "labeldown")),
            ("location", ("abovebar", "belowbar", "top", "bottom", "absolute")),
            ("size", ("tiny", "small", "normal", "large", "huge", "auto")),
            ("plot", ("style_line", "style_stepline", "style_histogram", "style_cross",
                      "style_area", "style_columns", "style_circles", "style_stepline_diamond")),
            ("extend", ("none", "left", "right", "both")),
            ("display", ("none", "all", "price_scale", "pane", "data_window", "status_line")),
            ("math", ("pi", "e")),
            ("strategy", ("long", "short", "percent_of_equity", "fixed", "cash",
                          "commission_cash_per_order", "commission_cash_per_contract",
                          "commission_percent", "slippage", "oca_none")),
            ("syminfo", ("tickerid", "currency", "mintick", "pointvalue", "type", "root",
                         "session", "timezone", "basecurrency", "country")),
            ("timeframe", ("period", "isdaily", "isweekly", "ismonthly", "isintraday",
                           "isminutes", "isseconds", "inseconds", "multiplier")),
            ("barmerge", ("gaps_off", "gaps_on", "lookahead_off", "lookahead_on")),
            ("format", ("price", "volume", "percent", "mintick", "inherit")),
        ):
            for member in members:
                g.vars[f"{ns}.{member}"] = member

    # -- public API -------------------------------------------------------
    def run(self, program: Program) -> PineResult:
        scope = self.global_scope
        # Two passes: collect function definitions first so calls resolve.
        for stmt in program.statements:
            if isinstance(stmt, FuncDef):
                self.functions[stmt.name] = stmt
        for stmt in program.statements:
            if isinstance(stmt, FuncDef):
                continue
            self.exec_stmt(stmt, scope)
        self.result.variables = {
            k: v for k, v in scope.vars.items() if isinstance(v, pd.Series)
        }
        return self.result

    # -- statements -------------------------------------------------------
    def exec_stmt(self, stmt: Node, scope: _Scope) -> None:
        if isinstance(stmt, ExprStmt):
            value = self.eval(stmt.expr, scope)
            if isinstance(stmt.expr, Call):
                self._handle_side_effect(stmt.expr, value)
            return

        if isinstance(stmt, Assign):
            self._exec_assign(stmt, scope)
            return

        if isinstance(stmt, TupleAssign):
            value = self.eval(stmt.expr, scope)
            parts = value if isinstance(value, tuple) else (value,)
            for idx, target in enumerate(stmt.targets):
                scope.set(target, parts[idx] if idx < len(parts) else float("nan"))
            return

        if isinstance(stmt, If):
            self.exec_if(stmt, scope)
            return

        if isinstance(stmt, For):
            self.exec_for(stmt, scope)
            return

        if isinstance(stmt, FuncDef):
            self.functions[stmt.name] = stmt
            return

        raise PineRuntimeError(f"unsupported statement {type(stmt).__name__}", stmt.line)

    def _exec_assign(self, stmt: Assign, scope: _Scope) -> None:
        value = self.eval(stmt.expr, scope)
        target = stmt.target
        if stmt.var and not scope.has(target):
            # 'var' keeps its initial value; for series evaluation that means
            # the value is committed on the first bar and carried forward.
            value = _carry_forward(value, self.index)
        if stmt.op != "=" and stmt.op != ":=":
            try:
                current = scope.get(target)
            except KeyError as exc:
                raise PineRuntimeError(f"undefined variable {target!r} in {stmt.op}", stmt.line) from exc
            op = stmt.op[0]
            if op == "+":
                value = current + value
            elif op == "-":
                value = current - value
            elif op == "*":
                value = current * value
            elif op == "/":
                value = current / value
            elif op == "%":
                value = current % value
        scope.set(target, value, is_new=stmt.declare)
        if stmt.type_name == "bool" and isinstance(value, pd.Series) and value.dtype != bool:
            scope.set(target, value.fillna(False).astype(bool))
        self._record_declaration(stmt, value)

    def _record_declaration(self, stmt: Assign, value: Any) -> None:
        if isinstance(value, pd.Series) and stmt.target not in self.result.variables:
            pass  # tracked at the end of run()

    def exec_if(self, stmt: If, scope: _Scope) -> None:
        cond = _as_bool(self.eval(stmt.cond, scope), self.index)
        body_scope = _Scope(scope)
        before = dict(scope.vars)
        # Side effects inside the branch must only fire where the condition holds.
        self._cond_stack.append(cond)
        try:
            for sub in stmt.body:
                self.exec_stmt(sub, body_scope)
        finally:
            self._cond_stack.pop()
        after = dict(body_scope.vars)
        for key, new_value in after.items():
            if key in before and _series_equal(before[key], new_value):
                continue
            old = before.get(key, _nan_like(new_value, self.index))
            scope.set(key, _merge_masked(old, new_value, cond))
        if stmt.orelse:
            else_scope = _Scope(scope)
            taken = ~cond
            self._cond_stack.append(taken)
            try:
                for sub in stmt.orelse:
                    self.exec_stmt(sub, else_scope)
            finally:
                self._cond_stack.pop()
            else_after = dict(else_scope.vars)
            for key, new_value in else_after.items():
                if key in before and _series_equal(before[key], new_value):
                    continue
                current = scope.get(key)
                scope.set(key, _merge_masked(current, new_value, taken))

    def exec_for(self, stmt: For, scope: _Scope) -> None:
        start = int(ind._n(self.eval(stmt.start, scope), 0))
        end = int(ind._n(self.eval(stmt.end, scope), 0))
        step = int(ind._n(self.eval(stmt.step, scope), 1)) if stmt.step is not None else 1
        if step == 0:
            raise PineRuntimeError("for loop step cannot be 0", stmt.line)
        iterations = 0
        value = start
        while (step > 0 and value <= end) or (step < 0 and value >= end):
            iterations += 1
            if iterations > MAX_LOOP_ITERATIONS:
                raise PineRuntimeError(
                    f"for loop exceeded {MAX_LOOP_ITERATIONS} iterations", stmt.line
                )
            loop_scope = _Scope(scope)
            loop_scope.vars[stmt.var] = value
            for sub in stmt.body:
                self.exec_stmt(sub, loop_scope)
            value += step

    # -- expressions ------------------------------------------------------
    def eval(self, node: Node, scope: _Scope) -> Any:
        if isinstance(node, Literal):
            return _broadcast(node.value, self.index)

        if isinstance(node, Name):
            try:
                return scope.get(node.name)
            except KeyError as exc:
                raise PineRuntimeError(f"undefined identifier {node.name!r}", node.line) from exc

        if isinstance(node, Member):
            dotted = _dotted(node)
            if dotted and scope.has(dotted):
                return scope.get(dotted)
            try:
                return scope.get(dotted)
            except KeyError as exc:
                # e.g. bare enum member `barstate.islast` handled above
                raise PineRuntimeError(f"undefined identifier {dotted!r}", node.line) from exc

        if isinstance(node, History):
            base = self.eval(node.obj, scope)
            offset = int(ind._n(self.eval(node.offset, scope), 0))
            if offset == 0:
                return base
            if isinstance(base, pd.Series):
                return base.shift(offset)
            return _broadcast(base, self.index).shift(offset)

        if isinstance(node, TupleLit):
            return tuple(self.eval(item, scope) for item in node.items)

        if isinstance(node, Unary):
            operand = self.eval(node.operand, scope)
            if node.op == "-":
                return -operand
            if node.op == "+":
                return operand
            return ~_as_bool(operand, self.index)

        if isinstance(node, Binary):
            return self.eval_binary(node, scope)

        if isinstance(node, Ternary):
            cond = _as_bool(self.eval(node.cond, scope), self.index)
            if_true = self.eval(node.if_true, scope)
            if_false = self.eval(node.if_false, scope)
            return _merge_masked(if_false, if_true, cond)

        if isinstance(node, Call):
            return self.eval_call(node, scope)

        raise PineRuntimeError(f"unsupported expression {type(node).__name__}", node.line)

    def eval_binary(self, node: Binary, scope: _Scope) -> Any:
        op = node.op
        if op == "and":
            return _as_bool(self.eval(node.left, scope), self.index) & _as_bool(
                self.eval(node.right, scope), self.index
            )
        if op == "or":
            return _as_bool(self.eval(node.left, scope), self.index) | _as_bool(
                self.eval(node.right, scope), self.index
            )
        left = self.eval(node.left, scope)
        right = self.eval(node.right, scope)
        if op == "+":
            return _binop(left, right, lambda a, b: a + b, self.index)
        if op == "-":
            return _binop(left, right, lambda a, b: a - b, self.index)
        if op == "*":
            return _binop(left, right, lambda a, b: a * b, self.index)
        if op == "/":
            return _div(left, right, self.index)
        if op == "%":
            return _binop(left, right, lambda a, b: np.mod(a, b), self.index)
        if op == "==":
            return _compare(left, right, "eq", self.index)
        if op == "!=":
            return _compare(left, right, "ne", self.index)
        if op == ">":
            return _compare(left, right, "gt", self.index)
        if op == ">=":
            return _compare(left, right, "ge", self.index)
        if op == "<":
            return _compare(left, right, "lt", self.index)
        if op == "<=":
            return _compare(left, right, "le", self.index)
        raise PineRuntimeError(f"unsupported operator {op!r}", node.line)

    def eval_call(self, node: Call, scope: _Scope) -> Any:
        callee = node.callee()

        # user-defined functions
        if callee in self.functions and not scope.has(callee):
            return self.call_user_function(callee, node, scope)

        # script header + inputs are declarations, not value-producing calls
        if callee in {"indicator", "strategy", "study"}:
            return float("nan")

        if callee.startswith("input.") or callee == "input":
            return self._input_call(callee, node, scope)

        # plotting calls: the value is produced by the side-effect handler
        if callee in _PLOT_CALLS:
            return float("nan")

        if callee in {"bgcolor", "barcolor", "fill", "hline", "alert", "alertcondition"}:
            return float("nan")

        # label/line/box/table constructors return an opaque handle
        if callee.split(".", 1)[0] in {"label", "line", "box", "table", "array", "matrix", "map"}:
            return float("nan")

        # strategy / indicator side-effect namespaces we intercept
        if callee.startswith("strategy."):
            return self._strategy_call(callee, node, scope)

        builtin = None
        if scope.has(callee):
            builtin = scope.get(callee)
        if builtin is None:
            raise PineRuntimeError(f"unknown function {callee!r}", node.line)

        args = [self.eval(a, scope) for a in node.args]
        # Resolve keyword arguments to positional where the builtin expects them.
        if node.kwargs:
            args.extend(self.eval(v, scope) for v in node.kwargs.values())
        try:
            return builtin(*args)
        except TypeError as exc:
            raise PineRuntimeError(
                f"bad arguments for {callee}: {exc}", node.line
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise PineRuntimeError(f"{callee} failed: {exc}", node.line) from exc

    def call_user_function(self, name: str, node: Call, scope: _Scope) -> Any:
        func = self.functions[name]
        args = [self.eval(a, scope) for a in node.args]
        call_scope = _Scope(scope)
        if len(args) > len(func.params):
            raise PineRuntimeError(
                f"{name}() expects {len(func.params)} arguments, got {len(args)}", node.line
            )
        for idx, param in enumerate(func.params):
            call_scope.vars[param] = args[idx] if idx < len(args) else float("nan")
        for key, val_node in node.kwargs.items():
            if key in func.params:
                call_scope.vars[key] = self.eval(val_node, scope)
        if func.expr is not None:
            return self.eval(func.expr, call_scope)
        result: Any = float("nan")
        for sub in func.body:
            if isinstance(sub, ExprStmt):
                result = self.eval(sub.expr, call_scope)
            else:
                self.exec_stmt(sub, call_scope)
        return result

    # -- plotting / strategy side effects ---------------------------------
    def _handle_side_effect(self, call: Call, value: Any) -> None:
        callee = call.callee()
        scope = self.global_scope

        if callee in {"plot", "plotshape", "plotchar", "plotarrow", "plotcandle", "plotbar"}:
            title = self._call_kwarg(call, scope, "title", default=None)
            series_value = value
            if callee == "plot" and call.args:
                series_value = self.eval(call.args[0], scope)
            elif callee == "plotshape":
                cond = self.eval(call.args[0], scope) if call.args else value
                series_value = _as_bool(cond, self.index).astype(float)
            if not isinstance(series_value, pd.Series):
                series_value = _broadcast(series_value, self.index)
            key = str(title) if title else f"{callee}_{len(self.result.plots) + 1}"
            self.result.plots[key] = series_value
            color = self._call_kwarg(call, scope, "color", default=None)
            if isinstance(color, str):
                self.result.plot_colors[key] = color
            style = self._call_kwarg(call, scope, "style", default=None)
            if isinstance(style, str):
                self.result.plot_styles[key] = style
            return

        if callee in {"alert", "alertcondition", "bgcolor", "fill", "barcolor", "hline", "table.new"}:
            if callee not in self._warned:
                self._warned.add(callee)
                self.result.notes.append(f"{callee}() accepted but not rendered")
            return

        if callee.startswith("label.") or callee.startswith("line.") or callee.startswith("box."):
            return

    def _call_kwarg(self, call: Call, scope: _Scope, name: str, default: Any = None) -> Any:
        node = call.kwargs.get(name)
        if node is None:
            return default
        try:
            value = self.eval(node, scope)
        except PineRuntimeError:
            return default
        if isinstance(value, pd.Series):
            if value.empty:
                return default
            last = value.iloc[-1]
            return None if pd.isna(last) else last
        return value

    def _input_call(self, callee: str, node: Call, scope: _Scope) -> Any:
        """``input.*`` returns its declared default value (first positional arg)."""
        if node.args:
            value = self.eval(node.args[0], scope)
            if isinstance(value, pd.Series):
                return float(value.iloc[-1])
            return value
        default = node.kwargs.get("defval")
        if default is not None:
            value = self.eval(default, scope)
            if isinstance(value, pd.Series):
                return float(value.iloc[-1])
            return value
        return float("nan")

    def _apply_conditions(self, mask: pd.Series) -> pd.Series:
        """AND *mask* with every enclosing `if` condition on the stack."""
        out = mask.reindex(self.index).fillna(False).astype(bool)
        for cond in self._cond_stack:
            out = out & cond.reindex(self.index).fillna(False).astype(bool)
        return out

    def _strategy_call(self, callee: str, node: Call, scope: _Scope) -> Any:
        action = callee.split(".", 1)[1]
        if action in {"entry", "order"}:
            if not node.args:
                return float("nan")
            direction = self.eval(node.args[0], scope)
            cond_node = node.kwargs.get("when")
            cond = self.eval(cond_node, scope) if cond_node is not None else pd.Series(True, index=self.index)
            mask = _as_bool(cond, self.index)
            mask = self._apply_conditions(mask)
            d = ind._n(direction, 1)
            if d >= 0:
                self.result.entries_long = _or(self.result.entries_long, mask, self.index)
            else:
                self.result.entries_short = _or(self.result.entries_short, mask, self.index)
            price = self._call_kwarg(node, scope, "limit", default=None) or self._call_kwarg(
                node, scope, "stop", default=None
            )
            tag = self._call_kwarg(node, scope, "comment", default=None) or self._call_kwarg(
                node, scope, "id", default=None
            )
            if price is not None and tag is not None:
                self.result.entry_prices[str(tag)] = float(price)
            return float("nan")

        if action in {"close", "close_all"}:
            cond_node = node.kwargs.get("when")
            cond = self.eval(cond_node, scope) if cond_node is not None else pd.Series(True, index=self.index)
            mask = self._apply_conditions(_as_bool(cond, self.index))
            self.result.exits = _or(self.result.exits, mask, self.index)
            return float("nan")

        if action in {"exit", "order"}:
            cond_node = node.kwargs.get("when")
            cond = self.eval(cond_node, scope) if cond_node is not None else pd.Series(True, index=self.index)
            mask = self._apply_conditions(_as_bool(cond, self.index))
            self.result.exits = _or(self.result.exits, mask, self.index)
            return float("nan")

        if action in {"long", "short", "equity", "position_size", "position_avg_price", "netprofit"}:
            return _broadcast(float("nan"), self.index)
        return _broadcast(float("nan"), self.index)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _dotted(node: Node) -> str:
    if isinstance(node, Name):
        return node.name
    if isinstance(node, Member):
        base = _dotted(node.obj)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _broadcast(value: Any, index: pd.Index) -> Any:
    if isinstance(value, pd.Series):
        return value
    if isinstance(value, bool):
        return pd.Series(value, index=index, dtype=bool)
    if isinstance(value, (int, float)):
        return pd.Series(float(value), index=index, dtype="float64")
    return value


def _nan_like(value: Any, index: pd.Index) -> Any:
    if isinstance(value, pd.Series):
        return pd.Series(float("nan"), index=index, dtype="float64")
    return float("nan")


def _carry_forward(value: Any, index: pd.Index) -> Any:
    """Emulate Pine's ``var``: freeze the first bar's value for the whole series.

    For scalar initialisers this returns a constant series. For series
    initialisers the first non-NaN value is carried forward, which matches the
    common ``var`` idiom (e.g. ``var float pivot = na`` then assigned later).
    """
    if isinstance(value, pd.Series):
        if value.empty:
            return value
        first = value.iloc[0]
        if pd.isna(first):
            return value
        return pd.Series(first, index=index, dtype="float64")
    return _broadcast(value, index)


def _as_bool(value: Any, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        if value.dtype == bool:
            return value.fillna(False)
        return value.fillna(0).astype(bool)
    if isinstance(value, bool):
        return pd.Series(value, index=index, dtype=bool)
    try:
        return pd.Series(bool(value), index=index, dtype=bool)
    except (TypeError, ValueError):
        return pd.Series(False, index=index, dtype=bool)


def _series_equal(a: Any, b: Any) -> bool:
    if a is b:
        return True
    if isinstance(a, pd.Series) and isinstance(b, pd.Series):
        return a.equals(b)
    return a == b


def _merge_masked(old: Any, new: Any, mask: pd.Series) -> Any:
    if not isinstance(new, pd.Series):
        new = _broadcast(new, mask.index)
    if not isinstance(old, pd.Series):
        old = _broadcast(old, mask.index)
    new = new.reindex(mask.index)
    old = old.reindex(mask.index)
    if new.dtype == bool or old.dtype == bool:
        merged = old.fillna(False).astype(bool).where(~mask, new.fillna(False).astype(bool))
        return merged
    return old.where(~mask, new)


def _or(existing: pd.Series | None, mask: pd.Series, index: pd.Index) -> pd.Series:
    mask = mask.reindex(index).fillna(False).astype(bool)
    if existing is None:
        return mask
    return (existing | mask).fillna(False).astype(bool)


def _scalar_series(value: Any, index: pd.Index) -> pd.Series:
    """Wrap a scalar as a full-length Series so alignment never degenerates."""
    return pd.Series(float(value), index=index, dtype="float64")


def _binary_operands(left: Any, right: Any, index: pd.Index) -> tuple[Any, Any]:
    """Coerce operands to the same index when at least one is a Series."""
    if not (isinstance(left, pd.Series) or isinstance(right, pd.Series)):
        return left, right
    lhs = left if isinstance(left, pd.Series) else _scalar_series(left, index)
    rhs = right if isinstance(right, pd.Series) else _scalar_series(right, index)
    return lhs, rhs


def _binop(left: Any, right: Any, op, index: pd.Index) -> Any:
    lhs, rhs = _binary_operands(left, right, index)
    return op(lhs, rhs)


def _div(left: Any, right: Any, index: pd.Index) -> Any:
    lhs, rhs = _binary_operands(left, right, index)
    if isinstance(rhs, pd.Series):
        rhs = rhs.replace(0, np.nan)
    return lhs / rhs


def _compare(left: Any, right: Any, mode: str, index: pd.Index) -> Any:
    lhs, rhs = _binary_operands(left, right, index)
    if mode == "eq":
        return lhs == rhs
    if mode == "ne":
        return lhs != rhs
    if mode == "gt":
        return lhs > rhs
    if mode == "ge":
        return lhs >= rhs
    if mode == "lt":
        return lhs < rhs
    return lhs <= rhs


def _flag(index: pd.Index, position: int) -> pd.Series:
    out = pd.Series(False, index=index, dtype=bool)
    if len(out):
        out.iloc[position] = True
    return out


def _bbw(source: Any, length: Any = 20, mult: Any = 2.0) -> pd.Series:
    basis, upper, lower = ind.bb(source, length, mult)
    return (upper - lower) / basis.replace(0, np.nan)


def _cmo(source: Any, length: Any = 14) -> pd.Series:
    src = ind._s(source)
    delta = src.diff()
    up = delta.clip(lower=0).rolling(max(1, int(ind._n(length, 14))), min_periods=1).sum()
    down = (-delta).clip(lower=0).rolling(max(1, int(ind._n(length, 14))), min_periods=1).sum()
    total = (up + down).replace(0, np.nan)
    return 100 * (up - down) / total


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def evaluate(source: str, data: pd.DataFrame, *, symbol: str = "", timeframe: str = "1d") -> tuple[PineResult, Program]:
    """Parse and evaluate *source* against *data*; returns (result, program)."""
    program = parse(source)
    evaluator = PineEvaluator(data, symbol=symbol, timeframe=timeframe)
    return evaluator.run(program), program

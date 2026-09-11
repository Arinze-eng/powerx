"""``python_code`` — run a whole Python program LOCALLY in ONE model call.

This is the smolagents ``CodeAgent`` idea ported into PowerX. The core insight
(github.com/huggingface/smolagents) is that a Re-Act agent pays one billed LLM
round-trip per decision, so an N-step task costs N+1 calls. smolagents collapses
that by letting the model emit ONE block of *Python code* which a deterministic,
sandboxed interpreter then executes end-to-end — loops, branches, data munging,
and tool calls all run WITHOUT re-consulting the model. A 10,000-iteration loop
inside a single code block therefore costs ZERO extra LLM tokens; only the final
result is returned to the model.

Why a *tool* rather than replacing the runner loop
--------------------------------------------------
It composes with everything already in the runner (credits, hooks, telemetry,
the registry's own safety/validation) exactly like ``run_plan`` does. The
interpreter needs to call sibling tools by name, so we inject the live registry
via ``bind_registry`` (set once per run). When no registry is bound the tool
errors cleanly and the model just falls back to normal step-by-step behaviour —
never a crash.

Security model
--------------
We do NOT use ``exec()``. Code is parsed to an AST and evaluated node by node,
with:

* an allow-list of importable modules (safe stdlib subset);
* forbidden dunder attribute access (no ``__class__``/``__subclasses__`` escape);
* filesystem/shell/network routed through PowerX's OWN registered tools
  (read_file/write_file/list_files/exec/search/web_fetch), so path confinement /
  allowed_dir still applies — there is NO raw ``open``/``os``/``subprocess``;
* a wall-clock timeout so a runaway loop cannot hang the turn.

The result of the program (last expression value, or an explicit ``final_answer``
call) is returned to the model as the SINGLE tool output.
"""

from __future__ import annotations

import ast
import os
import time
from typing import Any, Callable

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult


# --------------------------------------------------------------------------- #
# Safe primitives exposed to interpreted code                                  #
# --------------------------------------------------------------------------- #

_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "ascii": ascii, "bin": bin,
    "bool": bool, "bytes": bytes, "callable": callable, "chr": chr,
    "complex": complex, "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format, "frozenset": frozenset,
    "hash": hash, "hex": hex, "id": id, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "list": list,
    "map": map, "max": max, "min": min, "next": next, "oct": oct, "ord": ord,
    "pow": pow, "range": range, "repr": repr, "reversed": reversed,
    "round": round, "set": set, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "StopIteration": StopIteration,
    "ZeroDivisionError": ZeroDivisionError, "NameError": NameError,
    "RuntimeError": RuntimeError, "ArithmeticError": ArithmeticError,
    "AttributeError": AttributeError, "True": True, "False": False, "None": None,
}

_ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "math", "statistics", "random", "datetime", "time", "json", "re",
    "itertools", "functools", "collections", "string", "textwrap",
    "unicodedata", "operator", "copy", "hashlib", "base64", "csv", "decimal",
    "fractions", "calendar", "enum", "typing", "dataclasses", "numbers",
    "struct", "pathlib",
})

_FORBIDDEN_ATTR_PREFIXES = ("_class_", "_dict_", "_bases_", "_subclasses_", "_mro_")

# Bridge names -> (underlying tool name, ordered arg keys).
_BRIDGES: dict[str, tuple[str, list[str]]] = {
    "read_file": ("read_file", ["path"]),
    "write_file": ("write_file", ["path", "content"]),
    "list_files": ("list_files", ["path"]),
    "search": ("search", ["query"]),
    "web_fetch": ("web_fetch", ["url"]),
    "exec": ("exec", ["command"]),
}


def _is_forbidden_attr(name: str) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return True
    return any(name.startswith(p) for p in _FORBIDDEN_ATTR_PREFIXES)


class PythonCodeError(Exception):
    """Raised for user-code errors surfaced back to the model."""


class _FinalAnswer(BaseException):
    def __init__(self, value: Any) -> None:
        super().__init__(value)
        self.value = value


class _ReturnSignal(BaseException):
    def __init__(self, value: Any) -> None:
        super().__init__(value)
        self.value = value


class _BreakSignal(BaseException):
    pass


class _ContinueSignal(BaseException):
    pass


# --------------------------------------------------------------------------- #
# Binary / augmented operator tables                                           #
# --------------------------------------------------------------------------- #
import operator as _op  # noqa: E402

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul, ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv, ast.Mod: _op.mod, ast.Pow: _op.pow,
    ast.BitAnd: _op.and_, ast.BitOr: _op.or_, ast.BitXor: _op.xor,
    ast.LShift: _op.lshift, ast.RShift: _op.rshift, ast.MatMult: _op.matmul,
}

_AUG_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b, ast.BitAnd: lambda a, b: a & b,
    ast.BitOr: lambda a, b: a | b, ast.BitXor: lambda a, b: a ^ b,
    ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b,
}


# --------------------------------------------------------------------------- #
# The sandboxed AST evaluator                                                  #
# --------------------------------------------------------------------------- #

class _Executor:
    """Evaluates a Python AST against a controlled namespace.

    Loops, branches, function defs, comprehensions etc. all run here WITHOUT any
    model call. Tool calls are bridged to real PowerX tools via the injected
    async ``tool_call`` callback, awaited inline (local execution, not a round-trip).
    """

    def __init__(
        self,
        *,
        globals_: dict[str, Any],
        tool_call: Callable[[str, dict[str, Any]], Any],
        deadline: float,
    ) -> None:
        self.globals = globals_
        self._tool_call = tool_call
        self._deadline = deadline

    # -- helpers ------------------------------------------------------------ #
    def _check_time(self) -> None:
        if time.monotonic() > self._deadline:
            raise PythonCodeError("Execution timed out (wall-clock limit reached).")

    def _get(self, name: str) -> Any:
        try:
            return self.globals[name]
        except KeyError:
            raise PythonCodeError(f"Name '{name}' is not defined.") from None

    # -- async expression evaluation ---------------------------------------- #
    async def eval(self, node: ast.AST) -> Any:
        self._check_time()
        handler = getattr(self, f"_eval_{type(node).__name__}", None)
        if handler is None:
            raise PythonCodeError(f"Unsupported syntax: {type(node).__name__}")
        return await handler(node)

    async def _eval_Constant(self, node: ast.Constant) -> Any:
        return node.value

    async def _eval_Name(self, node: ast.Name) -> Any:
        return self._get(node.id)

    async def _eval_List(self, node: ast.List) -> Any:
        return [await self.eval(e) for e in node.elts]

    async def _eval_Tuple(self, node: ast.Tuple) -> Any:
        return tuple([await self.eval(e) for e in node.elts])

    async def _eval_Set(self, node: ast.Set) -> Any:
        return {await self.eval(e) for e in node.elts}

    async def _eval_Dict(self, node: ast.Dict) -> Any:
        out: dict[Any, Any] = {}
        for k, v in zip(node.keys, node.values):
            key = await self.eval(k) if k is not None else ...
            out[key] = await self.eval(v)
        return out

    async def _eval_BinOp(self, node: ast.BinOp) -> Any:
        left = await self.eval(node.left)
        right = await self.eval(node.right)
        fn = _BIN_OPS.get(type(node.op))
        if fn is None:
            raise PythonCodeError(f"Unsupported binary operator: {type(node.op).__name__}")
        try:
            return fn(left, right)
        except PythonCodeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PythonCodeError(f"Arithmetic error: {exc}") from exc

    async def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = await self.eval(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.Invert):
            return ~operand
        raise PythonCodeError(f"Unsupported unary operator: {type(node.op).__name__}")

    async def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = await self.eval(v)
                if not result:
                    break
            return result
        result = False
        for v in node.values:
            result = await self.eval(v)
            if result:
                break
        return result

    async def _eval_Compare(self, node: ast.Compare) -> Any:
        left = await self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = await self.eval(comparator)
            ok = self._apply_cmp(op, left, right)
            if not ok:
                return False
            left = right
        return True

    @staticmethod
    def _apply_cmp(op: ast.cmpop, a: Any, b: Any) -> bool:
        if isinstance(op, ast.Eq):
            return a == b
        if isinstance(op, ast.NotEq):
            return a != b
        if isinstance(op, ast.Lt):
            return a < b
        if isinstance(op, ast.LtE):
            return a <= b
        if isinstance(op, ast.Gt):
            return a > b
        if isinstance(op, ast.GtE):
            return a >= b
        if isinstance(op, ast.Is):
            return a is b
        if isinstance(op, ast.IsNot):
            return a is not b
        if isinstance(op, ast.In):
            return a in b
        if isinstance(op, ast.NotIn):
            return a not in b
        raise PythonCodeError(f"Unsupported comparison: {type(op).__name__}")

    async def _eval_Subscript(self, node: ast.Subscript) -> Any:
        base = await self.eval(node.value)
        key = await self._eval_slice(node.slice)
        try:
            return base[key]
        except PythonCodeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PythonCodeError(f"Subscript error: {exc}") from exc

    async def _eval_slice(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Slice):
            lower = await self.eval(node.lower) if node.lower else None
            upper = await self.eval(node.upper) if node.upper else None
            step = await self.eval(node.step) if node.step else None
            return slice(lower, upper, step)
        return await self.eval(node)

    async def _eval_Attribute(self, node: ast.Attribute) -> Any:
        if _is_forbidden_attr(node.attr):
            raise PythonCodeError(f"Access to attribute '{node.attr}' is forbidden.")
        value = await self.eval(node.value)
        try:
            return getattr(value, node.attr)
        except PythonCodeError:
            raise
        except AttributeError as exc:
            raise PythonCodeError(f"Attribute error: {exc}") from exc

    async def _eval_Call(self, node: ast.Call) -> Any:
        # Bridge tool calls: read_file(...), exec(...) etc. -> awaited locally.
        if isinstance(node.func, ast.Name) and node.func.id in _BRIDGES:
            args = [await self.eval(a) for a in node.args]
            kwargs = {kw.arg: await self.eval(kw.value) for kw in node.keywords if kw.arg}
            return await self._bridge_call(node.func.id, args, kwargs)

        func = await self._resolve_callable(node.func)
        args = [await self.eval(a) for a in node.args]
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                unpacked = await self.eval(kw.value)
                if not isinstance(unpacked, dict):
                    raise PythonCodeError("** unpacking requires a mapping.")
                kwargs.update(unpacked)
            else:
                kwargs[kw.arg] = await self.eval(kw.value)
        try:
            out = func(*args, **kwargs)
            # User-defined functions are async wrappers; await their results.
            import inspect as _inspect

            if _inspect.isawaitable(out):
                out = await out
            return out
        except (_FinalAnswer, _ReturnSignal, _BreakSignal, _ContinueSignal, PythonCodeError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise PythonCodeError(f"Error calling function: {exc}") from exc

    async def _resolve_callable(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Attribute):
            if _is_forbidden_attr(node.attr):
                raise PythonCodeError(f"Call to '{node.attr}' is forbidden.")
            owner = await self.eval(node.value)
            attr = getattr(owner, node.attr, None)
            if attr is None:
                raise PythonCodeError(f"Unknown function '{node.attr}'.")
            return attr
        return await self.eval(node)

    async def _bridge_call(self, name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        tool_name, keys = _BRIDGES[name]
        params: dict[str, Any] = dict(kwargs)
        for i, val in enumerate(args):
            if i < len(keys):
                params[keys[i]] = val
        return await self._tool_call(tool_name, params)

    # --- comprehensions ----------------------------------------------------- #
    async def _eval_ListComp(self, node: ast.ListComp) -> Any:
        return await self._comp(node.elt, node.generators)

    async def _eval_SetComp(self, node: ast.SetComp) -> Any:
        return set(await self._comp(node.elt, node.generators))

    async def _eval_GeneratorExp(self, node: ast.GeneratorExp) -> Any:
        return await self._comp(node.elt, node.generators)

    async def _eval_DictComp(self, node: ast.DictComp) -> Any:
        pairs = await self._comp((node.key, node.value), node.generators)
        return {k: v for k, v in pairs}

    async def _comp(
        self, elt: ast.expr | tuple[ast.expr, ast.expr], generators: list[ast.comprehension]
    ) -> list[Any]:
        results: list[Any] = []
        saved = self.globals
        scope = dict(saved)
        self.globals = scope

        async def rec(idx: int) -> None:
            if idx >= len(generators):
                if isinstance(elt, tuple):
                    results.append((await self.eval(elt[0]), await self.eval(elt[1])))
                else:
                    results.append(await self.eval(elt))
                return
            gen = generators[idx]
            iterable = await self.eval(gen.iter)
            for item in iterable:
                self._assign_target(gen.target, item, scope)
                keep = True
                for cond in gen.ifs:
                    if not await self.eval(cond):
                        keep = False
                        break
                if keep:
                    await rec(idx + 1)

        try:
            await rec(0)
        finally:
            self.globals = saved
        return results

    # -- async statement evaluation ----------------------------------------- #
    async def exec_body(self, body: list[ast.stmt]) -> Any:
        last_value: Any = None
        for stmt in body:
            val = await self.exec_stmt(stmt)
            if val is not None:
                last_value = val
        return last_value

    async def exec_stmt(self, node: ast.stmt) -> Any:
        self._check_time()
        handler = getattr(self, f"_exec_{type(node).__name__}", None)
        if handler is None:
            raise PythonCodeError(f"Unsupported statement: {type(node).__name__}")
        return await handler(node)

    async def _exec_Expr(self, node: ast.Expr) -> Any:
        return await self.eval(node.value)

    async def _exec_Assign(self, node: ast.Assign) -> Any:
        value = await self.eval(node.value)
        for target in node.targets:
            self._assign_target(target, value, self.globals)
        return None

    async def _exec_AugAssign(self, node: ast.AugAssign) -> Any:
        current = await self.eval(node.target)
        rhs = await self.eval(node.value)
        fn = _AUG_OPS.get(type(node.op))
        if fn is None:
            raise PythonCodeError(f"Unsupported augmented assign: {type(node.op).__name__}")
        try:
            new = fn(current, rhs)
        except PythonCodeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PythonCodeError(f"Augmented assignment error: {exc}") from exc
        self._assign_target(node.target, new, self.globals)
        return None

    async def _exec_AnnAssign(self, node: ast.AnnAssign) -> Any:
        if node.value is None:
            return None
        value = await self.eval(node.value)
        self._assign_target(node.target, value, self.globals)
        return None

    async def _exec_For(self, node: ast.For) -> Any:
        iterable = await self.eval(node.iter)
        broke = False
        for item in iterable:
            self._check_time()
            self._assign_target(node.target, item, self.globals)
            try:
                for stmt in node.body:
                    await self.exec_stmt(stmt)
            except _BreakSignal:
                broke = True
                break
            except _ContinueSignal:
                continue
        if not broke:
            for stmt in node.orelse:
                await self.exec_stmt(stmt)
        return None

    async def _exec_While(self, node: ast.While) -> Any:
        broke = False
        while await self.eval(node.test):
            self._check_time()
            try:
                for stmt in node.body:
                    await self.exec_stmt(stmt)
            except _BreakSignal:
                broke = True
                break
            except _ContinueSignal:
                continue
        if not broke:
            for stmt in node.orelse:
                await self.exec_stmt(stmt)
        return None

    async def _exec_If(self, node: ast.If) -> Any:
        branch = node.body if await self.eval(node.test) else node.orelse
        for stmt in branch:
            await self.exec_stmt(stmt)
        return None

    async def _exec_Return(self, node: ast.Return) -> Any:
        value = await self.eval(node.value) if node.value is not None else None
        raise _ReturnSignal(value)

    async def _exec_Pass(self, node: ast.Pass) -> Any:
        return None

    async def _exec_Break(self, node: ast.Break) -> Any:
        raise _BreakSignal()

    async def _exec_Continue(self, node: ast.Continue) -> Any:
        raise _ContinueSignal()

    async def _exec_Raise(self, node: ast.Raise) -> Any:
        if node.exc is None:
            raise PythonCodeError("raise without argument is not supported here.")
        exc = await self.eval(node.exc)
        if isinstance(exc, BaseException):
            raise exc
        raise PythonCodeError(str(exc))

    async def _exec_Try(self, node: ast.Try) -> Any:
        caught: BaseException | None = None
        try:
            for stmt in node.body:
                await self.exec_stmt(stmt)
        except PythonCodeError as exc:
            caught = exc
        except Exception as exc:  # noqa: BLE001 - mirrors python catch-all
            caught = exc
        if caught is not None:
            handled = False
            for handler in node.handlers:
                if self._matches_handler(handler, caught):
                    handled = True
                    if handler.name:
                        self.globals[handler.name] = caught
                    for s in handler.body:
                        await self.exec_stmt(s)
                    break
            if not handled:
                raise caught
        for stmt in node.finalbody:
            await self.exec_stmt(stmt)
        return None

    @staticmethod
    def _matches_handler(handler: ast.ExceptHandler, exc: BaseException) -> bool:
        if handler.type is None:
            return True
        candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for cand in candidates:
            resolved = None
            if isinstance(cand, ast.Name):
                resolved = _SAFE_BUILTINS.get(cand.id)
            elif isinstance(cand, ast.Attribute):
                parts: list[str] = []
                cur: ast.expr = cand
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    mod = _SAFE_BUILTINS.get(cur.id)
                    if mod is not None:
                        for p in reversed(parts):
                            mod = getattr(mod, p, None)
                        resolved = mod
            if resolved is not None and isinstance(resolved, type) and isinstance(exc, resolved):
                return True
        return False

    async def _exec_FunctionDef(self, node: ast.FunctionDef) -> Any:
        fn = self._make_function(node)
        self.globals[node.name] = fn
        return None

    def _make_function(self, node: ast.FunctionDef) -> Callable[..., Any]:
        param_names = [a.arg for a in node.args.args]
        captured_globals = self.globals  # closure over current global namespace

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            local = dict(zip(param_names, args))
            defaults = node.args.defaults
            for i, d in enumerate(reversed(defaults)):
                local.setdefault(param_names[len(param_names) - 1 - i], await self.eval(d))
            local.update(kwargs)
            saved = self.globals
            self.globals = {**captured_globals, **local}
            try:
                for stmt in node.body:
                    res = await self.exec_stmt(stmt)
                    if isinstance(res, _ReturnSignal):
                        return res.value
                return None
            except _ReturnSignal as rs:
                return rs.value
            finally:
                self.globals = saved

        return wrapper

    def _assign_target(self, target: ast.expr, value: Any, scope: dict[str, Any]) -> None:
        if isinstance(target, ast.Name):
            scope[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            items = list(value)
            if len(items) != len(target.elts):
                raise PythonCodeError("Cannot unpack: length mismatch.")
            for t, v in zip(target.elts, items):
                self._assign_target(t, v, scope)
        elif isinstance(target, ast.Starred):  # pragma: no cover
            raise PythonCodeError("Starred unpacking is not supported.")
        elif isinstance(target, ast.Subscript):
            base = self._eval_sync_target_base(target.value)
            key = self._eval_sync_slice(target.slice)
            base[key] = value
        elif isinstance(target, ast.Attribute):
            if _is_forbidden_attr(target.attr):
                raise PythonCodeError(f"Setting '{target.attr}' is forbidden.")
            obj = self._eval_sync_target_base(target.value)
            setattr(obj, target.attr, value)
        else:
            raise PythonCodeError(f"Cannot assign to {type(target).__name__}.")

    # Synchronous helpers used only for assignment TARGET bases (no awaits needed
    # because targets are simple names/subscripts/attributes on already-evaluated
    # values). They intentionally forbid bridge/dangerous constructs.
    def _eval_sync_target_base(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Name):
            return self._get(node.id)
        if isinstance(node, ast.Attribute):
            if _is_forbidden_attr(node.attr):
                raise PythonCodeError(f"Access to '{node.attr}' is forbidden.")
            owner = self._eval_sync_target_base(node.value)
            return getattr(owner, node.attr)
        if isinstance(node, ast.Subscript):
            base = self._eval_sync_target_base(node.value)
            key = self._eval_sync_slice(node.slice)
            return base[key]
        raise PythonCodeError(f"Unsupported assignment base: {type(node).__name__}")

    def _eval_sync_slice(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Slice):
            lower = self._eval_sync_simple(node.lower) if node.lower else None
            upper = self._eval_sync_simple(node.upper) if node.upper else None
            step = self._eval_sync_simple(node.step) if node.step else None
            return slice(lower, upper, step)
        return self._eval_sync_simple(node)

    def _eval_sync_simple(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self._get(node.id)
        raise PythonCodeError("Complex subscripts are not supported on assignment targets.")


# --------------------------------------------------------------------------- #
# Import handling                                                              #
# --------------------------------------------------------------------------- #

def _handle_import(node: ast.AST, executor: _Executor) -> None:
    import importlib
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in _ALLOWED_IMPORTS and alias.name not in _ALLOWED_IMPORTS:
                raise PythonCodeError(f"Import of '{alias.name}' is not allowed.")
            mod = importlib.import_module(alias.name)
            executor.globals[alias.asname or alias.name] = mod
    elif isinstance(node, ast.ImportFrom):
        if node.module is None:
            raise PythonCodeError("Relative imports are not allowed.")
        root = node.module.split(".")[0]
        if root not in _ALLOWED_IMPORTS and node.module not in _ALLOWED_IMPORTS:
            raise PythonCodeError(f"Import from '{node.module}' is not allowed.")
        mod = importlib.import_module(node.module)
        for alias in node.names:
            executor.globals[alias.asname or alias.name] = getattr(mod, alias.name)


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #

async def run_python_code(
    code: str,
    *,
    tool_call: Callable[[str, dict[str, Any]], Any],
    extra_globals: dict[str, Any] | None = None,
    timeout_seconds: float = 60.0,
) -> Any:
    """Execute *code* in the sandbox. Returns the final value.

    ``tool_call`` is an async callable ``(name, args) -> result`` bridging PowerX
    tools into the program. The model gets ONE tool output back.
    """
    tree = ast.parse(code)
    executor = _Executor(
        globals_=dict(_SAFE_BUILTINS),
        tool_call=tool_call,
        deadline=time.monotonic() + timeout_seconds,
    )
    if extra_globals:
        executor.globals.update(extra_globals)

    def _final_answer(value: Any) -> None:
        raise _FinalAnswer(value)

    executor.globals["final_answer"] = _final_answer

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _handle_import(node, executor)

    final_value: Any = None
    try:
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            res = await executor.exec_stmt(node)
            if res is not None:
                final_value = res
    except _FinalAnswer as fa:
        final_value = fa.value
    except _ReturnSignal as rs:
        final_value = rs.value

    return final_value


# --------------------------------------------------------------------------- #
# The PowerX tool                                                              #
# --------------------------------------------------------------------------- #

class PythonCodeTool(Tool):
    """Run a full Python program locally in ONE model call (smolagents-style)."""

    # Registered explicitly by AgentLoop._register_default_tools (which also binds
    # the live registry via bind_registry). Opt out of plugin-loader auto-discovery
    # so an UNBOUND instance is never registered.
    _plugin_discoverable = False

    def __init__(self) -> None:
        self._registry: Any | None = None

    def bind_registry(self, registry: Any) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "python_code"

    @property
    def description(self) -> str:
        return (
            "Execute an ENTIRE Python program in ONE call with ZERO extra model "
            "round-trips. Write plain Python that does ALL the work: loops, "
            "branches, string/data processing, and calls to other tools via the "
            "bridge functions read_file(path), write_file(path, content), "
            "list_files(path), search(query), web_fetch(url), exec(command). "
            "Assign variables freely; call final_answer(value) to return the "
            "result. A loop over hundreds of files costs ONE model call, not "
            "hundreds. Use this INSTEAD of many separate read/exec/search calls "
            "whenever a task needs more than ~2 steps or must loop. Imports are "
            "limited to safe stdlib (math, json, re, statistics, datetime, "
            "collections, itertools, functools, pathlib, ...). No network, no "
            "os/subprocess, no open()."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "A complete Python program. It may define variables, loop, "
                        "branch, and call the bridge functions above. End with a "
                        "final_answer(...) call or leave the last expression as the "
                        "result."
                    ),
                }
            },
            "required": ["code"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        registry = self._registry
        if registry is None:  # pragma: no cover - defensive
            return ToolResult.error(
                "python_code is not wired to a tool registry on this run; do the "
                "work with ordinary tool calls instead."
            )

        code = kwargs.get("code")
        if not isinstance(code, str) or not code.strip():
            return ToolResult.error("python_code requires non-empty 'code'.")

        async def _tool_call(name: str, args: dict[str, Any]) -> Any:
            if not registry.has(name):
                raise PythonCodeError(f"Unknown tool '{name}' called from python_code.")
            return await registry.execute(name, args)

        timeout = float(os.environ.get("POWERX_PYTHON_CODE_TIMEOUT", "60"))
        try:
            final_value = await run_python_code(
                code, tool_call=_tool_call, timeout_seconds=timeout
            )
        except PythonCodeError as exc:
            return ToolResult.error(f"python_code error: {exc}")
        except SyntaxError as exc:
            return ToolResult.error(f"Syntax error in your code: {exc}")
        except Exception as exc:  # noqa: BLE001 - unexpected fault
            logger.exception("python_code crashed unexpectedly")
            return ToolResult.error(f"python_code failed: {exc}")

        text = final_value if isinstance(final_value, str) else repr(final_value)
        if text is None:
            text = "(no result)"
        return ToolResult(
            "[python_code: ran locally, 0 additional model calls]\n" + text
        )

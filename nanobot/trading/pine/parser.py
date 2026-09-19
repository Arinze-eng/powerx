"""Pine Script (v5/v6 subset) tokenizer + parser.

Design
------
Pine is conceptually evaluated once per bar, with a rolling history window
(``close[1]``). This engine keeps that semantic model but evaluates the script
over the *whole series at once* (vectorised): every expression evaluates to a
``pandas.Series`` aligned to the bar index, and ``expr[n]`` becomes a shift.

That is exact for causal built-ins (SMA/EMA/RSI/ATR/crossover...) because those
are computed left-to-right from past data only, and it avoids the cost and
fragility of a per-bar Python interpreter. Masks implement ``if`` /
reassignment: ``x := expr`` under a condition becomes
``x = x.where(~cond, expr)``.

Supported grammar highlights
----------------------------
* ``indicator(...)`` / ``strategy(...)`` headers
* ``x = expr``, ``var x = expr``, ``varip``, optional type keyword
  (``var float x = na``), ``x := expr``, compound ``+=`` etc.
* tuple destructuring ``[a, b, c] = ta.macd(...)``
* history ``close[1]``, member calls ``ta.sma(...)``
* ``if`` / ``else if`` / ``else`` and ``for`` blocks (indentation based)
* user-defined functions: ``f(x) => expr`` and multi-line bodies
* ternary ``cond ? a : b``, ``and`` / ``or`` / ``not``
* guards ``barstate.*``, ``strategy.*``, ``plot*``, ``alert*``, ``label.*``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class PineSyntaxError(Exception):
    """Raised when the source cannot be parsed."""

    def __init__(self, message: str, line: int = 0, col: int = 0) -> None:
        self.line = line
        self.col = col
        super().__init__(f"line {line}: {message}" if line else message)


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<WS>[ \t]+)
    | (?P<NUMBER>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+)
    | (?P<STRING>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<OP><=|>=|==|!=|:=|\+=|-=|\*=|/=|%=|=>|[+\-*/%<>=?:,()\[\]\.])
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_INDENT_WIDTH = 4


@dataclass
class Token:
    kind: str
    value: Any
    line: int
    col: int

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Token({self.kind},{self.value!r},L{self.line})"


def _strip_comment(line: str) -> str:
    """Remove a ``//`` comment while respecting string literals."""
    out = []
    i = 0
    quote = ""
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and line[i : i + 2] == "//":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def tokenize(source: str) -> list[Token]:
    """Convert Pine source into a flat token stream with INDENT/DEDENT/NEWLINE."""
    tokens: list[Token] = []
    indent_stack = [0]
    # Line continuation: a trailing comma / open bracket means the logical line
    # continues, so no NEWLINE / INDENT processing happens for it.
    depth = 0

    for lineno, raw_line in enumerate(source.splitlines(), 1):
        line = _strip_comment(raw_line)
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" \t"))
        indent = len(line[:indent].replace("\t", " " * _INDENT_WIDTH))

        logical_start = depth == 0
        if logical_start:
            if indent > indent_stack[-1]:
                indent_stack.append(indent)
                tokens.append(Token("INDENT", indent, lineno, 0))
            else:
                while indent < indent_stack[-1]:
                    indent_stack.pop()
                    tokens.append(Token("DEDENT", indent, lineno, 0))
                if indent != indent_stack[-1]:
                    raise PineSyntaxError(
                        "inconsistent indentation", lineno, indent + 1
                    )

        pos = len(line) - len(line.lstrip(" \t"))
        while pos < len(line):
            match = _TOKEN_RE.match(line, pos)
            if not match:
                raise PineSyntaxError(
                    f"unexpected character {line[pos]!r}", lineno, pos + 1
                )
            pos = match.end()
            kind = match.lastgroup or ""
            if kind == "WS":
                continue
            if kind == "NUMBER":
                text = match.group()
                value: Any = float(text) if ("." in text or "e" in text or "E" in text) else int(text)
            elif kind == "STRING":
                value = match.group()[1:-1]
            else:
                value = match.group()
            if kind == "OP":
                if value in "([":
                    depth += 1
                elif value in ")]":
                    depth = max(0, depth - 1)
            tokens.append(Token(kind, value, lineno, match.start() + 1))

        if depth == 0:
            tokens.append(Token("NEWLINE", "\n", lineno, len(line)))

    while len(indent_stack) > 1:
        indent_stack.pop()
        tokens.append(Token("DEDENT", 0, lineno if source else 0, 0))
    tokens.append(Token("EOF", None, 0, 0))
    return tokens


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass
class Node:
    line: int = 0


@dataclass
class Literal(Node):
    value: Any = None


@dataclass
class Name(Node):
    name: str = ""


@dataclass
class Member(Node):
    """``ta.sma`` -> Member(Name('ta'), 'sma')."""

    obj: Node = None  # type: ignore[assignment]
    attr: str = ""


@dataclass
class History(Node):
    """``close[2]`` -> History(Name('close'), 2)."""

    obj: Node = None  # type: ignore[assignment]
    offset: Node = None  # type: ignore[assignment]


@dataclass
class TupleLit(Node):
    items: list[Node] = field(default_factory=list)


@dataclass
class Unary(Node):
    op: str = ""
    operand: Node = None  # type: ignore[assignment]


@dataclass
class Binary(Node):
    op: str = ""
    left: Node = None  # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass
class Ternary(Node):
    cond: Node = None  # type: ignore[assignment]
    if_true: Node = None  # type: ignore[assignment]
    if_false: Node = None  # type: ignore[assignment]


@dataclass
class Call(Node):
    func: Node = None  # type: ignore[assignment]
    args: list[Node] = field(default_factory=list)
    kwargs: dict[str, Node] = field(default_factory=dict)

    def callee(self) -> str:
        return _dotted(self.func)


def _dotted(node: Node) -> str:
    if isinstance(node, Name):
        return node.name
    if isinstance(node, Member):
        return f"{_dotted(node.obj)}.{node.attr}"
    return ""


# ---- statements -----------------------------------------------------------


@dataclass
class Stmt(Node):
    pass


@dataclass
class Assign(Stmt):
    target: str = ""
    expr: Node = None  # type: ignore[assignment]
    declare: bool = False  # '=' vs ':='
    var: bool = False
    type_name: str = ""
    op: str = "="  # '=', ':=', '+=', ...


@dataclass
class TupleAssign(Stmt):
    targets: list[str] = field(default_factory=list)
    expr: Node = None  # type: ignore[assignment]
    var: bool = False


@dataclass
class If(Stmt):
    cond: Node = None  # type: ignore[assignment]
    body: list[Stmt] = field(default_factory=list)
    orelse: list[Stmt] = field(default_factory=list)


@dataclass
class For(Stmt):
    var: str = ""
    start: Node = None  # type: ignore[assignment]
    end: Node = None  # type: ignore[assignment]
    step: Node | None = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class FuncDef(Stmt):
    name: str = ""
    params: list[str] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)
    expr: Node | None = None


@dataclass
class ExprStmt(Stmt):
    expr: Node = None  # type: ignore[assignment]


@dataclass
class Program:
    statements: list[Stmt] = field(default_factory=list)

    @property
    def header(self) -> Call | None:
        for stmt in self.statements:
            if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, Call):
                if stmt.expr.callee() in {"indicator", "strategy", "study"}:
                    return stmt.expr
        return None


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

_KEYWORD_NAMES = {"and", "or", "not", "if", "else", "for", "to", "by", "var", "varip", "na", "true", "false"}
_TYPE_KEYWORDS = {"int", "float", "bool", "string", "color", "label", "line", "table", "array", "matrix", "map"}


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # -- helpers ----------------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def at(self, kind: str, value: Any = None) -> bool:
        tok = self.cur
        if tok.kind != kind:
            return False
        return value is None or tok.value == value

    def accept(self, kind: str, value: Any = None) -> Token | None:
        if self.at(kind, value):
            return self.advance()
        return None

    def expect(self, kind: str, value: Any = None) -> Token:
        if not self.at(kind, value):
            raise PineSyntaxError(
                f"expected {value or kind}, found {self.cur.kind}"
                f" {self.cur.value!r}",
                self.cur.line,
                self.cur.col,
            )
        return self.advance()

    def skip_newlines(self) -> None:
        while self.cur.kind == "NEWLINE":
            self.advance()

    # -- entry ------------------------------------------------------------
    def parse(self) -> Program:
        program = Program()
        self.skip_newlines()
        while not self.at("EOF"):
            program.statements.extend(self.parse_statement())
            self.skip_newlines()
        return program

    def parse_block(self) -> list[Stmt]:
        """Parse an indented block (called when the caller saw NEWLINE)."""
        self.skip_newlines()
        self.expect("INDENT")
        body: list[Stmt] = []
        self.skip_newlines()
        while not self.at("DEDENT") and not self.at("EOF"):
            body.extend(self.parse_statement())
            self.skip_newlines()
        self.accept("DEDENT")
        return body

    # -- statements -------------------------------------------------------
    def parse_statement(self) -> list[Stmt]:
        tok = self.cur

        if tok.kind == "IDENT" and tok.value == "if":
            return [self.parse_if()]
        if tok.kind == "IDENT" and tok.value == "for":
            return [self.parse_for()]

        # var / varip declaration
        var = False
        type_name = ""
        if tok.kind == "IDENT" and tok.value in {"var", "varip"}:
            nxt = self.peek()
            if nxt.kind in {"IDENT", "OP", "LPAREN"} or nxt.value in {"[", "["}:
                # 'var' used as a variable name is legal in Pine but rare; treat
                # as declaration only when followed by a type or an identifier.
                if nxt.kind == "IDENT":
                    var = True
                    self.advance()
                    if self.cur.kind == "IDENT" and self.cur.value in _TYPE_KEYWORDS:
                        type_name = self.advance().value
                    tok = self.cur

        # type-only declaration: `float x = na`
        if tok.kind == "IDENT" and tok.value in _TYPE_KEYWORDS:
            if self.peek().kind == "IDENT" and self.peek(2).kind == "OP" and self.peek(2).value in {"=", ":="}:
                type_name = self.advance().value
                tok = self.cur

        # tuple assign: [a, b] = ...
        if tok.kind == "OP" and tok.value == "[":
            save = self.pos
            self.advance()
            targets: list[str] = []
            ok = True
            while not self.at("OP", "]"):
                if self.cur.kind == "IDENT":
                    targets.append(self.advance().value)
                else:
                    ok = False
                    break
                if not self.accept("OP", ","):
                    break
            if ok and self.accept("OP", "]"):
                if self.accept("OP", "=") or self.accept("OP", ":="):
                    expr = self.parse_expr()
                    self.end_of_statement()
                    return [TupleAssign(targets=targets, expr=expr, var=var, line=tok.line)]
            self.pos = save

        # function definition: name(params) => ...
        if tok.kind == "IDENT":
            save = self.pos
            name_tok = self.advance()
            if self.accept("OP", "("):
                params: list[str] = []
                while not self.at("OP", ")"):
                    if self.cur.kind == "IDENT":
                        pname = self.advance().value
                        if pname in _TYPE_KEYWORDS and self.cur.kind == "IDENT":
                            pname = self.advance().value
                        params.append(pname)
                    else:
                        break
                    if not self.accept("OP", ","):
                        break
                if self.accept("OP", ")") and self.accept("OP", "=>"):
                    if self.cur.kind == "NEWLINE":
                        body = self.parse_block()
                        return [FuncDef(name=name_tok.value, params=params, body=body, line=name_tok.line)]
                    expr = self.parse_expr()
                    self.end_of_statement()
                    return [FuncDef(name=name_tok.value, params=params, expr=expr, line=name_tok.line)]
            self.pos = save

        # assignment / reassignment
        if tok.kind == "IDENT":
            save = self.pos
            name = self.advance().value
            nxt = self.cur
            if nxt.kind == "OP" and nxt.value in {"=", ":=", "+=", "-=", "*=", "/=", "%="}:
                op = self.advance().value
                expr = self.parse_expr()
                self.end_of_statement()
                return [
                    Assign(
                        target=name,
                        expr=expr,
                        declare=op in {"=", "+=", "-=", "*=", "/=", "%="},
                        var=var,
                        type_name=type_name,
                        op=op,
                        line=tok.line,
                    )
                ]
            self.pos = save

        expr = self.parse_expr()
        self.end_of_statement()
        return [ExprStmt(expr=expr, line=tok.line)]

    def end_of_statement(self) -> None:
        if self.cur.kind in {"NEWLINE", "DEDENT", "EOF"}:
            self.accept("NEWLINE")
            return
        raise PineSyntaxError(
            f"unexpected token {self.cur.value!r} after expression",
            self.cur.line,
            self.cur.col,
        )

    def parse_if(self) -> If:
        line = self.expect("IDENT", "if").line
        cond = self.parse_expr()
        self.expect("NEWLINE")
        body = self.parse_block()
        orelse: list[Stmt] = []
        self.skip_newlines_no_dedent()
        if self.cur.kind == "IDENT" and self.cur.value == "else":
            self.advance()
            if self.cur.kind == "IDENT" and self.cur.value == "if":
                orelse = [self.parse_if()]
            else:
                self.expect("NEWLINE")
                orelse = self.parse_block()
        return If(cond=cond, body=body, orelse=orelse, line=line)

    def skip_newlines_no_dedent(self) -> None:
        while self.cur.kind == "NEWLINE":
            self.advance()

    def parse_for(self) -> For:
        line = self.expect("IDENT", "for").line
        var = self.expect("IDENT").value
        self.expect("OP", "=")
        start = self.parse_expr()
        self.expect("IDENT", "to")
        end = self.parse_expr()
        step = None
        if self.cur.kind == "IDENT" and self.cur.value == "by":
            self.advance()
            step = self.parse_expr()
        self.expect("NEWLINE")
        body = self.parse_block()
        return For(var=var, start=start, end=end, step=step, body=body, line=line)

    # -- expressions ------------------------------------------------------
    def parse_expr(self) -> Node:
        return self.parse_ternary()

    def parse_ternary(self) -> Node:
        cond = self.parse_or()
        if self.at("OP", "?"):
            line = self.advance().line
            if_true = self.parse_expr()
            self.expect("OP", ":")
            if_false = self.parse_expr()
            return Ternary(cond=cond, if_true=if_true, if_false=if_false, line=line)
        return cond

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self.cur.kind == "IDENT" and self.cur.value == "or":
            line = self.advance().line
            node = Binary(op="or", left=node, right=self.parse_and(), line=line)
        return node

    def parse_and(self) -> Node:
        node = self.parse_not()
        while self.cur.kind == "IDENT" and self.cur.value == "and":
            line = self.advance().line
            node = Binary(op="and", left=node, right=self.parse_not(), line=line)
        return node

    def parse_not(self) -> Node:
        if self.cur.kind == "IDENT" and self.cur.value == "not":
            line = self.advance().line
            return Unary(op="not", operand=self.parse_not(), line=line)
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
        node = self.parse_additive()
        while self.cur.kind == "OP" and self.cur.value in {"==", "!=", "<", "<=", ">", ">="}:
            op = self.advance().value
            node = Binary(op=op, left=node, right=self.parse_additive(), line=node.line)
        return node

    def parse_additive(self) -> Node:
        node = self.parse_multiplicative()
        while self.cur.kind == "OP" and self.cur.value in {"+", "-"}:
            op = self.advance().value
            node = Binary(op=op, left=node, right=self.parse_multiplicative(), line=node.line)
        return node

    def parse_multiplicative(self) -> Node:
        node = self.parse_unary()
        while self.cur.kind == "OP" and self.cur.value in {"*", "/", "%"}:
            op = self.advance().value
            node = Binary(op=op, left=node, right=self.parse_unary(), line=node.line)
        return node

    def parse_unary(self) -> Node:
        if self.cur.kind == "OP" and self.cur.value in {"-", "+"}:
            op = self.advance().value
            return Unary(op=op, operand=self.parse_unary(), line=self.cur.line)
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            if self.at("OP", "["):
                line = self.advance().line
                offset = self.parse_expr()
                self.expect("OP", "]")
                node = History(obj=node, offset=offset, line=line)
                continue
            if self.at("OP", "."):
                self.advance()
                attr = self.expect("IDENT").value
                node = Member(obj=node, attr=attr, line=node.line)
                continue
            if self.at("OP", "("):
                line = self.advance().line
                args: list[Node] = []
                kwargs: dict[str, Node] = {}
                while not self.at("OP", ")"):
                    if self.cur.kind == "IDENT" and self.peek().kind == "OP" and self.peek().value == "=":
                        key = self.advance().value
                        self.advance()
                        kwargs[key] = self.parse_expr()
                    else:
                        args.append(self.parse_expr())
                    if not self.accept("OP", ","):
                        break
                self.expect("OP", ")")
                node = Call(func=node, args=args, kwargs=kwargs, line=line)
                continue
            break
        return node

    def parse_primary(self) -> Node:
        tok = self.cur
        if tok.kind == "NUMBER":
            self.advance()
            return Literal(value=tok.value, line=tok.line)
        if tok.kind == "STRING":
            self.advance()
            return Literal(value=tok.value, line=tok.line)
        if tok.kind == "OP" and tok.value == "(":
            self.advance()
            node = self.parse_expr()
            self.expect("OP", ")")
            return node
        if tok.kind == "OP" and tok.value == "[":
            self.advance()
            items: list[Node] = []
            while not self.at("OP", "]"):
                items.append(self.parse_expr())
                if not self.accept("OP", ","):
                    break
            self.expect("OP", "]")
            return TupleLit(items=items, line=tok.line)
        if tok.kind == "IDENT":
            self.advance()
            if tok.value == "na":
                return Literal(value=float("nan"), line=tok.line)
            if tok.value == "true":
                return Literal(value=True, line=tok.line)
            if tok.value == "false":
                return Literal(value=False, line=tok.line)
            return Name(name=tok.value, line=tok.line)
        raise PineSyntaxError(f"unexpected token {tok.value!r}", tok.line, tok.col)


def parse(source: str) -> Program:
    """Parse Pine source, raising :class:`PineSyntaxError` on failure."""
    if not source or not source.strip():
        raise PineSyntaxError("empty script")
    return Parser(tokenize(source)).parse()


def header_options(program: Program) -> dict[str, Any]:
    """Extract ``indicator(...)`` / ``strategy(...)`` header options.

    Only literal values are resolved here; expressions stay as ``None``.
    """
    header = program.header
    options: dict[str, Any] = {"kind": "indicator", "overlay": True}
    if header is None:
        return options
    options["kind"] = header.callee()
    for key, node in header.kwargs.items():
        if isinstance(node, Literal):
            options[key] = node.value
    for positional in header.args[:1]:
        if isinstance(positional, Literal) and isinstance(positional.value, str):
            options["title"] = positional.value
    options.setdefault("title", options.get("title", "Pine Script"))
    return options

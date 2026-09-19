"""Static analysis and auto-fix for Pine Script.

The linter has two jobs:

1. **Diagnose** — find real syntax/semantic problems before the evaluator does,
   and produce messages a user (or the agent) can act on.
2. **Repair** — apply *safe, deterministic* rewrites for the failure modes that
   actually occur when hand-written Pine meets a non-TradingView engine:

   ===========================================  ==============================
   Problem                                      Automatic fix
   ===========================================  ==============================
   ``ta.sma`` casing / aliases (``sma``)        canonicalise to ``ta.sma``
   v4→v5 function form (``sma(x, 14)``)         prefix with ``ta.``
   ``//@version=4`` / missing version           stamp ``//@version=5``
   ``study(``                                  → ``indicator(``
   ``security(``                               → ``request.security(``
   tabs used for indentation                    → 4 spaces
   trailing ``\r`` (Windows line endings)       stripped
   ``iff(a,b,c)``                               → ternary ``a ? b : c``
   ``crossover``/``crossunder`` bare calls      → ``ta.crossover`` etc.
   ``=`` inside ``if`` first assignment         → ``:=`` (Pine requires :=)
   ``plot(series=...)`` argument name            → positional
   ===========================================  ==============================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nanobot.trading.pine.parser import PineSyntaxError, parse

# --------------------------------------------------------------------------
# function tables
# --------------------------------------------------------------------------

# bare v4 names -> canonical v5 names (only applied when the call is not
# already namespaced, i.e. `sma(` but not `ta.sma(`)
V4_TO_V5 = {
    "sma": "ta.sma", "ema": "ta.ema", "rma": "ta.rma", "wma": "ta.wma",
    "hma": "ta.hma", "vwma": "ta.vwma", "swma": "ta.swma",
    "atr": "ta.atr", "tr": "ta.tr", "rsi": "ta.rsi", "macd": "ta.macd",
    "bb": "ta.bb", "bbw": "ta.bbw", "stoch": "ta.stoch", "cci": "ta.cci",
    "mfi": "ta.mfi", "roc": "ta.roc", "mom": "ta.mom", "change": "ta.change",
    "highest": "ta.highest", "lowest": "ta.lowest",
    "highestbars": "ta.highestbars", "lowestbars": "ta.lowestbars",
    "sum": "ta.sum", "median": "ta.median", "linreg": "ta.linreg",
    "correlation": "ta.correlation", "barssince": "ta.barssince",
    "valuewhen": "ta.valuewhen", "crossover": "ta.crossover",
    "crossunder": "ta.crossunder", "cross": "ta.cross", "rising": "ta.rising",
    "falling": "ta.falling", "cum": "ta.cum", "vwap": "ta.vwap",
    "stdev": "ta.stdev", "dev": "ta.dev", "variance": "ta.variance",
    "cmo": "ta.cmo",
    "abs": "math.abs", "max": "math.max", "min": "math.min",
    "round": "math.round", "floor": "math.floor", "ceil": "math.ceil",
    "sqrt": "math.sqrt", "pow": "math.pow", "log": "math.log",
    "sign": "math.sign", "avg": "math.avg", "exp": "math.exp",
    "security": "request.security", "crossover_": "ta.crossover",
}

# Canonical member names, used to fix casing (ta.SMA -> ta.sma)
_TA_MEMBERS = {
    "sma", "ema", "rma", "wma", "hma", "vwma", "swma", "tr", "atr", "rsi",
    "macd", "bb", "bbw", "stoch", "cci", "mfi", "roc", "mom", "change",
    "highest", "lowest", "highestbars", "lowestbars", "sum", "median",
    "linreg", "correlation", "barssince", "valuewhen", "crossover",
    "crossunder", "cross", "rising", "falling", "cum", "vwap", "stdev",
    "dev", "variance", "cmo", "percentile_linear_interpolation",
}
_MATH_MEMBERS = {
    "abs", "max", "min", "round", "floor", "ceil", "sqrt", "pow", "log",
    "log10", "avg", "sign", "exp", "isna", "na", "isfinite", "todegrees",
    "toradians",
}
_STR_OR_BUILTIN = {
    "plot", "plotshape", "plotchar", "plotarrow", "plotcandle", "plotbar",
    "hline", "fill", "bgcolor", "barcolor", "alert", "alertcondition",
    "input", "input.int", "input.float", "input.bool", "input.string",
    "input.timeframe", "input.session", "input.source", "input.color",
    "indicator", "strategy", "study", "nz", "na", "iff", "fixnan",
    "barstate", "syminfo", "timeframe", "request.security",
}

# Pine built-in names that must never be treated as user variables.
RESERVED = {
    "close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4",
    "hlcc4", "time", "time_close", "bar_index", "last_bar_index", "na",
    "true", "false", "and", "or", "not", "if", "else", "for", "to", "by",
    "var", "varip", "import", "export", "type", "method", "switch",
    "strategy", "indicator", "input", "plot", "barstate", "math", "ta",
    "request", "array", "matrix", "map", "line", "label", "box", "table",
    "color", "int", "float", "bool", "string",
}

# `plot(series=close, ...)` -> `plot(close, ...)` (engine reads positionally)
_KWARG_TO_POSITIONAL = {
    "plot": ["series", "style", "color", "linewidth", "title", "transp"],
    "plotshape": ["series", "style", "location", "color", "text", "title"],
}


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning" | "info"
    message: str
    line: int = 0
    code: str = ""
    fixed: bool = False

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
            "code": self.code,
            "fixed": self.fixed,
        }

    def __str__(self) -> str:
        pos = f"line {self.line}: " if self.line else ""
        return f"[{self.severity}] {pos}{self.message}"


@dataclass
class LintReport:
    ok: bool = True
    diagnostics: list[Diagnostic] = field(default_factory=list)
    fixed_source: str | None = None
    version: str = ""

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    def summary(self) -> str:
        if not self.diagnostics:
            return "No issues found."
        counts: dict[str, int] = {}
        for d in self.diagnostics:
            counts[d.severity] = counts.get(d.severity, 0) + 1
        parts = [f"{v} {k}{'s' if v != 1 else ''}" for k, v in counts.items()]
        return ", ".join(parts)


# --------------------------------------------------------------------------
# fixing
# --------------------------------------------------------------------------


def normalize_source(source: str) -> tuple[str, list[Diagnostic]]:
    """Apply deterministic whitespace / header normalisation."""
    diags: list[Diagnostic] = []
    original = source
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if source != original:
        diags.append(Diagnostic("info", "Normalised line endings", 0, "IO001"))

    lines = source.split("\n")
    out: list[str] = []
    tabs_fixed = False
    for line in lines:
        stripped = line.lstrip(" \t")
        indent = line[: len(line) - len(stripped)]
        if "\t" in indent:
            tabs_fixed = True
            indent = indent.replace("\t", "    ")
        out.append(indent + stripped)
    if tabs_fixed:
        diags.append(
            Diagnostic("info", "Replaced tab indentation with 4 spaces", 0, "IO002")
        )
    return "\n".join(out), diags


def stamp_version(source: str) -> tuple[str, list[Diagnostic]]:
    """Ensure a ``//@version=N`` pragma exists; rewrite v1-v4 to v5."""
    diags: list[Diagnostic] = []
    match = re.search(r"^\s*//\s*@version\s*=\s*(\d+)", source, re.MULTILINE)
    if match:
        version = match.group(1)
        if version in {"1", "2", "3", "4"}:
            source = re.sub(
                r"^(\s*//\s*@version\s*=\s*)\d+",
                r"\g<1>5",
                source,
                count=1,
                flags=re.MULTILINE,
            )
            diags.append(
                Diagnostic(
                    "info",
                    f"Upgraded Pine v{version} pragma to v5 (engine subset covers v4 calls too)",
                    1,
                    "VER001",
                )
            )
        return source, diags
    diags.append(Diagnostic("info", "Added missing //@version=5 pragma", 1, "VER002"))
    return "//@version=5\n" + source, diags


def _is_namespaced(text: str, start: int) -> bool:
    prefix = text[max(0, start - 4) : start]
    return bool(re.search(r"(ta|math|request|str|array|matrix|map|color|line|label|box|table|input|strategy)\.$", prefix))


def upgrade_legacy_calls(source: str) -> tuple[str, list[Diagnostic]]:
    """Prefix bare v4 calls with their v5 namespace."""
    diags: list[Diagnostic] = []
    changed: set[str] = set()

    def repl(match: re.Match) -> str:
        name = match.group("name")
        if _is_namespaced(match.string, match.start("name")):
            return match.group(0)
        canonical = V4_TO_V5.get(name)
        if not canonical:
            return match.group(0)
        # Only rewrite when it looks like a call: name ( ... ) possibly on the
        # same line.
        after = match.string[match.end("name") :].lstrip()
        if not after.startswith("("):
            return match.group(0)
        changed.add(name)
        return canonical

    pattern = re.compile(r"\b(?<![\w.])(?P<name>" + "|".join(sorted(V4_TO_V5, key=len, reverse=True)) + r")\b")
    new = pattern.sub(repl, source)
    for name in sorted(changed):
        diags.append(
            Diagnostic(
                "info",
                f"Upgraded legacy call {name}() -> {V4_TO_V5[name]}()",
                0,
                "V5CALL",
            )
        )
    return new, diags


def fix_member_casing(source: str) -> tuple[str, list[Diagnostic]]:
    """Lowercase ``ta.SMA`` / ``math.ABS`` style mistakes."""
    diags: list[Diagnostic] = []

    def repl(match: re.Match) -> str:
        ns, member = match.group(1), match.group(2)
        pool = _TA_MEMBERS if ns == "ta" else _MATH_MEMBERS
        low = member.lower()
        if member != low and low in {m.lower() for m in pool}:
            diags.append(
                Diagnostic(
                    "info",
                    f"Fixed casing {ns}.{member} -> {ns}.{low}",
                    match.string[: match.start()].count("\n") + 1,
                    "CASE001",
                )
            )
            return f"{ns}.{low}"
        return match.group(0)

    pattern = re.compile(r"\b(ta|math)\.([A-Za-z_][A-Za-z0-9_]*)")
    return pattern.sub(repl, source), diags


def modernize_headers(source: str) -> tuple[str, list[Diagnostic]]:
    """``study(`` -> ``indicator(``, ``security(`` -> ``request.security(``."""
    diags: list[Diagnostic] = []
    if re.search(r"\bstudy\s*\(", source):
        source = re.sub(r"\bstudy\s*\(", "indicator(", source)
        diags.append(Diagnostic("info", "Replaced study() with indicator()", 0, "HDR001"))
    if re.search(r"(?<!request\.)\bsecurity\s*\(", source):
        source = re.sub(r"(?<!request\.)\bsecurity\s*\(", "request.security(", source)
        diags.append(
            Diagnostic("info", "Replaced security() with request.security()", 0, "HDR002")
        )
    return source, diags


def rewrite_iff(source: str) -> tuple[str, list[Diagnostic]]:
    """``iff(cond, a, b)`` -> ``(cond ? a : b)``."""
    diags: list[Diagnostic] = []
    pattern = re.compile(r"\biff\s*\(([^,()]+),([^,()]+),([^()]+)\)")

    def repl(match: re.Match) -> str:
        diags.append(Diagnostic("info", "Rewrote iff() as a ternary expression", 0, "IFF001"))
        return f"(({match.group(1).strip()}) ? ({match.group(2).strip()}) : ({match.group(3).strip()}))"

    new, count = pattern.subn(repl, source)
    if count == 0:
        diags.clear()
    return new, diags


def fix_if_assignments(source: str) -> tuple[str, list[Diagnostic]]:
    """Inside an ``if`` block, a plain ``=`` assignment must be ``:=``.

    Works by tracking indentation: once inside an indented block, any
    ``identifier = ...`` that is not the first declaration of that name and does
    not carry ``var`` is converted to ``:=``.
    """
    diags: list[Diagnostic] = []
    lines = source.split("\n")
    out: list[str] = []
    indent_stack: list[int] = []
    declared: set[str] = set()

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped:
            out.append(line)
            continue
        while indent_stack and indent <= indent_stack[-1] and not stripped.startswith("else"):
            indent_stack.pop()
        in_block = bool(indent_stack) and indent > indent_stack[-1]

        if stripped.startswith(("if ", "for ")) and stripped.endswith(":"):
            indent_stack.append(indent)
            out.append(line)
            continue

        m = re.match(r"^(?P<indent> *)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)(?P<rest>.*)$", line)
        if m and in_block and not stripped.startswith("var"):
            name = m.group("name")
            if name in declared and name not in RESERVED:
                diags.append(
                    Diagnostic(
                        "info",
                        f"Reassignment inside block: {name} = -> {name} :=",
                        len(out) + 1,
                        "ASSIGN001",
                    )
                )
                line = f"{m.group('indent')}{name} :={m.group('rest')}"
        elif m:
            declared.add(m.group("name"))
        out.append(line)
    return "\n".join(out), diags


def autofix(source: str) -> tuple[str, list[Diagnostic]]:
    """Run every safe fix in order and return (fixed_source, diagnostics)."""
    diags: list[Diagnostic] = []
    source, d = normalize_source(source)
    diags.extend(d)
    source, d = stamp_version(source)
    diags.extend(d)
    source, d = modernize_headers(source)
    diags.extend(d)
    source, d = rewrite_iff(source)
    diags.extend(d)
    source, d = fix_member_casing(source)
    diags.extend(d)
    source, d = upgrade_legacy_calls(source)
    diags.extend(d)
    source, d = fix_if_assignments(source)
    diags.extend(d)
    return source, diags


# --------------------------------------------------------------------------
# linting
# --------------------------------------------------------------------------

_SUSPICIOUS = [
    (re.compile(r"\brepaint\b", re.I), "warning", "Script mentions repainting; signals may not be causal.", "SEM001"),
    (re.compile(r"\brequest\.security\s*\([^)]*lookahead\s*=\s*barmerge\.lookahead_on", re.I),
     "warning", "lookahead=barmerge.lookahead_on makes the script repaint.", "SEM002"),
    (re.compile(r"\btimenow\b"), "warning", "timenow is unavailable on historical bars.", "SEM003"),
    (re.compile(r"\bstrategy\.(risk\.|commission\.|slippage\.)"), "info", "Strategy properties are parsed but not modelled in backtests.", "SEM004"),
    (re.compile(r"\bvarip\b"), "warning", "varip (intrabar persistence) is treated like var on closed bars.", "SEM005"),
    (re.compile(r"\blabel\.(new|set_)|table\.(new|cell)", re.I), "info", "Labels/tables are accepted but not rendered.", "SEM006"),
    (re.compile(r"\balert\s*\("), "info", "alert() calls are recorded, not dispatched.", "SEM007"),
]


def lint(source: str, *, autofix_enabled: bool = True) -> LintReport:
    """Lint *source* and optionally compute a fixed version."""
    report = LintReport()
    if not source or not source.strip():
        report.ok = False
        report.diagnostics.append(Diagnostic("error", "Script is empty", 0, "SYN000"))
        return report

    normalized, normal_diags = normalize_source(source)
    version_match = re.search(r"//\s*@version\s*=\s*(\d+)", normalized)
    report.version = version_match.group(1) if version_match else ""

    working = normalized
    if autofix_enabled:
        working, fix_diags = autofix(normalized)
        report.diagnostics.extend(fix_diags)
    else:
        _, fix_diags = stamp_version(normalized)
        working = normalized

    if autofix_enabled:
        report.fixed_source = working
    report.diagnostics.extend(normal_diags[:1])

    # syntax check on both versions
    for label, text in (("original", normalized), ("fixed", working)):
        try:
            parse(text)
        except PineSyntaxError as exc:
            if label == "original" and report.fixed_source is not None:
                # fixed version parsed fine -> report as auto-fixed
                report.diagnostics.append(
                    Diagnostic(
                        "warning",
                        f"Original failed to parse ({exc}); auto-fix repaired it",
                        exc.line,
                        "SYN001",
                    )
                )
            else:
                report.ok = False
                report.diagnostics.append(
                    Diagnostic("error", str(exc), exc.line, "SYN002")
                )

    # semantic warnings
    for pattern, severity, message, code in _SUSPICIOUS:
        for match in pattern.finditer(normalized):
            line = normalized[: match.start()].count("\n") + 1
            report.diagnostics.append(Diagnostic(severity, message, line, code))

    # duplicate variable declaration check
    declared: dict[str, int] = {}
    for idx, line in enumerate(working.split("\n"), 1):
        m = re.match(r"^\s*(?:var\s+|varip\s+)?(?:float\s+|int\s+|bool\s+|string\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) not in RESERVED:
            name = m.group(1)
            if name in declared:
                report.diagnostics.append(
                    Diagnostic(
                        "warning",
                        f"{name} declared again (first at line {declared[name]}); "
                        "use := for reassignment",
                        idx,
                        "VAR001",
                    )
                )
            else:
                declared[name] = idx

    if report.errors:
        report.ok = False
    return report


def format_report(report: LintReport, *, show_fixed: bool = False) -> str:
    lines: list[str] = []
    status = "PASS" if report.ok else "FAIL"
    lines.append(f"Pine lint: {status} ({report.summary()})")
    for diag in report.diagnostics:
        marker = {"error": "x", "warning": "!", "info": "-"}.get(diag.severity, "-")
        pos = f"L{diag.line} " if diag.line else ""
        lines.append(f"  {marker} {pos}{diag.message}  [{diag.code}]")
    if show_fixed and report.fixed_source:
        lines.append("")
        lines.append("--- fixed script ---")
        lines.append(report.fixed_source)
    return "\n".join(lines)

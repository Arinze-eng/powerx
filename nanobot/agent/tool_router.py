"""Generic pre-LLM tool router: map an ask to a tool call without the model.

The repo already ships narrow pre-LLM routers (``deterministic_router`` for
product-specific read-only lookups, ``task_router`` for recurring task
families). This module is the *declarative* generalization of the same
discipline: a registry of routes, each mapping intent patterns to a
registered tool with argument builders, so a routine ask costs ZERO provider
round-trips.

Fail-open by design, mirroring the existing routers:

* Anything ambiguous, write-shaped, image-bearing, or over-long falls
  straight through to the normal LLM path unchanged — correctness is never
  sacrificed, only routine cost disappears.
* A route's builder raising, or its tool not being registered on the run,
  is a miss, not an error.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

#: Flag: set POWERX_TOOL_ROUTER=0 to disable the router entirely.
_ENABLED = os.environ.get("POWERX_TOOL_ROUTER", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

#: Only short, single-ask texts are routed. Long/complex asks belong to the
#: model (or the planner), not a pattern match.
_MAX_ROUTE_CHARS = 400

#: Route argument builder: match -> tool args dict.
ArgBuilder = Callable[[re.Match[str]], dict[str, Any]]


def router_enabled() -> bool:
    return _ENABLED


@dataclass(slots=True)
class ToolRoute:
    """One intent -> (tool, args) mapping."""

    name: str
    patterns: tuple[re.Pattern[str], ...]
    build_args: ArgBuilder
    description: str = ""
    #: Route only fires when every keyword is present-ish? No: patterns carry
    #: the intent; this is documentation-only.
    example: str = ""

    def matches(self, text: str) -> re.Match[str] | None:
        for pattern in self.patterns:
            match = pattern.search(text)
            if match:
                return match
        return None


def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


class ToolRouter:
    """Ordered route registry consulted BEFORE any provider call."""

    def __init__(self) -> None:
        self._routes: list[ToolRoute] = []

    def register(
        self,
        name: str,
        patterns: tuple[str, ...],
        build_args: ArgBuilder,
        *,
        description: str = "",
        example: str = "",
    ) -> ToolRoute:
        route = ToolRoute(
            name=name,
            patterns=_compile(patterns),
            build_args=build_args,
            description=description,
            example=example,
        )
        self._routes.append(route)
        return route

    @property
    def routes(self) -> tuple[ToolRoute, ...]:
        return tuple(self._routes)

    def route(self, text: str) -> tuple[str, dict[str, Any]] | None:
        """Match ``text`` against the registry.

        Returns ``(tool_name, args)`` for the first matching route, or None
        to fall through to the normal LLM path. Never raises.
        """
        if not _ENABLED:
            return None
        text = (text or "").strip()
        if not text or len(text) > _MAX_ROUTE_CHARS:
            return None
        if "\n" in text:  # multi-line = structured ask, not a routine lookup
            return None
        for route in self._routes:
            match = route.matches(text)
            if match is None:
                continue
            try:
                args = route.build_args(match)
            except Exception as exc:  # noqa: BLE001 - fail-open
                logger.debug("tool router: route {} builder failed: {}", route.name, exc)
                continue
            if not isinstance(args, dict) or not args:
                continue
            logger.info("tool router: '{}' -> {} (no LLM call)", route.name, route.name)
            return route.name, args
        return None


def default_router() -> ToolRouter:
    """A starter registry with safe, read-only, universally useful routes.

    Routes emit the SAME generic tools the agent already registers
    (``exec`` for read-only shell, ``read_file``, ``list_dir``-style) so
    callers can execute them through the live ToolRegistry unchanged. All
    recipes are read-only; anything write-shaped is deliberately absent.
    """
    router = ToolRouter()

    def _pwd_list(_match: re.Match[str]) -> dict[str, Any]:
        return {"args": {"command": "ls -la"}}

    def _read_file(match: re.Match[str]) -> dict[str, Any]:
        return {"args": {"path": match.group(1).strip()}}

    def _find_file(match: re.Match[str]) -> dict[str, Any]:
        needle = match.group(1).strip().strip("'\"")
        return {"args": {"command": f"find . -iname '{needle}' -not -path '*/.git/*' | head -50"}}

    def _disk_usage(_match: re.Match[str]) -> dict[str, Any]:
        return {"args": {"command": "du -sh . 2>/dev/null | sort -rh | head -20"}}

    def _date(_match: re.Match[str]) -> dict[str, Any]:
        return {"args": {"command": "date -u '+%Y-%m-%d %H:%M:%S UTC'"}}

    router.register(
        "exec",
        (r"^(?:what(?:'s| is) )?(?:in |the )?(?:current )?dir(?:ectory)?\??$", r"^ls( -la)?\??$", r"^list (?:the )?files( here)?\??$"),
        _pwd_list,
        description="List the current directory",
        example="ls -la",
    )
    router.register(
        "read_file",
        (r"^read (?:the )?file ([\w./~ -]+\.[A-Za-z0-9]{1,8})$", r"^(?:show|open) ([\w./~ -]+\.[A-Za-z0-9]{1,8})$"),
        _read_file,
        description="Read one file by exact name",
        example="read file README.md",
    )
    router.register(
        "exec",
        (r"^find (?:a |the )?file (?:named |called )?(.+)$",),
        _find_file,
        description="Find files by name",
        example="find a file named config.json",
    )
    router.register(
        "exec",
        (r"^(?:what(?:'s| is) )?(?:the )?disk usage\??$", r"^du\??$"),
        _disk_usage,
        description="Show disk usage summary",
        example="what's the disk usage?",
    )
    router.register(
        "exec",
        (r"^(?:what(?:'s| is) )?(?:the )?(?:current )?(?:utc )?(?:time|date)(?: now)?\??$", r"^date\??$"),
        _date,
        description="Return the current UTC time",
        example="what time is it?",
    )
    return router

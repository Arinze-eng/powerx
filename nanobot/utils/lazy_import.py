"""Defer heavy third-party imports until the first time they are actually used.

Why this exists
---------------
The gateway is a long-lived, single-process deployment on a fixed-size plan
(488 MiB). Whatever the process imports at *module scope* is paid for on every
boot and then sits in RSS for the life of the container -- it is the floor that
every concurrent turn has to fit above, and no amount of garbage collection or
``malloc_trim`` can hand it back, because those modules really are still
referenced.

Most of that floor is libraries that are only needed on a specific code path:
``numpy`` (document/image forensics), ``aiohttp`` (sandbox backends),
``cryptography`` (Supabase token decryption), the MCP SDK (MCP OAuth). All of
them are used inside function bodies, but were imported at module scope, so
merely *registering the tools* dragged them into memory.

``lazy_module`` / ``lazy_attr`` return a transparent proxy that imports the real
object on first attribute access. Existing call sites such as ``np.zeros(...)``,
``aiohttp.ClientSession(...)`` or ``OAuthToken.model_validate(...)`` keep
working unchanged -- the import simply happens at the point of use, once.

Prefer this over sprinkling ``import`` statements inside functions: it is a
single-line change per module, it covers every call site at once, and it cannot
introduce an ``UnboundLocalError`` shadowing bug.
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

__all__ = [
    "lazy_attr",
    "lazy_module",
    "optional_attr",
    "optional_module",
]


class _LazyProxy:
    """Resolve ``<module>`` (and optionally ``.<attr>``) on first real use."""

    __slots__ = ("_module_name", "_attr_name", "_resolved")

    def __init__(self, module_name: str, attr_name: str | None = None) -> None:
        object.__setattr__(self, "_module_name", module_name)
        object.__setattr__(self, "_attr_name", attr_name)
        object.__setattr__(self, "_resolved", None)

    def _resolve(self) -> Any:
        resolved = object.__getattribute__(self, "_resolved")
        if resolved is None:
            module = importlib.import_module(object.__getattribute__(self, "_module_name"))
            attr_name = object.__getattribute__(self, "_attr_name")
            resolved = module if attr_name is None else getattr(module, attr_name)
            object.__setattr__(self, "_resolved", resolved)
        return resolved

    # --- attribute / call / isinstance forwarding -------------------------
    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def __setattr__(self, key: str, value: Any) -> None:
        setattr(self._resolve(), key, value)

    def __delattr__(self, item: str) -> None:
        delattr(self._resolve(), item)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._resolve()(*args, **kwargs)

    def __instancecheck__(self, instance: Any) -> bool:
        return isinstance(instance, self._resolve())

    def __subclasscheck__(self, subclass: type) -> bool:
        return issubclass(subclass, self._resolve())

    def __getitem__(self, item: Any) -> Any:
        return self._resolve()[item]

    def __iter__(self):
        return iter(self._resolve())

    def __dir__(self):
        return dir(self._resolve())

    def __repr__(self) -> str:
        attr_name = object.__getattribute__(self, "_attr_name")
        target = object.__getattribute__(self, "_module_name")
        if attr_name is not None:
            target = f"{target}.{attr_name}"
        state = "resolved" if object.__getattribute__(self, "_resolved") is not None else "pending"
        return f"<lazy {target} ({state})>"


def lazy_module(module_name: str) -> Any:
    """Return a proxy that imports ``module_name`` on first use.

    Use as a drop-in for ``import x`` / ``import x as y``::

        import numpy as np              ->  np = lazy_module("numpy")
        import aiohttp                  ->  aiohttp = lazy_module("aiohttp")
    """
    return _LazyProxy(module_name)


def lazy_attr(module_name: str, attr_name: str) -> Any:
    """Return a proxy that resolves ``module_name.attr_name`` on first use.

    Use as a drop-in for ``from m import x``::

        from m import X                 ->  X = lazy_attr("m", "X")
    """
    return _LazyProxy(module_name, attr_name)


class _OptionalProxy:
    """A lazily-resolved module or member that is *falsy* when it is missing.

    The module-scope optional-dependency idiom::

        try:
            import asyncssh
        except ImportError:
            asyncssh = None

    is not free. The ``try`` body runs on every boot, whether or not the code
    path that needs the library is ever taken, and the library then sits in RSS
    for the life of the container. Measured on this deployment that idiom was
    charging the gateway ~42 MB of its permanent floor for three libraries
    (``novita_sandbox``, ``asyncssh``, ``pydoll``) that most turns never touch.

    This proxy keeps the idiom's *observable* behaviour -- the value is falsy
    when the library is not installed, so ``if x is None`` becomes ``if not x``
    -- while deferring the import to the first real use. It is only for the
    optional case: a library that is definitely installed should keep a plain
    ``import``.
    """

    __slots__ = ("_module_name", "_attr_name", "_resolved", "_value")

    def __init__(self, module_name: str, attr_name: str | None = None) -> None:
        object.__setattr__(self, "_module_name", module_name)
        object.__setattr__(self, "_attr_name", attr_name)
        object.__setattr__(self, "_resolved", False)
        object.__setattr__(self, "_value", None)

    def _resolve(self) -> Any:
        if not object.__getattribute__(self, "_resolved"):
            module_name = object.__getattribute__(self, "_module_name")
            attr_name = object.__getattribute__(self, "_attr_name")
            try:
                module = importlib.import_module(module_name)
                value = module if attr_name is None else getattr(module, attr_name)
            except (ImportError, AttributeError):
                value = None
            object.__setattr__(self, "_value", value)
            object.__setattr__(self, "_resolved", True)
        return object.__getattribute__(self, "_value")

    # --- the missing-library signal the old idiom carried -------------------
    def __bool__(self) -> bool:
        return self._resolve() is not None

    # --- forwarding ---------------------------------------------------------
    def __getattr__(self, item: str) -> Any:
        target = self._resolve()
        if target is None:
            raise AttributeError(
                f"{object.__getattribute__(self, '_module_name')} is not installed"
            )
        return getattr(target, item)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        target = self._resolve()
        if target is None:
            raise RuntimeError(
                f"{object.__getattribute__(self, '_module_name')} is not installed"
            )
        return target(*args, **kwargs)

    def __getitem__(self, item: Any) -> Any:
        return self._resolve()[item]

    def __iter__(self):
        return iter(self._resolve())

    def __dir__(self):
        target = self._resolve()
        return dir(target) if target is not None else []

    def __repr__(self) -> str:
        attr_name = object.__getattribute__(self, "_attr_name")
        target = object.__getattribute__(self, "_module_name")
        if attr_name is not None:
            target = f"{target}.{attr_name}"
        state = "resolved" if object.__getattribute__(self, "_resolved") else "pending"
        return f"<optional {target} ({state})>"


def optional_module(module_name: str) -> Any:
    """Return a falsy-when-missing proxy that imports ``module_name`` on first use.

    Use as a drop-in for ``try: import x / except ImportError: x = None``.
    Replace the ``x is None`` guard with ``not x``::

        try:                            x = optional_module("asyncssh")
            import asyncssh             ...
        except ImportError:             if not x: ...
            asyncssh = None
    """
    return _OptionalProxy(module_name)


def optional_attr(module_name: str, attr_name: str) -> Any:
    """Return a falsy-when-missing proxy resolving ``module_name.attr_name`` lazily.

    Use as a drop-in for ``try: from m import X / except ImportError: X = None``.
    """
    return _OptionalProxy(module_name, attr_name)


def optional_installed(module_name: str) -> bool:
    """Whether *module_name* is importable, **without** importing it.

    ``bool(optional_module(x))`` answers the same question but pays the import to
    do it, which is the cost this module exists to avoid. Availability checks
    that gate a tool's ``enabled()`` run while the tool list is built on every
    turn, so they must use this one instead. Pass a *top-level* name: resolving a
    dotted name imports its parent packages, which is the thing being avoided.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, AttributeError):  # pragma: no cover - defensive
        return False

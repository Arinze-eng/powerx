"""Every lazily-imported execution-backend SDK must be a declared dependency.

The execution backends import their vendor SDK **lazily** so the tool module
stays importable without it. That is deliberate, but it has a cost: a missing
dependency does not fail the build, the install, or the test suite. It fails at
runtime in the deployed image, as a single sentence, at the moment an operator
tries to use the backend:

    "The Tenki SDK is not installed. Install it with `pip install tenki`"

That is exactly what happened with Tenki: the backend, the rotation, the admin
panel and 89 tests all shipped, and the image had no `tenki` package in it
because nobody had added the distribution to ``pyproject.toml``. The repository
has hit this before — see the comment on ``dulwich`` in ``pyproject.toml``, which
was "never declared, so GitStore silently failed in built images".

These tests make the omission loud and cheap to catch instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: module name imported lazily -> (distribution that provides it, the file that imports it)
BACKEND_SDK_IMPORTS: dict[str, tuple[str, str]] = {
    "tenki": ("tenki", "nanobot/agent/tools/tenki_backend.py"),
    "novita_sandbox": ("novita-sandbox", "nanobot/agent/tools/novita_sandbox.py"),
}


def _normalise(requirement: str) -> str:
    """Reduce a PEP 508 requirement to its bare distribution name.

    ``"websockets>=15.0.0,<16.0.0"`` -> ``"websockets"`` and
    ``"tenki[async]>=1.3"`` -> ``"tenki"``. Comparison is case-insensitive and
    treats ``-``/``_``/``.`` as equivalent, because distribution names are.
    """
    name = re.split(r"[<>=!~;\[\s]", requirement.strip(), maxsplit=1)[0]
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _declared_distributions() -> set[str]:
    """Every distribution named in ``[project] dependencies`` or any extra."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    declared = {_normalise(entry) for entry in project.get("dependencies", [])}
    for extra in project.get("optional-dependencies", {}).values():
        declared.update(_normalise(entry) for entry in extra)
    return declared


def test_every_execution_backend_sdk_is_declared_as_a_dependency() -> None:
    """The deployed image installs from pyproject; an undeclared SDK is absent."""
    declared = _declared_distributions()
    missing = {
        module: distribution
        for module, (distribution, _source) in BACKEND_SDK_IMPORTS.items()
        if _normalise(distribution) not in declared
    }
    assert not missing, (
        "these execution-backend SDKs are imported lazily, so nothing fails until an "
        "operator uses the backend in a deployed image. Declare them in "
        f"pyproject.toml [project] dependencies: {missing}"
    )


def test_the_sdk_map_still_matches_the_source_it_describes() -> None:
    """A renamed module would otherwise let the map above pass while proving nothing.

    Each entry claims a specific module is imported in a specific file. If that
    import is ever renamed or removed, the entry stops describing reality and the
    dependency assertion quietly becomes vacuous.
    """
    stale: dict[str, str] = {}
    for module, (_distribution, source) in BACKEND_SDK_IMPORTS.items():
        path = REPO_ROOT / source
        if not path.is_file():
            stale[module] = f"{source} does not exist"
            continue
        body = path.read_text(encoding="utf-8")
        if not re.search(rf"\b(from|import)\s+{re.escape(module)}\b", body):
            stale[module] = f"{source} no longer imports {module!r}"
    assert not stale, f"BACKEND_SDK_IMPORTS is out of date: {stale}"

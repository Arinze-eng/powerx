"""The host side of a dispatched turn: turn live configuration into a request.

The administrator already chooses the model and the sandbox -- on the provider
page and the execution page. This module is what carries those two choices, plus
the transcript, out to the sandbox. Nothing here decides anything: it reads the
resolved configuration through the same public accessors the rest of the
deployment uses, so the model a sandbox turn runs on is by construction the model
the admin page says is active, and cannot drift from it.

REFUSING IS A FEATURE
---------------------
:func:`build_turn_request` returns ``None`` when the active provider cannot be
reproduced from one credential. The case that matters is the admin **pool**: the
host builds a rotating provider over several keys, and a sandbox handed a single
``api_key`` would silently run on one lane of it -- a different model, a
different cost, a different failure mode, with nothing in the log to say so.
Refusing sends the turn back to the host, where the pool is intact. A turn that
cannot be reproduced faithfully must not be moved.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.remote_turn.protocol import ProviderCredentials, RemoteTurnRequest

#: (mtime, size, config) -- re-read when the file it came from changes. The size
#: is part of the key because two saves inside one filesystem timestamp tick
#: would otherwise look identical, and the second one is the admin's real choice.
_config_cache: tuple[int, int, Any] | None = None


def active_config() -> Any:
    """The live configuration, re-read whenever the file it came from changes.

    The loop does not hold a ``Config``: it holds the values derived from one, and
    the admin page mutates the file underneath it. Re-reading on a changed mtime
    is what keeps a dispatched turn on the model the admin has *now* selected
    rather than the one selected at process start, without paying a config parse
    on every turn.
    """
    global _config_cache

    from nanobot.config.loader import get_config_path, load_config

    path = get_config_path()
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (-1, -1)
    if _config_cache is not None and _config_cache[:2] == stamp:
        return _config_cache[2]
    config = load_config()
    _config_cache = (stamp[0], stamp[1], config)
    return config


def provider_credentials(config: Any) -> ProviderCredentials | None:
    """Read the active provider selection, or ``None`` if it cannot be sent."""
    preset = config.resolve_preset()
    model = str(preset.model or "")
    if not model:
        return None

    try:
        from nanobot.providers.factory import make_pool_provider

        if make_pool_provider(config, preset=preset) is not None:
            # Several lanes, one credential. See the module docstring.
            logger.info(
                "remote turn: refusing to dispatch -- the active provider is an "
                "admin pool, which a single credential cannot reproduce"
            )
            return None
    except Exception as exc:  # noqa: BLE001 - an unresolvable pool is not a reason to stop
        logger.debug("remote turn: pool check failed: {}", exc)

    name = str(config.get_provider_name(model, preset=preset) or preset.provider or "")
    if not name or name == "auto":
        return None
    provider_config = config.get_provider(model, preset=preset)
    return ProviderCredentials(
        provider=name,
        model=model,
        api_key=str(config.get_api_key(model, preset=preset) or ""),
        api_base=config.get_api_base(model, preset=preset),
        extra_headers=dict(getattr(provider_config, "extra_headers", None) or {}),
    )


def build_turn_request(
    config: Any,
    *,
    session_key: str,
    messages: list[dict[str, Any]],
    workspace: str | None = None,
    channel: str = "",
    chat_id: str = "",
    max_iterations: int = 0,
    max_tool_result_chars: int = 0,
    turn_budget_s: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> RemoteTurnRequest | None:
    """Package one turn for the sandbox, or ``None`` to run it on the host."""
    if not messages:
        return None
    credentials = provider_credentials(config)
    if credentials is None:
        return None
    return RemoteTurnRequest(
        session_key=session_key,
        messages=messages,
        credentials=credentials,
        workspace=workspace,
        max_iterations=max_iterations,
        max_tool_result_chars=max_tool_result_chars,
        turn_budget_s=turn_budget_s,
        channel=channel,
        chat_id=chat_id,
        metadata=dict(metadata or {}),
    )

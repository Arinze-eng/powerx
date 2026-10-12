"""The host side of a dispatched turn: turn live configuration into a request.

The administrator already chooses the model and the sandbox -- on the provider
page and the execution page. This module is what carries those two choices, plus
the transcript, out to the sandbox. Nothing here decides anything: it reads the
resolved configuration through the same public accessors the rest of the
deployment uses, so the model a sandbox turn runs on is by construction the model
the admin page says is active, and cannot drift from it.

THE POOL TRAVELS TOO
--------------------
A deployment whose model comes from the admin **pool** has no single provider:
the host rotates across several endpoints, so "which provider" has no one
answer. Handing the sandbox only ``credentials`` would silently pin the turn to
one lane of the pool -- a different endpoint, a different cost, a different
failure mode, with nothing in the log to say so.

So the pool travels as its own field -- the *whole* lane set the host rotates
over, main provider's fallback lane included, not just the admin's saved
entries. The worker does not reimplement the rotation: it sets the pool the way
the admin panel sets it (``PROVIDER-POOL-JSON`` plus the pool file) and lets the
project's own ``make_pool_provider`` pick it up, with the ambient admin lane
switched off so the sandbox contributes no lane of its own. Host and sandbox
then rotate over the same lanes by construction.

:func:`build_turn_request` returns ``None`` only when there is genuinely nothing
to run on -- no pool *and* no identified single provider. A turn that cannot be
reproduced faithfully must not be moved.
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


def active_pool(config: Any) -> list[dict[str, Any]]:
    """The lanes the active pool rotates over, or ``[]`` when no pool is in use.

    Asked of the provider factory rather than read from the environment directly,
    so what travels is the *whole* lane set -- the admin's saved entries plus the
    main provider's last-resort lane -- and not merely the source of it. Sending
    the saved entries alone would give the sandbox a shorter rotation than the
    host has, and a turn would then have one fewer endpoint to fall through to
    depending on where it ran.
    """
    try:
        from nanobot.providers.factory import pool_lane_entries

        return list(pool_lane_entries(config))
    except Exception as exc:  # noqa: BLE001 - a broken pool read is not fatal
        logger.warning("remote turn: could not read the provider pool: {}", exc)
        return []


def provider_credentials(config: Any) -> ProviderCredentials | None:
    """Read the active provider selection, or ``None`` if there is nothing to run on."""
    preset = config.resolve_preset()
    model = str(preset.model or "")
    if not model:
        return None

    name = str(config.get_provider_name(model, preset=preset) or preset.provider or "")
    if name == "auto":
        name = ""

    if not name and not active_pool(config):
        # No pool and no identified provider: nothing the sandbox could run a
        # turn against, so it stays on the host.
        logger.info(
            "remote turn: refusing to dispatch -- no provider pool is configured "
            "and the active model matches no single provider"
        )
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
        pool=active_pool(config),
        workspace=workspace,
        max_iterations=max_iterations,
        max_tool_result_chars=max_tool_result_chars,
        turn_budget_s=turn_budget_s,
        channel=channel,
        chat_id=chat_id,
        metadata=dict(metadata or {}),
    )

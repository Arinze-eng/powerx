"""Settings -> Brokers: per-user MT5 broker logins for the Brokers method.

This is the HTTP/WS-facing half of the feature whose storage lives in
``nanobot.trading.broker_credentials`` and whose execution lives in
``nanobot.agent.tools.broker_trade``. It exists so a user can enter their own
account number, password and server once, in the web app, and then trade --
without an operator having to configure a broker for the whole deployment.

Three rules shape everything here:

* **A password never leaves the server.** No action returns one, in any
  spelling. The payload reports only whether a password is stored.
* **One user, one identity.** Every action is keyed by the signed-in Supabase
  user id; a call with no identity is refused rather than served from a shared
  default, because serving it would show one user another user's account.
* **Validation is not installation.** ``test`` answers "will this server resolve
  to a terminal build we can install" -- the question that actually predicts a
  login working. It does not install anything and does not log in; a real login
  happens in the sandbox, from the agent turn.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BrokerSettingsError",
    "brokers_action",
    "brokers_payload",
]


class BrokerSettingsError(ValueError):
    """A user-facing validation failure from the Brokers pane."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _store():
    from nanobot.trading.broker_credentials import BrokerCredentialStore

    return BrokerCredentialStore()


def _require_user(user_id: str) -> str:
    key = (user_id or "").strip()
    if not key:
        raise BrokerSettingsError("Sign in to manage broker accounts", status=401)
    return key


def _string(payload: dict[str, Any] | None, key: str) -> str:
    value = (payload or {}).get(key)
    if value is None:
        return ""
    if not isinstance(value, (str, int)):
        raise BrokerSettingsError(f"{key} must be text")
    return str(value).strip()


def _truthy(payload: dict[str, Any] | None, key: str) -> bool:
    value = (payload or {}).get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    raise BrokerSettingsError(f"{key} must be boolean")


def _server_profile(server: str) -> dict[str, Any]:
    """What is known about the terminal build a *server* needs.

    The server name is what decides the build -- a generic MetaQuotes terminal
    cannot resolve an Exness server and silently never authorizes (see
    ``scripts/mt5_cli.py``'s broker registry). Surfacing that here means the
    user learns "this server needs the Deriv build" *before* a two-minute
    install, not after a login that fails with no error in the terminal log.
    """
    if not server:
        return {"server": "", "recognised": False, "verified": False, "install_dir": ""}
    from nanobot.trading.broker_discovery import (
        broker_is_verified,
        known_broker_candidates,
        known_broker_dir_name,
    )

    try:
        candidates = known_broker_candidates(server)
        return {
            "server": server,
            "recognised": bool(candidates),
            "verified": broker_is_verified(server),
            "install_dir": known_broker_dir_name(server),
            "installer_slugs": [slug for slug, _name in candidates][:4],
        }
    except Exception:  # noqa: BLE001 - an unknown server is an answer, not a 500
        return {"server": server, "recognised": False, "verified": False, "install_dir": ""}


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    """One stored account, as the pane is allowed to see it."""
    server = str(row.get("server") or "")
    return {
        "id": row.get("id"),
        "broker": str(row.get("broker") or ""),
        "server": server,
        "login": str(row.get("login") or ""),
        "is_default": bool(row.get("is_default")),
        "updated_at": row.get("updated_at") or "",
        # Never the password: only that one exists.
        "has_password": True,
        "server_profile": _server_profile(server),
    }


def _unavailable() -> dict[str, Any]:
    """The read model when this deployment has no credential storage at all."""
    return {
        "available": False,
        "enabled": False,
        "accounts": [],
        "count": 0,
        "default_broker": "",
        "default_server": "",
        "message": (
            "Broker accounts need Supabase storage to be configured on this "
            "deployment. An administrator can enable it."
        ),
    }


def _global_defaults() -> dict[str, str]:
    """The deployment's own default broker, for an account the user has not set up.

    Read from the same access config the admin page writes, so "the global
    default" is one value with one owner rather than a second setting that can
    disagree with it.
    """
    try:
        from nanobot.access_policy import access_policy

        broker, server = access_policy().default_broker()
        return {"default_broker": broker, "default_server": server}
    except Exception:  # noqa: BLE001 - a missing config is an empty default
        return {"default_broker": "", "default_server": ""}


def brokers_payload(*, user_id: str, store: Any = None) -> dict[str, Any]:
    """The Brokers pane's read model: the user's accounts, never their passwords."""
    key = _require_user(user_id)
    store = store or _store()
    if not store.enabled:
        return _unavailable()
    return _run(_list(key, store))


def _run(coro: Any) -> Any:
    """Run a store coroutine from a synchronous caller.

    The store's client is async (httpx), so it is driven to completion here
    rather than making every read path async.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside a loop: the caller must await it instead. This branch exists
    # so the failure is a clear message rather than "coroutine was never awaited".
    raise BrokerSettingsError(
        "broker credentials must be read off the event loop",
        status=500,
    )


async def brokers_action_async(
    action: str,
    *,
    user_id: str,
    payload: dict[str, Any] | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """Handle one Brokers action. The async form, for callers already on the loop."""
    key = _require_user(user_id)
    store = store or _store()
    if not store.enabled:
        raise BrokerSettingsError(
            "Broker accounts need Supabase storage to be configured on this deployment.",
            status=503,
        )
    if action == "list":
        return await _list(key, store)

    if action == "save":
        login = _string(payload, "login")
        password = _string(payload, "password")
        server = _string(payload, "server")
        broker = _string(payload, "broker")
        if not login:
            raise BrokerSettingsError("Enter your broker account number.")
        if not password:
            raise BrokerSettingsError("Enter your broker password.")
        if not server:
            raise BrokerSettingsError(
                "Enter the server your account is on (for example Exness-MT5Trial9 "
                "or Deriv-Demo). The server decides which terminal build is used."
            )
        await store.store_credentials(
            user_key=key,
            login=login,
            password=password,
            server=server,
            broker=broker,
            make_default=_truthy(payload, "is_default"),
        )
        result = await _list(key, store)
        result["last_action"] = "saved"
        return result

    if action == "delete":
        login = _string(payload, "login")
        server = _string(payload, "server")
        await store.delete_credentials(key, login=login, server=server)
        result = await _list(key, store)
        result["last_action"] = "deleted"
        return result

    if action == "test":
        server = _string(payload, "server")
        if not server:
            raise BrokerSettingsError("Enter the server name to check.")
        profile = _server_profile(server)
        if profile["recognised"]:
            message = (
                f"{server} resolves to a known build"
                + (f" ({profile['install_dir']})" if profile["install_dir"] else "")
                + ". Connect from a chat to install and log in."
            )
            if not profile["verified"]:
                message += (
                    " The installer for this brand has not been confirmed live, so "
                    "the install may need a manual download URL."
                )
        else:
            message = (
                f"No build is recorded for {server}. The agent can search for the "
                "broker's own MT5 download page when you connect."
            )
        return {"ok": True, "profile": profile, "message": message}

    raise BrokerSettingsError(f"Unknown brokers action: {action}", status=404)


async def _list(user_key: str, store: Any) -> dict[str, Any]:
    rows = await store.list_credentials(user_key)
    accounts = [_row_payload(row) for row in rows]
    defaults = _global_defaults()
    return {
        "available": True,
        "enabled": True,
        "accounts": accounts,
        "count": len(accounts),
        "default_broker": defaults["default_broker"],
        "default_server": defaults["default_server"],
        "message": "",
    }


def brokers_action(
    action: str,
    *,
    user_id: str,
    payload: dict[str, Any] | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """Synchronous entry point, for a caller with no event loop.

    The settings router awaits :func:`brokers_action_async` directly, so this
    only exists for scripts and tests that drive the domain outside a loop.
    """
    import asyncio

    return asyncio.run(
        brokers_action_async(action, user_id=user_id, payload=payload, store=store)
    )

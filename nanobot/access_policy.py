"""Who may do what: the administrator's feature gates, resolved per turn.

The product surface is split into three independent things a user can attempt:

* a **general task** — any ordinary agent turn;
* the **MT5 (Wine) trading method** — the sandbox tool that installs MetaTrader
  under Wine and drives it;
* the **separate broker flow** — a user's own broker credentials, entered in
  Settings, driving an MT5 server login directly.

Each has a switch in the config (``access.*``) that only an administrator can
flip, and the general-task switch additionally has a *per-user* override so
"off for everyone except these accounts" is expressible without turning the
whole product back on.

Why a per-user override lives in a JSON file and not in Supabase
----------------------------------------------------------------
The gate is consulted at the start of every turn, including turns whose user
identity comes from a Telegram chat id rather than a Supabase account, and it
has to answer when Supabase is unreachable — a network hiccup must not decide
who is allowed to work. A file beside ``admin_users.json`` is always readable,
is written only by the admin page, and needs no schema migration to ship. The
trade-off is honest: it is per-deployment state, not shared across replicas,
which is the same contract the admin user list already has.

Overrides are tri-state on purpose:

    absent  -> inherit the global switch
    True    -> allow this user even when the global switch is off
    False   -> deny this user even when the global switch is on
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

#: Feature keys the policy knows about. Kept as a tuple so the admin page, the
#: store and the tests all agree on the spelling.
FEATURES: tuple[str, ...] = ("general_task", "mt5_trading", "brokers")

_SOURCES = ("global", "user", "default")


class AccessDeniedError(RuntimeError):
    """Raised at turn start when an administrator switch refuses this turn.

    A plain ``RuntimeError`` subclass so nothing needs to import the hook module
    to catch it, and so the runner can recognise it by type the way it already
    recognises a billing refusal.
    """


#: Turn-metadata keys a trading surface sets to mark its turns. Any of these
#: being truthy exempts the turn from the general-task gate, because the point
#: of that gate is to refuse *general* work, not the product the user is paying
#: for. Kept here so the surfaces and the gate cannot drift apart.
TRADING_TURN_KEYS: tuple[str, ...] = ("turn_kind", "broker_turn", "trading")
_TRADING_TURN_VALUES = {"trading", "broker", "brokers", "trade"}


def turn_is_trading(
    metadata: dict[str, Any] | None,
    messages: list[dict[str, Any]] | None = None,
) -> bool:
    """Whether a turn belongs to the trading/broker surface.

    Two independent signals, because neither is sufficient on its own:

    1. **The surface said so.** A caller that already knows it is running a
       trading turn sets one of :data:`TRADING_TURN_KEYS` in the turn metadata.
       This is exact and is what the WebUI/Telegram trading entry points use.
    2. **The user asked for one.** Nothing in the pipeline classifies intent
       before the model picks a tool, so a turn whose opening message reads as a
       trade would otherwise be refused by the general-task switch *before* it
       could ever reach ``broker_trade`` -- locking a paying trading user out of
       the only thing they bought. :func:`message_is_trading` covers that gap.

    The general-task switch is a product switch, not a security boundary (the
    credential stores and the per-call tool gates are), so this deliberately
    errs toward letting a trading-looking turn through: the failure it prevents
    -- a trading user unable to trade -- is far worse than a user who phrases a
    general request as a trade.
    """
    meta = metadata or {}
    for key in TRADING_TURN_KEYS:
        value = meta.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in _TRADING_TURN_VALUES:
            return True
    if messages:
        return message_is_trading(_last_user_text(messages))
    return False


#: Wording that marks a message as trading/broker work. Word-boundary matched,
#: and deliberately narrow where an English word is also common speech: "order"
#: only fires next to a trading noun (so "in order to" does not count), and a
#: bare "lot" does not fire at all (so "a lot of files" does not count). A
#: pattern that fired on ordinary prose would hand every turn the exemption.
TRADING_INTENT_PATTERNS: tuple[str, ...] = (
    r"\btrad(e|es|ed|ing)\b",
    r"\bbroker(s)?\b",
    r"\bmt5\b",
    r"\bmetatrader\b",
    r"\bexness\b",
    r"\bderiv\b",
    r"\bforex\b",
    r"\bbuy\b",
    r"\bsell\b",
    r"\bgo long\b",
    r"\bgo short\b",
    r"\b(long|short) (position|the)\b",
    r"\bposition(s)?\b",
    r"\b(place|placed|open|opened|close|closed|submit|cancel|modify)"
    r" (an? |the |my )?(order|position|trade)\b",
    r"\b(pending|filled|open) orders?\b",
    r"\bentry (price|point|level)\b",
    r"\bstop[- ]?loss\b",
    r"\btake[- ]?profit\b",
    r"\b(sl|tp)\b",
    r"\bpips?\b",
    r"\b\d+(\.\d+)?\s*lots?\b",
    r"\blot size\b",
    r"\bleverage\b",
    r"\bdrawdown\b",
    r"\bequity\b",
    r"\bmargin\b",
    r"\bcandles?(stick)?s?\b",
    r"\bbid[-/ ]ask\b",
    r"\baccount (balance|equity|info|number|details)\b",
    r"\bmy (account|broker|positions?)\b",
    r"\btrading account\b",
    r"\btrading (strategy|symbols?|bot|platform)\b",
    r"\bbacktest(ing)?\b",
    r"\bnews? (trading|strategy)\b",
    # Instruments users actually name. The blanket "any six letters" version of
    # this was a bug: it matched "please", "thanks" and "create", which exempted
    # nearly every turn from the switch it was written to respect.
    r"\b(xau|xag)usd?\b",
    r"\b(brent|wti|usoil|uk100|us30|nas100|ger40)\b",
    r"\b(btc|eth|xrp|sol|ada|doge)usd\b",
    r"\b(eur|gbp|usd|aud|nzd|jpy|chf|cad)"
    r"(eur|gbp|usd|aud|nzd|jpy|chf|cad)\b",
)

_TRADING_INTENT_RE = re.compile("|".join(TRADING_INTENT_PATTERNS), re.IGNORECASE)

#: An instrument written the way a terminal shows it: six uppercase letters
#: (EURUSD) or a metal/index pair (XAUUSD). Case-SENSITIVE on purpose -- this is
#: the only pattern where capitalisation carries the meaning, and matching it
#: loosely is what made the earlier version exempt ordinary sentences.
_INSTRUMENT_RE = re.compile(r"\b(?:[A-Z]{6}|XAU|XAG)USD?\b")


def message_is_trading(text: str) -> bool:
    """Whether a user message reads as a trading/broker request.

    A heuristic, and intentionally a generous one -- see :func:`turn_is_trading`
    for why the bias runs toward allowing the turn.
    """
    candidate = text or ""
    if _TRADING_INTENT_RE.search(candidate):
        return True
    return bool(_INSTRUMENT_RE.search(candidate))


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """The newest user message's text, which is what the turn is about.

    Only the tail is considered: an earlier trading question in a long session
    must not permanently exempt every later turn from the switch.
    """
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # A multipart user message: concatenate its text parts and ignore
            # images, which carry no intent.
            parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
    return ""


@dataclass(frozen=True)
class AccessDecision:
    """The answer for one feature, with the reason a refusal can be shown."""

    feature: str
    allowed: bool
    source: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "allowed": self.allowed,
            "source": self.source,
            "reason": self.reason,
        }


def data_dir() -> Path:
    """The instance data directory, matching ``admin_registry._data_dir``."""
    configured = os.getenv("NANOBOT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.getenv("HOME", ".")) / ".nanobot"


def overrides_path() -> Path:
    return data_dir() / "user_access_overrides.json"


def turn_user_key(
    *,
    channel: str = "",
    chat_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """The identity a per-user override is keyed by, for one turn.

    WebUI/WebSocket turns carry the authenticated Supabase user id in turn
    metadata; Telegram turns are keyed by the numeric chat id. The two are
    namespaced so a Supabase UUID can never collide with a chat id, and an
    anonymous turn yields an empty key that no override can match — the global
    switch alone decides it.
    """
    meta = metadata or {}
    user_id = str(meta.get("supabase_user_id") or "").strip()
    if user_id:
        return f"supabase:{user_id}"
    if str(channel or "") == "telegram":
        chat = str(chat_id or "").strip()
        if chat.isdigit():
            return f"telegram:{chat}"
    api_key_id = str(meta.get("api_key_id") or "").strip()
    if api_key_id:
        return f"apikey:{api_key_id}"
    return ""


class AccessPolicy:
    """Resolve the three feature gates for a given user key.

    Constructed once per process (see :func:`access_policy`). Reads the config
    lazily so a test can inject one, and re-reads the override file when its
    mtime changes so an admin edit takes effect without a restart.
    """

    def __init__(self, config: Any | None = None, path: Path | None = None) -> None:
        self._config = config
        self._path = path or overrides_path()
        self._overrides: dict[str, dict[str, bool]] = {}
        self._mtime: float | None = None
        self._load_overrides(force=True)

    # ----------------------------------------------------------------- config

    def _access_config(self) -> Any | None:
        if self._config is None:
            try:
                from nanobot.config.loader import load_config

                self._config = load_config()
            except Exception as exc:  # pragma: no cover - config must never gate startup
                logger.warning("access policy could not load config: {}", exc)
                return None
        return getattr(self._config, "access", None)

    def set_config(self, config: Any) -> None:
        """Adopt a freshly loaded config (the admin page saves and refreshes)."""
        self._config = config

    def _global_flag(self, feature: str) -> bool:
        """The global switch. Defaults to *allowed* when unreadable.

        Failing open here is deliberate: a config that cannot be read is an
        infrastructure fault, and a gate that silently locks every user out of a
        product they paid for is a worse failure than one that stays open until
        the operator notices. The admin page always reads the real value.
        """
        access = self._access_config()
        if access is None:
            return True
        if feature == "general_task":
            return bool(getattr(access, "general_task_enabled", True))
        if feature == "mt5_trading":
            return bool(getattr(access, "mt5_trading_enabled", True))
        if feature == "brokers":
            return bool(getattr(access, "brokers_enabled", True))
        return True

    def default_broker(self) -> tuple[str, str]:
        """The admin's default (broker, server) pair, for prefilling Settings."""
        access = self._access_config()
        if access is None:
            return "", ""
        return (
            str(getattr(access, "default_broker", "") or "").strip().lower(),
            str(getattr(access, "default_server", "") or "").strip(),
        )

    # -------------------------------------------------------------- overrides

    def _load_overrides(self, *, force: bool = False) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            if force or self._overrides:
                self._overrides = {}
            self._mtime = None
            return
        if not force and self._mtime == stat.st_mtime:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            logger.warning("access overrides unreadable ({}); ignoring them", exc)
            self._overrides = {}
            self._mtime = stat.st_mtime
            return
        clean: dict[str, dict[str, bool]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                entry: dict[str, bool] = {}
                for feature in FEATURES:
                    if isinstance(value.get(feature), bool):
                        entry[feature] = value[feature]
                if entry:
                    clean[str(key)] = entry
        self._overrides = clean
        self._mtime = stat.st_mtime

    def overrides(self) -> dict[str, dict[str, bool]]:
        self._load_overrides()
        return {key: dict(value) for key, value in self._overrides.items()}

    def override_for(self, user_key: str, feature: str) -> bool | None:
        self._load_overrides()
        entry = self._overrides.get(str(user_key or ""))
        if not entry:
            return None
        value = entry.get(feature)
        return value if isinstance(value, bool) else None

    def set_override(
        self,
        user_key: str,
        *,
        general_task: bool | None = None,
        mt5_trading: bool | None = None,
        brokers: bool | None = None,
        clear: bool = False,
    ) -> dict[str, dict[str, bool]]:
        """Set (or clear) a user's overrides and persist them atomically.

        ``None`` means "leave this feature alone"; ``clear=True`` drops the whole
        entry, which is how a user goes back to inheriting the global switch.
        """
        key = str(user_key or "").strip()
        if not key:
            raise ValueError("a user key is required")
        self._load_overrides(force=True)
        table = dict(self._overrides)
        if clear:
            table.pop(key, None)
        else:
            entry = dict(table.get(key) or {})
            for feature, value in (
                ("general_task", general_task),
                ("mt5_trading", mt5_trading),
                ("brokers", brokers),
            ):
                if value is None:
                    continue
                entry[feature] = bool(value)
            if entry:
                table[key] = entry
            else:
                table.pop(key, None)
        self._write(table)
        return self.overrides()

    def _write(self, table: dict[str, dict[str, bool]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(table, indent=2, sort_keys=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)
        self._overrides = {k: dict(v) for k, v in table.items()}
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:  # pragma: no cover - the write just succeeded
            self._mtime = None

    # --------------------------------------------------------------- decisions

    def general_task(self, user_key: str = "") -> AccessDecision:
        """May this user run an ordinary agent turn?"""
        global_on = self._global_flag("general_task")
        override = self.override_for(user_key, "general_task")
        if override is True:
            return AccessDecision(
                "general_task", True, "user",
                "general tasks are allowed for this account",
            )
        if override is False:
            return AccessDecision(
                "general_task", False, "user",
                "General tasks are switched off for your account. Contact the operator "
                "if you need them enabled.",
            )
        if global_on:
            return AccessDecision("general_task", True, "global", "")
        return AccessDecision(
            "general_task", False, "global",
            "General tasks are currently switched off on this deployment. Trading and "
            "broker features are unaffected.",
        )

    def mt5_trading(self, user_key: str = "") -> AccessDecision:
        """May this caller use the MT5/Wine method? Absolute — no override.

        The per-user table deliberately cannot re-enable this one: the switch
        exists to retire the method, so a user-level allow would defeat it.
        """
        del user_key
        if self._global_flag("mt5_trading"):
            return AccessDecision("mt5_trading", True, "global", "")
        return AccessDecision(
            "mt5_trading", False, "global",
            "The MT5 (Wine) trading method is switched off on this deployment. Use the "
            "broker connection in Settings instead.",
        )

    def brokers(self, user_key: str = "") -> AccessDecision:
        """May this user use the separate broker flow?"""
        global_on = self._global_flag("brokers")
        override = self.override_for(user_key, "brokers")
        if override is True:
            return AccessDecision("brokers", True, "user", "")
        if override is False:
            return AccessDecision(
                "brokers", False, "user",
                "Broker connections are switched off for your account.",
            )
        if global_on:
            return AccessDecision("brokers", True, "global", "")
        return AccessDecision(
            "brokers", False, "global",
            "Broker connections are switched off on this deployment.",
        )

    def snapshot(self, user_key: str = "") -> dict[str, Any]:
        """Every gate for one user, for the admin page and for diagnostics."""
        return {
            "user_key": user_key,
            "features": {
                feature: self.decision(feature, user_key).to_dict()
                for feature in FEATURES
            },
            "global": {
                "general_task_enabled": self._global_flag("general_task"),
                "mt5_trading_enabled": self._global_flag("mt5_trading"),
                "brokers_enabled": self._global_flag("brokers"),
                "default_broker": self.default_broker()[0],
                "default_server": self.default_broker()[1],
            },
            "overrides": self.overrides(),
        }

    def decision(self, feature: str, user_key: str = "") -> AccessDecision:
        if feature == "general_task":
            return self.general_task(user_key)
        if feature == "mt5_trading":
            return self.mt5_trading(user_key)
        if feature == "brokers":
            return self.brokers(user_key)
        return AccessDecision(feature, True, "default", "")


_policy: AccessPolicy | None = None


def access_policy() -> AccessPolicy:
    """The process-wide policy. Constructed on first use."""
    global _policy
    if _policy is None:
        _policy = AccessPolicy()
    return _policy


def configure_access_policy(policy: AccessPolicy | None) -> None:
    """Replace the process-wide policy (tests, and the admin page after a save)."""
    global _policy
    _policy = policy


def reset_access_policy() -> None:
    """Drop the cached policy so the next read rebuilds it from disk."""
    configure_access_policy(None)


__all__ = [
    "AccessDecision",
    "AccessDeniedError",
    "AccessPolicy",
    "FEATURES",
    "TRADING_TURN_KEYS",
    "access_policy",
    "configure_access_policy",
    "data_dir",
    "overrides_path",
    "reset_access_policy",
    "turn_is_trading",
    "turn_user_key",
]

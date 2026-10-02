"""The verified administrator (the owner) — one definition for every channel.

WHY THIS EXISTS.

The owner of CDNAI must be recognised on every channel he talks on, and nobody
else may be. Before this module the only such concept was a Telegram-only
constant (``MINIS_BOT_ADMIN_EMAIL``) plus a Telegram-only runtime-context block,
so the same person was "the verified administrator" in one place and "a user you
do not know" everywhere else — and the identity template told the model in plain
words to deny knowing *anyone*, administrator included. The result was an owner
being told to introduce himself in his own deployment.

HOW THE IDENTITY IS RESOLVED, AND WHY THAT IS THE SECURITY BOUNDARY.

The administrator is identified by the **account the request was authenticated
with**, never by anything a message says. Concretely, resolution reads only
metadata keys the gateway itself writes while handling the turn:

* the WebUI/WebSocket channel verifies the Supabase session at bootstrap and
  stamps ``supabase_user_id`` plus the raw access JWT on every turn it carries;
* the Telegram channel reads the linked account row server-side and stamps
  ``supabase_user_id``/``auth_email``;
* the API gateway stamps ``supabase_user_id`` from the API key's owner row.

A message that *claims* to be the administrator therefore changes nothing: the
claim never reaches these keys. That is the whole point — a claim must not be
able to grant the role, or the role is worthless.

Resolution is fail-closed. No resolvable identity, an unverifiable token, or an
identity that does not match an allow-listed email/user id all answer "normal
user".

CONFIGURATION.

The owner's email ships with a default so the behaviour works with no setup, and
both lists are env-overridable for a deployment whose owner differs:

* ``NANOBOT_VERIFIED_ADMIN_EMAILS`` — comma-separated emails (replaces the
  default when set).
* ``NANOBOT_VERIFIED_ADMIN_USER_IDS`` — comma-separated Supabase/AgentX user
  ids, needed only when a channel can carry an id but no email (API keys).

WHAT "NOT RESTRICTED" MEANS.

It means the owner is never refused, stalled, or hedged at *because of who he
is*: no "I can only do that for an administrator", no re-confirmation theatre, no
pretending not to know him. It does not silently grant anything at all — no tool
gains a permission it did not have, and the two boundaries that protect *other
people* rather than the platform (another user's private data, and attacks on
systems he does not own) are unchanged. Those are not restrictions on him; they
would be restrictions on anyone. This distinction is written into
:data:`OWNER_PROMPT_NOTE` so the model cannot flatten it back into a refusal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

#: The verified administrator's email. One hard-coded default is deliberate: the
#: owner must be recognised on a fresh deployment with no configuration at all.
DEFAULT_VERIFIED_ADMIN_EMAIL = "allisonarinze@gmail.com"

#: What the administrator owns, used in prompt text.
OWNER_ORGANISATION = "CDNAI"

#: How the administrator is addressed. Shipped as a default so a fresh
#: deployment names him correctly; ``NANOBOT_OWNER_NAME`` overrides it.
#:
#: This exists because the model had to *derive* a name from
#: ``allisonarinze@gmail.com`` and got it wrong — it split the local part and
#: addressed the owner as "Allison". An email address is not a name, and nothing
#: in a prompt should have to guess one from it.
DEFAULT_OWNER_NAME = "Arinze"

#: Metadata keys carrying an already-verified email. Only the gateway writes
#: these; a user message cannot set them.
_EMAIL_KEYS: tuple[str, ...] = (
    "verified_admin_email",
    "user_email",
    "auth_email",
    "supabase_email",
)

#: Metadata keys carrying an authenticated account id.
_ID_KEYS: tuple[str, ...] = (
    "supabase_user_id",
    "agentx_user_id",
    "user_id",
)

#: Metadata key carrying the raw Supabase access JWT for this turn, used only
#: when no email was stamped directly.
_ACCESS_TOKEN_KEY = "supabase_access_token"

#: Metadata flag a channel may set once it has verified the account itself (the
#: Telegram runtime does this from the linked account row).
_FLAG_KEY = "is_verified_admin"

#: The prompt text handed to the model when the turn belongs to the owner.
OWNER_PROMPT_NOTE = (
    "The account this request was authenticated with IS the verified "
    "administrator of this deployment — {name}, the owner of {organisation}, "
    "reached at {email}. You always know him, on every channel, and you say so "
    "plainly when he asks. Address him as {name}; that is his name, and his email "
    "address is not a substitute for it — never derive a first name, surname or "
    "handle by splitting an email address or an account id, and never address him "
    "as anything that is not {name}. He is not restricted in a task and must never "
    "be refused, stalled, or hedged at because of who he is: no 'that is only "
    "available to an administrator', no asking him to prove it, no pretending not "
    "to know him. This grants him nothing a tool did not already allow — what it "
    "removes is the refusal and the friction. The two things that stay off the "
    "table are not restrictions on him: another person's private data, and attacks "
    "on systems he does not own."
)


def owner_name() -> str:
    """The administrator's name, for prompt text and job instructions.

    Env-overridable so a deployment with a different owner does not have to
    patch a template; the default keeps a fresh install correct.
    """
    configured = (os.environ.get("NANOBOT_OWNER_NAME") or "").strip()
    return configured or DEFAULT_OWNER_NAME


def _split_env(name: str) -> tuple[str, ...]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return ()
    return tuple(part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip())


def verified_admin_emails() -> frozenset[str]:
    """Emails that identify the verified administrator.

    The env override *replaces* the default rather than adding to it, so moving
    an installation to a new owner cannot leave the old owner still recognised.
    """
    configured = _split_env("NANOBOT_VERIFIED_ADMIN_EMAILS")
    return frozenset(configured or (DEFAULT_VERIFIED_ADMIN_EMAIL,))


def verified_admin_user_ids() -> frozenset[str]:
    """Account ids that identify the verified administrator (never lowercased)."""
    raw = (os.environ.get("NANOBOT_VERIFIED_ADMIN_USER_IDS") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.replace(";", ",").split(",") if part.strip())


def primary_admin_email() -> str:
    """The email to name in prompt text, deterministic when several are set."""
    emails = verified_admin_emails()
    if DEFAULT_VERIFIED_ADMIN_EMAIL in emails:
        return DEFAULT_VERIFIED_ADMIN_EMAIL
    return sorted(emails)[0] if emails else DEFAULT_VERIFIED_ADMIN_EMAIL


@dataclass(frozen=True)
class TurnOwner:
    """Who the authenticated account behind one turn is.

    ``is_verified_admin`` is the only thing callers branch on; ``source`` records
    which evidence decided it so a surprising answer can be traced without
    logging the identity itself.
    """

    is_verified_admin: bool = False
    email: str = ""
    source: str = ""


#: The answer for a turn with no resolvable identity — every normal user.
NORMAL_USER = TurnOwner()


def _clean_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _emails_from(metadata: Mapping[str, Any]) -> str:
    for key in _EMAIL_KEYS:
        email = _clean_email(metadata.get(key))
        if email:
            return email
    return ""


def _ids_from(metadata: Mapping[str, Any]) -> str:
    for key in _ID_KEYS:
        account_id = _clean_id(metadata.get(key))
        if account_id:
            return account_id
    return ""


def _verified_from_token(metadata: Mapping[str, Any]) -> tuple[str, str]:
    """Resolve ``(user_id, email)`` from the turn's Supabase access JWT.

    Cached by :class:`~nanobot.supabase_auth.SupabaseAuth`, and the WebUI
    bootstrap has already verified this exact token, so this is a cache hit in
    practice. Any failure answers empty and the turn stays a normal user.
    """
    token = str(metadata.get(_ACCESS_TOKEN_KEY) or "").strip()
    if not token:
        return ("", "")
    try:
        from nanobot.supabase_auth import SupabaseAuth

        user_id, email = SupabaseAuth().verify_access_token_sync(token)
    except Exception:  # noqa: BLE001 - an unverifiable token is simply not the owner
        return ("", "")
    return (_clean_id(user_id), _clean_email(email))


def match_identity(email: str = "", user_id: str = "") -> TurnOwner:
    """Decide whether a *verified* pair of facts belongs to the administrator.

    Kept separate from metadata parsing so every channel can reuse the decision
    with the identity evidence it actually has.
    """
    clean_email = _clean_email(email)
    clean_id = _clean_id(user_id)
    if clean_email and clean_email in verified_admin_emails():
        return TurnOwner(is_verified_admin=True, email=clean_email, source="email")
    if clean_id and clean_id in verified_admin_user_ids():
        return TurnOwner(is_verified_admin=True, email=clean_email, source="user_id")
    return NORMAL_USER


def owner_from_metadata(*sources: Mapping[str, Any] | None) -> TurnOwner:
    """Resolve the verified administrator from a turn's gateway-built metadata.

    Later sources are weaker than earlier ones but are still consulted, because a
    channel may stamp the id on the inbound message while the session carries the
    email. Nothing here reads message *text*.
    """
    merged: dict[str, Any] = {}
    for source in sources:
        if isinstance(source, Mapping):
            for key, value in source.items():
                merged.setdefault(key, value)

    # A channel that verified the account itself wins: it holds the linked
    # account row, which is the strongest evidence available.
    if merged.get(_FLAG_KEY) is True:
        email = _emails_from(merged)
        decision = match_identity(email=email, user_id=_ids_from(merged))
        if decision.is_verified_admin:
            return decision
        # Flagged but the email did not match an allow-listed owner: fall through
        # rather than trusting the flag on its own.
        decision = match_identity(email=email)
        if decision.is_verified_admin:
            return decision

    decision = match_identity(email=_emails_from(merged), user_id=_ids_from(merged))
    if decision.is_verified_admin:
        return decision

    token_id, token_email = _verified_from_token(merged)
    if token_id or token_email:
        decision = match_identity(email=token_email, user_id=token_id)
        if decision.is_verified_admin:
            return decision
        return NORMAL_USER

    return NORMAL_USER


def owner_prompt_note(owner: TurnOwner | None) -> str:
    """Prompt text naming the owner, or empty when this turn is a normal user."""
    if owner is None or not owner.is_verified_admin:
        return ""
    email = owner.email or primary_admin_email()
    return OWNER_PROMPT_NOTE.format(
        organisation=OWNER_ORGANISATION, email=email, name=owner_name()
    )


__all__ = [
    "DEFAULT_OWNER_NAME",
    "DEFAULT_VERIFIED_ADMIN_EMAIL",
    "NORMAL_USER",
    "OWNER_ORGANISATION",
    "OWNER_PROMPT_NOTE",
    "TurnOwner",
    "match_identity",
    "owner_from_metadata",
    "owner_name",
    "owner_prompt_note",
    "primary_admin_email",
    "verified_admin_emails",
    "verified_admin_user_ids",
]

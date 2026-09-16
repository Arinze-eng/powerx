"""Signed onlyfiles resolve-and-download: signing, tamper rejection, rewriting."""

from __future__ import annotations

from nanobot.webui.media_api import (
    rewrite_onlyfiles_markdown_links,
    sign_onlyfiles_resolver_url,
    verify_onlyfiles_resolver_sig,
)

_SECRET = b"x" * 32


def _signed(url: str) -> str:
    out = sign_onlyfiles_resolver_url(url, secret=_SECRET)
    assert out is not None
    return out


def _sig_payload(signed: str) -> tuple[str, str]:
    parts = signed.split("/")
    return parts[3], parts[4]


def test_sign_roundtrip() -> None:
    signed = _signed("https://onlyfiles.com/abc123/report.apk")
    assert signed.startswith("/api/dl/")
    sig, payload = _sig_payload(signed)
    assert verify_onlyfiles_resolver_sig(sig, payload, secret=_SECRET)


def test_sign_rejects_tampered_payload_and_bad_sig() -> None:
    signed = _signed("https://onlyfiles.com/abc123/report.apk")
    sig, payload = _sig_payload(signed)
    assert not verify_onlyfiles_resolver_sig(sig, "AAAA" + payload[4:], secret=_SECRET)
    assert not verify_onlyfiles_resolver_sig("bogus", payload, secret=_SECRET)
    assert not verify_onlyfiles_resolver_sig(sig, payload, secret=b"y" * 32)


def test_sign_rejects_non_onlyfiles_hosts() -> None:
    assert sign_onlyfiles_resolver_url("https://evil.com/x", secret=_SECRET) is None
    assert sign_onlyfiles_resolver_url("http://onlyfiles.com/x", secret=_SECRET) is None
    assert (
        sign_onlyfiles_resolver_url("https://onlyfiles.com.evil.test/x", secret=_SECRET)
        is None
    )


def test_rewrite_bare_and_markdown_targets() -> None:
    text = (
        "Get it: https://onlyfiles.com/abc123/report.apk or"
        " [click](https://onlyfiles.com/dl/1.2/abc123/report.apk)"
    )
    out = rewrite_onlyfiles_markdown_links(
        text, sign_onlyfiles=lambda u: sign_onlyfiles_resolver_url(u, secret=_SECRET)
    )
    assert out.count("/api/dl/") == 2
    # Bare URL keeps its visible text as the label.
    assert "[https://onlyfiles.com/abc123/report.apk](/api/dl/" in out
    # Markdown link keeps its label; only the target is swapped.
    assert "[click](/api/dl/" in out


def test_rewrite_leaves_non_onlyfiles_text_untouched() -> None:
    plain = "see https://example.com/f.bin and https://catbox.moe/x.zip"
    assert (
        rewrite_onlyfiles_markdown_links(plain, sign_onlyfiles=lambda _u: None) == plain
    )


def test_rewrite_passthrough_when_signer_returns_none() -> None:
    text = "https://onlyfiles.com/abc123/report.apk"
    assert (
        rewrite_onlyfiles_markdown_links(text, sign_onlyfiles=lambda _u: None) == text
    )

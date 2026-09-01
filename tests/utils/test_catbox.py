"""Tests for catbox.to transfer helpers (URL validation, shared-id parsing)."""

from __future__ import annotations

import pytest

from nanobot.utils.catbox import (
    is_catbox_download_url,
    is_catbox_url,
    shared_id_from_url,
)


def test_is_catbox_url_accepts_share_urls():
    assert is_catbox_url("https://catbox.to/AbCdEf1234567/file")
    assert is_catbox_url("https://catbox.to/AbCdEf1234567")
    assert is_catbox_url("https://catbox.to/download/AbCdEf1234567/hash/file.bin")


def test_is_catbox_url_rejects_other_hosts_and_schemes():
    assert not is_catbox_url("http://catbox.to/AbCdEf1234567/file")
    assert not is_catbox_url("https://gofile.io/d/AbCdEf1234567")
    assert not is_catbox_url("https://tmpfiles.org/dl/12345/file.txt")
    assert not is_catbox_url("https://evil.catbox.to.evil.com/x")
    assert not is_catbox_url("")
    assert not is_catbox_url("not a url")


def test_is_catbox_download_url_matches_raw_links():
    assert is_catbox_download_url(
        "https://catbox.to/download/AbCdEf1234567/o2bJHash/t3.txt"
    )
    assert not is_catbox_download_url("https://catbox.to/AbCdEf1234567/file")
    assert not is_catbox_download_url("https://catbox.to/download/two/segments")


def test_shared_id_from_url():
    assert shared_id_from_url("https://catbox.to/AbCdEf1234567/file") == "AbCdEf1234567"
    assert shared_id_from_url("https://catbox.to/AbCdEf1234567") == "AbCdEf1234567"
    assert (
        shared_id_from_url("https://catbox.to/download/AbCdEf1234567/hash/x.py")
        == "AbCdEf1234567"
    )
    assert shared_id_from_url("https://gofile.io/d/abc") is None
    assert shared_id_from_url("https://catbox.to/") is None
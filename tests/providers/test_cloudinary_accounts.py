"""Cloudinary accounts, rotation, and the image provider built on them.

Everything here is offline: the API is replaced by an ``httpx`` mock transport,
so the tests assert the *decisions* (which account is asked, what happens when
one refuses) rather than any particular generation.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanobot.providers import cloudinary as cld
from nanobot.providers.image_generation import (
    CloudinaryImageGenerationClient,
    get_image_gen_provider,
    image_gen_provider_names,
)

_ACCOUNT_A = cld.CloudinaryAccount("cloud-a", "111", "sec-a")
_ACCOUNT_B = cld.CloudinaryAccount("cloud-b", "222", "sec-b")

ASSET_URL = "https://res.cloudinary.com/cloud-a/image/upload/v1/gen.png"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def generation_payload(
    *,
    cloud: str = "cloud-a",
    remaining: int | None = 7,
    limit: int | None = 50,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "data": {
            "assets": [
                {
                    "bytes": 1234,
                    "format": "png",
                    "height": 64,
                    "width": 64,
                    "model": {"family": "flux", "id": "flux-2-pro", "tier": "premium"},
                    "storage": {
                        "asset_id": "asset-1",
                        "public_id": "gen",
                        "resource_type": "image",
                        "secure_url": f"https://res.cloudinary.com/{cloud}/image/upload/v1/gen.png",
                        "type": "upload",
                    },
                }
            ]
        },
        "request_id": "req-1",
    }
    if remaining is not None or limit is not None:
        payload["limits"] = {
            "addons_quota": [
                {"type": "image_generation", "limit": limit, "remaining": remaining}
            ]
        }
    return payload


# --------------------------------------------------------------------- parsing


def test_a_cloudinary_url_is_one_account() -> None:
    accounts = cld.parse_accounts("cloudinary://key-1:secret-1@cloud-a")
    assert accounts == [cld.CloudinaryAccount("cloud-a", "key-1", "secret-1")]


def test_the_accounts_variable_takes_several_separated_entries() -> None:
    raw = (
        "cloudinary://k1:s1@cloud-a\n"
        "cloudinary://k2:s2@cloud-b, cloudinary://k3:s3@cloud-c;"
        "cloudinary://k4:s4@cloud-d"
    )
    assert [a.cloud_name for a in cld.parse_accounts(raw)] == [
        "cloud-a",
        "cloud-b",
        "cloud-c",
        "cloud-d",
    ]


def test_the_triple_form_is_accepted_for_an_admin_pasted_key() -> None:
    assert [a.cloud_name for a in cld.parse_accounts("cloud-a/111/sec-a")] == ["cloud-a"]
    assert [a.cloud_name for a in cld.parse_accounts("cloud-a:111:sec-a")] == ["cloud-a"]


def test_a_repeated_cloud_name_is_kept_once() -> None:
    raw = "cloudinary://k1:s1@cloud-a cloudinary://k1:s1@cloud-a"
    assert len(cld.parse_accounts(raw)) == 1


def test_garbage_yields_no_accounts() -> None:
    assert cld.parse_accounts("") == []
    assert cld.parse_accounts(None) == []
    assert cld.parse_accounts("just some words") == []


# -------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, "Invalid credentials", "auth"),
        (403, "api key is not allowed", "auth"),
        (403, "the image_generation add-on is not enabled for this account", "quota"),
        (402, "payment required", "billing"),
        (429, "too many requests", "rate_limit"),
        (503, "service unavailable", "server"),
        (400, "the prompt must not be empty", "request"),
        (400, "generation quota exceeded", "quota"),
    ],
)
def test_failures_are_classified_by_what_the_next_account_can_fix(
    status: int, body: str, kind: str
) -> None:
    assert cld.classify_failure(status, body) == kind


def test_only_retryable_kinds_are_retried() -> None:
    assert cld.CloudinaryError("x", kind="rate_limit").retryable
    assert cld.CloudinaryError("x", kind="auth").retryable
    # A malformed request is not another account's to fix.
    assert not cld.CloudinaryError("x", kind="request").retryable


# -------------------------------------------------------------------- rotation


def test_an_unmeasured_account_outranks_an_almost_empty_one() -> None:
    pool = cld.CloudinaryPool([_ACCOUNT_A, _ACCOUNT_B])
    pool.record_quota("cloud-a", remaining=1, limit=50)
    assert [a.cloud_name for a in pool.ordered()] == ["cloud-b", "cloud-a"]


def test_the_account_with_more_allowance_left_is_asked_first() -> None:
    pool = cld.CloudinaryPool([_ACCOUNT_A, _ACCOUNT_B])
    pool.record_quota("cloud-a", remaining=30, limit=50)
    pool.record_quota("cloud-b", remaining=49, limit=50)
    assert [a.cloud_name for a in pool.ordered()] == ["cloud-b", "cloud-a"]


def test_an_account_with_nothing_left_is_taken_out_of_the_rotation() -> None:
    pool = cld.CloudinaryPool([_ACCOUNT_A, _ACCOUNT_B])
    pool.record_quota("cloud-a", remaining=0, limit=50)
    assert [a.cloud_name for a in pool.ordered()] == ["cloud-b"]
    assert pool.snapshot()[0]["available"] is False


def test_a_rejected_key_is_disabled_permanently_and_a_rate_limit_is_not() -> None:
    pool = cld.CloudinaryPool([_ACCOUNT_A, _ACCOUNT_B])
    pool.record_failure("cloud-a", "auth")
    pool.record_failure("cloud-b", "rate_limit")
    by_name = {row["cloud_name"]: row for row in pool.snapshot()}
    assert by_name["cloud-a"]["disabled"] is True
    assert by_name["cloud-b"]["disabled"] is False
    assert by_name["cloud-b"]["cooldown_s"] > 0


def test_an_exhausted_pool_still_offers_one_account() -> None:
    """A parked account must not turn into 'Cloudinary is unavailable'."""
    pool = cld.CloudinaryPool([_ACCOUNT_A])
    pool.record_quota("cloud-a", remaining=0, limit=1)
    assert [a.cloud_name for a in pool.ordered()] == ["cloud-a"]


def test_re_adding_a_cloud_adopts_a_rotated_secret() -> None:
    pool = cld.CloudinaryPool([_ACCOUNT_A])
    pool.record_failure("cloud-a", "auth")
    assert pool.snapshot()[0]["disabled"] is True
    pool.add(cld.CloudinaryAccount("cloud-a", "111", "new-secret"))
    assert pool.snapshot()[0]["disabled"] is False


# ------------------------------------------------------------ the client call


def _mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_successful_generation_records_the_served_allowance() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=generation_payload(remaining=7, limit=50))

    pool = cld.CloudinaryPool([_ACCOUNT_A])
    client = cld.CloudinaryClient(pool=pool, client=_mock_client(handler))
    result = await client.text_to_image("a teapot", model="auto")

    assert result.assets[0].secure_url == ASSET_URL
    assert (result.remaining, result.limit) == (7, 50)
    assert pool.snapshot()[0]["remaining"] == 7
    assert seen == ["/v2/generate/cloud-a/text_to_image"]


@pytest.mark.asyncio
async def test_a_rejected_key_fails_over_to_the_next_account() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cloud = request.url.path.split("/")[3]
        asked.append(cloud)
        if cloud == "cloud-a":
            return httpx.Response(401, json={"error": {"message": "Invalid credentials"}})
        return httpx.Response(200, json=generation_payload(cloud="cloud-b"))

    pool = cld.CloudinaryPool([_ACCOUNT_A, _ACCOUNT_B])
    client = cld.CloudinaryClient(pool=pool, client=_mock_client(handler))
    result = await client.text_to_image("a teapot", model="auto")

    assert asked == ["cloud-a", "cloud-b"]
    assert result.assets[0].cloud_name == "cloud-b"


@pytest.mark.asyncio
async def test_a_malformed_request_is_not_retried_on_another_account() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path.split("/")[3])
        return httpx.Response(400, json={"error": {"message": "prompt must not be empty"}})

    pool = cld.CloudinaryPool([_ACCOUNT_A, _ACCOUNT_B])
    client = cld.CloudinaryClient(pool=pool, client=_mock_client(handler))
    with pytest.raises(cld.CloudinaryError) as raised:
        await client.text_to_image("", model="auto")

    assert raised.value.kind == "request"
    # One account asked, not two: the second would have been wasted.
    assert asked == ["cloud-a"]


@pytest.mark.asyncio
async def test_every_account_tried_and_refused_reports_the_last_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    pool = cld.CloudinaryPool([_ACCOUNT_A, _ACCOUNT_B])
    client = cld.CloudinaryClient(pool=pool, client=_mock_client(handler))
    with pytest.raises(cld.CloudinaryError) as raised:
        await client.text_to_image("a teapot", model="auto")
    assert raised.value.kind == "rate_limit"


@pytest.mark.asyncio
async def test_reference_images_are_uploaded_and_then_addressed_by_url(
    tmp_path: Path,
) -> None:
    """A local path is not a URL, so an edit uploads it first."""
    calls: list[tuple[str, str]] = []
    reference = tmp_path / "ref.png"
    reference.write_bytes(PNG_BYTES)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/image/upload"):
            return httpx.Response(
                200,
                json={
                    "asset_id": "ref-1",
                    "public_id": "ref",
                    "resource_type": "image",
                    "secure_url": "https://res.cloudinary.com/cloud-a/image/upload/v1/ref.png",
                },
            )
        body = json.loads(request.content)
        assert body["reference_images"] == [
            {
                "source_type": "url",
                "url": "https://res.cloudinary.com/cloud-a/image/upload/v1/ref.png",
            }
        ]
        return httpx.Response(200, json=generation_payload())

    pool = cld.CloudinaryPool([_ACCOUNT_A])
    client = cld.CloudinaryClient(pool=pool, client=_mock_client(handler))
    uploaded = await client.upload(reference.read_bytes(), resource_type="image")
    result = await client.image_to_image("restyle [1]", [uploaded.secure_url], model="auto")

    assert uploaded.public_id == "ref"
    assert result.assets
    assert [path for _, path in calls] == [
        "/v1_1/cloud-a/image/upload",
        "/v2/generate/cloud-a/image_to_image",
    ]


@pytest.mark.asyncio
async def test_an_edit_refuses_more_reference_images_than_the_api_takes() -> None:
    pool = cld.CloudinaryPool([_ACCOUNT_A])
    client = cld.CloudinaryClient(pool=pool)
    with pytest.raises(cld.CloudinaryError):
        await client.image_to_image("x", ["https://example.com/a.png"] * 5)
    with pytest.raises(cld.CloudinaryError):
        await client.image_to_image("x", [])


@pytest.mark.asyncio
async def test_no_configured_account_is_an_explicit_error() -> None:
    client = cld.CloudinaryClient(pool=cld.CloudinaryPool([]))
    with pytest.raises(cld.CloudinaryError, match="no Cloudinary account"):
        await client.text_to_image("a teapot", model="auto")


# ----------------------------------------------------------- the tool provider


def test_cloudinary_is_a_registered_image_provider() -> None:
    assert "cloudinary" in image_gen_provider_names()
    assert get_image_gen_provider("cloudinary") is CloudinaryImageGenerationClient
    assert CloudinaryImageGenerationClient.provider_name == "cloudinary"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("", "auto"),
        ("auto", "auto"),
        # The tool ships an OpenRouter model id by default; it is not ours.
        ("openai/gpt-5.4-image-2", "auto"),
        ("flux-2-pro", "flux-2-pro"),
        ("flux:premium", "flux:premium"),
        ("nano-banana:standard", "nano-banana:standard"),
        ("something-else", "auto"),
        ("other-family:premium", "auto"),
    ],
)
def test_the_shared_model_setting_maps_onto_cloudinary_models(
    given: str, expected: str
) -> None:
    client = CloudinaryImageGenerationClient(api_key=None, api_base=None)
    assert client._cloudinary_model(given) == expected


@pytest.mark.asyncio
async def test_the_provider_returns_a_data_url_the_artifact_store_can_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool stores what a provider returns as a data URL, and so must this."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/image/upload"):
            raise AssertionError("a text-to-image request must not upload anything")
        return httpx.Response(200, json=generation_payload())

    # The generated asset is fetched by the shared downloader, which pins DNS
    # and deliberately ignores a caller's client, so it is replaced here rather
    # than routed through the mock transport.
    async def fake_download(url: str, **kwargs: Any) -> str:
        assert url == ASSET_URL
        return "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")

    monkeypatch.setattr(
        "nanobot.providers.image_generation._download_image_data_url", fake_download
    )

    pool = cld.CloudinaryPool([_ACCOUNT_A])
    media = cld.CloudinaryClient(pool=pool, client=_mock_client(handler))
    client = CloudinaryImageGenerationClient(api_key=None, api_base=None)
    monkeypatch.setattr(client, "_media_client", lambda: media)

    response = await client.generate(
        prompt="a teapot",
        model="openai/gpt-5.4-image-2",
        reference_images=None,
        aspect_ratio="1:1",
        image_size="1K",
    )

    assert response.images and response.images[0].startswith("data:image/png;base64,")
    assert response.raw["model"] == "auto"
    assert response.raw["remaining"] == 7


@pytest.mark.asyncio
async def test_the_provider_reports_missing_credentials_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nanobot.providers.image_generation import ImageGenerationError

    media = cld.CloudinaryClient(pool=cld.CloudinaryPool([]))
    client = CloudinaryImageGenerationClient(api_key=None, api_base=None)
    monkeypatch.setattr(client, "_media_client", lambda: media)

    with pytest.raises(ImageGenerationError, match="Cloudinary is not configured"):
        await client.generate(prompt="a teapot", model="auto")

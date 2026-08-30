from copy import deepcopy

from nanobot.providers.openai_compat_provider import (
    OpenAICompatProvider,
    _normalize_gemini_schema,
)


def test_gemini_normalizer_flattens_nullable_unions_recursively() -> None:
    original = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": ["string", "null"]},
            "count": {"type": ["integer", "null"]},
            "nested": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {"items": {"type": ["array", "null"]}},
            },
        },
        "required": ["value"],
    }
    snapshot = deepcopy(original)

    normalized = _normalize_gemini_schema(original)

    assert normalized == {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "count": {"type": "integer"},
            "nested": {
                "type": "object",
                "properties": {"items": {"type": "array"}},
            },
        },
        "required": ["value"],
    }
    assert original == snapshot


def test_gemini_provider_normalizes_generated_and_extra_tools() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "demo",
            "description": "Demo",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": ["string", "null"],
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
        },
    }
    provider = OpenAICompatProvider(
        api_key="test",
        api_base="https://gemini-proxy.example/v1",
        default_model="gemini-3.1-flash-lite",
        extra_body={"tools": [tool]},
    )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        tools=[tool],
        model=None,
        max_tokens=32,
        temperature=0,
        reasoning_effort=None,
        tool_choice="auto",
    )

    outbound = kwargs["tools"]
    assert outbound[0]["function"]["parameters"] == {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    assert outbound[1]["function"]["parameters"] == outbound[0]["function"]["parameters"]
    assert tool["function"]["parameters"]["properties"]["value"]["type"] == [
        "string",
        "null",
    ]


def test_non_gemini_openai_compat_provider_keeps_schema_unchanged() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "demo",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": ["string", "null"]}},
                "additionalProperties": False,
            },
        },
    }
    provider = OpenAICompatProvider(
        api_key="test",
        api_base="https://api.openai.com/v1",
        default_model="gpt-4o",
    )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        tools=[tool],
        model=None,
        max_tokens=32,
        temperature=0,
        reasoning_effort=None,
        tool_choice="auto",
    )

    assert kwargs["tools"] == [tool]

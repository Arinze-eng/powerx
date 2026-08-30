from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool


def test_novita_tool_schema_is_gemini_and_openai_compatible() -> None:
    schema = NovitaSandboxTool().parameters
    assert schema["type"] == "object"
    assert "additionalProperties" not in schema
    assert schema["required"] == ["action"]
    for value in schema["properties"].values():
        assert value["type"] in {"string", "integer"}
        assert not isinstance(value["type"], list)

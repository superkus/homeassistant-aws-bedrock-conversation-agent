"""Test tool formatting for Bedrock API."""
import pytest
from unittest.mock import MagicMock, patch

from custom_components.bedrock_conversation.bedrock_client import BedrockClient
from custom_components.bedrock_conversation.const import (
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_REGION,
    SERVICE_TOOL_NAME,
)


@pytest.fixture
def client():
    """Create a BedrockClient with mocked dependencies."""
    hass = MagicMock()
    hass.data = {}
    hass.async_add_executor_job = MagicMock()

    entry = MagicMock()
    entry.data = {
        CONF_AWS_ACCESS_KEY_ID: "test",
        CONF_AWS_SECRET_ACCESS_KEY: "test",
        CONF_AWS_REGION: "us-east-1",
    }
    entry.options = {}

    with patch("custom_components.bedrock_conversation.bedrock_client.boto3.Session"):
        return BedrockClient(hass, entry)


class TestFormatToolsForBedrock:
    """Tests for _format_tools_for_bedrock."""

    def test_no_llm_api_returns_empty(self, client):
        result = client._format_tools_for_bedrock(None)
        assert result == []

    def test_no_tools_returns_empty(self, client):
        llm_api = MagicMock()
        llm_api.tools = []
        result = client._format_tools_for_bedrock(llm_api)
        assert result == []

    def test_none_tools_returns_empty(self, client):
        llm_api = MagicMock()
        llm_api.tools = None
        result = client._format_tools_for_bedrock(llm_api)
        assert result == []

    def test_hass_service_tool_has_required_fields(self, client):
        tool = MagicMock()
        tool.name = SERVICE_TOOL_NAME
        tool.description = "Call a service"
        tool.parameters = MagicMock()

        llm_api = MagicMock()
        llm_api.tools = [tool]

        result = client._format_tools_for_bedrock(llm_api)
        assert len(result) == 1
        tool_def = result[0]
        assert tool_def["name"] == SERVICE_TOOL_NAME
        assert tool_def["input_schema"]["type"] == "object"
        assert "service" in tool_def["input_schema"]["properties"]
        assert "target_device" in tool_def["input_schema"]["properties"]
        assert "service" in tool_def["input_schema"]["required"]
        assert "target_device" in tool_def["input_schema"]["required"]

    def test_long_tool_name_truncated(self, client):
        tool = MagicMock()
        tool.name = "a" * 100
        tool.description = "Test"
        tool.parameters = None

        llm_api = MagicMock()
        llm_api.tools = [tool]

        result = client._format_tools_for_bedrock(llm_api)
        assert len(result[0]["name"]) <= 64

    def test_schema_type_enforced_to_object(self, client):
        tool = MagicMock()
        tool.name = "test_tool"
        tool.description = "Test"
        tool.parameters = None

        llm_api = MagicMock()
        llm_api.tools = [tool]

        result = client._format_tools_for_bedrock(llm_api)
        assert result[0]["input_schema"]["type"] == "object"

    def test_unsupported_schema_fields_removed(self, client):
        """Unsupported fields like $schema, title, additionalProperties should be removed."""
        tool = MagicMock()
        tool.name = "test_tool"
        tool.description = "Test"
        tool.parameters = None

        llm_api = MagicMock()
        llm_api.tools = [tool]

        result = client._format_tools_for_bedrock(llm_api)
        schema = result[0]["input_schema"]
        assert "$schema" not in schema
        assert "title" not in schema
        assert "additionalProperties" not in schema

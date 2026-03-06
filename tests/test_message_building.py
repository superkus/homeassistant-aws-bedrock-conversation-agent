"""Test Bedrock message building, role alternation, and tool ID uniqueness."""
import pytest
from unittest.mock import MagicMock, patch
from homeassistant.components import conversation

from custom_components.bedrock_conversation.bedrock_client import BedrockClient
from custom_components.bedrock_conversation.const import (
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_REGION,
)
from homeassistant.helpers import llm


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


class TestBuildBedrockMessages:
    """Tests for _build_bedrock_messages."""

    def test_simple_user_message(self, client):
        content = [
            conversation.SystemContent(content="System prompt"),
            conversation.UserContent(content="Hello"),
        ]
        messages = client._build_bedrock_messages(content)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"][0]["text"] == "Hello"

    def test_system_content_excluded_from_messages(self, client):
        content = [
            conversation.SystemContent(content="System prompt"),
            conversation.UserContent(content="Hi"),
        ]
        messages = client._build_bedrock_messages(content)
        # System content should not appear as a message
        for msg in messages:
            assert msg["role"] != "system"

    def test_user_assistant_alternation(self, client):
        content = [
            conversation.SystemContent(content="System"),
            conversation.UserContent(content="Hello"),
            conversation.AssistantContent(agent_id="test", content="Hi there"),
            conversation.UserContent(content="How are you?"),
        ]
        messages = client._build_bedrock_messages(content)
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"

    def test_consecutive_user_messages_merged(self, client):
        content = [
            conversation.UserContent(content="First"),
            conversation.UserContent(content="Second"),
        ]
        messages = client._build_bedrock_messages(content)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert len(messages[0]["content"]) == 2

    def test_consecutive_assistant_messages_merged(self, client):
        content = [
            conversation.UserContent(content="Hi"),
            conversation.AssistantContent(agent_id="a", content="Hello"),
            conversation.AssistantContent(agent_id="a", content="How can I help?"),
        ]
        messages = client._build_bedrock_messages(content)
        assert len(messages) == 2
        assert messages[1]["role"] == "assistant"
        # Content from both assistant messages should be merged
        assert len(messages[1]["content"]) == 2

    def test_starts_with_user_message(self, client):
        content = [
            conversation.AssistantContent(agent_id="a", content="Hello"),
        ]
        messages = client._build_bedrock_messages(content)
        # Should insert a dummy user message at the start
        assert messages[0]["role"] == "user"

    def test_tool_result_goes_to_user_message(self, client):
        tool_call = llm.ToolInput(tool_name="HassCallService", tool_args={"service": "light.turn_on"})
        content = [
            conversation.UserContent(content="Turn on light"),
            conversation.AssistantContent(
                agent_id="a", content="", tool_calls=[tool_call]
            ),
            conversation.ToolResultContent(
                agent_id="a",
                tool_call_id="tool_123",
                tool_name="HassCallService",
                tool_result={"result": "success"},
            ),
        ]
        messages = client._build_bedrock_messages(content)
        # Tool result should be in a user message
        last_user_msg = [m for m in messages if m["role"] == "user"][-1]
        has_tool_result = any(
            b.get("type") == "tool_result" for b in last_user_msg["content"]
        )
        assert has_tool_result

    def test_tool_use_ids_are_unique(self, client):
        """Multiple tool calls in one assistant message should have unique IDs."""
        tool1 = llm.ToolInput(tool_name="HassCallService", tool_args={"service": "light.turn_on", "target_device": "light.a"})
        tool2 = llm.ToolInput(tool_name="HassCallService", tool_args={"service": "light.turn_on", "target_device": "light.b"})
        content = [
            conversation.UserContent(content="Turn on all lights"),
            conversation.AssistantContent(
                agent_id="a", content="", tool_calls=[tool1, tool2]
            ),
        ]
        messages = client._build_bedrock_messages(content)
        tool_ids = []
        for msg in messages:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_ids.append(block["id"])
        assert len(tool_ids) == 2
        assert tool_ids[0] != tool_ids[1], "Tool IDs must be unique"

    def test_empty_content_not_added(self, client):
        content = [
            conversation.UserContent(content="Hi"),
            conversation.AssistantContent(agent_id="a", content=""),
        ]
        messages = client._build_bedrock_messages(content)
        # Assistant message with empty content and no tool calls should not be added
        assert len(messages) == 1

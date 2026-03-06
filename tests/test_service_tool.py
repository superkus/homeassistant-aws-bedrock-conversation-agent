"""Test HassServiceTool execution and validation."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio

from homeassistant.helpers import llm
from custom_components.bedrock_conversation import HassServiceTool
from custom_components.bedrock_conversation.const import SERVICE_TOOL_NAME


@pytest.fixture
def hass():
    mock_hass = MagicMock()
    mock_hass.services = MagicMock()
    mock_hass.services.async_call = AsyncMock()
    return mock_hass


@pytest.fixture
def tool(hass):
    return HassServiceTool(hass)


@pytest.fixture
def llm_context():
    return MagicMock()


class TestHassServiceTool:

    def test_tool_name(self, tool):
        assert tool.name == SERVICE_TOOL_NAME

    def test_tool_has_description(self, tool):
        assert len(tool.description) > 0

    @pytest.mark.asyncio
    async def test_successful_service_call(self, tool, hass, llm_context):
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={"service": "light.turn_on", "target_device": "light.kitchen"},
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "success"
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_service_param(self, tool, hass, llm_context):
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={"target_device": "light.kitchen"},
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "error"

    @pytest.mark.asyncio
    async def test_missing_target_device_param(self, tool, hass, llm_context):
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={"service": "light.turn_on"},
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_service_format(self, tool, hass, llm_context):
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={"service": "invalid_no_dot", "target_device": "light.kitchen"},
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "error"

    @pytest.mark.asyncio
    async def test_disallowed_domain(self, tool, hass, llm_context):
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={"service": "homeassistant.restart", "target_device": "test"},
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "error"

    @pytest.mark.asyncio
    async def test_disallowed_service(self, tool, hass, llm_context):
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={"service": "light.some_unknown_service", "target_device": "light.test"},
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "error"

    @pytest.mark.asyncio
    async def test_extra_allowed_args_passed(self, tool, hass, llm_context):
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={
                "service": "light.turn_on",
                "target_device": "light.kitchen",
                "brightness": 200,
            },
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "success"
        call_args = hass.services.async_call.call_args
        assert call_args[1].get("blocking") is False or call_args[0][2].get("brightness") == 200

    @pytest.mark.asyncio
    async def test_service_call_exception(self, tool, hass, llm_context):
        hass.services.async_call = AsyncMock(side_effect=Exception("Service failed"))
        tool_input = llm.ToolInput(
            tool_name=SERVICE_TOOL_NAME,
            tool_args={"service": "light.turn_on", "target_device": "light.kitchen"},
        )
        result = await tool.async_call(hass, tool_input, llm_context)
        assert result["result"] == "error"

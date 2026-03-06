"""Test inference profile resolution."""
import pytest
from unittest.mock import MagicMock, patch

from custom_components.bedrock_conversation.bedrock_client import BedrockClient
from custom_components.bedrock_conversation.const import (
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_REGION,
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


class TestResolveInferenceProfile:
    """Tests for _resolve_inference_profile."""

    def test_us_region_adds_us_prefix(self, client):
        result = client._resolve_inference_profile(
            "amazon.nova-lite-v1:0", {CONF_AWS_REGION: "us-east-1"}
        )
        assert result == "us.amazon.nova-lite-v1:0"

    def test_eu_region_adds_eu_prefix(self, client):
        result = client._resolve_inference_profile(
            "amazon.nova-lite-v1:0", {CONF_AWS_REGION: "eu-west-3"}
        )
        assert result == "eu.amazon.nova-lite-v1:0"

    def test_ap_region_adds_ap_prefix(self, client):
        result = client._resolve_inference_profile(
            "amazon.nova-lite-v1:0", {CONF_AWS_REGION: "ap-northeast-1"}
        )
        assert result == "ap.amazon.nova-lite-v1:0"

    def test_ca_region_adds_ca_prefix(self, client):
        result = client._resolve_inference_profile(
            "amazon.nova-lite-v1:0", {CONF_AWS_REGION: "ca-central-1"}
        )
        assert result == "ca.amazon.nova-lite-v1:0"

    def test_sa_region_adds_sa_prefix(self, client):
        result = client._resolve_inference_profile(
            "amazon.nova-lite-v1:0", {CONF_AWS_REGION: "sa-east-1"}
        )
        assert result == "sa.amazon.nova-lite-v1:0"

    def test_already_prefixed_model_unchanged(self, client):
        result = client._resolve_inference_profile(
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0", {CONF_AWS_REGION: "eu-west-3"}
        )
        assert result == "us.anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_eu_prefixed_model_unchanged(self, client):
        result = client._resolve_inference_profile(
            "eu.amazon.nova-lite-v1:0", {CONF_AWS_REGION: "us-east-1"}
        )
        assert result == "eu.amazon.nova-lite-v1:0"

    def test_arn_model_unchanged(self, client):
        arn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku"
        result = client._resolve_inference_profile(arn, {CONF_AWS_REGION: "eu-west-3"})
        assert result == arn

    def test_unknown_region_returns_as_is(self, client):
        result = client._resolve_inference_profile(
            "amazon.nova-lite-v1:0", {CONF_AWS_REGION: "xx-unknown-1"}
        )
        assert result == "amazon.nova-lite-v1:0"

    def test_falls_back_to_entry_data_region(self, client):
        """When options dict has no region, falls back to entry.data."""
        result = client._resolve_inference_profile("amazon.nova-lite-v1:0", {})
        # entry.data has us-east-1
        assert result == "us.amazon.nova-lite-v1:0"

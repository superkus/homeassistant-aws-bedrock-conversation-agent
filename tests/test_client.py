"""Tests for the boto3 wrapper: model listing, streaming and fallbacks."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock

from botocore.exceptions import ClientError, EndpointConnectionError, EventStreamError
from homeassistant.core import HomeAssistant
import pytest

from custom_components.bedrock_conversation.client import (
    BedrockAccessDeniedError,
    BedrockAuthError,
    BedrockClient,
    BedrockConnectionError,
    BedrockError,
    _profile_preference,
    translate_error,
)
from custom_components.bedrock_conversation.const import BEDROCK_STREAM_QUEUE_MAXSIZE

from .conftest import ENTRY_DATA


def _client_error(code: str, message: str = "msg") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ConverseStream")


class FakeEventStream:
    """Stands in for botocore's EventStream."""

    def __init__(self, events: list[dict[str, Any]], error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.closed = False

    def __iter__(self):
        yield from self.events
        if self.error is not None:  # e.g. an EventStreamError mid-stream
            raise self.error

    def close(self) -> None:
        self.closed = True


TEXT_DELTA = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hola"}}}
# Messages that would trigger the profile / prompt-cache retries before any event.
RETRYABLE_MESSAGES = [
    "Retry your request with the ID or ARN of an inference profile",
    "This model doesn't support prompt caching",
]


def mid_stream_errors() -> list[Exception]:
    """A ValidationException and an EventStreamError for each retryable message."""
    errors: list[Exception] = []
    for message in RETRYABLE_MESSAGES:
        response = {"Error": {"Code": "ValidationException", "Message": message}}
        errors += [
            ClientError(response, "ConverseStream"),
            EventStreamError(response, "ConverseStream"),
        ]
    return errors


PROFILES = [
    {
        "inferenceProfileId": "us.anthropic.claude-x",
        "inferenceProfileName": "US Claude X",
        "status": "ACTIVE",
        "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-x"}],
    },
    {
        "inferenceProfileId": "eu.anthropic.claude-x",
        "inferenceProfileName": "EU Claude X",
        "status": "ACTIVE",
        "models": [{"modelArn": "arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-x"}],
    },
    {
        "inferenceProfileId": "eu.cohere.embed",
        "inferenceProfileName": "EU Embed",
        "status": "ACTIVE",
        "models": [{"modelArn": "arn:aws:bedrock:eu-west-3::foundation-model/cohere.embed"}],
    },
]
FOUNDATION_MODELS = {
    "modelSummaries": [
        {
            "modelId": "anthropic.claude-x",
            "providerName": "Anthropic",
            "modelName": "Claude X",
            "inputModalities": ["TEXT", "IMAGE"],
            "inferenceTypesSupported": ["INFERENCE_PROFILE"],
            "responseStreamingSupported": True,
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            "modelId": "amazon.nova-lite-v1:0",
            "providerName": "Amazon",
            "modelName": "Nova Lite",
            "inputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "responseStreamingSupported": True,
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            "modelId": "anthropic.claude-old",
            "providerName": "Anthropic",
            "modelName": "Claude Old",
            "inputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "LEGACY"},
        },
    ]
}


@pytest.fixture
def client(hass: HomeAssistant) -> BedrockClient:
    """A client with fake boto3 clients."""
    client = BedrockClient(hass, ENTRY_DATA)
    client._runtime = MagicMock()
    client._control = MagicMock()
    client._control.list_foundation_models.return_value = FOUNDATION_MODELS
    client._control.get_paginator.return_value.paginate.return_value = [
        {"inferenceProfileSummaries": PROFILES}
    ]
    return client


def test_boto_clients_use_two_total_attempts(hass: HomeAssistant) -> None:
    """Botocore makes one initial request and at most one retry."""
    client = BedrockClient(hass, ENTRY_DATA)
    client._create_clients()
    try:
        assert client._runtime.meta.config.retries["total_max_attempts"] == 2
        assert client._control.meta.config.retries["total_max_attempts"] == 2
    finally:
        client._runtime.close()
        client._control.close()


async def _collect(client: BedrockClient, kwargs: dict) -> list[dict]:
    return [event async for event in client.async_converse_stream(kwargs)]


@pytest.mark.parametrize(
    ("error", "cls"),
    [
        (_client_error("ExpiredTokenException"), BedrockAuthError),
        (_client_error("UnrecognizedClientException"), BedrockAuthError),
        (_client_error("InvalidSignatureException"), BedrockAuthError),
        (_client_error("AccessDeniedException"), BedrockAccessDeniedError),
        (_client_error("ThrottlingException"), BedrockError),
        (EndpointConnectionError(endpoint_url="https://x"), BedrockConnectionError),
    ],
)
def test_translate_error(error: Exception, cls: type) -> None:
    """botocore errors map to our error classes (exact class)."""
    assert type(translate_error(error)) is cls


@pytest.mark.parametrize(
    ("region", "first"),
    [
        ("eu-west-3", "eu"),
        ("us-east-1", "us"),
        ("ap-south-1", "apac"),
        ("ap-northeast-1", "jp"),
        ("us-gov-west-1", "us-gov"),
    ],
)
def test_profile_preference(region: str, first: str) -> None:
    """APAC regions use apac. (1.x used the invalid ap. prefix)."""
    assert _profile_preference(region)[0] == first
    assert "ap" not in _profile_preference(region)


async def test_list_models(client: BedrockClient) -> None:
    """Profiles for text models first, then on-demand models; legacy dropped."""
    models = await client.async_list_models()
    assert [m.id for m in models] == [
        "eu.anthropic.claude-x",
        "us.anthropic.claude-x",
        "amazon.nova-lite-v1:0",
    ]
    assert models[-1].label == "Amazon Nova Lite (amazon.nova-lite-v1:0)"


async def test_list_models_without_profile_permission(client: BedrockClient) -> None:
    """Missing ListInferenceProfiles permission still lists base models."""
    client._control.get_paginator.return_value.paginate.side_effect = _client_error(
        "AccessDeniedException"
    )
    assert [m.id for m in await client.async_list_models()] == ["amazon.nova-lite-v1:0"]


async def test_stream_events(client: BedrockClient) -> None:
    """Events come through the executor thread in order; the stream is closed."""
    stream = FakeEventStream([{"messageStart": {}}, {"messageStop": {"stopReason": "end_turn"}}])
    client._runtime.converse_stream.return_value = {"stream": stream}
    assert await _collect(client, {"modelId": "amazon.nova-lite-v1:0"}) == stream.events
    assert stream.closed


async def test_stream_error_translated(client: BedrockClient) -> None:
    """A ClientError in the worker thread surfaces as BedrockError."""
    client._runtime.converse_stream.side_effect = _client_error("ThrottlingException", "slow")
    with pytest.raises(BedrockError) as err:
        await _collect(client, {"modelId": "amazon.nova-lite-v1:0"})
    assert err.value.code == "ThrottlingException"


async def test_inference_profile_fallback(client: BedrockClient) -> None:
    """A base id that needs a profile is retried with the region's profile."""
    client._runtime.converse_stream.side_effect = [
        _client_error(
            "ValidationException",
            "Invocation of model ID anthropic.claude-x with on-demand throughput isn't "
            "supported. Retry your request with the ID or ARN of an inference profile",
        ),
        {"stream": FakeEventStream([{"messageStart": {}}])},
        {"stream": FakeEventStream([{"messageStart": {}}])},
    ]
    await _collect(client, {"modelId": "anthropic.claude-x"})
    assert client._runtime.converse_stream.call_args.kwargs["modelId"] == "eu.anthropic.claude-x"
    # The resolved profile is remembered for the next request.
    await _collect(client, {"modelId": "anthropic.claude-x"})
    assert client._runtime.converse_stream.call_count == 3
    assert client._runtime.converse_stream.call_args.kwargs["modelId"] == "eu.anthropic.claude-x"


async def test_prefixed_ids_are_never_rewritten(client: BedrockClient) -> None:
    """Profile ids (incl. global. and apac.) are sent exactly as chosen."""
    client._runtime.converse_stream.side_effect = _client_error(
        "ValidationException", "needs an inference profile"
    )
    for model in ("global.anthropic.claude-x", "apac.anthropic.claude-x"):
        with pytest.raises(BedrockError):
            await _collect(client, {"modelId": model})
    assert [c.kwargs["modelId"] for c in client._runtime.converse_stream.call_args_list] == [
        "global.anthropic.claude-x",
        "apac.anthropic.claude-x",
    ]


async def test_prompt_cache_fallback(client: BedrockClient) -> None:
    """Models without prompt caching are retried without cache points."""
    client._runtime.converse_stream.side_effect = [
        _client_error("ValidationException", "This model doesn't support prompt caching"),
        {"stream": FakeEventStream([{"messageStart": {}}])},
    ]
    kwargs = {
        "modelId": "amazon.nova-lite-v1:0",
        "system": [{"text": "hi"}, {"cachePoint": {"type": "default"}}],
        "toolConfig": {"tools": [{"toolSpec": {"name": "t"}}, {"cachePoint": {"type": "default"}}]},
    }
    await _collect(client, kwargs)
    sent = client._runtime.converse_stream.call_args.kwargs
    assert sent["system"] == [{"text": "hi"}]
    assert sent["toolConfig"]["tools"] == [{"toolSpec": {"name": "t"}}]
    assert kwargs["system"][-1] == {"cachePoint": {"type": "default"}}  # caller's copy intact


async def test_other_validation_errors_not_retried(client: BedrockClient) -> None:
    """Unrelated validation errors are raised immediately."""
    client._runtime.converse_stream.side_effect = _client_error("ValidationException", "bad input")
    with pytest.raises(BedrockError):
        await _collect(client, {"modelId": "amazon.nova-lite-v1:0"})
    assert client._runtime.converse_stream.call_count == 1


@pytest.mark.parametrize("error", mid_stream_errors())
async def test_error_after_text_is_not_retried(client: BedrockClient, error: Exception) -> None:
    """Once text was streamed, a retry would repeat it: the error is raised as is."""
    stream = FakeEventStream([{"messageStart": {}}, TEXT_DELTA], error=error)
    client._runtime.converse_stream.return_value = {"stream": stream}
    received: list[dict] = []
    with pytest.raises(BedrockError) as err:
        async for event in client.async_converse_stream({"modelId": "anthropic.claude-x"}):
            received.append(event)
    assert received == [{"messageStart": {}}, TEXT_DELTA]
    assert err.value.code == "ValidationException"
    assert client._runtime.converse_stream.call_count == 1
    assert stream.closed


async def _wait_until(predicate, timeout: float = 2) -> None:
    """Yield to executor workers until a test condition is true."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class _GatedEventStream:
    """Keep an executor worker occupied until the test releases it."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.started = threading.Event()
        self.closed = False

    def __iter__(self):
        self.started.set()
        self.release.wait(5)
        yield {"messageStop": {"stopReason": "end_turn"}}

    def close(self) -> None:
        self.closed = True


async def test_stream_concurrency_is_bounded_per_client(client: BedrockClient) -> None:
    """A third stream waits asynchronously instead of occupying the executor."""
    release = threading.Event()
    streams = [_GatedEventStream(release) for _ in range(3)]
    client._runtime.converse_stream.side_effect = [{"stream": stream} for stream in streams]

    tasks = [
        asyncio.create_task(_collect(client, {"modelId": "amazon.nova-lite-v1:0"})) for _ in streams
    ]
    try:
        await _wait_until(lambda: sum(stream.started.is_set() for stream in streams) == 2)
        await asyncio.sleep(0.05)
        assert client._runtime.converse_stream.call_count == 2
    finally:
        release.set()
    await asyncio.gather(*tasks)
    assert client._runtime.converse_stream.call_count == 3
    assert all(stream.closed for stream in streams)


class _FastEventStream:
    """Produce enough events to fill the thread-to-loop bridge."""

    def __init__(self) -> None:
        self.produced = 0
        self.closed = threading.Event()
        self._lock = threading.Lock()

    def __iter__(self):
        for index in range(1000):
            with self._lock:
                self.produced += 1
            yield {"event": index}

    def produced_count(self) -> int:
        with self._lock:
            return self.produced

    def close(self) -> None:
        self.closed.set()


async def test_stream_bridge_backpressures_and_releases_on_close(client: BedrockClient) -> None:
    """The producer stops at the queue bound and cancellation unblocks it."""
    stream = _FastEventStream()
    client._runtime.converse_stream.return_value = {"stream": stream}
    events = client._async_stream_once({"modelId": "amazon.nova-lite-v1:0"})

    assert await anext(events) == {"event": 0}
    await _wait_until(lambda: stream.produced_count() >= BEDROCK_STREAM_QUEUE_MAXSIZE + 1)
    await asyncio.sleep(0.05)
    # One event was consumed, the queue holds the configured maximum, and the
    # producer may have fetched one more event before blocking on queue.put().
    assert stream.produced_count() <= BEDROCK_STREAM_QUEUE_MAXSIZE + 2

    async with asyncio.timeout(1):
        await events.aclose()
    await _wait_until(stream.closed.is_set)

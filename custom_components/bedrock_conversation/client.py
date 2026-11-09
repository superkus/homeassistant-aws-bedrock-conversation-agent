"""AWS Bedrock client: credentials, model discovery and Converse streaming.

All boto3 calls are blocking, so they run in the executor. Streaming events
are handed back to the event loop through an asyncio queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
import copy
from dataclasses import dataclass
import threading
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionError as BotoConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from homeassistant.core import HomeAssistant

from .const import (
    BEDROCK_CONNECT_TIMEOUT,
    BEDROCK_MAX_ATTEMPTS,
    BEDROCK_MAX_CONCURRENT_STREAMS,
    BEDROCK_READ_TIMEOUT,
    BEDROCK_STREAM_QUEUE_MAXSIZE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_REGION,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_SESSION_TOKEN,
    DEFAULT_AWS_REGION,
    LOGGER,
)

_AUTH_ERROR_CODES = frozenset(
    {
        "UnrecognizedClientException",
        "InvalidSignatureException",
        "SignatureDoesNotMatch",
        "InvalidClientTokenId",
        "ExpiredToken",
        "ExpiredTokenException",
    }
)
_REGIONAL_PROFILE_PREFIXES = (
    "us.",
    "us-gov.",
    "eu.",
    "apac.",
    "jp.",
    "au.",
    "ca.",
    "global.",
)


class BedrockError(Exception):
    """Base error for Bedrock calls."""

    def __init__(self, code: str, message: str) -> None:
        """Initialise with the AWS error code and message."""
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BedrockAuthError(BedrockError):
    """Credentials are invalid or expired."""


class BedrockAccessDeniedError(BedrockError):
    """Credentials are valid but lack IAM permission or model access."""


class BedrockConnectionError(BedrockError):
    """AWS could not be reached."""


@dataclass(frozen=True, slots=True)
class ModelOption:
    """A model or inference profile the user can pick."""

    id: str
    label: str


def translate_error(err: Exception) -> BedrockError:
    """Map a botocore exception to one of our error classes."""
    if isinstance(err, BedrockError):
        return err
    if isinstance(err, NoCredentialsError):
        return BedrockAuthError("NoCredentials", str(err))
    if isinstance(err, ClientError):
        error = err.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", str(err))
        if code in _AUTH_ERROR_CODES:
            return BedrockAuthError(code, message)
        if code == "AccessDeniedException":
            return BedrockAccessDeniedError(code, message)
        return BedrockError(code, message)
    # Connect timeouts are ConnectionErrors; read timeouts are not, but for
    # the user both mean "AWS did not answer".
    if isinstance(err, (BotoConnectionError, ReadTimeoutError)):
        return BedrockConnectionError(type(err).__name__, str(err))
    return BedrockError(type(err).__name__, str(err))


def _boto_config() -> Config:
    return Config(
        connect_timeout=BEDROCK_CONNECT_TIMEOUT,
        read_timeout=BEDROCK_READ_TIMEOUT,
        retries={"total_max_attempts": BEDROCK_MAX_ATTEMPTS, "mode": "standard"},
    )


def get_bedrock_regions() -> dict[str, str]:
    """Return region code -> label for every region with Bedrock Runtime.

    Reads botocore's bundled endpoint data, so it needs no network access.
    Blocking (reads files): call it from the executor.
    """
    session = boto3.Session()
    # Newer botocore releases no longer list "bedrock-runtime" endpoints; the
    # control-plane "bedrock" service (used to list models) is listed, and it
    # is available wherever the runtime is.
    regions = session.get_available_regions("bedrock") or session.get_available_regions(
        "bedrock-runtime"
    )
    labels: dict[str, str] = {}
    try:
        endpoints = session._session.get_component("data_loader").load_data("endpoints")
        for partition in endpoints.get("partitions", []):
            for code, info in partition.get("regions", {}).items():
                labels[code] = info.get("description", code)
    except Exception:
        LOGGER.debug("Could not load region descriptions", exc_info=True)
    return {code: f"{labels.get(code, code)} ({code})" for code in sorted(regions)}


def _profile_preference(region: str) -> list[str]:
    """Return inference profile prefixes to prefer for a region, best first."""
    if region.startswith("us-gov-"):
        return ["us-gov", "global"]
    if region == "ap-northeast-1":
        return ["jp", "apac", "global"]
    if region == "ap-southeast-2":
        return ["au", "apac", "global"]
    if region.startswith("ap-"):
        return ["apac", "global"]
    if region.startswith("eu-"):
        return ["eu", "global"]
    if region.startswith("ca-"):
        return ["ca", "us", "global"]
    return ["us", "global"]


def _strip_cache_points(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of Converse kwargs without prompt cache points."""
    kwargs = copy.copy(kwargs)
    if "system" in kwargs:
        kwargs["system"] = [b for b in kwargs["system"] if "cachePoint" not in b]
    if "toolConfig" in kwargs:
        tool_config = dict(kwargs["toolConfig"])
        tool_config["tools"] = [t for t in tool_config["tools"] if "cachePoint" not in t]
        kwargs["toolConfig"] = tool_config
    return kwargs


class BedrockClient:
    """Thin async wrapper around the boto3 Bedrock clients."""

    def __init__(self, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Initialise from config entry data (credentials and region)."""
        self.hass = hass
        self.region: str = data.get(CONF_AWS_REGION) or DEFAULT_AWS_REGION
        self._credentials = {
            "aws_access_key_id": data.get(CONF_AWS_ACCESS_KEY_ID) or None,
            "aws_secret_access_key": data.get(CONF_AWS_SECRET_ACCESS_KEY) or None,
            # An empty session token must be None or boto3 signs it and fails.
            "aws_session_token": data.get(CONF_AWS_SESSION_TOKEN) or None,
        }
        self._runtime: Any = None
        self._control: Any = None
        # Models that rejected prompt cache points; skip caching for them.
        self._no_cache_models: set[str] = set()
        # model id -> inference profile id that worked for it.
        self._profile_overrides: dict[str, str] = {}
        # Shared by every agent using this credentials client. Semaphore waits
        # yield to the event loop instead of occupying another executor thread.
        self._stream_slots = asyncio.Semaphore(BEDROCK_MAX_CONCURRENT_STREAMS)

    def _create_clients(self) -> None:
        session = boto3.Session(region_name=self.region, **self._credentials)
        config = _boto_config()
        self._runtime = session.client("bedrock-runtime", config=config)
        self._control = session.client("bedrock", config=config)

    async def async_setup(self) -> None:
        """Create the boto3 clients (endpoint data loads from disk)."""
        await self.hass.async_add_executor_job(self._create_clients)

    async def _async_control(self, method: str, **kwargs: Any) -> Any:
        if self._control is None:
            await self.async_setup()

        def _call() -> Any:
            return getattr(self._control, method)(**kwargs)

        try:
            return await self.hass.async_add_executor_job(_call)
        except Exception as err:
            raise translate_error(err) from err

    async def async_validate(self) -> None:
        """Check the credentials can list model metadata in the region."""
        await self._async_control("list_foundation_models", byOutputModality="TEXT")

    async def _async_list_profiles(self) -> list[dict[str, Any]]:
        if self._control is None:
            await self.async_setup()

        def _call() -> list[dict[str, Any]]:
            paginator = self._control.get_paginator("list_inference_profiles")
            profiles: list[dict[str, Any]] = []
            for page in paginator.paginate(typeEquals="SYSTEM_DEFINED"):
                profiles.extend(page.get("inferenceProfileSummaries", []))
            return profiles

        try:
            return await self.hass.async_add_executor_job(_call)
        except Exception as err:
            raise translate_error(err) from err

    async def async_list_models(self) -> list[ModelOption]:
        """Return selectable model and inference-profile metadata for this region.

        The listing APIs do not verify that the credentials have model access
        or ``InvokeModelWithResponseStream`` permission; invocation does that.
        Inference profiles come first because newer models are only reachable
        through them. Base models are listed only when their metadata advertises
        on-demand invocation; inactive (legacy) models are left out.
        """
        response = await self._async_control("list_foundation_models", byOutputModality="TEXT")
        text_models: dict[str, dict[str, Any]] = {}
        on_demand: list[ModelOption] = []
        for model in response.get("modelSummaries", []):
            if model.get("modelLifecycle", {}).get("status", "ACTIVE") != "ACTIVE":
                continue
            if "TEXT" not in model.get("inputModalities", []):
                continue
            if model.get("responseStreamingSupported") is False:
                continue
            text_models[model["modelId"]] = model
            if "ON_DEMAND" in model.get("inferenceTypesSupported", []):
                label = f"{model.get('providerName', '')} {model.get('modelName', '')}"
                on_demand.append(
                    ModelOption(model["modelId"], f"{label.strip()} ({model['modelId']})")
                )

        profiles: list[ModelOption] = []
        try:
            for profile in await self._async_list_profiles():
                if profile.get("status", "ACTIVE") != "ACTIVE":
                    continue
                base_ids = {
                    m.get("modelArn", "").rsplit("/", 1)[-1] for m in profile.get("models", [])
                }
                if not base_ids & text_models.keys():
                    continue
                profile_id = profile["inferenceProfileId"]
                name = profile.get("inferenceProfileName", profile_id)
                profiles.append(ModelOption(profile_id, f"{name} ({profile_id})"))
        except BedrockAccessDeniedError:
            # Older IAM policies may lack ListInferenceProfiles; base-model
            # metadata can still be listed, and the user can type a profile id.
            LOGGER.warning(
                "Missing bedrock:ListInferenceProfiles permission; only base models are listed"
            )

        profiles.sort(key=lambda m: m.label.lower())
        on_demand.sort(key=lambda m: m.label.lower())
        return profiles + on_demand

    async def _async_find_profile(self, model_id: str) -> str | None:
        """Find a system inference profile that serves ``model_id`` here."""
        candidates = [
            p["inferenceProfileId"]
            for p in await self._async_list_profiles()
            if any(m.get("modelArn", "").endswith(f"/{model_id}") for m in p.get("models", []))
        ]
        for prefix in _profile_preference(self.region):
            for candidate in candidates:
                if candidate.startswith(f"{prefix}."):
                    return candidate
        return candidates[0] if candidates else None

    async def async_converse_stream(self, kwargs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield one ConverseStream response within this client's stream limit.

        Waiting for a slot is asynchronous. The caller's turn timeout remains
        outside this method, so it covers both this wait and response streaming.
        """
        async with self._stream_slots:
            async for event in self._async_converse_stream(kwargs):
                yield event

    async def _async_converse_stream(self, kwargs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Stream events, applying configuration fallbacks before the first one.

        Before any event has been yielded, two configuration problems are
        fixed transparently and the call is retried once each:
        a model that needs an inference profile, and a model that does not
        support prompt caching.
        """
        model_id = kwargs["modelId"]
        kwargs = dict(kwargs)
        if model_id in self._profile_overrides:
            kwargs["modelId"] = self._profile_overrides[model_id]
        if model_id in self._no_cache_models:
            kwargs = _strip_cache_points(kwargs)

        tried_profile = tried_cache = False
        while True:
            yielded = False
            try:
                async for event in self._async_stream_once(kwargs):
                    yielded = True
                    yield event
                return
            except BedrockError as err:
                if yielded or err.code != "ValidationException":
                    raise
                message = err.message.lower()
                if (
                    "inference profile" in message
                    and not tried_profile
                    and not kwargs["modelId"].startswith((*_REGIONAL_PROFILE_PREFIXES, "arn:"))
                ):
                    tried_profile = True
                    profile = await self._async_find_profile(kwargs["modelId"])
                    if profile is None:
                        raise
                    LOGGER.info(
                        "Model %s needs an inference profile; using %s",
                        model_id,
                        profile,
                    )
                    self._profile_overrides[model_id] = profile
                    kwargs["modelId"] = profile
                    continue
                # Loose match on purpose: AWS words this differently per model
                # ("cache", "caching", "cachePoint").
                if "cach" in message and not tried_cache:
                    tried_cache = True
                    LOGGER.info(
                        "Model %s does not support prompt caching; disabling it",
                        model_id,
                    )
                    self._no_cache_models.add(model_id)
                    kwargs = _strip_cache_points(kwargs)
                    continue
                raise

    async def _async_stream_once(self, kwargs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if self._runtime is None:
            await self.async_setup()

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=BEDROCK_STREAM_QUEUE_MAXSIZE)
        stop = threading.Event()

        def put(kind: str, payload: Any) -> bool:
            """Put from the worker, waiting while the async consumer catches up."""
            pending = asyncio.run_coroutine_threadsafe(queue.put((kind, payload)), loop)
            while not stop.is_set():
                try:
                    pending.result(timeout=0.1)
                except FutureTimeoutError:
                    continue
                return True
            pending.cancel()
            return False

        def worker() -> None:
            stream = None
            error: Exception | None = None
            try:
                stream = self._runtime.converse_stream(**kwargs)["stream"]
                if stop.is_set():  # cancelled while connecting
                    return
                for event in stream:
                    if stop.is_set() or not put("event", event):
                        break
            except Exception as err:
                error = err
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        LOGGER.debug("Could not close Bedrock event stream", exc_info=True)
                if error is not None:
                    put("error", error)
                put("done", None)

        self.hass.async_add_executor_job(worker)
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    return
                if kind == "error":
                    raise translate_error(payload) from payload
                yield payload
        finally:
            # Release a producer blocked on a full queue when the consumer
            # times out, is cancelled, or stops reading before stream end.
            stop.set()

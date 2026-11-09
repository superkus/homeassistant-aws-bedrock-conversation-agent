"""Constants for the AWS Bedrock Conversation integration."""

from __future__ import annotations

import logging
import re
from typing import Final

from homeassistant.const import (
    CONF_LLM_HASS_API as CONF_LLM_HASS_API,  # re-exported
    CONF_MODEL,
    CONF_PROMPT as CONF_PROMPT,  # re-exported
)
from homeassistant.helpers import llm

DOMAIN: Final = "bedrock_conversation"
LOGGER = logging.getLogger(__package__)

# ID of the LLM API this integration registers (kept stable for existing users).
HOME_LLM_API_ID: Final = f"{DOMAIN}_services"
# These APIs enforce Settings -> Voice assistants -> Expose for every entity.
EXPOSURE_BOUNDED_LLM_API_IDS: Final = frozenset({llm.LLM_API_ASSIST, HOME_LLM_API_ID})


def exposure_bounded_llm_api_ids(configured: object) -> list[str]:
    """Return configured API IDs that enforce per-entity exposure."""
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple)):
        return []
    return [
        api_id
        for api_id in configured
        if isinstance(api_id, str) and api_id in EXPOSURE_BOUNDED_LLM_API_IDS
    ]


# Config entry data (credentials + region)
CONF_AWS_ACCESS_KEY_ID: Final = "aws_access_key_id"
CONF_AWS_SECRET_ACCESS_KEY: Final = "aws_secret_access_key"
CONF_AWS_SESSION_TOKEN: Final = "aws_session_token"
CONF_AWS_REGION: Final = "aws_region"

# Config sub-entry data (per-agent settings). CONF_PROMPT and
# CONF_LLM_HASS_API come from homeassistant.const, like in HA's own LLM
# integrations.
CONF_MODEL_ID: Final = CONF_MODEL
CONF_MAX_TOKENS: Final = "max_tokens"
CONF_TEMPERATURE: Final = "temperature"
CONF_MAX_TOOL_CALL_ITERATIONS: Final = "max_tool_call_iterations"
CONF_PROMPT_CACHING: Final = "prompt_caching"
CONF_CONFIRM_SENSITIVE_ACTIONS: Final = "confirm_sensitive_actions"
CONF_EXTRA_ATTRIBUTES_TO_EXPOSE: Final = "extra_attributes_to_expose"

DEFAULT_AWS_REGION: Final = "us-east-1"
# Each agent is a sub-entry of this type under the credentials entry.
SUBENTRY_CONVERSATION: Final = "conversation"
DEFAULT_AGENT_NAME: Final = "AWS Bedrock"
_DEFAULT_TITLE_RE = re.compile(r"AWS Bedrock \([a-z0-9-]+\)")


def default_title(region: str) -> str:
    """Return the automatic title of an entry for ``region``."""
    return f"AWS Bedrock ({region})"


def is_default_title(title: str | None) -> bool:
    """Return True if ``title`` is an automatic title (not chosen by the user)."""
    return bool(title) and _DEFAULT_TITLE_RE.fullmatch(title) is not None


DEFAULT_PROMPT: Final = llm.DEFAULT_INSTRUCTIONS_PROMPT
DEFAULT_LLM_HASS_API: Final = [llm.LLM_API_ASSIST]
# Spoken answers are short; a low ceiling also bounds cost if the model rambles.
DEFAULT_MAX_TOKENS: Final = 1024
DEFAULT_TEMPERATURE: Final = 0.5
DEFAULT_MAX_TOOL_CALL_ITERATIONS: Final = 10
DEFAULT_PROMPT_CACHING: Final = True
DEFAULT_CONFIRM_SENSITIVE_ACTIONS: Final = True
DEFAULT_EXTRA_ATTRIBUTES: Final = [
    "brightness",
    "rgb_color",
    "temperature",
    "current_temperature",
    "target_temperature",
    "humidity",
    "fan_mode",
    "hvac_mode",
    "hvac_action",
    "preset_mode",
    "current_position",
    "volume_level",
]

# Timeouts in seconds. With streaming, the read timeout applies between chunks,
# so it bounds a stalled connection rather than the length of the answer.
BEDROCK_CONNECT_TIMEOUT: Final = 5
BEDROCK_READ_TIMEOUT: Final = 15
BEDROCK_MAX_ATTEMPTS: Final = 2
# Upper bound for one whole conversation turn, including time waiting for a
# Bedrock stream slot and all tool iterations.
TURN_TIMEOUT: Final = 60
SERVICE_CALL_TIMEOUT: Final = 10
# One credentials client shares Home Assistant's executor across all its agents.
BEDROCK_MAX_CONCURRENT_STREAMS: Final = 2
# Bound events buffered between botocore's worker thread and the event loop.
BEDROCK_STREAM_QUEUE_MAXSIZE: Final = 16
# Home Assistant creates one task per tool call in a model response.
MAX_TOOL_CALLS_PER_RESPONSE: Final = 10
# Pending confirmations expire so a stale "yes" cannot trigger an old request.
CONFIRMATION_TIMEOUT: Final = 120

# Bedrock tool names must match ^[a-zA-Z0-9_-]{1,64}$
BEDROCK_TOOL_NAME_MAX_LEN: Final = 64

# --- Custom "AWS Bedrock Services" LLM API -----------------------------------

SERVICE_TOOL_NAME: Final = "HassCallService"
STATE_TOOL_NAME: Final = "HassGetState"

# Services the custom tool may call. Anything else is rejected.
SERVICE_TOOL_ALLOWED_SERVICES: Final = frozenset(
    {
        "light.turn_on",
        "light.turn_off",
        "light.toggle",
        "switch.turn_on",
        "switch.turn_off",
        "switch.toggle",
        "fan.turn_on",
        "fan.turn_off",
        "fan.set_percentage",
        "fan.oscillate",
        "fan.set_direction",
        "fan.set_preset_mode",
        "climate.set_temperature",
        "climate.set_humidity",
        "climate.set_fan_mode",
        "climate.set_hvac_mode",
        "climate.set_preset_mode",
        "cover.open_cover",
        "cover.close_cover",
        "cover.stop_cover",
        "cover.set_cover_position",
        "media_player.turn_on",
        "media_player.turn_off",
        "media_player.toggle",
        "media_player.volume_up",
        "media_player.volume_down",
        "media_player.volume_set",
        "media_player.volume_mute",
        "media_player.media_play",
        "media_player.media_pause",
        "media_player.media_stop",
        "media_player.media_next_track",
        "media_player.media_previous_track",
        "lock.lock",
        "lock.unlock",
        "script.turn_on",
        "scene.turn_on",
        "input_boolean.turn_on",
        "input_boolean.turn_off",
        "input_boolean.toggle",
        "input_number.set_value",
        "input_select.select_option",
        "timer.start",
        "timer.pause",
        "timer.cancel",
        "timer.finish",
    }
)

# --- Sensitive actions needing spoken confirmation ----------------------------

# Cover device classes whose opening gives physical access to the home.
SENSITIVE_COVER_DEVICE_CLASSES: Final = frozenset({"door", "garage", "gate"})
# A confirmation must start with one of these phrases (matched after
# lower-casing and removing accents and punctuation). Weak fillers ("ok",
# "claro") are left out on purpose: speech recognition produces them by accident.
# fmt: off
AFFIRMATIVE_PHRASES: Final = frozenset({
    # Spanish
    "si", "vale", "confirmo", "confirmado", "adelante", "hazlo", "de acuerdo",
    "correcto", "afirmativo", "por supuesto",
    # English
    "yes", "yeah", "yep", "confirm", "confirmed", "do it", "go ahead", "affirmative",
    # French, German, Portuguese
    "oui", "ja", "sim",
})
# Words allowed after the yes phrase ("si, por favor", "yes, go ahead").
# Anything else (a correction, a condition) means the answer is not a plain yes.
AFFIRMATIVE_FOLLOW_WORDS: Final = frozenset({
    "por", "favor", "hazlo", "adelante", "vale", "claro", "gracias", "confirmo",
    "abrela", "abrelo", "abre", "abrir", "desbloquea", "desbloquealo", "desbloqueala",
    "ejecuta", "ejecutalo", "activa", "activala", "activalo",
    "please", "go", "ahead", "do", "it", "thanks", "bitte", "merci",
})
# Weak fillers that may come before the yes phrase ("okay, yes"). On their own
# they are not a yes.
AFFIRMATIVE_FILLERS: Final = frozenset({"ok", "okay"})
# Single-word yes phrases that may not be repeated after the yes phrase:
# "ja ja ja" is laughter, not a German yes.
AFFIRMATIVE_NOT_REPEATABLE: Final = frozenset({"ja"})
# fmt: on

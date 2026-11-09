"""Spoken confirmation for sensitive actions.

Sensitive means: unlocking, opening doors/garages/gates, running scripts, and
anything else that changes state through a tool this module does not
understand. The model never decides that such an action was confirmed:

1. The agent holds the call back and asks a fixed, code-built question naming
   the action and devices (the model does not write it, so injected text
   cannot disguise what is being confirmed).
2. Only a short, clear "yes" from the same user on the same device in the
   *next* turn runs exactly the held call. Anything else cancels it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
import unicodedata

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import intent, llm

from .const import (
    AFFIRMATIVE_FILLERS,
    AFFIRMATIVE_FOLLOW_WORDS,
    AFFIRMATIVE_NOT_REPEATABLE,
    AFFIRMATIVE_PHRASES,
    CONFIRMATION_TIMEOUT,
    SENSITIVE_COVER_DEVICE_CLASSES,
    SERVICE_TOOL_NAME,
)

_PUNCTUATION_RE = re.compile(r"[^\w\s]")

# Intents (HA's "Device control" API) that can open or unlock things.
# HassTurnOff unlocks a lock; HassTurnOn/HassToggle/HassSetPosition open
# covers; HassTurnOn runs scripts and activates scenes.
_OPENING_INTENTS = {"HassTurnOn", "HassToggle", "HassSetPosition"}
_UNLOCKING_INTENTS = {"HassTurnOff", "HassToggle"}
# A failed match for these reasons means the intent handler will fail too,
# so nothing can happen. Any other failure is treated as sensitive.
_HARMLESS_MATCH_FAILURES = {
    intent.MatchFailedReason.NAME,
    intent.MatchFailedReason.AREA,
    intent.MatchFailedReason.FLOOR,
    intent.MatchFailedReason.DOMAIN,
    intent.MatchFailedReason.DEVICE_CLASS,
    intent.MatchFailedReason.ASSISTANT,
    intent.MatchFailedReason.INVALID_AREA,
    intent.MatchFailedReason.INVALID_FLOOR,
}
_CALL_ACTION_TOOL = "homeassistant__call_action"
_MAX_SPOKEN_TARGETS = 5
_MAX_NAME_LEN = 60
# HA integrations whose non-intent tools only control ordinary devices or read
# data (lights, media, lists, calendars...). Their own annotations are trusted.
_TRUSTED_TOOL_INTEGRATIONS = {
    "assist_satellite",
    "calendar",
    "climate",
    "fan",
    "history",
    "humidifier",
    "lawn_mower",
    "light",
    "llm",
    "media_player",
    "search",
    "system_health",
    "system_log",
    "todo",
    "vacuum",
    "weather",
}

CONFIRMATION_REQUIRED_RESULT = {
    "status": "confirmation_required",
    "performed": False,
    "instructions": (
        "This action was NOT performed. The system has asked the user to "
        "confirm it and performs it itself only if the user says yes. Never "
        "say that it was done unless a later tool result reports success."
    ),
}

CONFIRMATION_IMPOSSIBLE_RESULT = {
    "status": "refused",
    "performed": False,
    "reason": (
        "This action needs spoken confirmation, but this request comes from "
        "neither a user nor a device that could give it. Tell the user it was "
        "not done."
    ),
}

# Fixed question text per language; anything else falls back to English.
_QUESTIONS = {
    "es": ("Necesito tu confirmación para {actions}. ¿Lo hago?", " y "),
    "en": ("I need your confirmation to {actions}. Shall I go ahead?", " and "),
}
_VERBS = {
    "es": {
        "unlock": "abrir la cerradura {}",
        "open": "abrir {}",
        "run": "ejecutar {}",
        "activate": "activar {}",
        "act": "ejecutar la acción {}",
        "timer": "ejecutar «{}» cuando acabe el temporizador",
        "more": "{} más",
        "also": " y también ",
    },
    "en": {
        "unlock": "unlock {}",
        "open": "open {}",
        "run": "run {}",
        "activate": "activate {}",
        "act": "perform the action {}",
        "timer": "run \u201c{}\u201d when the timer ends",
        "more": "{} more",
        "also": " and also ",
    },
}


@dataclass(frozen=True, slots=True)
class SensitiveAction:
    """Why a tool call needs confirmation, in words the user understands."""

    verb: str
    targets: tuple[str, ...]


@dataclass(slots=True)
class PendingConfirmation:
    """Held tool calls waiting for the requester's "yes"."""

    calls: list[llm.ToolInput]
    user_id: str | None
    device_id: str | None
    created: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        """Return True once the confirmation window has passed."""
        return time.monotonic() - self.created > CONFIRMATION_TIMEOUT

    def same_requester(self, llm_context: llm.LLMContext) -> bool:
        """Return True if the answer comes from who was asked."""
        user_id = llm_context.context.user_id if llm_context.context else None
        return (user_id, llm_context.device_id) == (self.user_id, self.device_id)


def normalize(text: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(_PUNCTUATION_RE.sub(" ", text).split())


def is_affirmative(text: str) -> bool:
    """Return True only for a clear "yes" ("sí", "sí, ábrela", "yes go ahead").

    The answer must be a yes phrase, optionally after a weak filler ("okay,
    yes") and optionally followed only by words from a short list ("por
    favor", "hazlo", "ábrela"...) or by more yes words ("sí, sí"). Questions
    ("¿sí?"), conditionals ("si llueve, ...") and corrections ("yes, the back
    door instead") are not a yes.
    """
    if "?" in text or "¿" in text:
        return False
    words = normalize(text).split()
    while words and words[0] in AFFIRMATIVE_FILLERS:
        words = words[1:]
    for size in (2, 1):
        if " ".join(words[:size]) in AFFIRMATIVE_PHRASES:
            return all(_is_follow_word(word) for word in words[size:])
    return False


def _is_follow_word(word: str) -> bool:
    """Return True if the word may follow the yes phrase without changing it."""
    if word in AFFIRMATIVE_FOLLOW_WORDS:
        return True
    return word in AFFIRMATIVE_PHRASES and word not in AFFIRMATIVE_NOT_REPEATABLE


def confirmation_question(language: str | None, actions: list[SensitiveAction]) -> str:
    """Build the question the agent speaks, naming every held action."""
    lang = (language or "en").split("-")[0].lower()
    template, conjunction = _QUESTIONS.get(lang, _QUESTIONS["en"])
    verbs = _VERBS.get(lang, _VERBS["en"])
    parts = [
        verbs.get(action.verb, verbs["act"]).format(
            _join(tuple(spoken_name(t) for t in action.targets), conjunction, verbs["more"])
        )
        for action in actions
    ]
    return template.format(actions=_join(tuple(parts), verbs["also"]))


def _join(items: tuple[str, ...], conjunction: str, more: str | None = None) -> str:
    if more is not None and len(items) > _MAX_SPOKEN_TARGETS:
        hidden = len(items) - _MAX_SPOKEN_TARGETS
        items = (*items[:_MAX_SPOKEN_TARGETS], more.format(hidden))
    if len(items) <= 1:
        return "".join(items)
    last = items[-1]
    # Spanish "y" becomes "e" before an i- sound ("Garaje e Invernadero").
    if conjunction == " y " and normalize(last).startswith(("i", "hi")):
        conjunction = " e "
    return ", ".join(items[:-1]) + conjunction + last


def spoken_name(value: object) -> str:
    """Return a name safe to speak: one line, at most 60 characters."""
    return " ".join(str(value).split())[:_MAX_NAME_LEN]


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return spoken_name(state.name if state else entity_id)


def _sensitive_state_verb(
    hass: HomeAssistant, state: State, *, opening: bool, unlocking: bool
) -> str | None:
    if state.domain == "lock":
        return "unlock" if unlocking else None
    if state.domain == "cover":
        sensitive = state.attributes.get("device_class") in SENSITIVE_COVER_DEVICE_CLASSES
        return "open" if opening and sensitive else None
    if state.domain == "script":
        return "run" if opening else None
    if state.domain == "scene" and opening and _scene_is_sensitive(hass, state):
        return "activate"
    return None


def _scene_is_sensitive(hass: HomeAssistant, scene: State) -> bool:
    """Return False only for a scene with known, non-sensitive members."""
    entity_ids = scene.attributes.get("entity_id")
    if not isinstance(entity_ids, list):
        return True
    for entity_id in entity_ids:
        if not isinstance(entity_id, str) or (member := hass.states.get(entity_id)) is None:
            return True
        if member.domain == "lock":
            return True
        if (
            member.domain == "cover"
            and member.attributes.get("device_class") in SENSITIVE_COVER_DEVICE_CLASSES
        ):
            return True
    return False


def service_call_action(
    hass: HomeAssistant, service: object, target: object
) -> SensitiveAction | None:
    """Assess an explicit service call on one or more entities."""
    if not isinstance(service, str):
        return None
    if isinstance(target, str):
        targets = [target]
    elif isinstance(target, list) and all(isinstance(item, str) for item in target):
        targets = list(dict.fromkeys(target))
    else:
        return None
    names = tuple(_name(hass, entity_id) for entity_id in targets)
    if service in ("lock.unlock", "lock.open"):
        return SensitiveAction("unlock", names)
    if service == "script.turn_on":
        return SensitiveAction("run", names)
    states = [hass.states.get(entity_id) for entity_id in targets]
    if service in ("cover.open_cover", "cover.set_cover_position") and any(
        state is None or state.attributes.get("device_class") in SENSITIVE_COVER_DEVICE_CLASSES
        for state in states
    ):
        return SensitiveAction("open", names)
    if service == "scene.turn_on" and any(
        state is None or _scene_is_sensitive(hass, state) for state in states
    ):
        return SensitiveAction("activate", names)
    return None


def _intent_action(hass: HomeAssistant, intent_name: str, args: dict) -> SensitiveAction | None:
    # HA's IntentTool drops blank slots before handling, so a blank name
    # does not narrow the match. Look at the slots the handler will see.
    args = {k: v for k, v in args.items() if not intent.is_blank_slot_value(v)}
    if intent_name == "HassStartTimer" and args.get("conversation_command"):
        # The command runs later through HA's own agent, which never asks.
        return SensitiveAction("timer", (spoken_name(args["conversation_command"]),))
    opening = intent_name in _OPENING_INTENTS
    unlocking = intent_name in _UNLOCKING_INTENTS
    if not (opening or unlocking):
        return None

    def as_list(value: object) -> list[str] | None:
        if value is None:
            return None
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    name = args.get("name")
    constraints = intent.MatchTargetsConstraints(
        # The handler treats "all" as "no name filter"; so must we.
        name=None if name == "all" else name,
        area_name=args.get("area"),
        floor_name=args.get("floor"),
        domains=as_list(args.get("domain")),
        device_classes=as_list(args.get("device_class")),
        assistant="conversation",
        # Superset of what the handler can reach, whatever area the
        # requesting satellite is in.
        allow_duplicate_names=True,
    )
    if not constraints.has_constraints:
        return None  # the handler refuses to target everything
    asked_for = [
        spoken_name(value)
        for key in ("name", "area", "floor")
        if isinstance(value := args.get(key), str)
    ]
    if not asked_for:
        asked_for = [intent_name]
    try:
        result = intent.async_match_targets(hass, constraints)
    except Exception:  # if we cannot tell, ask
        return SensitiveAction("act", tuple(asked_for))
    if not result.is_match:
        if result.no_match_reason in _HARMLESS_MATCH_FAILURES:
            return None
        return SensitiveAction("act", tuple(asked_for))

    by_verb: dict[str, list[str]] = {}
    for state in result.states:
        if verb := _sensitive_state_verb(hass, state, opening=opening, unlocking=unlocking):
            by_verb.setdefault(verb, []).append(state.name)
    if not by_verb:
        return None
    # One action per call; if a call mixes kinds, the strongest wording wins.
    verb = next(v for v in ("unlock", "open", "run", "activate") if v in by_verb)
    return SensitiveAction(verb, tuple(name for names in by_verb.values() for name in names))


def assess(
    hass: HomeAssistant, tool_input: llm.ToolInput, tool: llm.Tool | None
) -> SensitiveAction | None:
    """Return why this tool call needs confirmation, or None if it does not."""
    name = tool_input.tool_name
    base_name = name.rsplit("__", 1)[-1]
    args = tool_input.tool_args or {}

    if base_name == SERVICE_TOOL_NAME:
        target = args.get("target_devices", args.get("target_device"))
        return service_call_action(hass, args.get("service"), target)
    if name.endswith(_CALL_ACTION_TOOL):
        # Can call any action on any target: always ask, naming both.
        return SensitiveAction("act", (_describe_call_action(hass, args),))
    if base_name == "play_media" and _is_external_url(args.get("media_content_id")):
        # A speaker fetching an arbitrary URL can leak data or play anything.
        return SensitiveAction("act", (spoken_name(args.get("media_content_id")),))
    if isinstance(tool, llm.IntentTool):
        if tool.integration == "intent_script":
            return SensitiveAction("run", (tool.title or tool.intent_type,))
        return _intent_action(hass, tool.intent_type, args)
    if name.startswith("script__") or "__script__" in name:
        object_id = name.rsplit("script__", 1)[-1]
        return SensitiveAction("run", (_name(hass, f"script.{object_id}"),))
    if tool is None:
        # Not offered this turn (cannot normally happen): judge by name.
        if base_name.startswith("Hass"):
            return _intent_action(hass, base_name, args)
        return SensitiveAction("act", (base_name,))
    if tool.integration in _TRUSTED_TOOL_INTEGRATIONS or (
        tool.annotations.read_only and _is_core_tool(tool)
    ):
        return None
    # A tool that may change things in a way this module does not understand
    # (other integrations, MCP servers, admin tools): ask rather than guess.
    return SensitiveAction("act", (tool.title or base_name,))


def _is_core_tool(tool: llm.Tool) -> bool:
    """Return True for tools defined by HA itself or by this integration.

    Annotations of other tools (e.g. from MCP servers) are third-party hints
    and are not trusted to mark a tool as read-only.
    """
    module = type(tool).__module__
    return tool.integration != "mcp" and module.startswith(("homeassistant.", f"{__package__}."))


def _is_external_url(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(
        ("http://", "https://", "data:", "ftp://", "rtsp://")
    )


def _describe_call_action(hass: HomeAssistant, args: dict) -> str:
    """Describe a homeassistant__call_action request: action and targets."""
    action = spoken_name(args.get("action", "?"))
    target = args.get("target") if isinstance(args.get("target"), dict) else {}
    entity_ids = target.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    targets = [_name(hass, str(e)) for e in entity_ids[:_MAX_SPOKEN_TARGETS]]
    for key in ("area_id", "floor_id", "label_id", "device_id"):
        if target.get(key):
            targets.append(spoken_name(target[key]))
    return f"{action} ({', '.join(targets)})" if targets else action

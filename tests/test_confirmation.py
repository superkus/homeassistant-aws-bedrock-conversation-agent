"""Tests for sensitive-action detection, the "yes" check and the question."""

from __future__ import annotations

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, llm
from homeassistant.setup import async_setup_component
import pytest

from custom_components.bedrock_conversation.confirmation import (
    SensitiveAction,
    assess,
    confirmation_question,
    is_affirmative,
)
from custom_components.bedrock_conversation.llm_api import HassGetStateTool, HassServiceTool


@pytest.mark.parametrize(
    "text",
    [
        "Sí",
        "si",
        "¡Sí, por favor!",
        "vale",
        "Yes",
        "go ahead",
        "Adelante.",
        "Yes, go ahead.",
        "sí, ábrela",
        "de acuerdo",
        # A repeated yes, or a yes after a filler, is still a clear yes (COR-1).
        "Sí, sí",
        "yes yes",
        "Vale, sí",
        "Okay, yes",
    ],
)
def test_affirmative(text: str) -> None:
    """Clear yes answers, alone or followed by words without a negation."""
    assert is_affirmative(text)


@pytest.mark.parametrize(
    "text",
    [
        "no",
        "sí pero no",
        "no, déjalo",
        "¿Sí?",
        "Yes?",
        "ok",
        "claro",
        "",
        "sí, espera",
        "vale, no lo abras",
        "abre la puerta",
        "Si llueve, abre la persiana",  # "si" meaning "if"
        "vale, si abre la puerta",  # a repeated "si" in the tail can still mean "if"
        "okay",  # a filler alone is not a yes
        "ja ja ja",
        "yes the back door instead",
        "sí, pero la trasera",  # a correction after the yes
    ],
)
def test_not_affirmative(text: str) -> None:
    """Questions, negations, fillers and other requests are not a yes."""
    assert not is_affirmative(text)


def test_question_is_built_in_code() -> None:
    """The question names every action and ends with '?' (keeps listening)."""
    actions = [
        SensitiveAction("unlock", ("Puerta",)),
        SensitiveAction("open", ("Garaje", "Cancela")),
    ]
    assert confirmation_question("es", actions) == (
        "Necesito tu confirmación para abrir la cerradura Puerta y también abrir Garaje y Cancela. ¿Lo hago?"
    )
    many = [SensitiveAction("open", tuple(f"Puerta {i}" for i in range(8)))]
    assert confirmation_question("es", many).endswith("Puerta 4 y 3 más. ¿Lo hago?")
    odd = [SensitiveAction("open", ("Garaje", "Invernadero\n<b>raro</b>" + "x" * 100))]
    spoken = confirmation_question("es", odd)
    assert "Garaje e Invernadero <b>raro</b>" in spoken  # names are cleaned
    assert "\n" not in spoken
    assert "x" * 61 not in spoken  # and truncated
    assert confirmation_question("en-GB", actions[:1]) == (
        "I need your confirmation to unlock Puerta. Shall I go ahead?"
    )
    assert confirmation_question("de", actions[:1]).startswith("I need your confirmation")


@pytest.fixture
async def tools(hass: HomeAssistant) -> dict[str, llm.Tool]:
    """Exposed devices and HA's real Device control tools."""
    for component in ("homeassistant", "intent", "llm", "conversation"):
        assert await async_setup_component(hass, component, {})
    hall = ar.async_get(hass).async_create("Hall")
    bedroom = ar.async_get(hass).async_create("Dormitorio")
    entity_registry = er.async_get(hass)
    for entity_id, attrs, area in [
        ("cover.garage", {"friendly_name": "Garaje", "device_class": "garage"}, None),
        ("cover.persiana", {"friendly_name": "Persiana", "device_class": "shutter"}, None),
        ("lock.puerta", {"friendly_name": "Puerta"}, hall),
        ("switch.puerta", {"friendly_name": "Puerta"}, bedroom),
        ("light.salon", {"friendly_name": "Salón"}, None),
        (
            "scene.llegar",
            {"friendly_name": "Llegar a casa", "entity_id": ["lock.puerta", "light.salon"]},
            None,
        ),
        ("scene.cine", {"friendly_name": "Cine", "entity_id": ["light.salon"]}, None),
    ]:
        domain, object_id = entity_id.split(".")
        # Intent matching uses registry names (HA 2026.11), like real devices.
        entry = entity_registry.async_get_or_create(
            domain,
            "test",
            object_id,
            suggested_object_id=object_id,
            original_name=attrs["friendly_name"],
        )
        entity_registry.async_update_entity(
            entry.entity_id, aliases=[er.COMPUTED_NAME], area_id=area.id if area else None
        )
        hass.states.async_set(entity_id, "closed", attrs)
        async_expose_entity(hass, "conversation", entity_id, True)
    context = llm.LLMContext(
        platform="test", context=Context(), language="es", assistant="conversation", device_id=None
    )
    instance = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)
    found = {tool.name: tool for tool in instance.tools}
    found["HassCallService"] = HassServiceTool()
    found["HassGetState"] = HassGetStateTool()
    return found


def _call(tool_name: str, /, **args: object) -> llm.ToolInput:
    return llm.ToolInput(tool_name=tool_name, tool_args=args)


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            _call("HassCallService", service="lock.unlock", target_device="lock.puerta"),
            ("unlock", ("Puerta",)),
        ),
        (_call("HassCallService", service="lock.lock", target_device="lock.puerta"), None),
        (
            _call("HassCallService", service="cover.open_cover", target_device="cover.garage"),
            ("open", ("Garaje",)),
        ),
        (
            _call("HassCallService", service="cover.open_cover", target_device="cover.persiana"),
            None,
        ),
        (
            _call("HassCallService", service="script.turn_on", target_device="script.x"),
            ("run", ("script.x",)),
        ),
        (
            _call("HassCallService", service="scene.turn_on", target_device="scene.llegar"),
            ("activate", ("Llegar a casa",)),
        ),
        (_call("HassCallService", service="scene.turn_on", target_device="scene.cine"), None),
        (_call("HassCallService", service="light.turn_on", target_device="light.salon"), None),
        (_call("HassGetState", entity_id="lock.puerta"), None),
        (_call("intent__HassTurnOff", name="Puerta", domain=["lock"]), ("unlock", ("Puerta",))),
        (_call("intent__HassTurnOn", name="Puerta", domain=["lock"]), None),  # lock
        (_call("intent__HassTurnOn", name="Garaje"), ("open", ("Garaje",))),
        (_call("intent__HassTurnOff", name="Garaje"), None),  # close
        (_call("intent__HassTurnOn", name="Persiana"), None),
        (_call("intent__HassTurnOn", name="Salón"), None),
        (_call("intent__HassTurnOn", name="Llegar a casa"), ("activate", ("Llegar a casa",))),
        # Found by review: "all" means "no name filter" for the intent handler.
        (_call("intent__HassTurnOff", name="all", domain=["lock"]), ("unlock", ("Puerta",))),
        (_call("intent__HassTurnOn", name="all", domain="cover"), ("open", ("Garaje",))),
        # Found by review: a duplicate name resolves by the satellite's area.
        (_call("intent__HassTurnOff", name="Puerta"), ("unlock", ("Puerta",))),
        (_call("intent__HassTurnOff", name="Nada"), None),  # matches nothing: handler fails too
        (_call("homeassistant__call_action", action="lock.unlock"), ("act", ("lock.unlock",))),
        (
            _call(
                "homeassistant__call_action",
                action="script.turn_on",
                target={"entity_id": "lock.puerta"},
            ),
            ("act", ("script.turn_on (Puerta)",)),
        ),
        # Found by review: HA drops blank slots, so a blank name widens the match.
        (_call("intent__HassTurnOff", name="  ", area="Hall"), ("unlock", ("Puerta",))),
        (
            _call("intent__HassTurnOff", name="Puerta", area=" ", domain="lock"),
            ("unlock", ("Puerta",)),
        ),
        # Found by review: a timer command runs later through HA's own agent.
        (
            _call("intent__HassStartTimer", seconds=1, conversation_command="abre la puerta"),
            ("timer", ("abre la puerta",)),
        ),
        (_call("intent__HassStartTimer", minutes=5), None),
        (_call("script__abrir_cancela"), ("run", ("script.abrir_cancela",))),
        (_call("llm__GetDateTime"), None),
    ],
)
async def test_assess(
    hass: HomeAssistant, tools: dict[str, llm.Tool], call: llm.ToolInput, expected: tuple | None
) -> None:
    """Unlocking, opening doors/garages/gates, scripts and risky scenes are held."""
    action = assess(hass, call, tools.get(call.tool_name))
    assert (None if action is None else (action.verb, action.targets)) == expected


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param(None, id="absent"),
        pytest.param("lock.puerta", id="malformed-string"),
        pytest.param(["light.missing"], id="unresolved-member"),
    ],
)
@pytest.mark.parametrize("tool_path", ["HassCallService", "HassTurnOn"])
async def test_scene_without_valid_membership_metadata_requires_confirmation(
    hass: HomeAssistant,
    tools: dict[str, llm.Tool],
    metadata: object | None,
    tool_path: str,
) -> None:
    """Scenes with missing or malformed membership metadata fail closed."""
    attributes: dict[str, object] = {"friendly_name": "Llegar a casa"}
    if metadata is not None:
        attributes["entity_id"] = metadata
    hass.states.async_set("scene.llegar", "scening", attributes)
    if tool_path == "HassCallService":
        call = _call("HassCallService", service="scene.turn_on", target_device="scene.llegar")
    else:
        call = _call("intent__HassTurnOn", name="Llegar a casa")

    assert assess(hass, call, tools.get(call.tool_name)) == SensitiveAction(
        "activate", ("Llegar a casa",)
    )


async def test_unknown_tools_fail_closed(hass: HomeAssistant, tools: dict[str, llm.Tool]) -> None:
    """Tools from other integrations that may change things are held."""

    class McpTool(llm.Tool):
        name = "mcp__open_gate"
        title = "Open gate"
        integration = "mcp"

        async def async_call(self, *args):  # pragma: no cover
            return llm.ToolResult(data={})

    class ReadOnlyTool(McpTool):
        name = "mcp__read"
        annotations = llm.ToolAnnotations(read_only=True)

    assert assess(hass, _call("mcp__open_gate"), McpTool()) == SensitiveAction(
        "act", ("Open gate",)
    )
    # MCP annotations come from the remote server, so "read-only" is not trusted.
    assert assess(hass, _call("mcp__read"), ReadOnlyTool()) is not None
    # Domain tools from HA's own device integrations are trusted.
    light_tools = [t for n, t in tools.items() if n.startswith("light__")]
    for tool in light_tools:
        assert assess(hass, _call(tool.name, name="Salón", brightness=50), tool) is None


async def test_play_media_urls_are_held(hass: HomeAssistant) -> None:
    """A speaker fetching an arbitrary URL is held; a library search is not."""

    class PlayMedia(llm.Tool):
        name = "media_player__play_media"
        integration = "media_player"

        async def async_call(self, *args):  # pragma: no cover
            return llm.ToolResult(data={})

    url = "https://evil.example/x.mp3?d=1"
    held = assess(hass, _call(PlayMedia.name, media_content_id=url), PlayMedia())
    assert held == SensitiveAction("act", (url,))
    assert assess(hass, _call(PlayMedia.name, search_query="jazz"), PlayMedia()) is None

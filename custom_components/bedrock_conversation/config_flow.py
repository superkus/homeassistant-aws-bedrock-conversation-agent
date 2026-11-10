"""Config flow for AWS Bedrock Conversation.

Setup asks for the region and credentials first, validates access to Bedrock's
model-listing metadata, then presents model and inference-profile metadata for
that region. Listing does not verify model access or runtime invocation
permission.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, override

from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio

from .client import (
    BedrockAccessDeniedError,
    BedrockAuthError,
    BedrockClient,
    BedrockConnectionError,
    BedrockError,
    ModelOption,
    get_bedrock_regions,
)
from .const import (
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_REGION,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_SESSION_TOKEN,
    CONF_CONFIRM_SENSITIVE_ACTIONS,
    CONF_COST_SENSOR_ENABLED,
    CONF_LLM_HASS_API,
    CONF_MAX_TOKENS,
    CONF_MAX_TOOL_CALL_ITERATIONS,
    CONF_MODEL_ID,
    CONF_PROMPT,
    CONF_PROMPT_CACHING,
    CONF_TEMPERATURE,
    DEFAULT_AGENT_NAME,
    DEFAULT_AWS_REGION,
    DEFAULT_CONFIRM_SENSITIVE_ACTIONS,
    DEFAULT_COST_SENSOR_ENABLED,
    DEFAULT_LLM_HASS_API,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOOL_CALL_ITERATIONS,
    DEFAULT_PROMPT,
    DEFAULT_PROMPT_CACHING,
    DEFAULT_TEMPERATURE,
    DOMAIN,
    EXPOSURE_BOUNDED_LLM_API_IDS,
    HOME_LLM_API_ID,
    LOGGER,
    SUBENTRY_CONVERSATION,
    default_title,
    is_default_title,
)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _credentials_schema(defaults: Mapping[str, Any]) -> dict[Any, Any]:
    return {
        probatio.Required(
            CONF_AWS_ACCESS_KEY_ID, default=defaults.get(CONF_AWS_ACCESS_KEY_ID, "")
        ): TextSelector(),
        # Secret keeps the values out of validation error messages.
        probatio.Required(probatio.Secret(CONF_AWS_SECRET_ACCESS_KEY)): _PASSWORD,
        probatio.Optional(probatio.Secret(CONF_AWS_SESSION_TOKEN)): _PASSWORD,
    }


async def _async_validate(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """Return an error key, or None if the credentials work in the region."""
    client = BedrockClient(hass, data)
    try:
        await client.async_validate()
    except BedrockAuthError:
        return "invalid_auth"
    except BedrockAccessDeniedError:
        return "access_denied"
    except BedrockConnectionError:
        return "cannot_connect"
    except BedrockError:
        LOGGER.exception("Unexpected error validating AWS credentials")
        return "unknown"
    return None


async def _async_models(hass: HomeAssistant, data: Mapping[str, Any]) -> list[ModelOption]:
    try:
        return await BedrockClient(hass, data).async_list_models()
    except BedrockError as err:
        LOGGER.warning("Could not list Bedrock models: %s", err)
        return []


def _model_selector(models: list[ModelOption], current: str | None) -> SelectSelector:
    options = [SelectOptionDict(value=m.id, label=m.label) for m in models]
    if current and current not in {m.id for m in models}:
        options.insert(0, SelectOptionDict(value=current, label=current))
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            mode=SelectSelectorMode.DROPDOWN,
            # Allows inference profile ARNs and models missing from the list.
            custom_value=True,
            sort=False,
        )
    )


def _api_selector(hass: HomeAssistant) -> SelectSelector:
    """Select exposure-bounded APIs only.

    HA's admin "Home Assistant" API and arbitrary third-party APIs may call
    services without respecting Settings -> Voice assistants -> Expose. The
    two APIs below both enforce that per-entity exposure list.
    """
    options = {
        api.id: api.name
        for api in llm.async_get_apis(hass)
        if api.id in EXPOSURE_BOUNDED_LLM_API_IDS
    }
    # The integration registers this API when the entry first loads, which is
    # after the first-install selector is shown.
    options.setdefault(HOME_LLM_API_ID, "AWS Bedrock Services")
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=api_id, label=name) for api_id, name in options.items()
            ],
            multiple=True,
        )
    )


def _clean_options(options: dict[str, Any]) -> dict[str, Any]:
    """Drop an empty API selection (meaning: no device control)."""
    if not options.get(CONF_LLM_HASS_API):
        options.pop(CONF_LLM_HASS_API, None)
    return options


class BedrockConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 3
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._data: dict[str, Any] = {}
        self._models: list[ModelOption] = []

    def _abort_if_duplicate(self, user_input: Mapping[str, Any], step_id: str) -> None:
        """Abort if another entry uses the same key in the same region."""
        current = self._get_reconfigure_entry().entry_id if step_id == "reconfigure" else None
        for entry in self._async_current_entries(include_ignore=False):
            if (
                entry.entry_id != current
                and entry.data.get(CONF_AWS_ACCESS_KEY_ID) == user_input[CONF_AWS_ACCESS_KEY_ID]
                and entry.data.get(CONF_AWS_REGION) == user_input[CONF_AWS_REGION]
            ):
                raise AbortFlow("already_configured")

    async def _async_regions(self) -> dict[str, str]:
        regions = await self.hass.async_add_executor_job(get_bedrock_regions)
        return regions or {DEFAULT_AWS_REGION: DEFAULT_AWS_REGION}

    async def _async_region_and_credentials(
        self, step_id: str, user_input: dict[str, Any] | None, defaults: Mapping[str, Any]
    ) -> ConfigFlowResult | None:
        """Show/validate the region + credentials form; None when valid."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._abort_if_duplicate(user_input, step_id)
            if not (error := await _async_validate(self.hass, user_input)):
                self._data = {CONF_AWS_SESSION_TOKEN: "", **user_input}
                self._models = await _async_models(self.hass, self._data)
                return None
            errors["base"] = error

        regions = await self._async_regions()
        schema = {
            probatio.Required(
                CONF_AWS_REGION, default=defaults.get(CONF_AWS_REGION, DEFAULT_AWS_REGION)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=k, label=v) for k, v in regions.items()],
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=False,
                )
            ),
            **_credentials_schema(defaults),
        }
        if step_id == "reconfigure":
            cost_sensor_default = defaults.get(
                CONF_COST_SENSOR_ENABLED, DEFAULT_COST_SENSOR_ENABLED
            )
            if user_input is not None and CONF_COST_SENSOR_ENABLED in user_input:
                cost_sensor_default = user_input[CONF_COST_SENSOR_ENABLED]
            schema[
                probatio.Optional(
                    CONF_COST_SENSOR_ENABLED,
                    default=cost_sensor_default,
                )
            ] = BooleanSelector()
        return self.async_show_form(
            step_id=step_id, data_schema=probatio.Schema(schema), errors=errors
        )

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: region and credentials."""
        if result := await self._async_region_and_credentials("user", user_input, {}):
            return result
        return await self.async_step_model()

    async def async_step_model(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: pick from model metadata returned for the region."""
        if user_input is not None:
            # Credentials and region in the entry; the first agent as a
            # sub-entry (more can be added later with "Add agent").
            return self.async_create_entry(
                title=default_title(self._data[CONF_AWS_REGION]),
                data=self._data,
                subentries=[
                    {
                        "subentry_type": SUBENTRY_CONVERSATION,
                        "data": _clean_options({**user_input, CONF_PROMPT: DEFAULT_PROMPT}),
                        "title": DEFAULT_AGENT_NAME,
                        "unique_id": None,
                    }
                ],
            )
        schema = {
            probatio.Required(CONF_MODEL_ID): _model_selector(self._models, None),
            probatio.Optional(CONF_LLM_HASS_API, default=DEFAULT_LLM_HASS_API): _api_selector(
                self.hass
            ),
        }
        return self.async_show_form(
            step_id="model",
            data_schema=probatio.Schema(schema),
            errors={} if self._models else {"base": "no_models"},
            description_placeholders={
                "region": self._data[CONF_AWS_REGION],
                "count": str(len(self._models)),
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials were rejected; ask for new ones."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials for the same region."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_AWS_SESSION_TOKEN: "", **user_input}
            if not (error := await _async_validate(self.hass, data)):
                return self._async_update_and_finish(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(_credentials_schema(entry.data)),
            errors=errors,
            description_placeholders={"region": entry.data[CONF_AWS_REGION]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change region or credentials; select a model only after a region change."""
        entry = self._get_reconfigure_entry()
        if result := await self._async_region_and_credentials(
            "reconfigure", user_input, entry.data
        ):
            return result
        if self._data[CONF_AWS_REGION] == entry.data.get(CONF_AWS_REGION):
            # Same region (e.g. rotated keys): every agent keeps its model.
            return self._async_update_and_finish(entry, data=self._data)
        return await self.async_step_reconfigure_model()

    async def async_step_reconfigure_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the replacement model after the region changes."""
        entry = self._get_reconfigure_entry()
        agents = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CONVERSATION]
        if user_input is not None:
            # The model is set for every agent: their old models may not exist
            # in the new region. Each agent can be changed afterwards.
            for agent in agents:
                self.hass.config_entries.async_update_subentry(
                    entry, agent, data={**agent.data, CONF_MODEL_ID: user_input[CONF_MODEL_ID]}
                )
            return self._async_update_and_finish(
                entry,
                # Keep a title the user chose.
                title=default_title(self._data[CONF_AWS_REGION])
                if is_default_title(entry.title)
                else entry.title,
                data=self._data,
            )
        current = agents[0].data.get(CONF_MODEL_ID) if agents else None
        # Offer the current model even if unlisted (e.g. a typed profile ARN)
        # when the region did not change.
        same_region = self._data[CONF_AWS_REGION] == entry.data.get(CONF_AWS_REGION)
        known = current in {m.id for m in self._models} or (bool(current) and same_region)
        return self.async_show_form(
            step_id="reconfigure_model",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_MODEL_ID, default=current if known else probatio.UNDEFINED
                    ): _model_selector(self._models, current if known else None),
                }
            ),
            errors={} if self._models else {"base": "no_models"},
            description_placeholders={
                "region": self._data[CONF_AWS_REGION],
                "count": str(len(self._models)),
            },
        )

    @callback
    def _async_update_and_finish(self, entry: ConfigEntry, **changes: Any) -> ConfigFlowResult:
        """Save the changes and reload the entry once.

        A loaded entry has an update listener that reloads it (HA warns when a
        flow schedules its own reload then). An entry that failed to set up
        has none, so the flow reloads it to retry with the new settings.
        """
        if entry.update_listeners:
            return self.async_update_and_abort(entry, **changes)
        return self.async_update_reload_and_abort(entry, **changes)

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Agents are sub-entries: several can share one set of credentials."""
        return {SUBENTRY_CONVERSATION: AgentSubentryFlow}


class AgentSubentryFlow(ConfigSubentryFlow):
    """Add or change one agent: model, instructions, device control, limits."""

    _options: dict[str, Any]

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add an agent."""
        self._options = {CONF_PROMPT: DEFAULT_PROMPT, CONF_LLM_HASS_API: DEFAULT_LLM_HASS_API}
        return await self.async_step_set_options()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change an agent."""
        self._options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_set_options()

    async def async_step_set_options(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show and save the agent settings."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        is_new = self.source == SOURCE_USER
        options = self._options

        if user_input is not None:
            title = user_input.pop(CONF_NAME, None)
            # Merge so settings that are not on the form are kept. Cleared
            # optional fields are left out by the frontend: drop them too.
            merged = {**options, **user_input}
            for key in (CONF_PROMPT, CONF_LLM_HASS_API):
                if not user_input.get(key):
                    merged.pop(key, None)
            for key in (CONF_MAX_TOKENS, CONF_MAX_TOOL_CALL_ITERATIONS):
                merged[key] = int(merged[key])
            if is_new:
                return self.async_create_entry(title=title or DEFAULT_AGENT_NAME, data=merged)
            return self.async_update_and_abort(entry, self._get_reconfigure_subentry(), data=merged)

        models = await _async_models(self.hass, entry.data)
        schema: dict[Any, Any] = {}
        if is_new:
            schema[probatio.Required(CONF_NAME, default=DEFAULT_AGENT_NAME)] = TextSelector()
        schema.update(_agent_schema(self.hass, options, models))
        return self.async_show_form(
            step_id="set_options",
            data_schema=probatio.Schema(schema),
            errors={} if models else {"base": "no_models"},
            description_placeholders={
                "region": entry.data[CONF_AWS_REGION],
                "count": str(len(models)),
            },
        )


def _agent_schema(
    hass: HomeAssistant, options: Mapping[str, Any], models: list[ModelOption]
) -> dict[Any, Any]:
    """Form fields for one agent's settings."""
    return {
        probatio.Required(
            CONF_MODEL_ID, default=options.get(CONF_MODEL_ID, probatio.UNDEFINED)
        ): _model_selector(models, options.get(CONF_MODEL_ID)),
        probatio.Optional(
            CONF_PROMPT,
            description={"suggested_value": options.get(CONF_PROMPT, DEFAULT_PROMPT)},
        ): TemplateSelector(),
        probatio.Optional(
            CONF_LLM_HASS_API,
            description={"suggested_value": options.get(CONF_LLM_HASS_API, [])},
        ): _api_selector(hass),
        probatio.Required(
            CONF_CONFIRM_SENSITIVE_ACTIONS,
            default=options.get(CONF_CONFIRM_SENSITIVE_ACTIONS, DEFAULT_CONFIRM_SENSITIVE_ACTIONS),
        ): BooleanSelector(),
        probatio.Required(
            CONF_MAX_TOKENS, default=options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)
        ): NumberSelector(
            NumberSelectorConfig(min=64, max=64000, step=1, mode=NumberSelectorMode.BOX)
        ),
        probatio.Required(
            CONF_TEMPERATURE, default=options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE)
        ): NumberSelector(NumberSelectorConfig(min=0, max=1, step=0.05)),
        probatio.Required(
            CONF_MAX_TOOL_CALL_ITERATIONS,
            default=max(
                1,
                int(options.get(CONF_MAX_TOOL_CALL_ITERATIONS, DEFAULT_MAX_TOOL_CALL_ITERATIONS)),
            ),
        ): NumberSelector(NumberSelectorConfig(min=1, max=20, step=1, mode=NumberSelectorMode.BOX)),
        probatio.Required(
            CONF_PROMPT_CACHING, default=options.get(CONF_PROMPT_CACHING, DEFAULT_PROMPT_CACHING)
        ): BooleanSelector(),
    }

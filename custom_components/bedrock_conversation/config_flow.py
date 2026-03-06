"""Config flow for AWS Bedrock Conversation integration."""
from __future__ import annotations

import logging
from typing import Any

import boto3
import voluptuous as vol
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    BotoCoreError,
)

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm, selector

from .const import (
    AVAILABLE_MODELS,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_REGION,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_SESSION_TOKEN,
    CONF_EXTRA_ATTRIBUTES_TO_EXPOSE,
    CONF_LLM_HASS_API,
    CONF_MAX_TOKENS,
    CONF_MAX_TOOL_CALL_ITERATIONS,
    CONF_MODEL_ID,
    CONF_PROMPT,
    CONF_REFRESH_SYSTEM_PROMPT,
    CONF_REMEMBER_CONVERSATION,
    CONF_REMEMBER_NUM_INTERACTIONS,
    CONF_TEMPERATURE,
    CONF_TOP_K,
    CONF_TOP_P,
    DEFAULT_AWS_REGION,
    DEFAULT_EXTRA_ATTRIBUTES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOOL_CALL_ITERATIONS,
    DEFAULT_MODEL_ID,
    DEFAULT_PROMPT,
    DEFAULT_REFRESH_SYSTEM_PROMPT,
    DEFAULT_REMEMBER_CONVERSATION,
    DEFAULT_REMEMBER_NUM_INTERACTIONS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
    DOMAIN,
    HOME_LLM_API_ID,
)

_LOGGER = logging.getLogger(__name__)


async def get_available_models_for_region(
    hass: HomeAssistant,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_session_token: str | None,
    aws_region: str,
) -> list[str]:
    """Get available foundation models for the specified region."""
    try:
        def _get_models():
            import boto3
            from botocore.exceptions import ClientError
            
            # Create the boto3 session
            session = boto3.Session(
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                aws_session_token=aws_session_token or None,
                region_name=aws_region,
            )
            
            bedrock_client = session.client('bedrock')
            
            try:
                # List foundation models
                response = bedrock_client.list_foundation_models()
                models = []
                
                for model in response.get('modelSummaries', []):
                    model_id = model.get('modelId')
                    model_name = model.get('modelName', '')
                    
                    # Filter for models that support text generation and tool use
                    input_modalities = model.get('inputModalities', [])
                    output_modalities = model.get('outputModalities', [])
                    
                    if ('TEXT' in input_modalities and 'TEXT' in output_modalities):
                        models.append(model_id)
                
                # Also try to get inference profiles
                try:
                    profiles_response = bedrock_client.list_inference_profiles()
                    for profile in profiles_response.get('inferenceProfileSummaries', []):
                        profile_id = profile.get('inferenceProfileId')
                        if profile_id and profile_id not in models:
                            models.append(profile_id)
                except ClientError as e:
                    # Inference profiles might not be available in all regions
                    _LOGGER.debug("Could not fetch inference profiles: %s", e)
                
                return sorted(models)
                
            except ClientError as e:
                _LOGGER.error("Error fetching models from Bedrock: %s", e)
                return AVAILABLE_MODELS  # Fallback to static list
        
        # Run in executor to avoid blocking
        models = await hass.async_add_executor_job(_get_models)
        return models if models else AVAILABLE_MODELS
        
    except Exception as e:
        _LOGGER.error("Unexpected error fetching models: %s", e)
        return AVAILABLE_MODELS  # Fallback to static list


async def validate_aws_credentials(hass: HomeAssistant, aws_access_key_id: str, aws_secret_access_key: str, aws_session_token: str | None = None, aws_region: str | None = None) -> dict[str, str] | None:
    """Validate AWS credentials by attempting to list foundation models."""
    if aws_region is None:
        aws_region = DEFAULT_AWS_REGION
    
    try:
        # Run boto3 client creation in executor to avoid blocking
        def _create_client():
            session = boto3.Session(
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                aws_session_token=aws_session_token or None,
                region_name=aws_region,
            )
            return session.client("bedrock")

        bedrock_client = await hass.async_add_executor_job(_create_client)
        
        # Try to list foundation models to verify credentials work.
        # Use a lambda so the bound method is called correctly in the executor.
        await hass.async_add_executor_job(
            lambda: bedrock_client.list_foundation_models()
        )
        return None
        
    except NoCredentialsError as e:
        _LOGGER.debug("Caught NoCredentialsError: %s", e)
        return {"base": "invalid_credentials"}
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        _LOGGER.debug("Caught ClientError with code %s: %s", error_code, e)
        if error_code == "UnrecognizedClientException":
            return {"base": "invalid_credentials"}
        elif error_code == "AccessDeniedException":
            return {"base": "access_denied"}
        else:
            _LOGGER.error("Unexpected error validating AWS credentials: %s", e)
            return {"base": "unknown_error"}
    except BotoCoreError as e:
        _LOGGER.debug("Caught BotoCoreError: %s", e)
        _LOGGER.error("BotoCore error validating AWS credentials: %s", e)
        return {"base": "unknown_error"}
    except Exception as e:
        _LOGGER.debug("Caught unexpected Exception: %s", e)
        _LOGGER.error("Unknown error validating AWS credentials: %s", e)
        return {"base": "unknown_error"}


class BedrockConversationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AWS Bedrock Conversation."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            # Validate credentials
            validation_error = await validate_aws_credentials(
                self.hass,
                user_input[CONF_AWS_ACCESS_KEY_ID],
                user_input[CONF_AWS_SECRET_ACCESS_KEY],
                user_input.get(CONF_AWS_SESSION_TOKEN),
                user_input.get(CONF_AWS_REGION),
            )
            
            if validation_error:
                errors.update(validation_error)
            else:
                # Create entry
                title = f"AWS Bedrock ({user_input.get(CONF_AWS_REGION, DEFAULT_AWS_REGION)})"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_AWS_REGION: user_input.get(CONF_AWS_REGION, DEFAULT_AWS_REGION),
                        CONF_AWS_ACCESS_KEY_ID: user_input[CONF_AWS_ACCESS_KEY_ID],
                        CONF_AWS_SECRET_ACCESS_KEY: user_input[CONF_AWS_SECRET_ACCESS_KEY],
                        CONF_AWS_SESSION_TOKEN: user_input.get(CONF_AWS_SESSION_TOKEN, ""),
                    },
                    options={
                        CONF_MODEL_ID: DEFAULT_MODEL_ID,
                        CONF_PROMPT: DEFAULT_PROMPT,
                        CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
                        CONF_TEMPERATURE: DEFAULT_TEMPERATURE,
                        CONF_TOP_P: DEFAULT_TOP_P,
                        CONF_TOP_K: DEFAULT_TOP_K,
                        CONF_REFRESH_SYSTEM_PROMPT: DEFAULT_REFRESH_SYSTEM_PROMPT,
                        CONF_REMEMBER_CONVERSATION: DEFAULT_REMEMBER_CONVERSATION,
                        CONF_REMEMBER_NUM_INTERACTIONS: DEFAULT_REMEMBER_NUM_INTERACTIONS,
                        CONF_MAX_TOOL_CALL_ITERATIONS: DEFAULT_MAX_TOOL_CALL_ITERATIONS,
                        CONF_EXTRA_ATTRIBUTES_TO_EXPOSE: DEFAULT_EXTRA_ATTRIBUTES,
                        CONF_LLM_HASS_API: HOME_LLM_API_ID,
                    }
                )
        
        data_schema = vol.Schema({
            vol.Required(CONF_AWS_REGION, default=DEFAULT_AWS_REGION): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        "us-east-1",      # N. Virginia
                        "us-west-2",      # Oregon  
                        "ap-southeast-1", # Singapore
                        "ap-northeast-1", # Tokyo
                        "eu-central-1",   # Frankfurt
                        "eu-west-3",      # Paris
                        "ca-central-1",   # Canada
                        "ap-south-1",     # Mumbai
                        "eu-west-1",      # Ireland
                        "eu-west-2",      # London
                        "ap-southeast-2", # Sydney
                        "sa-east-1",      # São Paulo
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_AWS_ACCESS_KEY_ID): str,
            vol.Required(CONF_AWS_SECRET_ACCESS_KEY): str,
            vol.Optional(CONF_AWS_SESSION_TOKEN): str,
        })
        
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return BedrockConversationOptionsFlow()


class BedrockConversationOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for AWS Bedrock Conversation."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._selected_region: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Select AWS region."""
        if user_input is not None:
            self._selected_region = user_input[CONF_AWS_REGION]
            return await self.async_step_options()

        current_region = self.config_entry.options.get(
            CONF_AWS_REGION,
            self.config_entry.data.get(CONF_AWS_REGION, DEFAULT_AWS_REGION)
        )

        region_schema = vol.Schema({
            vol.Required(
                CONF_AWS_REGION,
                default=current_region
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="us-east-1", label="US East (N. Virginia)"),
                        selector.SelectOptionDict(value="us-west-2", label="US West (Oregon)"),
                        selector.SelectOptionDict(value="us-east-2", label="US East (Ohio)"),
                        selector.SelectOptionDict(value="eu-central-1", label="Europe (Frankfurt)"),
                        selector.SelectOptionDict(value="eu-west-1", label="Europe (Ireland)"),
                        selector.SelectOptionDict(value="eu-west-2", label="Europe (London)"),
                        selector.SelectOptionDict(value="eu-west-3", label="Europe (Paris)"),
                        selector.SelectOptionDict(value="ap-southeast-1", label="Asia Pacific (Singapore)"),
                        selector.SelectOptionDict(value="ap-northeast-1", label="Asia Pacific (Tokyo)"),
                        selector.SelectOptionDict(value="ap-south-1", label="Asia Pacific (Mumbai)"),
                        selector.SelectOptionDict(value="ap-southeast-2", label="Asia Pacific (Sydney)"),
                        selector.SelectOptionDict(value="ca-central-1", label="Canada (Central)"),
                        selector.SelectOptionDict(value="sa-east-1", label="South America (São Paulo)"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        })

        return self.async_show_form(
            step_id="init",
            data_schema=region_schema,
            description_placeholders={"current_region": current_region},
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2: Configure model and parameters."""
        if user_input is not None:
            # Merge region into the saved options
            user_input[CONF_AWS_REGION] = self._selected_region
            return self.async_create_entry(title="", data=user_input)

        # Get available LLM APIs with error handling
        try:
            llm_api_ids = [
                api.id for api in llm.async_get_apis(self.hass)
                if api.id != "nlsql"
            ]
        except Exception as e:
            _LOGGER.warning("Error getting LLM APIs: %s", e)
            llm_api_ids = []

        if HOME_LLM_API_ID not in llm_api_ids:
            llm_api_ids.append(HOME_LLM_API_ID)
        if not llm_api_ids:
            llm_api_ids = [HOME_LLM_API_ID]

        # Dynamically fetch models for the selected region
        aws_region = self._selected_region
        available_models = AVAILABLE_MODELS  # Fallback
        try:
            aws_access_key_id = self.config_entry.data.get(CONF_AWS_ACCESS_KEY_ID)
            aws_secret_access_key = self.config_entry.data.get(CONF_AWS_SECRET_ACCESS_KEY)
            aws_session_token = self.config_entry.data.get(CONF_AWS_SESSION_TOKEN)

            if aws_access_key_id and aws_secret_access_key:
                _LOGGER.info("🔍 Fetching available models for region: %s", aws_region)
                available_models = await get_available_models_for_region(
                    self.hass,
                    aws_access_key_id,
                    aws_secret_access_key,
                    aws_session_token,
                    aws_region,
                )
                _LOGGER.info("✅ Found %d models in region %s", len(available_models), aws_region)
            else:
                _LOGGER.warning("⚠️ Missing AWS credentials, using static model list")
        except Exception as e:
            _LOGGER.error("❌ Error fetching models for region, using static list: %s", e)

        # Ensure current model is in the list
        current_model = self.config_entry.options.get(CONF_MODEL_ID, DEFAULT_MODEL_ID)
        if current_model not in available_models:
            available_models.insert(0, current_model)

        options_schema = vol.Schema({
            vol.Optional(
                CONF_MODEL_ID,
                default=current_model
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=available_models,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_PROMPT,
                default=self.config_entry.options.get(CONF_PROMPT, DEFAULT_PROMPT)
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.TEXT,
                    multiline=True,
                )
            ),
            vol.Optional(
                CONF_MAX_TOKENS,
                default=self.config_entry.options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100, max=100000, step=100, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_TEMPERATURE,
                default=self.config_entry.options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=1, step=0.05, mode=selector.NumberSelectorMode.SLIDER
                )
            ),
            vol.Optional(
                CONF_TOP_P,
                default=self.config_entry.options.get(CONF_TOP_P, DEFAULT_TOP_P)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=1, step=0.05, mode=selector.NumberSelectorMode.SLIDER
                )
            ),
            vol.Optional(
                CONF_TOP_K,
                default=self.config_entry.options.get(CONF_TOP_K, DEFAULT_TOP_K)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=500, step=10, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_REFRESH_SYSTEM_PROMPT,
                default=self.config_entry.options.get(CONF_REFRESH_SYSTEM_PROMPT, DEFAULT_REFRESH_SYSTEM_PROMPT)
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_REMEMBER_CONVERSATION,
                default=self.config_entry.options.get(CONF_REMEMBER_CONVERSATION, DEFAULT_REMEMBER_CONVERSATION)
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_REMEMBER_NUM_INTERACTIONS,
                default=self.config_entry.options.get(CONF_REMEMBER_NUM_INTERACTIONS, DEFAULT_REMEMBER_NUM_INTERACTIONS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=20, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_MAX_TOOL_CALL_ITERATIONS,
                default=self.config_entry.options.get(CONF_MAX_TOOL_CALL_ITERATIONS, DEFAULT_MAX_TOOL_CALL_ITERATIONS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=10, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_LLM_HASS_API,
                default=self.config_entry.options.get(CONF_LLM_HASS_API, HOME_LLM_API_ID)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=llm_api_ids,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        })

        return self.async_show_form(
            step_id="options",
            data_schema=options_schema,
            description_placeholders={"region": aws_region},
        )

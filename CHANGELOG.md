# Changelog

All notable changes to this project will be documented in this file.

## [1.0.37] - 2026-03-06

### Added
- **Dynamic model discovery** — the options flow now fetches available foundation models and inference profiles from Bedrock for the selected region
- **Two-step options flow** — first select your AWS region, then configure model and parameters with a region-specific model list
- **Automatic inference profile resolution** — model IDs are automatically prefixed with the correct regional identifier (`us.`, `eu.`, `ap.`, `ca.`, `sa.`) based on the configured AWS region
- **Region changeable in options** — switch AWS regions without reconfiguring the integration; the Bedrock client auto-recreates on region change
- **Comprehensive Bedrock API error handling** — specific error messages for inference profile requirements, role alternation errors, duplicate tool IDs, access denied, and model errors
- **Tool ID uniqueness enforcement** — guaranteed unique tool_use IDs using timestamp + index + UUID, with duplicate detection and automatic replacement during message merging
- **Message role alternation validation** — post-processing merges consecutive same-role messages and validates strict user/assistant alternation before API calls
- **Nova model optimizations** — temperature=0 for tool calling (AWS recommendation) and tool choice parameter for improved reliability
- **Tool schema validation** — removes unsupported fields (`$schema`, `title`, `additionalProperties`), enforces `type: "object"`, and validates tool name length (64 char limit)
- **Robust numeric type conversion** — handles Decimal objects from Home Assistant config via `int(float(value))` conversion
- **Debug logging** — message role sequences, request structure, tool IDs, and device data for troubleshooting

### Fixed
- **Timezone handling** — use Home Assistant's `dt_util.now()` instead of `datetime.now()` with string timezone; resolves "tzinfo argument must be None or of a tzinfo subclass" error
- **BigDecimal type error** — numeric parameters (max_tokens, temperature, top_p, top_k) are now explicitly cast to int/float before serialization
- **Message role alternation** — consecutive user or assistant messages are merged to satisfy Bedrock's strict alternation requirement
- **Duplicate tool_use IDs** — multiple simultaneous tool calls (e.g. controlling several devices at once) no longer produce duplicate IDs
- **Nova model variable scope** — moved `tools` variable definition before its reference in the temperature logic
- **Inference profiles in non-US regions** — models like Nova Lite now work in Paris (eu-west-3) and other non-US regions
- **API timeout** — increased from 30s to 90s; added boto3-level connect (10s) and read (120s) timeouts with adaptive retry
- **Template rendering fallback** — if the Jinja device template fails, a plain-text device list is generated instead of crashing

### Changed
- Default model remains `anthropic.claude-3-haiku-20240307-v1:0` (stable, widely available)
- Model list is now fetched dynamically instead of using a hardcoded static list
- Improved JSON serialization with `ensure_ascii=False` and compact separators

## [1.0.0] - 2025-12-20

### Added
- Initial release
- Support for Anthropic Claude, Amazon Nova, Meta Llama, and Mistral model families
- Anthropic Messages API for Claude models, Converse API for all others
- `HassCallService` tool for device control via natural language
- Configurable system prompt with Jinja2 templates
- Conversation memory with configurable history length
- AWS credential management with session token support
- HACS-compatible installation

### History

#### 2026-03-04 — Stability and compatibility fixes
- Fix config flow 500 error for HACS installation (remove `ai_task` dependency, use `OptionsFlow`)
- Fix invalid Bedrock model identifiers (remove incorrect `us.` prefix, use stable Claude 3.x IDs)
- Fix critical bugs: race condition in client init, unstable `id()` for tool tracking, null LLM API check
- Fix timezone handling in system prompt generation
- Fix BigDecimal type error for numeric parameters
- Fix message role alternation (merge consecutive same-role messages)
- Fix duplicate `tool_use_id` for multi-device actions
- Comprehensive Bedrock API compatibility fixes (tool schema, Nova optimizations, error handling)
- Add dynamic model selection based on AWS region

#### 2026-02-24 — Refactor and Converse API support
- Fix `invoke_model`: add explicit `contentType`/`accept`, encode body as bytes
- Fix empty session token causing `SignatureDoesNotMatch`
- Add Converse API path for non-Anthropic models (Nova, Llama, Mistral)
- Fix `OptionsFlow`: remove deprecated `__init__(config_entry)` pattern
- Type `runtime_data` with `BedrockRuntimeData` dataclass
- Fix `strings.json`/translations error key mismatch
- Optimize `utils.py`: pre-compute CSS3 color RGB mapping at module load

#### 2025-12-22 — Early development iteration
- Fix async/await bug in client setup
- Fix tool-ID tracking and result parsing
- Move response body read to executor thread to prevent event loop blocking
- Add timeouts and error returns
- Fix `StopReason` parsing and tool call parsing
- Fix `top_p` issue with Claude
- Improve system prompts and add context bits
- Fix config flow and model selection
- Update test dependencies and test environment

#### 2025-12-21 — Core implementation
- Complete conversation agent and Bedrock client implementation
- Add tool calling loop with error handling
- Add conversation memory and history management
- Fix `async_get_api` params and LLM context
- Fix tests and formatting
- Fix interface and broken dependencies
- Fix action runner test environment

#### 2025-12-20 — Initial release
- Remove add-on implementation, ship as custom component
- Add `invoke_model` support with model-specific data shapes
- Add memory and format handling
- Bump to v1.0.0

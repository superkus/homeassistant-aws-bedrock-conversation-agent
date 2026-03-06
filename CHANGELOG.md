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

# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-11-09

Rewrite for **Home Assistant 2026.11.0 or later** (Python 3.14). **Make a full Home Assistant backup before upgrading**; going back to 1.x requires restoring it.

### Fixed
- No responses on Home Assistant 2026.11: the "AWS Bedrock Services" LLM API raised `AttributeError: ... 'requires_admin'` because it did not initialise Home Assistant's `llm.API` base class.

### Added
- Setup in two steps: region (list from the AWS SDK's bundled region data) and credentials, then a model chosen from active streaming text model and system inference-profile metadata returned for that region. The list does not prove model access; Bedrock checks access and `bedrock:InvokeModelWithResponseStream` permission when the agent invokes the selected model or profile. Custom model IDs, inference profile IDs and ARNs are accepted.
- Reauthentication when AWS rejects the credentials, and **Reconfigure** to change the region or credentials (after a region change, the model chosen there is set for every agent; with the same region every agent keeps its model; a custom title is kept).
- Several agents per set of credentials: each agent is a **Conversation agent** sub-entry with its own model, instructions, device control APIs, spoken confirmation, max tokens, temperature, tool rounds and prompt caching. Setup creates a first agent named "AWS Bedrock"; **Add agent** adds more and **Reconfigure agent** changes one (the integration must be loaded). Each change reloads the integration.
- Multiple device control APIs per agent. Home Assistant's built-in **Device control** (Assist) API is the default for new installs and recommended; the "AWS Bedrock Services" API is kept.
- Spoken confirmation for sensitive actions: unlocking, opening door, garage and gate covers, running scripts (including `intent_script` tools), scenes that set a lock or such a cover, timers that run a command, playing web URLs, `homeassistant__call_action`, and state-changing tools from unknown integrations (for example MCP; their read-only hints are not trusted). The agent asks a fixed question built by the integration and the turn ends; only a clear "yes" from the same user and device within 2 minutes runs exactly the held calls. Blank names, the name "all" and duplicate names are checked against every device they can reach. Requests with neither a user nor a device are refused. Other agents using the "AWS Bedrock Services" API are refused sensitive services.
- `HassGetState`, a read-only tool in the "AWS Bedrock Services" API for the current state of one exposed entity (the device list in the prompt is a snapshot for the conversation).
- Conversations that contain tool calls from other agents (for example local intent handling) or that were opened by an announcement work; missing tool results are filled in.
- A clear error asking to raise "Maximum tokens in a response" when a reply is cut off in the middle of a tool call.
- Voice-friendly timeouts: 5 s to connect, 15 s without data from AWS, 60 s per request. Each credentials entry runs at most two Bedrock streams concurrently and buffers at most 16 stream events.
- At most 10 tool calls are accepted from one model response; larger batches fail before Home Assistant runs any call.
- Prompt caching, turned off automatically for models that do not support it.
- Streamed answers.
- Spanish translation.

### Changed
- All models use the Bedrock Converse API with streaming (`ConverseStream`); the Anthropic Messages API path (`invoke_model`) is gone. The IAM policy needs `bedrock:InvokeModelWithResponseStream`.
- Inference profiles are no longer guessed from the region. If Bedrock says a model needs one, a matching system inference profile is looked up and used.
- Conversation history and the system prompt come from Home Assistant's chat log. The instructions are a Home Assistant template; Home Assistant adds the date and the device context of the selected APIs. Clearing **Instructions** in **Reconfigure agent** restores the default.
- The "AWS Bedrock Services" device list is marked as data (prompt-injection hardening), and `HassCallService` only acts on single, exposed entities. `input_text.set_value` and `input_datetime.set_datetime` are no longer allowed. Other conversation agents using this API are refused sensitive services, because they cannot ask for confirmation.
- New defaults: region `us-east-1`, max tokens 1024, temperature 0.5, max tool rounds 10.
- Errors are spoken as short translated messages; AWS details are only logged. Setup errors show only the AWS error code.
- The agent settings are no longer entry options: the **Configure** screen is gone, use **Reconfigure agent**. The agent is registered only under its conversation entity ID, no longer also under the config entry ID.
- Requirements: `boto3>=1.40.0` only; `webcolors` was removed (Home Assistant's colour table is used).
- Development: Python 3.14, `homeassistant` and `pytest-homeassistant` pinned to 2026.11.0, Ruff for lint and format, new test suite.

### Removed
- Options Top P, Top K, refresh prompt each turn, remember conversation and interactions to remember.
- The `<persona>`, `<current_date>` and `<devices>` prompt placeholders and the built-in persona prompts.
- The hard-coded model lists.
- `media_player.play_media` from the legacy "AWS Bedrock Services" API. Media playback now requires another selected exposure-bounded API that provides a playback tool.

### Migration
- 1.x entries are migrated automatically to version 2.1 (log line `Migrated <entry id> to version 2.1`): the region moves into the entry data, removed options are dropped, the placeholders are removed from the prompt (a prompt that had only placeholders, such as the 1.x default, falls back to Home Assistant's default instructions), prompts containing `{{`, `{%` or `{#` are wrapped in `{% raw %}` so they stay literal, a custom entry title is kept (only the automatic `AWS Bedrock (<region>)` title is updated to the region in use), the API choice is kept (stored as a list; the API ID of early 1.x versions is mapped to the current one), and the conversation entity keeps its entity ID.
- Then to version 3.1 (log line `Migrated <entry id> to version 3.1 (agent moved to a sub-entry)`): the agent settings move from the entry options into one agent sub-entry titled like the entry; the conversation entity and its device move to it and keep their entity ID, so voice assistants keep working. An interrupted migration completes on the next start. Entries of a newer version (4 or later) are refused.
- Back up first, and never delete and re-add the integration. Download the full backup and record its `.tar` filename, local path, integration entry ID and conversation entity ID. To roll back, open **Settings → System → Backups**, upload that `.tar` if it is not listed, select it, choose a full restore, and confirm. After the restore completes, restart Home Assistant and verify that the recorded entry and entity IDs are present and the agent answers. Version 1.0.37 does not work on Home Assistant 2026.11.

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

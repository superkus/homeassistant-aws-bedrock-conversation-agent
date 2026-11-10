# Development Guide

## Project structure

```
custom_components/bedrock_conversation/
├── __init__.py       # Entry setup/unload, registers the "AWS Bedrock Services" API, migrations 1 → 2.1 → 3.1
├── brand/           # Local HA icon assets and their source/conversion record
├── client.py         # BedrockClient: boto3 clients, region list, model listing, ConverseStream,
│                     # inference profile, temperature and prompt cache fallbacks, error translation
├── config_flow.py    # Setup (region + credentials → model → first agent), reauth, reconfigure,
│                     # AgentSubentryFlow (add / reconfigure agent)
├── confirmation.py   # assess(): which tool calls are sensitive; is_affirmative(); the fixed question
├── cost.py           # 12-hour Cost Explorer coordinator; billing failures stay sensor-only
├── const.py          # Setting keys, defaults, timeouts, service allowlist, AFFIRMATIVE_PHRASES
├── conversation.py   # BedrockConversationEntity (one per agent sub-entry): chat log → ConverseStream, tool rounds, held calls
├── llm_api.py        # Legacy "AWS Bedrock Services" LLM API: HassCallService and HassGetState
├── messages.py       # Pure converters: chat log → Converse messages, HA tools → toolSpec
├── sensor.py         # Optional entry-level monetary month-to-date Bedrock cost sensor
├── utils.py          # closest_color() for the device list (HA's colour table)
├── manifest.json     # Metadata, requirements, version (source of truth for releases)
├── strings.json      # UI strings (translations/en.json, translations/es.json)
tests/                # pytest suite (see tests/README.md)
Makefile              # venv, deps, test, lint, format, clean, version, release
requirements-test.txt # homeassistant, pytest-homeassistant and integration requirements
requirements-dev.txt  # requirements-test.txt + ruff
```

## How a turn works

1. `BedrockConversationEntity._async_handle_message` calls `chat_log.async_provide_llm_data`. Home Assistant renders the instructions template, adds the date and the prompts of the selected LLM APIs, and keeps the conversation history.
2. `_async_resolve_pending`: if an earlier turn held calls for this conversation, they run now only if the answer comes from the same user and device (`PendingConfirmation.same_requester`), within `CONFIRMATION_TIMEOUT` (120 s), and `confirmation.is_affirmative` accepts the text. Otherwise they are cancelled. Either way the calls and their results (or a "cancelled, not performed" result) are written to the chat log.
3. `_async_handle_chat_log` builds the Converse request (`messages.build_system`, `build_tool_config`, `convert_content`) and streams it through `BedrockClient.async_converse_stream`. Each credentials client admits at most two simultaneous streams; excess turns wait asynchronously within the 60 s turn timeout. The worker-to-event-loop bridge buffers at most 16 events and backpressures botocore when the consumer falls behind. `_DeltaStream` turns stream events into chat log deltas; the chat log runs the tool calls.
4. `_DeltaStream` accepts at most 128 raw tool calls, collapses homogeneous `HassCallService` and `HassGetState` calls into bulk operations, then accepts at most 10 distinct operations before handing any calls to Home Assistant. Either limit rejects the complete response before any tool runs. When spoken confirmation is enabled, it checks every accepted operation with `confirmation.assess`. Sensitive calls are marked external, answered with `CONFIRMATION_REQUIRED_RESULT`, and stored once per distinct call (`hold_for_confirmation`, bound to the user and device; refused with `CONFIRMATION_IMPOSSIBLE_RESULT` when the request has neither). The agent then adds the question from `confirmation_question` (fixed text per language, not written by the model) and the turn ends.
5. Steps 3–4 repeat until the model answers without tool calls, up to the agent's "max tool rounds" setting. The whole turn is limited to `TURN_TIMEOUT` (60 s); botocore uses a 5 s connect and 15 s read timeout (between stream chunks) with one retry.

`convert_content` keeps the Converse rules: roles alternate starting with `user` (a conversation opened by an announcement gets a neutral user turn in front), tool calls to tools not offered now (for example from HA's local intent handling) are sent as text, and every tool use gets a result (a missing one is filled in as an error).

If the stream stops with `max_tokens` while a tool call's arguments are incomplete, the turn fails with `response_too_long` ("Use Reconfigure agent to increase Maximum tokens in a response").

- `HassCallService` in the legacy AWS Bedrock Services API accepts one exposed entity (`target_device`) or up to 100 exposed entities for the same action (`target_devices`). `HassGetState` likewise accepts `entity_id` or up to 100 `entity_ids`. If a model still emits one call per entity, homogeneous service calls and state reads are collapsed into one bounded operation; every original Bedrock tool-use ID still receives a compact result.
- A response may contain up to 128 raw tool calls, but after homogeneous service calls and state reads are grouped, it may contain at most 10 distinct operations. Both limits reject the complete response before any tool runs.

`HassCallService` in `llm_api.py` refuses sensitive services (`confirmation.service_call_action`) when called by another platform's agent (`llm_context.platform != DOMAIN`), because only this agent can ask for confirmation.

`BedrockClient.async_converse_stream` can make one fallback attempt for each model-compatibility error before yielding the first event: when Bedrock reports that the model needs an inference profile (it picks a system profile for the region, see `_profile_preference`), that a newer model rejects the deprecated `temperature` parameter (temperature is then omitted for that model), or that prompt caching is not supported (cache points are removed). The three fallbacks can compose within one request, and each decision is remembered per model for the client's lifetime.

Errors are mapped in `client.translate_error` and `BedrockConversationEntity._error` to the translated messages under `exceptions` in `strings.json`. Authentication errors also start a reauth flow.

## Config entries

- `data`: `aws_region`, `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`; optional entry-level `cost_sensor_enabled`. `options` is empty.
- Sub-entries of type `conversation` (`SUBENTRY_CONVERSATION`), one per agent, created and changed by `AgentSubentryFlow` (`async_get_supported_subentry_types`). Sub-entry `data`: `model`, `prompt`, `llm_hass_api` (list of API IDs), `confirm_sensitive_actions`, `max_tokens`, `temperature`, `max_tool_call_iterations`, `prompt_caching`. Setup creates the first one, titled `DEFAULT_AGENT_NAME` ("AWS Bedrock"). The flow aborts with `entry_not_loaded` unless the entry is loaded. Any sub-entry change reloads the entry (update listener).
- `conversation.async_setup_entry` adds one `BedrockConversationEntity` per sub-entry; its unique ID is the sub-entry ID. The agent is registered only under its entity ID, not also under the config entry ID (as in Home Assistant's own LLM integrations with sub-entries).
- `sensor.async_setup_entry` adds one entry-level `BedrockMonthToDateCostSensor` when enabled. Entries with the same access-key ID share a `BedrockCostCoordinator`, so they share 12-hour and manual Cost Explorer refreshes while retaining one sensor entity per entry. Different access-key IDs for the same AWS account cannot be identified without another permission and can expose duplicate account-wide totals. The coordinator queries signed, account-wide Amazon Bedrock `UnblendedCost`; initial and later refresh failures mark only its sensors unavailable.
- Entry reconfigure keeps every agent's model when the region is unchanged (e.g. rotated keys). After a region change, `async_step_reconfigure_model` sets the chosen model on every agent, because their models may not exist in the new region. A custom title is kept.
- Version 3.1. `async_migrate_entry` refuses versions newer than 3 and runs the steps in order:
  - 1 → 2.1: the pure, idempotent `migrate_v1_to_v2` removes 1.x placeholders, wraps prompts with template syntax in `{% raw %}`, drops a prompt left empty (HA's default applies) and refreshes only the automatic `AWS Bedrock (<region>)` title. Logs `Migrated <id> to version 2.1`.
  - 2 → 3.1: `_migrate_v2_to_v3` moves the options into one sub-entry titled like the entry, and re-points the conversation entity (new unique ID, same entity ID) and its device to it. It is safe to re-run after an interruption: an existing sub-entry is reused and the entity is found by config entry. Logs `Migrated <id> to version 3.1 (agent moved to a sub-entry)`.
- If you change the stored format, bump the version and add a migration step with tests in `tests/test_init.py`.
- `async_setup_entry`, `async_unload_entry` and `async_migrate_entry` return `None` and raise on failure (the Home Assistant 2026.11 convention). Setup errors carry only the AWS error code; the message can contain the account ID and is only logged.

## Dependencies

`manifest.json` requires only `boto3>=1.40.0` (colour names come from `homeassistant.util.color`, so `webcolors` is gone). The requirement is a floor, not a pin: Home Assistant's package constraints decide the installed version (boto3 1.42.97 on 2026.11.0), and a pin in the manifest would conflict with them. `requirements-test.txt` pins that same version for tests. Cost Explorer uses the same boto3 package and credentials; its default-off sensor requires a separate IAM statement allowing `ce:GetCostAndUsage` on `Resource: "*"` because the action exposes account-wide billing data and cannot be restricted to individual resources. It adds no Python dependency.

## Environment

Home Assistant 2026.11 needs **Python 3.14** (3.14.2 or newer).

```bash
make deps     # create .venv with python3.14 and install requirements-dev.txt
make test     # pytest with coverage
make lint     # ruff check + ruff format --check
make format   # ruff --fix + ruff format
make clean    # remove .venv, coverage files and __pycache__
```

Use `make PYTHON=/path/to/python3.14 deps` if `python3.14` is not on your `PATH`.

`homeassistant` and `pytest-homeassistant` are pinned to the same release (2026.11.0) in `requirements-test.txt`. Keep them in step when you move to a newer Home Assistant, and update `hacs.json` (`homeassistant`) with them. CI (`.github/workflows/test.yml`) runs the same lint and test commands on Python 3.14 for pushes and pull requests to `main`.

### Single tests

```bash
.venv/bin/pytest tests/test_client.py -v
.venv/bin/pytest tests/test_confirmation.py::test_affirmative -v
```

All AWS calls are faked (`FakeBedrock` in `tests/conftest.py`); the tests make no network requests.

## Trying it in Home Assistant

Copy `custom_components/bedrock_conversation` into the `config/custom_components/` directory of a Home Assistant 2026.11+ instance, restart, and follow [TESTING_GUIDE.md](TESTING_GUIDE.md). Debug logging:

```yaml
logger:
  logs:
    custom_components.bedrock_conversation: debug
```

## Code style

- Ruff for linting and formatting (`pyproject.toml`: Python 3.14 target, line length 100).
- Follow Home Assistant conventions: blocking boto3 calls run in the executor, user-facing errors are translated `HomeAssistantError`s, UI text lives in `strings.json` and `translations/`.

## Releasing

1. Set the version in `custom_components/bedrock_conversation/manifest.json` (and `pyproject.toml`).
2. Add the entry to `CHANGELOG.md`.
3. Commit, then run `make release`.

`make release` runs tests and lint, refuses a dirty tree or an existing tag, creates and pushes the tag `v<version>`, and creates a GitHub release with `gh`, using `CHANGELOG.md` as release notes.

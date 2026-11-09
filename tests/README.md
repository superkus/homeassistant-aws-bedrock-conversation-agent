# Tests for AWS Bedrock Conversation

The tests use `pytest-homeassistant` (pinned to the same release as `homeassistant`, 2026.11.0) and need Python 3.14. AWS is faked by `FakeBedrock` in `conftest.py`; no network requests are made.

## Running

```bash
make test                                   # from the repo root: .venv + coverage
.venv/bin/pytest tests/test_client.py -v    # one file
./run_tests.sh                              # installs requirements-test.txt into the current Python
```

## Files

| File | Covers |
|------|--------|
| `test_client.py` | Error translation, model listing, streaming, inference profile and prompt cache fallbacks |
| `test_config_flow.py` | Setup (region → models → first agent), errors, duplicates, reauth, reconfigure (model set on every agent), add and reconfigure agent |
| `test_init.py` | Migrations 1 → 2.1 (prompts, titles) and 2 → 3.1 (sub-entry, entity ID kept), setup errors, API registration |
| `test_confirmation.py` | Affirmative answers, the fixed question, and which tool calls are held (`assess`) |
| `test_conversation.py` | Answers and memory, request shape, bulk and grouped tool calls, safety limits, spoken errors, reauth, confirmation flow (same device, expiry, duplicates, "all"), other agents refused sensitive services, truncated tool calls, tool calls from other agents |
| `test_llm_api.py` | `HassCallService` single and bulk target validation, atomic exposure checks, `HassGetState`, the device list as delimited data |
| `test_messages.py` | Chat log → Converse messages (foreign tool calls, missing results, conversations starting with the assistant), tool schemas and names, cache points |
| `test_utils.py` | `closest_color` |

Coverage is printed in the terminal (`--cov-report=term-missing`).

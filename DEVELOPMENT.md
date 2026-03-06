# Development Guide

## Project Structure

```
.
├── custom_components/
│   └── bedrock_conversation/
│       ├── __init__.py          # Integration setup, HassServiceTool, BedrockServicesAPI
│       ├── bedrock_client.py    # Bedrock API client, message building, tool formatting
│       ├── config_flow.py       # Config and options flows, credential validation
│       ├── const.py             # Constants, model lists, default prompts
│       ├── conversation.py      # ConversationEntity, message processing loop
│       ├── manifest.json        # Integration metadata and dependencies
│       ├── strings.json         # UI strings
│       ├── translations/en.json # English translations
│       └── utils.py             # Color matching utilities
├── tests/                       # Unit tests
├── Makefile                     # Build automation
├── requirements-dev.txt         # Dev tools (black, ruff, mypy)
├── requirements-test.txt        # Test dependencies (pytest, pytest-asyncio)
└── pytest.ini                   # Test configuration
```

## Key Components

### `__init__.py`
- Registers the `BedrockServicesAPI` LLM API with Home Assistant
- Defines `HassServiceTool` — the tool the model calls to control devices
- Validates service calls against allowed domains and services from `const.py`

### `bedrock_client.py`
- `BedrockClient` — manages the boto3 client lifecycle with lazy initialization and region change detection
- `_resolve_inference_profile()` — maps AWS regions to inference profile prefixes (`us.`, `eu.`, `ap.`, etc.)
- `_build_bedrock_messages()` — converts Home Assistant conversation content to Bedrock message format with role alternation enforcement and unique tool ID generation
- `_format_tools_for_bedrock()` — converts Home Assistant tool definitions to Bedrock's tool schema format
- `_generate_system_prompt()` — renders the system prompt with persona, date/time, and exposed device list
- `async_generate()` — routes to either the Anthropic Messages API (`invoke_model`) or the Converse API based on model type

### `config_flow.py`
- Initial setup flow: region selection, credential validation
- Two-step options flow: region selection → dynamic model list + parameters
- `get_available_models_for_region()` — fetches foundation models and inference profiles from Bedrock

### `conversation.py`
- `BedrockConversationEntity` — the conversation agent entity
- `async_process()` — main message processing loop with tool calling iteration

### `const.py`
- All configuration keys, defaults, allowed services, and model lists
- System prompt templates (persona, date, devices) with multi-language support

## Quick Start

```bash
make deps      # Install dependencies
make test      # Run tests with coverage
make lint      # Run ruff and flake8
make format    # Auto-format with black and isort
make typecheck # Run mypy
```

## Running Tests

```bash
# Full suite
make test

# Single file
pytest tests/test_bedrock_client.py -v

# Single test
pytest tests/test_utils.py::test_closest_color -v

# With output
pytest tests/ -v -s
```

Tests mock all AWS calls — no real API requests are made.

## Local Testing with Home Assistant

```bash
# Copy to your HA config directory
cp -r custom_components/bedrock_conversation /path/to/ha/config/custom_components/

# Restart Home Assistant
# Test your changes
```

### Enable Debug Logging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.bedrock_conversation: debug
```

This enables detailed logging including message role sequences, tool IDs, request structure, and API responses.

## Code Style

- **Formatter**: black (line length 88)
- **Import sorting**: isort
- **Linter**: ruff + flake8
- **Type checking**: mypy
- Follow Home Assistant coding conventions

## API Routing

The integration uses two different Bedrock APIs depending on the model:

| Model Family | API | Why |
|-------------|-----|-----|
| Anthropic Claude | `invoke_model` (Messages API) | Native format, supports all Claude features |
| Amazon Nova, Meta Llama, Mistral | `converse` (Converse API) | Unified interface for non-Anthropic models |

The routing is determined by checking if the model ID contains `anthropic.claude`.

## Inference Profile Resolution

Newer Bedrock models require regional inference profiles instead of direct model IDs. The `_resolve_inference_profile()` method handles this transparently:

```
Region eu-west-3 + model amazon.nova-lite-v1:0
  → resolved to eu.amazon.nova-lite-v1:0
```

The mapping:
- `us-east-1`, `us-east-2`, `us-west-2` → `us.`
- `eu-central-1`, `eu-west-1`, `eu-west-2`, `eu-west-3` → `eu.`
- `ap-southeast-1`, `ap-northeast-1`, `ap-south-1`, `ap-southeast-2` → `ap.`
- `ca-central-1` → `ca.`
- `sa-east-1` → `sa.`

Models that already have a prefix or are ARNs are left unchanged.

## Releasing

```bash
# 1. Update version in manifest.json
# 2. Update CHANGELOG.md
# 3. Commit
git add .
git commit -m "Release v1.0.38"

# 4. Create release
make release
```

The Makefile extracts the version from `manifest.json`, creates a git tag, and pushes it.

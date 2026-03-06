# AWS Bedrock Conversation for Home Assistant

A custom integration that connects Amazon Bedrock foundation models to Home Assistant's conversation system, enabling natural language control of smart home devices.

## Features

- **Multi-model support** — Anthropic Claude, Amazon Nova, Meta Llama, Mistral
- **Native tool calling** — control devices through natural language using `HassCallService`
- **Dynamic model discovery** — fetches available models and inference profiles per region
- **Automatic inference profile resolution** — adds correct regional prefix (`us.`, `eu.`, `ap.`, etc.)
- **Two-step configuration** — select region first, then choose from available models
- **Rich system prompts** — auto-generated with device states, areas, and attributes
- **Conversation memory** — configurable history across turns
- **Dual API support** — Messages API for Claude, Converse API for Nova/Llama/Mistral

## Installation

### HACS (Recommended)

1. Open HACS → Integrations
2. Three-dot menu → Custom repositories
3. Add: `https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent`
4. Install "AWS Bedrock Conversation"
5. Restart Home Assistant

### Manual

Copy this folder to `config/custom_components/bedrock_conversation/` and restart Home Assistant.

## Setup

### AWS Prerequisites

1. Enable model access in the [Bedrock console](https://console.aws.amazon.com/bedrock/)
2. Create an IAM user with these permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:ListFoundationModels",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": "*"
    }
  ]
}
```

### Home Assistant Configuration

1. **Settings → Devices & Services → Add Integration → AWS Bedrock Conversation**
2. Enter AWS credentials and region
3. Click **Configure** to select model and parameters:
   - Step 1: Choose AWS region
   - Step 2: Choose model (dynamically fetched) and configure parameters
4. **Settings → Voice Assistants → Expose** — expose the entities you want to control
5. **Settings → Voice Assistants → Add Assistant** — create an assistant using the Bedrock agent

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| AWS Region | us-west-2 | Bedrock region (changeable in options) |
| Model | claude-3-haiku | Foundation model or inference profile |
| Max Tokens | 4096 | Maximum response length |
| Temperature | 1.0 | Randomness (0–1) |
| Top P | 0.999 | Nucleus sampling (0–1) |
| Top K | 250 | Token selection limit |
| Refresh prompt each turn | On | Regenerate system prompt with current device states |
| Remember conversation | On | Keep chat history |
| Interactions to remember | 10 | Number of past exchanges |
| Max tool call iterations | 5 | Tool calling loop limit |
| LLM API | Bedrock Services | Tool API for device control |

## Supported Services

The `HassCallService` tool supports these domains:

- **light** — turn_on, turn_off, toggle (with brightness, color)
- **switch** — turn_on, turn_off, toggle
- **fan** — turn_on, turn_off, set_percentage, oscillate, set_direction, set_preset_mode
- **climate** — set_temperature, set_humidity, set_fan_mode, set_hvac_mode, set_preset_mode
- **cover** — open_cover, close_cover, stop_cover, set_cover_position
- **media_player** — turn_on, turn_off, toggle, volume controls, media controls
- **lock** — lock, unlock
- **script** — turn_on
- **scene** — turn_on
- **input_boolean** — turn_on, turn_off, toggle
- **input_number** — set_value
- **input_text** — set_value
- **input_select** — select_option
- **input_datetime** — set_datetime
- **timer** — start, pause, cancel, finish

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| "Model not found" | Model not available in region | Change region in Configure |
| "Requires inference profile" | Model needs regional prefix | Handled automatically; check region |
| "Invalid credentials" | Wrong AWS keys | Verify Access Key ID and Secret |
| "Access denied" | Missing IAM permissions | Add `bedrock:InvokeModel` to IAM policy |
| "Roles must alternate" | Message format issue | Restart the conversation |
| Timeout errors | Slow network or large prompt | Check connectivity; reduce max_tokens |
| Devices not controlled | Entities not exposed | Expose in Settings → Voice Assistants |

Enable debug logging for detailed diagnostics:

```yaml
logger:
  default: info
  logs:
    custom_components.bedrock_conversation: debug
```

## Requirements

- Home Assistant 2024.12.0+
- Python packages: `boto3>=1.35.0`, `webcolors>=24.8.0` (installed automatically)

## License

MIT

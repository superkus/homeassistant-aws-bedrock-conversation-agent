# AWS Bedrock Conversation for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/superkus/homeassistant-aws-bedrock-conversation-agent)](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration that connects Amazon Bedrock foundation models to Home Assistant's conversation system, enabling natural language control of your smart home devices.

## Features

- **Multi-model support** — Anthropic Claude, Amazon Nova, Meta Llama, and Mistral families
- **Native tool calling** — control lights, switches, fans, climate, locks, covers, media players, and more through natural language
- **Dynamic model discovery** — automatically fetches available models and inference profiles for your selected AWS region
- **Automatic inference profile resolution** — transparently adds the correct regional prefix (`us.`, `eu.`, `ap.`, etc.) so models work across all Bedrock regions
- **Two-step configuration** — pick your region first, then choose from models actually available there
- **Rich system prompts** — auto-generated with exposed device states, areas, and attributes
- **Conversation memory** — configurable history length across turns
- **Dual API support** — uses the Anthropic Messages API for Claude models and the Converse API for all others (Nova, Llama, Mistral)
- **Full UI configuration** — no YAML editing required

## Supported Model Families

| Provider | Models | API |
|----------|--------|-----|
| Anthropic | Claude 3.5 Sonnet, Claude 3.5 Haiku, Claude 3 Opus, Claude 3 Haiku | Messages API (invoke_model) |
| Amazon | Nova Pro, Nova Lite, Nova Micro | Converse API |
| Meta | Llama 3.2 (1B–90B), Llama 3.1 (8B–405B) | Converse API |
| Mistral | Mistral Large, Mistral Small | Converse API |

The model list is fetched dynamically from Bedrock when you open the configuration, so newly released models appear automatically.

## Quick Start

1. [Install the integration](INSTALL.md)
2. Go to **Settings → Devices & Services → Add Integration → AWS Bedrock Conversation**
3. Enter your AWS credentials and region
4. Configure your model and options
5. Expose devices in **Settings → Voice Assistants → Expose**
6. Create a voice assistant using the Bedrock conversation agent

See [INSTALL.md](INSTALL.md) for detailed setup instructions.

## Documentation

| Document | Description |
|----------|-------------|
| [INSTALL.md](INSTALL.md) | Prerequisites, AWS setup, installation, and configuration |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Development environment, testing, code style, and releasing |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [TESTING_GUIDE.md](TESTING_GUIDE.md) | Manual testing procedures for device control |

## Architecture

This is a **custom component** (not an add-on). It registers as a Home Assistant conversation agent and provides a built-in LLM tool (`HassCallService`) for device control.

```
User message
  → Home Assistant conversation system
    → BedrockConversationEntity.async_process()
      → System prompt generation (devices, areas, attributes)
      → Bedrock API call (Messages API or Converse API)
        → Tool call loop (if model requests device actions)
      → Response returned to user
```

## Support

- **Issues**: [GitHub Issues](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent/issues)
- **Repository**: [GitHub](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent)

## License

MIT — see [LICENSE](LICENSE)

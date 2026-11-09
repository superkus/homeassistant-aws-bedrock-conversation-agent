# AWS Bedrock Conversation for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/superkus/homeassistant-aws-bedrock-conversation-agent)](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration that uses Amazon Bedrock models as a conversation agent, so you can talk to your home and control exposed devices.

Version 2.0.0 requires **Home Assistant 2026.11.0 or later**. It fixes the 2026.11 breakage where 1.x stopped answering (an `AttributeError` on `requires_admin` in its LLM API). Read [Upgrading from 1.x](#upgrading-from-1x) before you update.

This is a fork of [cronus42/homeassistant-aws-bedrock-conversation-agent](https://github.com/cronus42/homeassistant-aws-bedrock-conversation-agent), maintained by [superkus](https://github.com/superkus).

## Features

- **Bedrock Converse API with streaming** for every model; answers appear while they are generated.
- **Model metadata discovery**: pick a region, then choose from the active streaming text models and system inference profiles returned by Bedrock's regional listing APIs. The list does not prove model access; Bedrock checks access and `bedrock:InvokeModelWithResponseStream` permission when the agent invokes the selected model or profile. You can also type any model ID, inference profile ID or ARN.
- **Automatic inference profile fallback**: if a model can only be called through an inference profile, the integration finds a matching one for your region and uses it.
- **Device control bounded by Home Assistant exposure settings**: the built-in **Device control** (Assist) API is the default and recommended choice. The 1.x **AWS Bedrock Services** API is still available. Both honor **Settings → Voice assistants → Expose**; unrestricted admin and third-party APIs are not offered.
- **Spoken confirmation for sensitive actions**: unlocking, opening doors, garages and gates, running scripts and unknown state-changing tools are held until you answer a clear "yes" from the same device within 2 minutes. See [INSTALL.md](INSTALL.md#spoken-confirmation-for-sensitive-actions) for details and limitations.
- **Conversation memory** through Home Assistant's chat log, including turns handled by other agents (such as local intent handling) and conversations started by an announcement.
- **Prompt caching** on models that support it, to lower cost and latency.
- **Several agents** per set of credentials: each agent (a sub-entry) has its own model, instructions and settings.
- **Reauthentication and reconfiguration**: change the region or credentials without removing the integration.
- UI configuration only; no YAML.

## Quick start

1. [Install the integration](INSTALL.md) (HACS or manual) and restart Home Assistant.
2. **Settings → Devices & services → Add integration → AWS Bedrock Conversation**.
3. Choose the AWS region and enter your IAM credentials.
4. Choose a model and the device control API. This creates a first agent named **AWS Bedrock**; use **Add agent** on the integration page for more.
5. Expose devices in **Settings → Voice assistants → Expose**.
6. Select the Bedrock agent as the conversation agent of a voice assistant.

See [INSTALL.md](INSTALL.md) for the IAM policy, all agent settings and troubleshooting.

## Upgrading from 1.x

Version 1.0.37 does not work on Home Assistant 2026.11, so update both together:

1. **Make a full Home Assistant backup** (**Settings → System → Backups**) and download it. Record the downloaded `.tar` filename and its local path, and keep that copy outside the Home Assistant host. Also record the current integration entry ID and conversation entity ID for the post-restore check.
2. Update Home Assistant to 2026.11.0 or later and this integration to 2.0.0, then restart.
3. Check **Settings → System → Logs** for both `Migrated <entry id> to version 2.1` and `Migrated <entry id> to version 3.1 (agent moved to a sub-entry)`, and that the entry is loaded with one agent.
4. Ask the voice assistant something. The conversation entity ID is unchanged, so voice assistants and automations keep using it.
5. **Never delete and re-add the integration** to fix a problem: the new entry gets a new ID, so voice assistants and automations that point at the old agent can stop working, and your settings are lost. Use **Reconfigure** (credentials, region) or **Reconfigure agent** instead.

Existing entries are migrated automatically on first start:

- Removed options are dropped: Top P, Top K, refresh prompt each turn, remember conversation, interactions to remember. Home Assistant's chat log now handles memory and the system prompt.
- The legacy **AWS Bedrock Services** API no longer offers `media_player.play_media`. Media playback now depends on another selected exposure-bounded API that provides a playback tool.
- The `<current_date>`, `<devices>` and `<persona>` placeholders are removed from your prompt; Home Assistant adds the date, and the selected API adds the devices. A prompt that contained only placeholders (such as the 1.x default) is replaced by Home Assistant's default instructions.
- Home Assistant now renders the prompt as a template. A prompt containing `{{`, `{%` or `{#` is wrapped in `{% raw %}…{% endraw %}`, so it stays literal text as in 1.x.
- A title you gave the entry is kept; only the automatic `AWS Bedrock (<region>)` title is updated to the region in use.
- The agent settings move from the entry options into one **Conversation agent** sub-entry with the entry's title. The conversation entity and its device move with it and keep their entity ID; your device control API choice is kept. If the migration is interrupted, it completes on the next start.
- Entries from a newer version of the integration (entry version 4 or later) are refused instead of being changed.

**Rollback procedure**:

1. Open **Settings → System → Backups** on Home Assistant.
2. Upload the recorded `.tar` file if that backup is not already listed, then select the backup with the recorded filename.
3. Choose a **full restore** and confirm it.
4. After the restore completes, restart Home Assistant.
5. Verify that the preserved AWS Bedrock Conversation config entry and its conversation entity are present with the same IDs as before the upgrade, then ask the agent a test question.

A migrated entry cannot be used by 1.x, and 1.0.37 does not work on Home Assistant 2026.11.

## Documentation

| Document | Description |
|----------|-------------|
| [INSTALL.md](INSTALL.md) | AWS setup, installation, agents and troubleshooting |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Code layout, development environment, tests and releases |
| [TESTING_GUIDE.md](TESTING_GUIDE.md) | Manual test checklist in a real Home Assistant |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

## How it works

This is a custom integration (not an add-on). It adds one conversation entity per agent; agents are **Conversation agent** sub-entries of a config entry that holds the credentials and region.

```
User message
  → Home Assistant conversation (chat log: history, instructions, selected APIs)
    → BedrockConversationEntity
      → Bedrock ConverseStream (system prompt, messages, tools)
        → tool calls run by Home Assistant (sensitive ones held; the agent asks a fixed question and the turn ends)
        → repeated until the model answers or the tool round limit is hit
  → streamed answer

Next turn: a clear "yes" from the same user and device within 2 minutes runs the held calls; anything else cancels them.
```

## Support

- **Issues**: [GitHub Issues](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent/issues)
- **Repository**: [GitHub](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent)

## License

MIT, see [LICENSE](LICENSE).

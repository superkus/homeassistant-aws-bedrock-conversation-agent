# AWS Bedrock Conversation

Home Assistant conversation agent backed by Amazon Bedrock (Converse API with streaming). Requires **Home Assistant 2026.11.0 or later**.

Full documentation: [repository README](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent) and [INSTALL.md](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent/blob/main/INSTALL.md). Forked from [cronus42/homeassistant-aws-bedrock-conversation-agent](https://github.com/cronus42/homeassistant-aws-bedrock-conversation-agent).

## Setup

1. Create an IAM user with this policy (`ConverseStream` needs `InvokeModelWithResponseStream`):

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

2. **Settings → Devices & services → Add integration → AWS Bedrock Conversation**: choose the region, enter the credentials, then choose a model or inference profile from the metadata returned for that region (custom IDs are allowed) and the device control API. Listing metadata does not prove model access; Bedrock checks access and `bedrock:InvokeModelWithResponseStream` permission on invocation.
3. Expose devices in **Settings → Voice assistants → Expose** and select the agent in a voice assistant.

Setup creates the entry (credentials and region) and a first agent named "AWS Bedrock". **Add agent** on the integration page adds more agents that share the credentials, each with its own settings; **Reconfigure agent** changes one (the integration must be loaded, and each change reloads it). There is no **Configure** screen.

Use **Reconfigure** to change the region or credentials; after a region change, the model you then choose is set for every agent (each can be changed afterwards); with the same region, the agents keep their models. Rejected credentials start a reauthentication flow.

## Agent settings

| Setting | Default |
|---------|---------|
| Model | chosen at setup for the first agent |
| Instructions (template; Home Assistant adds the date and device context) | Home Assistant's default instructions |
| Device control APIs | Device control (Assist) for new installs; "AWS Bedrock Services" (1.x) also available. Both enforce Home Assistant's per-entity **Expose** list; unrestricted admin and third-party APIs are not offered. |
| Spoken confirmation for unlocking, opening doors/garages/gates, scripts and unknown state-changing tools (see INSTALL.md for what is covered and the limitations) | On |
| Maximum tokens | 1024 |
| Temperature | 0.5 |
| Maximum tool rounds per request | 10 |
| Tool calls accepted in one model response | Up to 128 raw calls; homogeneous `HassCallService` calls are collapsed, then at most 10 distinct operations are allowed. Exceeding either limit rejects the response before any tool runs. |
| Prompt caching (turned off automatically if the model does not support it) | On |

## "AWS Bedrock Services" API

The 1.x API, kept for existing users. It adds the exposed devices to the prompt (a snapshot taken for the conversation), offers a read-only `HassGetState` tool for the current state of one exposed entity, and a `HassCallService` tool. Use `target_device` for one entity or `target_devices` for up to 100 exposed entities receiving the same service and settings. If a model emits one call per entity, homogeneous calls are automatically collapsed into one Home Assistant service operation. The tool allows these services:

- **light**: turn_on, turn_off, toggle
- **switch**, **input_boolean**: turn_on, turn_off, toggle
- **fan**: turn_on, turn_off, set_percentage, oscillate, set_direction, set_preset_mode
- **climate**: set_temperature, set_humidity, set_fan_mode, set_hvac_mode, set_preset_mode
- **cover**: open_cover, close_cover, stop_cover, set_cover_position
- **media_player**: turn_on, turn_off, toggle, volume_up, volume_down, volume_set, volume_mute, media_play, media_pause, media_stop, media_next_track, media_previous_track
- **lock**: lock, unlock
- **script**, **scene**: turn_on
- **input_number**: set_value
- **input_select**: select_option
- **timer**: start, pause, cancel, finish

The 1.x `media_player.play_media` service is no longer offered by this API. Media playback requires another selected exposure-bounded API that provides a playback tool.

Other conversation agents using this API are refused `lock.unlock`, opening door/garage/gate covers, `script.turn_on` and scenes that set a lock or such a cover, because only this integration's agent can ask for confirmation.

## Upgrading from 1.x

Back up Home Assistant first, and never delete and re-add the integration. Entries are migrated automatically (log lines `Migrated <entry id> to version 2.1` and `Migrated <entry id> to version 3.1 (agent moved to a sub-entry)`); the agent settings move into one agent sub-entry titled like the entry, and the conversation entity keeps its entity ID. Removed options (Top P, Top K, refresh prompt, conversation memory settings) are dropped, the `<persona>`, `<current_date>` and `<devices>` placeholders are removed from the prompt, and prompts with template syntax are wrapped in `{% raw %}`. To roll back, restore the backup (1.0.37 does not work on Home Assistant 2026.11). Details: [Upgrading from 1.x](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent#upgrading-from-1x).

## Requirements

`boto3>=1.40.0`, installed by Home Assistant.

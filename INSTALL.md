# Installation Guide

## Prerequisites

- **Home Assistant 2026.11.0 or later**
- An **AWS account** with access to Amazon Bedrock in the region you want to use
- **IAM credentials** (access key ID and secret access key) with the policy below
- A model that supports **tool use** if you want device control (for example Anthropic Claude or Amazon Nova)

Upgrading from 1.x? Follow the checklist in [Upgrading from 1.x](README.md#upgrading-from-1x); make a full Home Assistant backup first.

## 1. AWS setup

### Model access

Open the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/) in your region and make sure the models you want are available to your account. Some providers (for example Anthropic) ask you to accept their terms or fill in a short form before first use.

### IAM user and policy

Create an IAM user for Home Assistant and attach this policy:

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

| Permission | Used for |
|------------|----------|
| `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` | Answering. Conversations use `ConverseStream`, which needs `InvokeModelWithResponseStream`; model access and this permission are checked when the agent invokes a model. |
| `bedrock:ListFoundationModels` | Validating credentials for metadata discovery and listing foundation-model metadata. This does not prove model access. |
| `bedrock:ListInferenceProfiles` | Listing inference-profile metadata and the automatic inference profile fallback. Without it only base-model metadata is listed. |

Then create an access key for the user and keep it somewhere safe. A session token is only needed for temporary credentials; when they expire, Home Assistant asks for new ones.

## 2. Install the integration

### HACS (recommended)

1. Open **HACS**.
2. Three-dot menu (top right) → **Custom repositories**.
3. Add `https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent` with type **Integration**.
4. Find **AWS Bedrock Conversation** and click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy `custom_components/bedrock_conversation` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## 3. Add the integration

**Settings → Devices & services → Add integration → AWS Bedrock Conversation**

1. **Region and credentials**: choose the AWS region from the list (taken from the AWS SDK's region data) and enter the access key ID, secret access key and, for temporary credentials only, the session token. The check confirms that AWS accepts the credentials for model metadata discovery in that region; it does not test access to a selected model.
2. **Model**: choose from the active streaming text model and system inference-profile metadata returned for that region. Entries with a geographic prefix (`us.`, `eu.`, `apac.`, `global.`, ...) are inference profiles, which newer models require. You can also type a model ID, inference profile ID or ARN that is not in the list. Bedrock checks actual model access and `bedrock:InvokeModelWithResponseStream` permission when the agent invokes the selected model or profile.
3. **Device control**: choose which APIs the model may use. **Device control** (Home Assistant's built-in Assist API) is selected by default and recommended. Leave it empty for a chat-only assistant.

This creates the integration entry (region and credentials) and a first agent named **AWS Bedrock**. Each combination of access key and region can be added once.

## 4. Agents

Each agent is a **Conversation agent** sub-entry of the integration entry, with its own conversation entity and settings. All agents of an entry share its credentials and region.

- **Add agent** on the integration page adds another agent, for example one with a different model or instructions. You choose a name and the settings below; the model list is the one for the entry's region.
- **Reconfigure agent** (three-dot menu of the agent) changes one agent.

The integration must be loaded to add or change an agent; otherwise the flow stops with "The integration is not loaded. Check the credentials first." Each change reloads the integration. There is no **Configure** (options) screen.

Agent settings:

| Setting | Default | Description |
|---------|---------|-------------|
| Name | AWS Bedrock | Only when adding an agent |
| Model | (chosen at setup for the first agent) | Model or inference profile; the selector shows metadata fetched for the entry's region and allows custom IDs. Listing an item does not prove that it can be invoked. |
| Instructions | Home Assistant's default instructions | What the assistant should do and how it should answer. Home Assistant templates are supported. Home Assistant adds the current date and, with device control enabled, the exposed devices; you do not need placeholders for them. Clear the field to go back to the default. |
| Device control | Device control (Assist) for new installs | Choose one or both exposure-bounded APIs: **Device control** or **AWS Bedrock Services**. Both enforce **Settings → Voice assistants → Expose**. The unrestricted administrative Home Assistant API and arbitrary third-party APIs are not offered. AWS Bedrock Services supplies a snapshot plus bulk-capable `HassCallService` and read-only `HassGetState` (up to 100 entities each); models that emit one call per entity are grouped automatically. It does not supply `media_player.play_media`. Empty means chat only. |
| Spoken confirmation | On | Ask before unlocking, opening doors, garages or gates, running scripts and other sensitive actions (see below) |
| Maximum tokens in a response | 1024 | 64 to 64000. If a reply is cut off by this limit in the middle of a tool call, the assistant says so; raise the value. |
| Temperature | 0.5 | 0 to 1 |
| Maximum tool rounds per request | 10 | 1 to 20. How many times the model may call tools before it must answer |
| Prompt caching | On | Caches the instructions and tool list between requests. Turned off automatically for models that do not support it. |

To change the **region or credentials**, use **Reconfigure** in the entry's menu. If the region changes, you then choose a model from the list for the new region; it is set for **every** agent, because their old models may not exist in the new region. If only the credentials change, every agent keeps its model. Change individual agents afterwards with **Reconfigure agent**. A custom entry title is kept.

### Spoken confirmation for sensitive actions

When the model asks for a sensitive action, the integration holds it back and the agent asks a fixed question built by the integration, naming the action and the devices, for example "I need your confirmation to unlock Front door. Shall I go ahead?" or "Necesito tu confirmación para abrir Garaje. ¿Lo hago?". The turn ends there.

The held action runs only if your **next** message:

- comes from the same user **and** the same device (voice satellite) that made the request,
- arrives within **2 minutes**, and
- is a clear "yes": one of the accepted phrases ("yes", "yeah", "go ahead", "do it", "confirm", "sí", "vale", "adelante", "hazlo", "de acuerdo", "oui", "ja", "sim", ...), alone or followed only by a few words such as "por favor", "hazlo", "ábrela" or "please" ("sí, ábrela", "yes, go ahead"). The lists are `AFFIRMATIVE_PHRASES` and `AFFIRMATIVE_FOLLOW_WORDS` in `const.py`.

Questions ("¿sí?", "yes?"), negations ("no", "sí, espera"), corrections ("yes, the back door instead"), conditionals ("si llueve, …") and fillers such as "ok" or "claro" (speech recognition produces them by accident) are not a yes.

If the request has neither a user nor a device (for example an automation calling `conversation.process`), nobody could confirm it, so sensitive actions are refused instead of asked.

After a yes, exactly the held calls run, each identical call once. Anything else cancels them; declined and expired requests are recorded in the conversation, so the model knows nothing was done. The check is done by the integration, not by the model, so text in device names or media titles cannot confirm an action.

What is held:

- unlocking (`lock.unlock`, `lock.open`, and Assist intents that unlock a lock, such as turn off or toggle),
- opening a cover whose device class is `door`, `garage` or `gate`,
- running scripts (`script.turn_on`, script tools, `intent_script` tools),
- activating a scene that sets a lock or such a cover,
- `homeassistant__call_action` (can call any action; the question names the action and its targets),
- timers that run a command when they end (`HassStartTimer` with a command), because Home Assistant's own agent runs that command later without asking,
- playing a web URL on a media player (searching the media library is not held),
- state-changing tools from integrations it does not know, for example MCP servers. Their "read-only" hints are not trusted, because they come from the remote server.

For each held action, the question names at most five targets and summarizes the rest ("… and 3 more"). The question can contain several action descriptions. A model response may contain at most 128 raw tool calls. The integration collapses homogeneous `HassCallService` and `HassGetState` calls into bulk operations, then allows at most 10 distinct operations. If either limit is exceeded, the entire response is rejected before any tool runs.

Intents that target devices by area, by the name "all", by a blank name or by a name several devices share are checked against every device they can reach.

Other conversation agents that use the **AWS Bedrock Services** API cannot ask for confirmation, so that API refuses them unlocking, opening doors, garages and gates, scripts and sensitive scenes.

Limitations:

- If the voice assistant has **Prefer handling commands locally** on, Home Assistant's built-in agent handles commands it understands, such as "unlock the front door", without asking this agent, so **no confirmation is asked**. Turn that option off in **Settings → Voice assistants → (assistant)** if you rely on the confirmation.
- Garage door or gate relays exposed as a `switch` or `button`, and `valve` entities, are not treated as sensitive. Expose them as a cover with the right device class, or do not expose them.

Turn the setting off in **Reconfigure agent** if you do not want confirmation.

## 5. Expose devices

**Settings → Voice assistants → Expose**: select the entities the assistant may see and control. Only exposed entities are sent to the model.

## 6. Create a voice assistant

1. **Settings → Voice assistants → Add assistant**.
2. Choose an AWS Bedrock agent as **Conversation agent**, set the language and, optionally, speech-to-text and text-to-speech.
3. Save, then try "Turn on the living room light" or "What's the temperature in the bedroom?".

## Troubleshooting

Enable debug logging to see Bedrock errors, inference profile fallbacks and token usage:

```yaml
logger:
  logs:
    custom_components.bedrock_conversation: debug
```

| Message or symptom | Cause | Fix |
|--------------------|-------|-----|
| "AWS rejected these credentials" at setup, or the entry asks you to reauthenticate in **Settings → Devices & services** | Wrong or expired access key, secret or session token | Enter new credentials in the reauthentication dialog, or use **Reconfigure** |
| "AWS accepted these credentials, but model metadata could not be listed" | IAM policy is missing a model-listing permission | Grant `bedrock:ListFoundationModels` and `bedrock:ListInferenceProfiles` as shown above |
| "No models could be listed for this region" | Metadata discovery failed or returned no matching active streaming text models or profiles | Type the model or inference profile ID, or choose another region with **Reconfigure** |
| "Access to this Bedrock model was denied (...)" | The selected model is not accessible in this region, or IAM lacks `bedrock:InvokeModelWithResponseStream` | Check model access in the Bedrock console and the invocation permission in the IAM policy |
| "AWS Bedrock returned an error (ValidationException)" | Often a model parameter, tool schema, or message-history incompatibility | Check the Home Assistant log for the full AWS validation message; choose another model if the integration cannot adapt automatically |
| `temperature` is deprecated for this model | Newer reasoning model does not accept temperature | The integration retries without temperature and remembers that choice automatically |
| "The answer was blocked by a content filter." | Bedrock stopped the response because of a model content policy or configured guardrail | Rephrase the request; if you configured a Bedrock guardrail, review its policy |
| "AWS Bedrock took too long to answer." | A whole request, including all tool rounds, took more than 60 seconds | In **Reconfigure agent**, use a faster model or lower the maximum tokens or tool rounds |
| "Could not reach AWS Bedrock." | No connection to AWS, or AWS sent nothing for 15 seconds (connect timeout 5 seconds, one retry) | Check the network connection to AWS |
| "I could not complete that request." | The model used all its tool rounds | Raise **Maximum tool rounds per request** in **Reconfigure agent**, or rephrase |
| "The model sent an invalid tool request." | The model returned malformed JSON for a tool call | Rephrase the request or choose a model that reliably supports tool use |
| "The model sent too many tool requests in one response (maximum 128). No tools were run." | The model exceeded the absolute raw-call safety limit | Rephrase the request; the rejected batch did not run |
| "The model requested too many distinct tool operations in one response (maximum 10). No tools were run." | More than ten operations remained after homogeneous service calls and state reads were grouped | Ask for fewer different actions at once; the rejected batch did not run |
| An action through **AWS Bedrock Services** fails, or the model reports that it did not finish within 10 seconds | `HassCallService` received a Home Assistant service error, or its blocking service call exceeded the 10-second execution limit | Check the Home Assistant log and try the service directly. The tool returns a failure result to the model instead of reporting success. |
| "The conversation is too long for this model." | Context window full | Start a new conversation |
| "The answer was cut off by the token limit." | The reply hit **Maximum tokens in a response** in the middle of a tool call | Raise **Maximum tokens in a response** in **Reconfigure agent** |
| The entry shows a setup error with only an AWS error code (for example `AccessDeniedException`) | Setup failed; the AWS message can contain your account ID, so only the code is shown. Access denied is logged as an error, other failures at debug level | Check the log (enable debug logging if needed); fix the IAM policy or network |
| Devices are not controlled | Entities not exposed, no device control API selected, or a model without tool use | Expose the entities, select **Device control** in **Reconfigure agent**, choose a tool-capable model |
| The assistant asks for confirmation | The action is sensitive (see above) | Answer "yes", or turn off spoken confirmation in **Reconfigure agent** |
| "The integration is not loaded. Check the credentials first." when adding or changing an agent | The entry failed to set up (credentials, IAM or network) | Fix the entry first (reauthenticate or **Reconfigure**), then retry |
| "Unlock the front door" runs without confirmation | **Prefer handling commands locally** is on, so Home Assistant's built-in agent handled it | Turn that option off in the voice assistant |
| Another agent says an action "needs confirmation, which this assistant cannot ask for" | That agent uses the **AWS Bedrock Services** API, which refuses sensitive actions to agents that cannot ask | Use this integration's agent for those actions |
| Integration missing after install | Home Assistant not restarted, or older than 2026.11.0 | Restart; check **Settings → System → Logs** |

AWS error details can contain your account ID, so they are written to the log but not spoken.

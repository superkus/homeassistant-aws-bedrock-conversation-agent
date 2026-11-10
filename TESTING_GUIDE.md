# Manual Testing Guide

A checklist for trying a build in a real Home Assistant 2026.11.0+ instance with real AWS credentials. The automated tests are described in [tests/README.md](tests/README.md).

## Preparation

1. Make a Home Assistant backup if the instance matters to you.
2. Copy `custom_components/bedrock_conversation` into `config/custom_components/` and restart Home Assistant.
3. Enable debug logging and restart again:

   ```yaml
   logger:
     logs:
       custom_components.bedrock_conversation: debug
   ```

4. Expose a few test entities in **Settings → Voice assistants → Expose**: a light, a switch, a lock, a garage door or gate cover, and a script.

## Setup and configuration

- [ ] Add the integration: the region list appears, wrong credentials show "AWS rejected these credentials", valid ones lead to the model list for that region.
- [ ] The model list shows inference profiles first, then base models; typing a custom ID is accepted.
- [ ] After setup the entry has one **Conversation agent** named "AWS Bedrock"; there is no **Configure** button.
- [ ] **Add agent** with another model and name: a second conversation entity appears and answers; both share the credentials.
- [ ] **Reconfigure agent** shows that agent's settings and saves them; the integration reloads and the other agent is unchanged.
- [ ] **Reconfigure** to another region: the model list is fetched for the new region, the chosen model is set on every agent (with the same region, no model is asked and every agent keeps its model), and an automatic title changes to `AWS Bedrock (<region>)` (a custom title is kept).
- [ ] With the entry not loaded (for example wrong credentials), **Add agent** stops with "The integration is not loaded. Check the credentials first."
- [ ] With a 1.x entry (restore a 1.x backup on a test instance, then update): the log shows `Migrated <entry id> to version 2.1` and then `Migrated <entry id> to version 3.1 (agent moved to a sub-entry)`; the entry has one agent titled like the entry, keeps its conversation entity ID and a custom title, voice assistants using it still answer, and the removed options and placeholders are gone. A prompt containing `{{ … }}` is kept literally (wrapped in `{% raw %}`).
- [ ] **Reconfigure agent**, clear **Instructions** and save: Home Assistant's default instructions are back.

## Conversation

Use **Device control** (Assist API) first, then repeat the device tests with **AWS Bedrock Services**.

- [ ] "Hello, what can you do?": the answer streams in and nothing is changed.
- [ ] "Turn on the kitchen light", "Dim the living room light to 30 %", "Turn off the switch".
- [ ] "Which lights are on?": answered from the exposed devices.
- [ ] Follow-up: "And turn it off again": the agent remembers the previous turn.
- [ ] A non-existent or unexposed device: a clear answer, nothing changes.
- [ ] Device control empty in the agent settings: chat-only answers, no device actions.
- [ ] With **AWS Bedrock Services**: change a light by hand, then ask "Is the kitchen light on?": the answer uses `HassGetState` and is current.
- [ ] With **Prefer handling commands locally** on: "Turn on the kitchen light" (handled locally), then a follow-up the local agent cannot handle: the Bedrock agent answers without an error.
- [ ] An announcement that starts a conversation (`assist_satellite.start_conversation`): the reply is answered without an error.

## Spoken confirmation

Turn off **Prefer handling commands locally** in the voice assistant first; otherwise Home Assistant's built-in agent handles "unlock the front door" itself and no confirmation is asked.

- [ ] "Unlock the front door": the agent says "I need your confirmation to unlock Front door. Shall I go ahead?" (in Spanish: "Necesito tu confirmación para abrir la cerradura …. ¿Lo hago?") and the lock stays locked.
- [ ] Answer "yes" (or "sí, ábrela"): the lock unlocks.
- [ ] Repeat and answer "no", "yes?", "ok", "yes, the back door instead" or something else: nothing happens, and asking "did you unlock it?" gets "no".
- [ ] "In 1 minute, unlock the front door" (a timer with a command): confirmation is asked before the timer is set.
- [ ] Answer "yes" from another satellite or user, or after more than 2 minutes: nothing happens.
- [ ] "Open the garage door", "Run the <script>" and a scene that includes a lock: confirmation is asked.
- [ ] "Close the garage door" and "Lock the front door": run without confirmation.
- [ ] With **Spoken confirmation** off: sensitive actions run directly.
- [ ] Another agent (for example a second LLM integration) using **AWS Bedrock Services**: "unlock" is refused.

## Month-to-date account cost

- [ ] Without enabling the sensor, no cost entity exists and conversations work with the original Bedrock-only IAM policy.
- [ ] Add the separate optional statement allowing `ce:GetCostAndUsage` on `Resource: "*"`, then use entry-level **Reconfigure** to enable the sensor: an entry-level monetary sensor appears in AWS's returned currency. Confirm that this account-wide action cannot be restricted to individual resources.
- [ ] Check its attributes: current UTC billing period, `UnblendedCost`, account-wide/all-regions scope, AWS estimated flag and last update.
- [ ] Run Home Assistant's **Update entity** action: the sensor refreshes on demand.
- [ ] Remove `ce:GetCostAndUsage` and refresh: only the cost sensor becomes unavailable; conversation agents still answer. Restore the permission afterwards.
- [ ] At the next UTC month boundary, `last_reset` moves to the first of the new month.

Remember that Cost Explorer data is delayed and can be revised; compare it with the AWS Billing console rather than expecting an immediate per-request increase. Billing adjustments, refunds and credits can reduce the value or make it negative. With multiple enabled entries, confirm that each entry has an entity and that entries sharing an access-key ID show the shared result; never sum duplicate account-wide views from credentials belonging to the same AWS account.

## Errors

- [ ] Remove `bedrock:InvokeModelWithResponseStream` from the IAM policy: "Access to this Bedrock model was denied (AccessDeniedException)".
- [ ] Deactivate the access key: the answer says the credentials were rejected and Home Assistant asks to reauthenticate.
- [ ] A model without tool use and device control enabled: "AWS Bedrock returned an error (ValidationException)".
- [ ] Set **Maximum tool rounds** to 1 and ask for several actions at once: may end with "I could not complete that request."
- [ ] Set **Maximum tokens in a response** to 64 and ask for several actions at once: may end with "The answer was cut off by the token limit. Increase …".

## What to look for in the log

- `Model <id> needs an inference profile; using <profile>`: the fallback worked.
- `Model <id> does not support prompt caching; disabling it`: caching was turned off for that model.
- `Bedrock usage: {...}`: token usage per request (debug).
- `Access denied by AWS Bedrock`, `AWS rejected the credentials`, `AWS Bedrock error`: the full AWS error behind a spoken message.

## Reporting issues

Open a [GitHub issue](https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent/issues) with the Home Assistant and integration versions, region, model ID, the exact request, and the debug log. Remove account IDs and keys from the log first.

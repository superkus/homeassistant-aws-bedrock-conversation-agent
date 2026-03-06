# Installation Guide

## Prerequisites

- **Home Assistant** 2024.12.0 or later
- **AWS Account** with access to Amazon Bedrock
- **AWS IAM credentials** (Access Key ID and Secret Access Key)
- **Model access** enabled in the AWS Bedrock console for your chosen models

## 1. AWS Setup

### Enable Bedrock Models

1. Log into the [AWS Console](https://console.aws.amazon.com/)
2. Navigate to **Amazon Bedrock**
3. Select your preferred region (e.g. `us-east-1`, `eu-west-3`)
4. Go to **Model access** in the left sidebar
5. Request access to the models you want to use (Claude, Nova, Llama, etc.)
6. Wait for approval (usually instant for most models)

### Create an IAM User

1. Go to the [IAM Console](https://console.aws.amazon.com/iam/)
2. Create a new user for Home Assistant
3. Attach a policy with these permissions:

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

> `ListFoundationModels` and `ListInferenceProfiles` are needed so the integration can dynamically populate the model dropdown in the configuration UI. `InvokeModel` is required for the actual conversation calls.

4. Create access keys and save them securely

## 2. Install the Integration

### Method 1: HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Go to **Integrations**
3. Click the three-dot menu (top right) → **Custom repositories**
4. Add this repository URL:
   ```
   https://github.com/superkus/homeassistant-aws-bedrock-conversation-agent
   ```
5. Set category to **Integration**
6. Find "AWS Bedrock Conversation" and click **Install**
7. **Restart Home Assistant**

### Method 2: Manual

1. Copy the `custom_components/bedrock_conversation` folder into your Home Assistant `config/custom_components/` directory
2. **Restart Home Assistant**

## 3. Add the Integration

1. Go to **Settings → Devices & Services**
2. Click **+ Add Integration**
3. Search for **AWS Bedrock Conversation**
4. Enter your AWS credentials:
   - **AWS Region** — select from the dropdown (e.g. US East N. Virginia, Europe Paris)
   - **AWS Access Key ID**
   - **AWS Secret Access Key**
   - **AWS Session Token** — optional, only needed for temporary credentials
5. Click **Submit**

## 4. Configure Options

After adding the integration, click **Configure** to access the two-step options flow:

### Step 1 — Select Region

Choose the AWS region where your Bedrock models are available. The model list in the next step is fetched dynamically for this region.

### Step 2 — Model and Parameters

- **Bedrock Model** — dropdown populated with models available in your selected region (foundation models and inference profiles)
- **System Prompt Template** — Jinja2 template with `<persona>`, `<current_date>`, and `<devices>` placeholders
- **Max Tokens** — maximum response length (default: 4096)
- **Temperature** — randomness, 0–1 (default: 1.0)
- **Top P** — nucleus sampling, 0–1 (default: 0.999)
- **Top K** — token selection limit, 1–500 (default: 250)
- **Refresh prompt each turn** — regenerate the system prompt with current device states every message (default: on)
- **Remember conversation** — keep chat history across turns (default: on)
- **Interactions to remember** — number of past exchanges to include (default: 10)
- **Max tool call iterations** — limit on tool calling loops per message (default: 5)
- **Home Assistant LLM API** — which tool API to use for device control (default: Bedrock Services)

## 5. Expose Devices

For the assistant to control your devices, they must be exposed:

1. Go to **Settings → Voice Assistants → Expose**
2. Select the entities you want the assistant to control
3. Toggle **Expose** for each entity

Only exposed entities appear in the system prompt sent to the model.

## 6. Create a Voice Assistant

1. Go to **Settings → Voice Assistants**
2. Click **Add Assistant**
3. Configure:
   - **Name**: e.g. "Bedrock Assistant"
   - **Conversation agent**: AWS Bedrock Conversation
   - **Language**: your preference
   - Optionally configure STT/TTS
4. Click **Create**

## 7. Test

1. Open the Home Assistant UI
2. Click the conversation icon or microphone
3. Select your Bedrock assistant
4. Try: "Turn on the living room light" or "What's the temperature?"

## Troubleshooting

### Integration doesn't appear after install

- Make sure you **restarted Home Assistant** after copying the files or installing via HACS
- Check **Settings → System → Logs** for import errors

### "Invalid credentials" error

- Double-check your AWS Access Key ID and Secret Access Key
- Ensure the IAM user has the required Bedrock permissions
- If using session tokens, make sure they haven't expired

### "Access denied" error

- The IAM user needs `bedrock:InvokeModel` permission
- Verify model access is enabled in the Bedrock console for your region
- Some models require explicit access approval

### "Model not found" error

- The model may not be available in your selected region
- Go to **Configure** and change the region — the model list will update automatically
- The integration automatically resolves inference profiles, but the model must exist in the region

### Devices not being controlled

- Ensure entities are **exposed** in Settings → Voice Assistants → Expose
- Check that the LLM API is set to "AWS Bedrock Services" in the integration options
- Verify `max_tool_call_iterations` is greater than 0

### Slow or timed-out responses

- The integration uses a 90-second timeout for API calls
- Use a faster model like Claude 3 Haiku or Nova Lite for quicker responses
- Reduce `max_tokens` to limit response length
- Check your network connectivity to AWS

## Supported Regions

The integration supports all AWS regions where Bedrock is available, including:

| Region | Location |
|--------|----------|
| us-east-1 | US East (N. Virginia) |
| us-west-2 | US West (Oregon) |
| us-east-2 | US East (Ohio) |
| eu-central-1 | Europe (Frankfurt) |
| eu-west-1 | Europe (Ireland) |
| eu-west-2 | Europe (London) |
| eu-west-3 | Europe (Paris) |
| ap-southeast-1 | Asia Pacific (Singapore) |
| ap-northeast-1 | Asia Pacific (Tokyo) |
| ap-south-1 | Asia Pacific (Mumbai) |
| ap-southeast-2 | Asia Pacific (Sydney) |
| ca-central-1 | Canada (Central) |
| sa-east-1 | South America (São Paulo) |

Model availability varies by region. The integration fetches the actual available models when you open the configuration.

# Brand asset provenance

## Upstream source

- Publisher and owner: Amazon Web Services (AWS)
- Asset: Amazon Bedrock AWS Architecture Icon
- Official source page: <https://aws.amazon.com/architecture/icons/>
- Icon package: `Icon-package_07312026.zip`
- Package date: 2026-07-31
- Download URL: <https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/architecture-icon-release/Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip>
- Package SHA-256: `d2d166c453526471749d520e0db022c459abef759d2946cf2dd1d1c992dc6526`
- Source member: `Architecture-Service-Icons_07312026/Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock_64@5x.png`
- Source member properties: PNG, 400 × 400, 8-bit RGB
- Extracted source SHA-256: `9d63380f96ee2fb150a3425f52df0b765384acd669cc5a95d97fb5156c3c7f37`

The package was downloaded from the URL above and retained through conversion and review. It is not committed because it contains the complete 16 MB AWS icon set; the immutable URL, package hash, member path and member hash make the conversion reproducible.

## Conversion record

The source member was extracted and resized on macOS 27.2 with `sips-316`:

```sh
unzip -p Icon-package_07312026.zip \
  'Architecture-Service-Icons_07312026/Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock_64@5x.png' \
  > amazon-bedrock-official.png

sips -z 256 256 amazon-bedrock-official.png \
  --out custom_components/bedrock_conversation/brand/icon.png
sips -z 512 512 amazon-bedrock-official.png \
  --out custom_components/bedrock_conversation/brand/icon@2x.png
```

## Output verification

| File | Format and dimensions | SHA-256 |
|------|-----------------------|---------|
| `icon.png` | PNG, 256 × 256, 8-bit RGB | `effc46a856d54c2071a982d6193d74508a229c7f133e7373cb435c2a2fe0858f` |
| `icon@2x.png` | PNG, 512 × 512, 8-bit RGB | `1a2c4610f92b6685c822f1ac4af017ab613299bd3d6a069c4ef26d628c31feff` |

Verify the committed outputs:

```sh
shasum -a 256 custom_components/bedrock_conversation/brand/icon.png \
  custom_components/bedrock_conversation/brand/icon@2x.png
```

## Rights and use

AWS owns the Amazon Bedrock name and architecture icon. The repository's MIT License does not apply to these two PNG files. Their use is subject to the official [AWS Architecture Icons page](https://aws.amazon.com/architecture/icons/), [AWS Trademark Guidelines](https://aws.amazon.com/trademark-guidelines/), and [AWS Site Terms](https://aws.amazon.com/terms/). The architecture-icons page permits customers and partners to use its icon assets in technical materials. This project uses the mark only to identify the AWS service it integrates with and claims no AWS affiliation or endorsement.

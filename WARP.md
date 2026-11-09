# WARP.md

Guidance for AI coding agents (Warp, Goose via `.goosehints`) working in this repository.

The developer documentation is [DEVELOPMENT.md](DEVELOPMENT.md): code layout, how a conversation turn works, config entry format and migrations, environment and releases. User-facing behaviour is in [README.md](README.md) and [INSTALL.md](INSTALL.md).

Essentials:

- Home Assistant custom integration in `custom_components/bedrock_conversation`, version 2.x, for Home Assistant 2026.11.0+ on Python 3.14.
- `make deps`, `make test`, `make lint`, `make format` (all use `.venv`, created with `python3.14`).
- `homeassistant` and `pytest-homeassistant` are pinned to the same release in `requirements-test.txt`; keep them in step.
- Tests fake AWS (`FakeBedrock` in `tests/conftest.py`); never call real AWS from tests.
- Blocking boto3 calls run in the executor; user-facing text goes in `strings.json` and `translations/`.
- When behaviour, settings or permissions change, update README.md, INSTALL.md, DEVELOPMENT.md and CHANGELOG.md in the same change.

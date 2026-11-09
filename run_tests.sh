#!/bin/bash
# Run the test suite (same as `make test`, without creating .venv).
set -e
pip install -r requirements-test.txt
pytest tests/ -v --cov=custom_components.bedrock_conversation --cov-report=term-missing

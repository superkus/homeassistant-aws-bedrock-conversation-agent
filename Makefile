.PHONY: venv deps test lint format clean version release

PYTHON ?= python3.14

venv:
	$(PYTHON) -m venv .venv

deps: venv
	.venv/bin/pip install -r requirements-dev.txt

test: deps
	.venv/bin/pytest tests/ --cov=custom_components.bedrock_conversation --cov-report=term-missing

lint: deps
	.venv/bin/ruff check custom_components tests
	.venv/bin/ruff format --check custom_components tests

format: deps
	.venv/bin/ruff check --fix custom_components tests
	.venv/bin/ruff format custom_components tests

clean:
	rm -rf .venv htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

version:
	@python3 -c "import json; print(json.load(open('custom_components/bedrock_conversation/manifest.json'))['version'])"

# Tags the manifest version and creates a GitHub release (needs a clean tree).
release: test lint
	@VERSION=$$(python3 -c "import json; print(json.load(open('custom_components/bedrock_conversation/manifest.json'))['version'])") && \
	if ! git diff-index --quiet HEAD -- || [ -n "$$(git ls-files --others --exclude-standard)" ]; then echo "Error: uncommitted changes"; exit 1; fi && \
	if git tag | grep -qx "v$$VERSION"; then echo "Error: tag v$$VERSION exists"; exit 1; fi && \
	git tag -a "v$$VERSION" -m "Release v$$VERSION" && \
	git push origin "v$$VERSION" && \
	gh release create "v$$VERSION" --title "v$$VERSION" --notes-file CHANGELOG.md

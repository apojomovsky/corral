.PHONY: test check setup-hooks

test:
	uv run python -m unittest

check: test

setup-hooks:
	git config core.hooksPath .githooks

.PHONY: help bootstrap deps sync setup-hooks test test-matrix format lint typecheck audit check pre-pr-check install

PYTHONS ?= 3.11 3.14
UNITTEST = python -m unittest discover -t . -s tests

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 16

bootstrap: ## First run: needs only uv; sets up Python, the venv, the hooks, then checks the host
	@command -v uv >/dev/null || { \
	  echo "bootstrap: uv is missing. Install it, open a new shell, and re-run make bootstrap:"; \
	  echo; echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	uv sync --locked
	@$(MAKE) --no-print-directory setup-hooks
	@$(MAKE) --no-print-directory deps

deps: ## Check the host programs in system-deps.toml; print how to install what is missing
	@uv run --locked corral deps

sync: ## Install the locked environment into .venv
	uv sync --locked

setup-hooks: ## Point git at .githooks (commit-msg, pre-commit, pre-push)
	git config core.hooksPath .githooks
	@echo "git hooks installed (commit-msg, pre-commit, pre-push)"

test: ## Run the unittest suite on the pinned Python
	uv run --locked $(UNITTEST) -v

test-matrix: ## Run the suite on every supported Python (PYTHONS="3.11 3.14")
	@for v in $(PYTHONS); do echo "== Python $$v"; \
	  uv run --locked --isolated --python $$v $(UNITTEST) || exit 1; done

format: ## Format the code (ruff, through uv)
	uv format --preview-features format-command

lint: ## Fail when the code is not formatted
	uv format --check --preview-features format-command

typecheck: ## Type-check the project (ty, through uv)
	uv check --locked --preview-features check-command

audit: ## Check the locked dependencies for known vulnerabilities
	uv audit --locked

check: lint typecheck test-matrix ## Everything CI runs

pre-pr-check: ## Takeoff ritual: PR_BODY=<file> required, TEST=1 also runs the suite
	@uv run --script scripts/pre_pr_check.py --body "$(PR_BODY)" $(if $(filter 1,$(TEST)),--test,)

install: ## Install the corral CLI for your user from this checkout (uv tool)
	uv tool install --reinstall .

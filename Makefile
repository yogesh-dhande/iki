# Common tasks across the monorepo. `make check` runs exactly what CI runs.
.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PNPM ?= pnpm

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: setup
setup: ## Install all toolchains' dependencies from the lockfiles
	uv sync --locked
	$(PNPM) install --frozen-lockfile
	cd worker && cargo fetch --locked

.PHONY: check
check: check-workflows check-python check-web check-rust ## Run every CI check locally

.PHONY: check-workflows
check-workflows: ## Lint GitHub workflows (actionlint, zizmor)
	uvx --from actionlint-py==1.7.12.25 actionlint
	uvx zizmor==1.32.0 --persona=auditor .github/

.PHONY: check-python
check-python: ## Ruff, mypy (strict) and pytest
	uv run ruff check
	uv run ruff format --check
	uv run mypy
	uv run pytest

.PHONY: check-web
check-web: ## ESLint, Prettier, tsc and Vitest
	$(PNPM) lint
	$(PNPM) format:check
	$(PNPM) typecheck
	$(PNPM) test
	$(PNPM) build

.PHONY: check-rust
check-rust: ## rustfmt, clippy (warnings denied) and cargo test
	cd worker && cargo fmt --all --check
	cd worker && cargo clippy --locked --all-targets -- -D warnings
	cd worker && cargo test --locked --all-targets

.PHONY: fmt
fmt: ## Format all code
	uv run ruff check --fix
	uv run ruff format
	$(PNPM) format
	cd worker && cargo fmt --all

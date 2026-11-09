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
check: check-workflows check-chart check-deps check-python check-web check-rust ## Run every CI check locally (except dev-ci)

.PHONY: check-workflows
check-workflows: ## Lint GitHub workflows (actionlint, zizmor)
	uvx --from actionlint-py==1.7.12.25 actionlint
	uvx zizmor==1.32.0 --persona=auditor .github/

.PHONY: check-deps
check-deps: ## Vulnerabilities and licenses in every lockfile (osv-scanner, cargo-deny)
	scripts/supply-chain/check-deps.sh

.PHONY: check-image
check-image: ## Build the server image and scan it (grype)
	docker build -f server/Dockerfile -t iki-server:scan .
	grype iki-server:scan --config .grype.yaml

.PHONY: check-chart
check-chart: ## Helm lint (strict) with the minimum required values
	helm lint --strict deploy/helm/iki \
	  --set environment=ci --set publicUrl=https://iki.example.com \
	  --set 'allowedHosts={iki.example.com}' --set secretKey.existingSecret=s \
	  --set encryptionKeys.existingSecret=e --set database.existingSecret=d
	helm lint --strict deploy/helm/iki -f deploy/dev/values-dev.yaml

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

# --- Local Kubernetes (kind + Tilt) ---------------------------------------------

.PHONY: dev-up
dev-up: ## Create the kind cluster and start Tilt (interactive)
	deploy/dev/up.sh
	tilt up

.PHONY: dev-ci
dev-ci: ## Bring the dev cluster up headless and run the end-to-end checks
	deploy/dev/up.sh
	tilt ci --timeout 20m
	deploy/dev/e2e.sh

.PHONY: dev-sso
dev-sso: ## Set up sign-in on the dev cluster through the wizard (bob becomes admin)
	uv run python deploy/dev/sso.py

.PHONY: dev-e2e
dev-e2e: ## Run the end-to-end checks against a running dev cluster
	deploy/dev/e2e.sh

.PHONY: dev-creds
dev-creds: ## Print the generated dev credentials
	@deploy/dev/creds.sh

.PHONY: dev-down
dev-down: ## Delete the kind cluster and everything in it
	-tilt down
	kind delete cluster --name iki

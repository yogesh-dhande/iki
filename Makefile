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
	uv run playwright install chromium firefox webkit

.PHONY: check
# check-web builds the web app, which the browser tests in check-python load.
check: check-workflows check-chart check-infra check-deps check-web check-python check-rust ## Run every CI check locally (except dev-ci)

.PHONY: check-workflows
check-workflows: ## Lint GitHub workflows (actionlint, zizmor)
	uvx --from actionlint-py==1.7.12.25 actionlint
	uvx --from actionlint-py==1.7.12.25 actionlint plugin-template/.github/workflows/*.yml \
	  deploy/config-template/.github/workflows/*.yml
	uvx zizmor==1.32.0 --persona=auditor .github/ plugin-template/.github/ \
	  deploy/config-template/.github/

.PHONY: check-deps
check-deps: ## Vulnerabilities and licenses in every lockfile (osv-scanner, cargo-deny)
	scripts/supply-chain/check-deps.sh

.PHONY: check-image
check-image: ## Build the server image and scan it (grype)
	docker build -f server/Dockerfile -t iki-server:scan .
	grype iki-server:scan --config .grype.yaml

#: For promtool, to test the alerts: the version otel-lgtm (deploy/dev) runs.
PROMETHEUS_IMAGE := prom/prometheus:v3.16.0@sha256:e04987641b57704f408e31f6373ba4138ecf27cb4a165d24aabb6c38fddd0f06

.PHONY: check-chart
check-chart: ## Helm lint (strict) with the minimum required values
	helm lint --strict deploy/helm/iki \
	  --set environment=ci --set publicUrl=https://iki.example.com \
	  --set 'allowedHosts={iki.example.com}' --set secretKey.existingSecret=s \
	  --set encryptionKeys.existingSecret=e --set database.existingSecret=d \
	  --set database.migrator.existingSecret=m
	helm lint --strict deploy/helm/iki -f deploy/dev/values-dev.yaml
	@# Iki's alerts (ADR 0098): valid, and each fires when and how its tests say.
	docker run --rm --network none --user "$$(id -u):$$(id -g)" \
	  -v "$(CURDIR)/deploy/helm/iki/files:/rules:ro" -w /rules --entrypoint promtool \
	  $(PROMETHEUS_IMAGE) test rules alerts.test.yaml

#: The AWS (ADR 0097) and Google Cloud (ADR 0107) modules, each checked on its own as the
#: root a team copies it into, and the bootstrap and production roots that put them together.
INFRA_CLOUDS := deploy/aws deploy/gcp
INFRA_MODULES := $(foreach cloud,$(INFRA_CLOUDS),$(wildcard $(cloud)/modules/*) $(cloud)/bootstrap $(cloud)/production)
CHECKOV_VERSION := 3.4.5

.PHONY: check-infra
check-infra: ## OpenTofu: fmt, validate and tests (mocked providers) for the AWS and Google Cloud modules and roots; Checkov's checks
	tofu fmt -recursive -check -diff $(INFRA_CLOUDS)
	@# Secrets' values never enter OpenTofu's state: no module writes one (ADR 0097, 0107).
	@# On Google Cloud, an HMAC key's or service account key's secret, or a database user's
	@# password, would be in the state too.
	@! grep -rnE 'resource "(aws_secretsmanager_secret_version|google_secret_manager_secret_version|google_storage_hmac_key|google_service_account_key)"|resource "random_(password|bytes|string)"|^\s*(root_)?password(_wo)?\s*=' \
	  --include='*.tf' $(INFRA_CLOUDS) \
	  || (echo "A module writes a secret's value, which would put it in OpenTofu's state." && false)
	@set -e; for module in $(INFRA_MODULES); do \
	  echo "== $$module"; \
	  tofu -chdir=$$module init -input=false -backend=false -lockfile=readonly -no-color >/dev/null; \
	  tofu -chdir=$$module validate -no-color; \
	  tofu -chdir=$$module test -no-color; \
	done
	@# One run per cloud: given several --directory flags, Checkov 3.4.5 scans the first
	@# one once per flag and never the rest.
	@set -e; for cloud in $(INFRA_CLOUDS); do \
	  echo "== checkov $$cloud"; \
	  uvx checkov==$(CHECKOV_VERSION) --directory $$cloud --framework terraform --quiet --compact --skip-path tests; \
	done

#: Tests run in parallel: workers take a file's tests in runs, and idle ones take over the rest.
PYTEST_PARALLEL ?= -n auto --dist worksteal

.PHONY: check-python
check-python: ## Ruff, mypy (strict) and pytest
	uv run ruff check
	uv run ruff format --check
	uv run mypy
	uv run pytest $(PYTEST_PARALLEL)

.PHONY: core
core: ## Regenerate the core plugin's app in the server, and make its migrations
	uv run python scripts/build_core.py
	cd server && uv run python -m iki_server.plugins.makemigrations core

.PHONY: client
client: ## Regenerate the API clients (TypeScript, and the Python SDK's types) from the OpenAPI document
	scripts/openapi.sh > web/packages/sdk/openapi.json
	$(PNPM) --filter @ikisci/sdk generate
	uv run python -m iki_codegen.sdk web/packages/sdk/openapi.json > sdk/python/src/iki/schema.py

.PHONY: protocol
protocol: ## Regenerate the worker's protocol types from the server's schemas and API
	scripts/openapi.sh --worker > worker/crates/iki-protocol/worker-openapi.json
	uv run python -m iki_codegen.protocol worker/crates/iki-protocol/worker-openapi.json \
	  > worker/crates/iki-protocol/src/generated.rs

.PHONY: check-web
check-web: ## ESLint, Prettier, tsc, Vitest, the generated client, and the build
	$(PNPM) lint
	$(PNPM) format:check
	$(PNPM) typecheck
	$(PNPM) test
	scripts/check-client.sh
	$(PNPM) build

.PHONY: web
web: ## Build the web app into the server's static files (server/src/iki_server/web/static/app)
	$(PNPM) --filter @ikisci/app build

.PHONY: web-dev
web-dev: ## Serve the web app with instant reloads at http://localhost:5173 (needs the dev cluster)
	$(PNPM) --filter @ikisci/app dev

.PHONY: check-scale
check-scale: check-scale-import check-scale-grid ## Load tests: an import of 10M entities, and the grid at 100M rows

.PHONY: check-scale-import
check-scale-import: ## Load test: import 10M entities, interrupted halfway (IKI_SCALE_RECORDS sets how many)
	uv run pytest server/tests/test_scale_import.py --scale -s

.PHONY: check-scale-grid
check-scale-grid: ## Load test: the grid at 100M rows (IKI_SCALE_GRID_ROWS sets how many)
	uv run pytest server/tests/test_scale_grid.py --scale -s

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
dev-ci: ## Bring the dev cluster up headless; run the end-to-end checks, then the restore test
	deploy/dev/up.sh
	tilt ci --timeout 20m
	deploy/dev/e2e.sh
	deploy/dev/restore-test.sh

.PHONY: dev-sso
dev-sso: ## Set up sign-in on the dev cluster through the wizard (bob becomes admin)
	uv run python deploy/dev/sso.py

.PHONY: dev-e2e
dev-e2e: ## Run the end-to-end checks against a running dev cluster
	deploy/dev/e2e.sh

.PHONY: dev-restore-test
dev-restore-test: ## Back the dev database up, restore it into a new one, and check it (ADR 0099)
	deploy/dev/restore-test.sh

.PHONY: dev-creds
dev-creds: ## Print the generated dev credentials
	@deploy/dev/creds.sh

.PHONY: dev-down
dev-down: ## Delete the kind cluster and everything in it
	-tilt down
	kind delete cluster --name iki

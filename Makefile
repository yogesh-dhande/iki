# Common tasks across the monorepo. `make check` runs exactly what CI runs.
.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PNPM ?= pnpm

ifneq ($(filter check check-python,$(MAKECMDGOALS)),)
ifeq ($(IKI_SKIP_BROWSER_TESTS),1)
$(error IKI_SKIP_BROWSER_TESTS=1 is refused by the full gate. For a partial local run use IKI_SKIP_BROWSER_TESTS=1 uv run pytest)
endif
endif

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: setup
setup: test-browsers ## Install all toolchains' dependencies from the lockfiles
	uv sync --locked
	$(PNPM) install --frozen-lockfile
	cd worker && cargo fetch --locked

.PHONY: test-browsers
test-browsers: ## Install the browser revisions required by the locked Playwright
	uv run --locked playwright install chromium firefox webkit

.PHONY: hooks
hooks: ## Install the pre-commit hook for this clone and its worktrees
	scripts/git-hooks/install

.PHONY: check
# check-web builds the web app, which the browser tests in check-python load.
check: check-workflows check-chart check-infra check-deps check-web check-python check-rust ## Run every CI check locally (except dev-ci)

.PHONY: check-workflows
check-workflows: ## Lint GitHub workflows (actionlint, zizmor)
	uvx --from actionlint-py==1.7.12.25 actionlint
	shopt -s nullglob; workflows=({plugin-template,deploy/config-template,examples/plugins/*}/.github/workflows/*.{yml,yaml}); \
	  uvx --from actionlint-py==1.7.12.25 actionlint "$${workflows[@]}"
	shopt -s nullglob; workflows=(.github/ plugin-template/.github/ deploy/config-template/.github/ examples/plugins/*/.github/); \
	  uvx zizmor==1.32.0 --persona=auditor $(if $(GITHUB_ACTIONS),--format=github) "$${workflows[@]}"

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
	helm lint --strict deploy/helm/iki -f deploy/dev/values-dev.yaml -f deploy/dev/fixtures/values.yaml
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

.PHONY: test-wasm
test-wasm: ## Build the shared WASM and the plugin modules declared in this checkout
	scripts/build-wasm.sh
	uv run --locked python scripts/ci/check_test_prerequisites.py wasm

.PHONY: check-python
check-python: test-browsers test-wasm ## Ruff, mypy (strict), workspace imports on Python 3.12 and pytest
	uv run --locked python scripts/ci/check_test_prerequisites.py web
	scripts/check-imports-py312 --all
	uv run ruff check
	uv run ruff format --check
	uv run mypy
	@# A surrounding VM gate can collect traces for the whole command in its own directory.
	IKI_GATE=1 $(if $(IKI_BROWSER_ARTIFACTS),,scripts/ci/with-browser-traces.sh) uv run pytest $(PYTEST_PARALLEL)
	IKI_WORKER="$${IKI_WORKER:-$${IKI_WORKER_BIN:-$(CURDIR)/worker/target/debug/iki-worker}}" \
	  uv run --locked python scripts/ci/check_plugin_examples.py

.PHONY: core
core: ## Regenerate the core plugin's app and migrations and the file format reference
	uv run python scripts/build_core.py
	cd server && uv run python -m iki_server.plugins.makemigrations iki__core
	uv run python scripts/build_format_reference.py

.PHONY: client
client: ## Regenerate the API clients (TypeScript, and the Python SDK's types) from the OpenAPI document
	scripts/openapi.sh > web/packages/sdk/openapi.json
	$(PNPM) --filter @ikisci/sdk generate
	uv run python -m iki_codegen.sdk web/packages/sdk/openapi.json > sdk/python/src/iki/schema.py

.PHONY: first-party
first-party: ## List each first-party plugin's current tree (ADR 0114) and the HRID prefixes Iki keeps (ADR 0033)
	uv run python scripts/first_party_trees.py

.PHONY: protocol
protocol: ## Regenerate the worker's protocol types from the server's schemas and API
	scripts/openapi.sh --worker > worker/crates/iki-protocol/worker-openapi.json
	uv run python -m iki_codegen.protocol worker/crates/iki-protocol/worker-openapi.json \
	  > worker/crates/iki-protocol/src/generated.rs

.PHONY: check-web
# The parsers first, as CI builds them: the expression tests and the app's own load them.
check-web: ## The parsers (iki-wasm), ESLint, Prettier, tsc, Vitest, the generated client, and the build
	$(PNPM) --filter @ikisci/app build:wasm
	$(PNPM) lint
	$(PNPM) format:check
	$(PNPM) typecheck
	$(PNPM) test
	scripts/check-client.sh
	$(PNPM) build

.PHONY: web
web: ## Build the server's web app and the standalone package stories
	$(PNPM) --filter @ikisci/app --filter @ikisci/component --filter @ikisci/science --filter @ikisci/ui build

.PHONY: web-dev
web-dev: ## Serve the web app with instant reloads at http://localhost:5173 (needs the dev cluster)
	$(PNPM) --filter @ikisci/app dev

.PHONY: check-scale
check-scale: check-scale-import check-scale-grid check-scale-queue check-scale-files ## Load tests: an import of 10M entities, the grid at 100M rows, the queue, and 1M files

.PHONY: check-scale-import
check-scale-import: test-wasm ## Load test: import 10M entities, interrupted halfway (IKI_SCALE_RECORDS sets how many)
	uv run pytest server/tests/test_scale_import.py --scale -s

.PHONY: check-scale-grid
check-scale-grid: test-wasm ## Load test: the grid at 100M rows (IKI_SCALE_GRID_ROWS sets how many)
	uv run pytest server/tests/test_scale_grid.py --scale -s

.PHONY: check-scale-queue
check-scale-queue: test-wasm ## Load test: 32 workers leasing from 100k queued jobs behind 1M finished, then packs of 100 runs within projects' quotas (IKI_SCALE_QUEUE_* set the sizes)
	uv run pytest server/tests/test_scale_queue.py --scale -s

.PHONY: check-scale-files
check-scale-files: test-wasm ## Load test: a project's Files at 1M files (IKI_SCALE_FILES sets how many)
	uv run pytest server/tests/test_scale_files.py --scale -s

.PHONY: check-slurm
check-slurm: ## iki-worker on a Slurm cluster in a container (deploy/dev/slurm) against the real server: builds both (and iki-wasm, for plugins' rules), then runs server/tests/test_slurm_e2e.py
	scripts/build-wasm.sh && built=$$(deploy/dev/slurm/build.sh) && env $$built uv run pytest server/tests/test_slurm_e2e.py --slurm -p no:xdist -v

# --- The live cloud tests (deploy/live) -------------------------------------------
# Each deploys Iki to a real cloud, checks it and tears it all down, billing that cloud: run
# them when make live-due says a cloud is due, and before tagging a release (release.yml
# refuses a tag without them). A passing run of a clean tree appends to deploy/live-runs.json:
# commit it.

.PHONY: live-due
live-due: ## Which clouds' live tests are due: each one's last passing run, and what it tests that changed since (STRICT=1: exit 1 if any is due)
	python3 deploy/live/live_runs.py due $(if $(STRICT),--strict)

.PHONY: check-gcp-live
check-gcp-live: ## Deploy Iki to the Google Cloud project IKI_GCP_PROJECT for real, check it, tear it all down. Bills that project: a few US dollars a run. Never on CI.
	@test -n "$(IKI_GCP_PROJECT)" || (echo "Set IKI_GCP_PROJECT: the project to deploy in, and bill." && false)
	uv run pytest deploy/gcp/tests/test_gcp_live.py --gcp-live -p no:xdist -v -s

.PHONY: gcp-live-cost
gcp-live-cost: ## What the last check-gcp-live run cost: lifetimes from the audit logs, list prices from the Billing Catalog
	@test -n "$(IKI_GCP_PROJECT)" || (echo "Set IKI_GCP_PROJECT: the project the run deployed in." && false)
	PYTHONPATH=deploy/live uv run python deploy/gcp/tests/gcp_cost.py

.PHONY: check-aws-live
check-aws-live: ## Deploy Iki to AWS as IKI_AWS_ROLE_ARN for real, check it, tear it all down. Bills that account: a few US dollars a run. Never on CI.
	@test -n "$(IKI_AWS_ROLE_ARN)" || (echo "Set IKI_AWS_ROLE_ARN: the role to deploy as, whose account is billed." && false)
	@test -n "$(IKI_AWS_TOKEN_COMMAND)" || (echo "Set IKI_AWS_TOKEN_COMMAND: it prints a web identity token the role trusts." && false)
	uv run pytest deploy/aws/tests/test_aws_live.py --aws-live -p no:xdist -v -s

.PHONY: aws-live-cost
aws-live-cost: ## What the last check-aws-live run cost: lifetimes from CloudTrail, list prices from the AWS Price List (ESTIMATE=1: before a run)
	@test -n "$(IKI_AWS_ROLE_ARN)" || (echo "Set IKI_AWS_ROLE_ARN: the role the run deployed as." && false)
	PYTHONPATH=deploy/live:deploy/aws/tests uv run python deploy/aws/tests/aws_cost.py $(if $(ESTIMATE),--estimate)

.PHONY: check-rust
check-rust: ## rustfmt, clippy (warnings denied) and cargo test
	cd worker && cargo fmt --all --check
	cd worker && cargo clippy --locked --all-targets -- -D warnings
	cd worker && cargo test --locked --all-targets
	scripts/build-dna-annotation-wasm.sh --check
	# Built from the crates above, and the same bytes as the tracked digest names.
	scripts/build-plasmid-tools-wasm.sh --check
	scripts/build-oligo-checks-wasm.sh --check

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

#: How long `make dev-ci` waits for the cluster to come up. The images build from scratch on a
#: cold cache, and on a machine running several gates at once they build alongside each other, so
#: a timeout tuned to an idle machine fails a run that was only sharing the disk and the cores.
DEV_CI_TIMEOUT ?= 40m

.PHONY: dev-ci dev-product-ci
export IKI_DEV_FIXTURES ?= 0
dev-ci: override IKI_DEV_FIXTURES := 1
dev-ci: ## Run the dev end-to-end and restore checks with the CI fixture plugins
dev-product-ci: override IKI_DEV_FIXTURES := 0
dev-product-ci: ## Run the same end-to-end and restore checks with only product plugins
dev-ci dev-product-ci:
	deploy/dev/up.sh
	tilt ci --timeout $(DEV_CI_TIMEOUT)
	deploy/dev/e2e.sh
	deploy/dev/restore-test.sh

.PHONY: dev-sso
dev-sso: ## Set up sign-in on the dev cluster through the wizard (bob becomes admin)
	uv run python -m deploy.dev.sso

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

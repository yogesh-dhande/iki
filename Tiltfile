# Local development on kind. Run `make dev-up` once, then `tilt up`.
# `tilt ci` brings everything up headless and exits when it's healthy (CI uses it).

allow_k8s_contexts('kind-iki')
update_settings(k8s_upsert_timeout_secs=300)

if k8s_context() != 'kind-iki':
    fail("Tilt must run against the kind-iki cluster. Run `make dev-up` first.")

# --- Dependencies -------------------------------------------------------------

k8s_yaml([
    'deploy/dev/manifests/namespace.yaml',
    'deploy/dev/manifests/postgres.yaml',
    'deploy/dev/manifests/seaweedfs.yaml',
    'deploy/dev/manifests/keycloak.yaml',
    'deploy/dev/manifests/otel-lgtm.yaml',
    'deploy/dev/manifests/mailpit.yaml',
    'deploy/dev/manifests/webhook-receiver.yaml',
])

k8s_kind('Cluster', api_version='postgresql.cnpg.io/v1', pod_readiness='wait')
k8s_resource(
    'iki-db',
    extra_pod_selectors=[{'cnpg.io/cluster': 'iki-db'}],
    labels=['deps'],
)
k8s_resource('seaweedfs', port_forwards=['8333:8333'], labels=['deps'])
k8s_resource(
    'keycloak',
    port_forwards=['8080:8080'],
    labels=['deps'],
    links=[link('http://keycloak.iki.localhost:8080/admin/', 'Keycloak admin')],
)
k8s_resource(
    'mailpit',
    objects=['iki-email:secret'],
    port_forwards=['8025:8025'],
    labels=['deps'],
    links=[link('http://localhost:8025', 'Mailpit (email)')],
)
k8s_resource(
    'webhook-receiver',
    objects=['webhook-receiver:configmap'],
    port_forwards=['8090:8080'],
    labels=['deps'],
    links=[link('http://localhost:8090/requests', 'Webhooks received')],
)
k8s_resource(
    'otel-lgtm',
    port_forwards=['3300:3000'],
    labels=['observability'],
    links=[link('http://localhost:3300', 'Grafana')],
)

# --- Iki ------------------------------------------------------------------------

docker_build(
    'iki-server',
    '.',
    dockerfile='server/Dockerfile',
    only=[
        'pyproject.toml', 'uv.lock', 'LICENSE', 'NOTICE', 'server', 'cli',
        # The web app, built in the image's own stage.
        'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'tsconfig.base.json', 'web',
    ],
    ignore=[
        'server/tests', '**/__pycache__', '**/node_modules',
        # A local `make web` build; the image builds its own.
        'server/src/iki_server/web/static/app',
    ],
)

k8s_yaml(helm(
    'deploy/helm/iki',
    name='iki',
    namespace='iki',
    values=['deploy/dev/values-dev.yaml'],
))

k8s_resource('iki-migrate', resource_deps=['iki-db'], labels=['iki'])
k8s_resource(
    'iki-web',
    resource_deps=['iki-migrate', 'otel-lgtm'],
    port_forwards=['8000:8000'],
    labels=['iki'],
    links=[
        link('http://localhost:8000/', 'Iki'),
        link('http://localhost:8000/healthz/ready', 'Readiness'),
    ],
)
k8s_resource('iki-tasks', resource_deps=['iki-migrate', 'otel-lgtm'], labels=['iki'])

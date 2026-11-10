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

# The server image, then the dev cluster's plugins on top (deploy/dev/plugins.lock.yaml),
# the way a deployment builds its own image.
custom_build(
    'iki-server',
    'deploy/dev/build-server.sh $EXPECTED_REF',
    deps=[
        'pyproject.toml', 'uv.lock', 'LICENSE', 'NOTICE', 'server', 'cli', 'codegen',
        'spec/pyproject.toml', 'spec/README.md', 'spec/LICENSE', 'spec/NOTICE', 'spec/src',
        # The web app, built in the image's own stage.
        'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'tsconfig.base.json', 'web',
        # The plugins and how they're added.
        'deploy/dev/plugins.lock.yaml', 'deploy/dev/settings', 'deploy/dev/build-server.sh',
        'deploy/plugins.Dockerfile',
    ],
    ignore=[
        'server/tests', 'cli/tests', 'codegen/tests', '**/__pycache__', '**/node_modules',
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

# Gives the dev bucket the CORS and lifecycle rules Iki needs (browser
# uploads from localhost:8000 and the web dev server), once the web pod is up.
local_resource(
    'storage-setup',
    # Iki's bucket, and a versioned lab bucket to try registering files in place from.
    cmd='kubectl --context kind-iki -n iki exec deploy/iki-web -c web -- '
    + 'python -m iki_server.manage setup_storage --enable-versioning && '
    + 'kubectl --context kind-iki -n iki exec deploy/iki-web -c web -- '
    + 'python -m iki_server.manage shell -c "'
    + 'from iki_server.files import storage; s3 = storage.client(); '
    + 'names = [b[\'Name\'] for b in s3.list_buckets()[\'Buckets\']]; '
    + '\'lab-data\' in names or s3.create_bucket(Bucket=\'lab-data\'); '
    + 's3.put_bucket_versioning(Bucket=\'lab-data\', '
    + 'VersioningConfiguration={\'Status\': \'Enabled\'}); '
    + 'print(\'Bucket lab-data keeps versions; register files from s3://lab-data/runs/.\')"',
    resource_deps=['iki-web', 'seaweedfs'],
    labels=['iki'],
)

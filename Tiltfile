# Local development on kind. Run `make dev-up` once, then `tilt up`.
# `tilt ci` brings everything up headless and exits when it's healthy (CI uses it).

allow_k8s_contexts('kind-iki')
update_settings(k8s_upsert_timeout_secs=300)

if k8s_context() != 'kind-iki':
    fail("Tilt must run against the kind-iki cluster. Run `make dev-up` first.")

fixtures = os.getenv('IKI_DEV_FIXTURES', '0')
if fixtures not in ['0', '1']:
    fail('IKI_DEV_FIXTURES must be 0 (product) or 1 (CI fixtures).')

plugin_deps = [
    'deploy/dev/build-plugins.sh', 'deploy/dev/plugins.lock.yaml',
    'deploy/dev/compute.yaml', 'deploy/dev/settings', 'plugins',
]
chart_values = ['deploy/dev/values-dev.yaml']
if fixtures == '1':
    plugin_deps += ['deploy/dev/fixtures', 'plugin-template']
    chart_values.append('deploy/dev/fixtures/values.yaml')

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
if fixtures == '1':
    k8s_yaml([
        'deploy/dev/fixtures/namespaces.yaml',
        'deploy/dev/manifests/fake-lims.yaml',
        'deploy/dev/manifests/fake-foldcloud.yaml',
        'deploy/dev/manifests/shared-filesystem.yaml',
        'deploy/dev/manifests/bucket.yaml',
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
if fixtures == '1':
    k8s_resource(
        'fake-lims',
        objects=['fake-lims:configmap'],
        port_forwards=['8091:8080'],
        labels=['deps'],
        links=[link('http://localhost:8091/requests', 'Fake LIMS requests')],
    )
    k8s_resource(
        'fake-foldcloud',
        objects=['fake-foldcloud:configmap'],
        port_forwards=['8092:8080'],
        labels=['deps'],
        links=[link('http://localhost:8092/requests', 'Fake FoldCloud requests')],
    )
# Iki's Grafana dashboard (ADR 0098), from the chart's own file, for otel-lgtm to load.
watch_file('deploy/helm/iki/files/dashboards')
k8s_yaml(local(
    'kubectl create configmap iki-dashboards --namespace iki'
    + ' --from-file=deploy/helm/iki/files/dashboards --dry-run=client -o yaml',
    quiet=True,
))
k8s_resource(
    'otel-lgtm',
    objects=['grafana-iki-dashboards:configmap', 'iki-dashboards:configmap'],
    port_forwards=['3300:3000'],
    labels=['observability'],
    links=[link('http://localhost:3300', 'Grafana')],
)

# --- Iki ------------------------------------------------------------------------

# The server image, then the dev cluster's plugins on top (deploy/dev/plugins.lock.yaml),
# the way a deployment builds its own image.
custom_build(
    'iki-server',
    'IKI_DEV_FIXTURES=' + fixtures + ' deploy/dev/build-server.sh $EXPECTED_REF',
    deps=plugin_deps + [
        'pyproject.toml', 'uv.lock', 'LICENSE', 'NOTICE', 'server', 'cli', 'codegen',
        'spec/pyproject.toml', 'spec/README.md', 'spec/LICENSE', 'spec/NOTICE', 'spec/backend',
        'spec/src',
        # The web app, built in the image's own stage.
        'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'tsconfig.base.json', 'web',
        # iki-wasm, built in the image's own stage, and for `iki deploy build` (ADR 0127).
        'worker/Cargo.toml', 'worker/Cargo.lock', 'worker/rust-toolchain.toml',
        'worker/crates', 'worker/.cargo', 'deploy/dev/build-wasm.sh', 'scripts/build-wasm-image.sh',
        'scripts/build-oligo-wasm.sh', 'scripts/build-dna-annotation-wasm.sh',
        'scripts/verify-server-wasm.sh',
        'scripts/build-server-aot.sh', 'scripts/verify-server-aot.py',
        # The plugins' modules, built in the same stage and checked against their digests.
        'scripts/build-plasmid-tools-wasm.sh', 'scripts/build-oligo-checks-wasm.sh',
        'plugins/plasmid/data/plasmid-tools.wasm.sha256', 'plugins/dna/data/oligo-checks.wasm.sha256',
        'deploy/dev/build-server.sh', 'deploy/plugins.Dockerfile',
    ],
    ignore=[
        'server/tests', 'cli/tests', 'codegen/tests', '**/__pycache__', '**/node_modules',
        # A local `make web` build; the image builds its own.
        'server/src/iki_server/web/static/app',
        # What deploy/dev/build-wasm.sh writes.
        '**/iki_spec/iki_wasm*', '**/oligo-measurement.wasm', '**/iki_spec/dna-annotation.wasm',
        '**/iki_spec/rust-standard-library.html', '**/iki_spec/modules/*.wasm',
    ],
)

# The worker image, then the dev cluster's plugins and their environments on top.
custom_build(
    'iki-worker',
    'IKI_DEV_FIXTURES=' + fixtures + ' deploy/dev/build-worker.sh $EXPECTED_REF',
    deps=plugin_deps + [
        'worker/Cargo.toml', 'worker/Cargo.lock', 'worker/rust-toolchain.toml',
        'worker/crates', 'worker/.cargo', 'worker/Dockerfile', 'deploy/worker.Dockerfile',
        'deploy/dev/build-worker.sh', 'deploy/dev/build-wasm.sh', 'scripts/build-wasm-image.sh',
        'server/Dockerfile', 'scripts/build-oligo-wasm.sh', 'scripts/build-dna-annotation-wasm.sh',
        'scripts/verify-server-wasm.sh', 'server/iki_wasm.wasm.sha256',
        'scripts/build-plasmid-tools-wasm.sh', 'scripts/build-oligo-checks-wasm.sh',
        # What `iki deploy build --workers` reads and runs.
        'cli', 'spec/src',
    ],
    ignore=[
        '**/__pycache__', '**/iki_spec/iki_wasm*', '**/oligo-measurement.wasm',
        '**/iki_spec/dna-annotation.wasm', '**/iki_spec/rust-standard-library.html',
        '**/iki_spec/modules/*.wasm',
    ],
)

k8s_yaml(helm(
    'deploy/helm/iki',
    name='iki',
    namespace='iki',
    values=chart_values,
))

k8s_resource(
    'iki-clamd',
    objects=['iki-clamd:configmap', 'iki-clamd:networkpolicy', 'iki-clamd:serviceaccount'],
    labels=['deps'],
)
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
k8s_resource(
    'iki-egress-proxy',
    objects=['iki-egress-proxy:serviceaccount', 'iki-egress-proxy:networkpolicy'],
    resource_deps=['iki-migrate'],
    labels=['iki'],
)

# Starts a pod in iki-runs for each container-tier run (ADR 0061).
k8s_resource(
    'iki-provisioner',
    objects=[
        'iki-provisioner:serviceaccount:iki',
        'iki-provisioner:role:iki-runs',
        'iki-provisioner:rolebinding:iki-runs',
        'iki-run:serviceaccount:iki-runs',
        'iki-runs:networkpolicy:iki-runs',
        'iki-provisioner-kubernetes-api:networkpolicy',
        'iki-web-from-runs:networkpolicy',
        'iki-egress-proxy-from-runs:networkpolicy',
    ],
    resource_deps=['iki-migrate', 'iki-worker'],
    labels=['iki'],
)

# A backend-tier registration token for the workers, made in the cluster.
local_resource(
    'worker-registration',
    cmd='deploy/dev/worker-registration.sh',
    deps=['deploy/dev/worker-registration.sh'],
    resource_deps=['iki-web'],
    labels=['iki'],
)
k8s_resource(
    'iki-worker',
    objects=['iki-worker:serviceaccount', 'iki-worker-egress:networkpolicy'],
    resource_deps=['worker-registration'],
    labels=['iki'],
)

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

{{/* Chart name, truncated to fit Kubernetes name limits. */}}
{{- define "iki.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified release name. */}}
{{- define "iki.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "iki.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "iki.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: iki
{{- end -}}

{{/* Selector labels for one component (web, migrate, ...). Call with (dict "ctx" $ "component" "web"). */}}
{{- define "iki.selectorLabels" -}}
app.kubernetes.io/name: {{ include "iki.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "iki.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{/* Pod-level security context shared by every Iki pod (PodSecurity "restricted"). */}}
{{- define "iki.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "iki.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
privileged: false
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* Environment shared by the web and migrate containers. */}}
{{/*
Environment for app containers (web, tasks, maintenance): connects as the
app's least-privileged database role. Pass the root context, or a dict with
"ctx" (the root context) and optionally "service" (the OpenTelemetry service
name) and "poolMaxSize".
*/}}
{{- define "iki.env" -}}
{{- $ctx := .ctx | default . -}}
- name: IKI_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $ctx.Values.database.existingSecret }}
      key: {{ $ctx.Values.database.uriKey }}
{{ include "iki.commonEnv" . }}
{{- end -}}

{{/*
Environment for the migrate Job: connects as the migrator, which owns the
schema, and knows the app's role so it can grant it privileges.
*/}}
{{- define "iki.migratorEnv" -}}
{{- $ctx := . -}}
- name: IKI_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $ctx.Values.database.migrator.existingSecret }}
      key: {{ $ctx.Values.database.migrator.uriKey }}
- name: IKI_APP_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $ctx.Values.database.existingSecret }}
      key: {{ $ctx.Values.database.uriKey }}
{{ include "iki.commonEnv" . }}
{{- end -}}

{{- define "iki.commonEnv" -}}
{{- $ctx := .ctx | default . -}}
- name: IKI_ENVIRONMENT
  value: {{ $ctx.Values.environment | quote }}
- name: IKI_PUBLIC_URL
  value: {{ $ctx.Values.publicUrl | quote }}
- name: IKI_ALLOW_LOCAL_PLUGINS
  value: {{ $ctx.Values.plugins.allowLocal | quote }}
{{- with $ctx.Values.plugins.mirrors }}
- name: IKI_PLUGIN_MIRRORS
  value: {{ join "," . | quote }}
{{- end }}
- name: IKI_ALLOWED_HOSTS
  value: {{ join "," $ctx.Values.allowedHosts | quote }}
- name: IKI_HTTPS
  value: {{ $ctx.Values.https | quote }}
- name: IKI_LOG_LEVEL
  value: {{ $ctx.Values.log.level | quote }}
- name: IKI_LOG_FORMAT
  value: {{ $ctx.Values.log.format | quote }}
- name: IKI_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $ctx.Values.secretKey.existingSecret }}
      key: {{ $ctx.Values.secretKey.key }}
- name: IKI_ENCRYPTION_KEYS
  valueFrom:
    secretKeyRef:
      name: {{ $ctx.Values.encryptionKeys.existingSecret }}
      key: {{ $ctx.Values.encryptionKeys.key }}
{{- with $ctx.Values.bundles.signingKey.existingSecret }}
- name: IKI_BUNDLE_SIGNING_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $ctx.Values.bundles.signingKey.key }}
{{- end }}
{{- with $ctx.Values.bundles.trusted }}
{{- $pairs := list }}
{{- range $name, $keys := . }}
{{- range $keys }}
{{- $pairs = append $pairs (printf "%s=%s" $name .) }}
{{- end }}
{{- end }}
- name: IKI_BUNDLE_TRUSTED_KEYS
  value: {{ join "," $pairs | quote }}
{{- end }}
- name: IKI_SESSION_IDLE_TIMEOUT_MINUTES
  value: {{ $ctx.Values.sessions.idleTimeoutMinutes | quote }}
- name: IKI_SESSION_MAX_AGE_HOURS
  value: {{ $ctx.Values.sessions.maxAgeHours | quote }}
- name: IKI_API_RATE_LIMIT_PER_MINUTE
  value: {{ $ctx.Values.api.rateLimitPerMinute | quote }}
- name: IKI_RUNS_CHECKPOINT_INTERVAL
  value: {{ $ctx.Values.checkpoints.interval | quote }}
{{- if $ctx.Values.containerTier.enabled }}
{{- $namespaces := include "iki.runNamespaces" $ctx | fromJsonArray }}
{{- /* Where run pods go: the provisioner starts them there, and the API and tasks say
which version of an environment's volumes machines there get (ADR 0075). */}}
- name: IKI_RUNS_NAMESPACE
  value: {{ first $namespaces | quote }}
- name: IKI_RUNS_NAMESPACES
  value: {{ rest $namespaces | join "," | quote }}
{{- end }}
{{- with $ctx.Values.vmTier }}
{{- if .enabled }}
{{- /* Whose machines the VM tier starts, and on which cloud: the provisioner starts them,
and the API and tasks say where an environment's setup builds go and which version of its
volumes machines there get (ADR 0075). The rest is the provisioner's alone. */}}
- name: IKI_RUNS_VM_OWNER
  value: {{ .owner | quote }}
{{- if eq (.cloud | default "aws") "gcp" }}
- name: IKI_RUNS_VM_CLOUD
  value: gcp
- name: IKI_RUNS_VM_GCP_PROJECT
  value: {{ .gcp.project | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- with $ctx.Values.selfHostedWorkers.version }}
- name: IKI_WORKER_VERSION
  value: {{ . | quote }}
{{- end }}
- name: IKI_WORKER_UPDATES
  value: {{ $ctx.Values.selfHostedWorkers.autoUpdate | quote }}
- name: IKI_WORKER_DOWNLOADS
  value: {{ $ctx.Values.selfHostedWorkers.downloadsUrl | quote }}
- name: IKI_OAUTH_ACCESS_TOKEN_MINUTES
  value: {{ $ctx.Values.oauth.accessTokenMinutes | quote }}
- name: IKI_OAUTH_REFRESH_TOKEN_DAYS
  value: {{ $ctx.Values.oauth.refreshTokenDays | quote }}
- name: IKI_OAUTH_CLIENT_METADATA_DOCUMENTS
  value: {{ $ctx.Values.oauth.clientMetadataDocuments | quote }}
{{- if $ctx.Values.email.existingSecret }}
- name: IKI_EMAIL_URL
  valueFrom:
    secretKeyRef:
      name: {{ $ctx.Values.email.existingSecret }}
      key: {{ $ctx.Values.email.urlKey }}
- name: IKI_EMAIL_FROM
  value: {{ required "email.from is required when email.existingSecret is set" $ctx.Values.email.from | quote }}
- name: IKI_EMAIL_ALLOW_PLAINTEXT
  value: {{ $ctx.Values.email.allowPlaintext | quote }}
{{- end }}
{{- with $ctx.Values.files }}
{{- if .bucket }}
{{- $backend := .backend | default "s3" }}
{{- if and (eq $backend "gcs") .credentials.existingSecret }}
{{- fail "files.credentials is an S3 access key: with files.backend gcs, pods reach Cloud Storage as their Google service account (serviceAccount.annotations), with no key" }}
{{- end }}
{{- $scheme := ternary "gs://" "s3://" (eq $backend "gcs") }}
{{- range (.registerLocations | default list) }}
{{- if not (hasPrefix $scheme .) }}
{{- fail (printf "files.registerLocations: %s must start %s with files.backend %s" . $scheme $backend) }}
{{- end }}
{{- end }}
- name: IKI_FILES_BACKEND
  value: {{ $backend | quote }}
- name: IKI_FILES_BUCKET
  value: {{ .bucket | quote }}
- name: IKI_FILES_PREFIX
  value: {{ .prefix | quote }}
- name: IKI_FILES_REGION
  value: {{ .region | quote }}
- name: IKI_FILES_ENDPOINT_URL
  value: {{ .endpoint | quote }}
- name: IKI_FILES_PUBLIC_ENDPOINT_URL
  value: {{ .publicEndpoint | default .endpoint | quote }}
- name: IKI_FILES_WORKER_ENDPOINT_URL
  value: {{ .workerEndpoint | quote }}
- name: IKI_FILES_MAX_UPLOAD_SIZE
  value: {{ .maxUploadSize | quote }}
- name: IKI_FILES_ARTIFACT_RETENTION
  value: {{ .artifactRetention | quote }}
- name: IKI_FILES_ARTIFACT_MIN_RETENTION
  value: {{ .artifactMinRetention | quote }}
- name: IKI_FILES_REGISTER_LOCATIONS
  value: {{ join "," (.registerLocations | default list) | quote }}
{{- $aliases := list }}
{{- range $old, $new := (.bucketAliases | default dict) }}
{{- $aliases = append $aliases (printf "%s=%s" $old $new) }}
{{- end }}
- name: IKI_FILES_BUCKET_ALIASES
  value: {{ join "," $aliases | quote }}
{{- if .credentials.existingSecret }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .credentials.existingSecret }}
      key: {{ .credentials.accessKeyIdKey }}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .credentials.existingSecret }}
      key: {{ .credentials.secretAccessKeyKey }}
{{- end }}
{{- end }}
{{- end }}
- name: IKI_WEBHOOK_ALLOWED_NETWORKS
  value: {{ join "," $ctx.Values.webhooks.allowedNetworks | quote }}
- name: IKI_WEBHOOK_ALLOW_HTTP
  value: {{ $ctx.Values.webhooks.allowHttp | quote }}
{{- if $ctx.Values.webhooks.caSecret.name }}
- name: IKI_WEBHOOK_CA_FILE
  value: /etc/iki/webhook-ca/{{ $ctx.Values.webhooks.caSecret.key }}
{{- end }}
{{- if $ctx.Values.egressProxy.enabled }}
- name: IKI_EGRESS_PROXY_URL
  value: {{ printf "http://%s-egress-proxy:%d" (include "iki.fullname" $ctx) (int $ctx.Values.egressProxy.port) | quote }}
- name: IKI_EGRESS_ALLOWED_NETWORKS
  value: {{ join "," $ctx.Values.egressProxy.allowedNetworks | quote }}
{{- with $ctx.Values.egressProxy.upstreamProxy }}
{{- if .existingSecret }}
- name: IKI_EGRESS_UPSTREAM_PROXY
  valueFrom:
    secretKeyRef:
      name: {{ .existingSecret }}
      key: {{ .key }}
{{- else if .url }}
- name: IKI_EGRESS_UPSTREAM_PROXY
  value: {{ .url | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- with $ctx.Values.containerTier.imagePolicy }}
{{- if .approvals.repository }}
{{- /* Who may sign runs' images, for workers that check them themselves: a Slurm
cluster's (ADR 0133), with no admission controller in front of its nodes. */}}
{{- $iki := dict "issuer" .iki.issuer }}
{{- with .iki.subject }}{{ $_ := set $iki "subject" . }}{{ end }}
{{- with .iki.subjectRegExp }}{{ $_ := set $iki "subject_regexp" . }}{{ end }}
{{- $approvals := dict "issuer" .approvals.issuer "repository" .approvals.repository }}
{{- with .approvals.subject }}{{ $_ := set $approvals "subject" . }}{{ end }}
{{- with .approvals.subjectRegExp }}{{ $_ := set $approvals "subject_regexp" . }}{{ end }}
- name: IKI_IMAGE_POLICY
  value: {{ dict "iki" $iki "approvals" $approvals | toJson | quote }}
{{- end }}
{{- end }}
- name: IKI_DB_SSLMODE
  value: {{ $ctx.Values.database.sslmode | quote }}
{{- if $ctx.Values.database.caSecret.name }}
- name: IKI_DB_SSLROOTCERT
  value: /etc/iki/db-ca/{{ $ctx.Values.database.caSecret.key }}
{{- end }}
- name: IKI_DB_POOL_MIN_SIZE
  value: {{ $ctx.Values.database.pool.minSize | quote }}
- name: IKI_DB_POOL_MAX_SIZE
  value: {{ .poolMaxSize | default $ctx.Values.database.pool.maxSize | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .service | default "iki-server" }}
- name: K8S_POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: K8S_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "k8s.pod.name=$(K8S_POD_NAME),k8s.namespace.name=$(K8S_NAMESPACE)%s" (ternary (printf ",%s" $ctx.Values.otel.resourceAttributes) "" (ne $ctx.Values.otel.resourceAttributes "")) | quote }}
{{- if $ctx.Values.otel.endpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ $ctx.Values.otel.endpoint | quote }}
{{- end }}
{{- end -}}

{{- define "iki.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if .Values.database.caSecret.name }}
- name: db-ca
  mountPath: /etc/iki/db-ca
  readOnly: true
{{- end }}
{{- if .Values.webhooks.caSecret.name }}
- name: webhook-ca
  mountPath: /etc/iki/webhook-ca
  readOnly: true
{{- end }}
{{- end -}}

{{- define "iki.volumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: 64Mi
{{- if .Values.database.caSecret.name }}
- name: db-ca
  secret:
    secretName: {{ .Values.database.caSecret.name }}
    items:
      - key: {{ .Values.database.caSecret.key }}
        path: {{ .Values.database.caSecret.key }}
{{- end }}
{{- if .Values.webhooks.caSecret.name }}
- name: webhook-ca
  secret:
    secretName: {{ .Values.webhooks.caSecret.name }}
    items:
      - key: {{ .Values.webhooks.caSecret.key }}
        path: {{ .Values.webhooks.caSecret.key }}
{{- end }}
{{- end -}}

{{/*
The container tier's namespaces: its own, then any others templates may name.
*/}}
{{- define "iki.runNamespaces" -}}
{{- $c := .Values.containerTier -}}
{{- $own := required "containerTier.namespace is required: the namespace run pods go in, not this release's" $c.namespace -}}
{{- if eq $own .Release.Namespace -}}
{{- fail "containerTier.namespace must not be this release's namespace: plugins' images run there, and must never reach Iki's own Secrets" -}}
{{- end -}}
{{- $all := uniq (concat (list $own) ($c.extraNamespaces | default list)) -}}
{{- if has .Release.Namespace $all -}}
{{- fail "containerTier.extraNamespaces must not include this release's namespace" -}}
{{- end -}}
{{- toJson $all -}}
{{- end -}}

{{/*
Network policy peers, as seen from another namespace: a peer that names pods but no
namespace means pods in this release's.
*/}}
{{- define "iki.peersFromElsewhere" -}}
{{- $out := list -}}
{{- range .peers -}}
{{- if and (hasKey . "podSelector") (not (hasKey . "namespaceSelector")) -}}
{{- $out = append $out (merge (dict "namespaceSelector" (dict "matchLabels" (dict "kubernetes.io/metadata.name" $.namespace))) .) -}}
{{- else -}}
{{- $out = append $out . -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}

{{/*
Where workers and run pods reach the server: the pods and namespaces in
networkPolicy.ingressFrom (an ingress controller in the cluster), then
networkPolicy.egress.server.to. ingressFrom's address blocks aren't added: they're
where traffic to web pods comes from, not where workers connect. As JSON.
*/}}
{{- define "iki.serverPeers" -}}
{{- $out := list -}}
{{- range .Values.networkPolicy.ingressFrom | default list -}}
{{- if not (hasKey . "ipBlock") -}}
{{- $out = append $out . -}}
{{- end -}}
{{- end -}}
{{- toJson (concat $out (.Values.networkPolicy.egress.server.to | default list)) -}}
{{- end -}}

{{/*
Fails unless every address block in .peers is a /8 (IPv4) or a /32 (IPv6) or
narrower. IPv6 blocks in ::/16 are refused too: IPv4 addresses written as IPv6
(::ffff:0:0/96) are there, and some network plugins match them as IPv4. .path
names the value they came from.
*/}}
{{- define "iki.checkNarrowPeers" -}}
{{- range .peers -}}
{{- with .ipBlock -}}
{{- $cidr := .cidr | default "" | toString -}}
{{- $parts := splitList "/" $cidr -}}
{{- $bound := ternary 32 8 (contains ":" $cidr) -}}
{{- if or (ne (len $parts) 2) (lt (atoi (last $parts)) $bound) -}}
{{- fail (printf "%s: %s is wider than workers and run pods may reach, a /%d at most: list the server's and object storage's own addresses (ADR 0155)" $.path $cidr $bound) -}}
{{- end -}}
{{- if regexMatch "^(::|0{1,4}:)" $cidr -}}
{{- fail (printf "%s: %s is in ::/16, where IPv6 writes IPv4 addresses: list the server's and object storage's own addresses (ADR 0155)" $.path $cidr) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Workers and run pods run plugins' commands, so they reach only the server, object
storage, the egress proxy and what networkPolicy.egress.workers adds (ADR 0155).
Fails unless each of those names somewhere specific.
*/}}
{{- define "iki.checkWorkerEgress" -}}
{{- $np := .Values.networkPolicy -}}
{{- $workers := .Values.backendWorkers.enabled -}}
{{- $runs := .Values.containerTier.enabled -}}
{{- if and $np.enabled (or $workers $runs) -}}
{{- $egress := $np.egress -}}
{{- if or $workers (not .Values.containerTier.serverInCluster) -}}
{{- if not (include "iki.serverPeers" . | fromJsonArray) -}}
{{- fail "networkPolicy.egress.server.to: workers and run pods reach the server only where it says: list your load balancer's addresses, or an ingress controller in the cluster (ADR 0155)" -}}
{{- end -}}
{{- include "iki.checkNarrowPeers" (dict "path" "networkPolicy.egress.server.to" "peers" ($egress.server.to | default list)) -}}
{{- end -}}
{{- if .Values.files.bucket -}}
{{- include "iki.checkNarrowPeers" (dict "path" "networkPolicy.egress.objectStorage.to" "peers" ($egress.objectStorage.to | default list)) -}}
{{- end -}}
{{- range $i, $rule := $egress.workers | default list -}}
{{- $path := printf "networkPolicy.egress.workers[%d].to" $i -}}
{{- if not $rule.to -}}
{{- fail (printf "%s: list where workers and run pods may go; a rule without it lets them reach anywhere (ADR 0155)" $path) -}}
{{- end -}}
{{- include "iki.checkNarrowPeers" (dict "path" $path "peers" $rule.to) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The iki-worker image run pods copy the binary from.
*/}}
{{- define "iki.runsWorkerImage" -}}
{{- $w := .Values.containerTier.workerImage -}}
{{- if $w.digest -}}
{{- printf "%s@%s" $w.repository $w.digest -}}
{{- else -}}
{{- printf "%s:%s" $w.repository ($w.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

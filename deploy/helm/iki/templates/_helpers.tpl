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
- name: IKI_SESSION_IDLE_TIMEOUT_MINUTES
  value: {{ $ctx.Values.sessions.idleTimeoutMinutes | quote }}
- name: IKI_SESSION_MAX_AGE_HOURS
  value: {{ $ctx.Values.sessions.maxAgeHours | quote }}
- name: IKI_API_RATE_LIMIT_PER_MINUTE
  value: {{ $ctx.Values.api.rateLimitPerMinute | quote }}
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
- name: IKI_FILES_MAX_UPLOAD_SIZE
  value: {{ .maxUploadSize | quote }}
- name: IKI_FILES_REGISTER_LOCATIONS
  value: {{ join "," (.registerLocations | default list) | quote }}
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

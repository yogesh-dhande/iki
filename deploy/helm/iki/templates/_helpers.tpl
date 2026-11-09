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
{{- define "iki.env" -}}
- name: IKI_ENVIRONMENT
  value: {{ .Values.environment | quote }}
- name: IKI_ALLOWED_HOSTS
  value: {{ join "," .Values.allowedHosts | quote }}
- name: IKI_HTTPS
  value: {{ .Values.https | quote }}
- name: IKI_LOG_LEVEL
  value: {{ .Values.log.level | quote }}
- name: IKI_LOG_FORMAT
  value: {{ .Values.log.format | quote }}
- name: IKI_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretKey.existingSecret }}
      key: {{ .Values.secretKey.key }}
- name: IKI_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.uriKey }}
- name: IKI_DB_SSLMODE
  value: {{ .Values.database.sslmode | quote }}
{{- if .Values.database.caSecret.name }}
- name: IKI_DB_SSLROOTCERT
  value: /etc/iki/db-ca/{{ .Values.database.caSecret.key }}
{{- end }}
- name: IKI_DB_POOL_MIN_SIZE
  value: {{ .Values.database.pool.minSize | quote }}
- name: IKI_DB_POOL_MAX_SIZE
  value: {{ .Values.database.pool.maxSize | quote }}
- name: OTEL_SERVICE_NAME
  value: iki-server
- name: K8S_POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: K8S_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "k8s.pod.name=$(K8S_POD_NAME),k8s.namespace.name=$(K8S_NAMESPACE)%s" (ternary (printf ",%s" .Values.otel.resourceAttributes) "" (ne .Values.otel.resourceAttributes "")) | quote }}
{{- if .Values.otel.endpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.otel.endpoint | quote }}
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
{{- end -}}

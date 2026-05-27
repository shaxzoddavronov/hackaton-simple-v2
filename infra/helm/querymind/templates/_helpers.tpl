{{/*
Helpers for the QueryMind chart.
*/}}

{{/*
Chart name truncated to 63 chars for Kubernetes resource-name limits.
*/}}
{{- define "querymind.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name: <release>-<chart>, or just <release> if it
already contains the chart name. Used as the base for every resource.
*/}}
{{- define "querymind.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label (chart-version).
*/}}
{{- define "querymind.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "querymind.labels" -}}
helm.sh/chart: {{ include "querymind.chart" . }}
{{ include "querymind.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels (without the version, so rollouts don't break selectors).
*/}}
{{- define "querymind.selectorLabels" -}}
app.kubernetes.io/name: {{ include "querymind.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-component selector labels. Pass component name as the 2nd arg via list.
*/}}
{{- define "querymind.componentLabels" -}}
{{- $ctx := index . 0 -}}
{{- $component := index . 1 -}}
app.kubernetes.io/name: {{ include "querymind.name" $ctx }}
app.kubernetes.io/instance: {{ $ctx.Release.Name }}
app.kubernetes.io/component: {{ $component }}
{{- end -}}

{{/*
Derived DATABASE_URL: prefer the explicit override, else synthesize the
in-cluster Postgres URL when postgres.enabled.
*/}}
{{- define "querymind.databaseUrl" -}}
{{- if .Values.secrets.databaseUrl -}}
{{- .Values.secrets.databaseUrl -}}
{{- else if .Values.postgres.enabled -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgres.%s.svc.cluster.local:%d/%s" .Values.postgres.auth.username .Values.secrets.postgresPassword (include "querymind.fullname" .) .Release.Namespace (int .Values.postgres.service.port) .Values.postgres.auth.database -}}
{{- else -}}
{{- fail "secrets.databaseUrl must be set when postgres.enabled=false" -}}
{{- end -}}
{{- end -}}

{{/*
Derived REDIS_URL: explicit override, else in-cluster Redis Service.
*/}}
{{- define "querymind.redisUrl" -}}
{{- if .Values.secrets.redisUrl -}}
{{- .Values.secrets.redisUrl -}}
{{- else if .Values.redis.enabled -}}
{{- printf "redis://%s-redis.%s.svc.cluster.local:%d/0" (include "querymind.fullname" .) .Release.Namespace (int .Values.redis.service.port) -}}
{{- else -}}
{{- fail "secrets.redisUrl must be set when redis.enabled=false" -}}
{{- end -}}
{{- end -}}

{{/*
Derived RATE_LIMIT_STORAGE_URL: same Redis, db=1.
*/}}
{{- define "querymind.rateLimitStorageUrl" -}}
{{- if .Values.redis.enabled -}}
{{- printf "redis://%s-redis.%s.svc.cluster.local:%d/1" (include "querymind.fullname" .) .Release.Namespace (int .Values.redis.service.port) -}}
{{- else -}}
{{- "memory://" -}}
{{- end -}}
{{- end -}}

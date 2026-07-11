{{/*
Shared template helpers. Naming is deliberately stable: several object
names are a HARD CONTRACT with src/rolemesh/container/k8s_runtime.py
(verify_infrastructure reads them by exact name). Those names are NOT
derived from the release name — see networkpolicy.yaml / gateway.yaml.
*/}}

{{/* Chart name, truncated to the 63-char DNS-1123 label limit. */}}
{{- define "rolemesh.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified release-scoped name for release-owned objects
(Deployments, ServiceAccounts, the NATS/Postgres Services). NOT used for
the contract-named objects (NetworkPolicies, gateway Service, data PVC).
*/}}
{{- define "rolemesh.fullname" -}}
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

{{/* Common labels stamped on every release-owned object. */}}
{{- define "rolemesh.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "rolemesh.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
The orchestrator ServiceAccount name. The orchestrator Deployment runs
under it and verify_infrastructure self-checks its RBAC; the Role/Binding
target the same name.
*/}}
{{- define "rolemesh.orchestrator.serviceAccountName" -}}
{{- printf "%s-orchestrator" (include "rolemesh.fullname" .) -}}
{{- end -}}

{{/*
The Secret name carrying EGRESS_TOKEN_SECRET / WS_TICKET_SECRET / DB
password. Release-scoped (not a contract name).
*/}}
{{- define "rolemesh.secretName" -}}
{{- printf "%s-secrets" (include "rolemesh.fullname" .) -}}
{{- end -}}

{{/*
NATS connection URL. Bundled NATS is exposed by a Service named literally
"nats" (NOT release-scoped) so the host matches the docker compose contract
(nats://nats:4222) byte-for-byte and the agent-side DNS resolution of the
name `nats` through the gateway resolver works identically on both runtimes
(the contract suite's Topology.nats_host == "nats"). When external, the
operator's URL is used verbatim. Single source of truth so orchestrator,
webui and gateway never drift.
*/}}
{{- define "rolemesh.natsUrl" -}}
{{- if .Values.nats.enabled -}}
nats://nats:4222
{{- else -}}
{{- required "nats.enabled=false requires nats.externalUrl" .Values.nats.externalUrl -}}
{{- end -}}
{{- end -}}

{{/* Bundled-NATS Service name — literal "nats" (see rolemesh.natsUrl). */}}
{{- define "rolemesh.natsServiceName" -}}
nats
{{- end -}}

{{/*
DATABASE_URL (business pool). Bundled Postgres composes the URL from the
Secret-held password; external mode uses the operator's URL verbatim.
*/}}
{{- define "rolemesh.databaseUrl" -}}
{{- if .Values.postgres.enabled -}}
{{- printf "postgresql://%s:$(POSTGRES_PASSWORD)@%s-postgres:5432/%s" .Values.postgres.user (include "rolemesh.fullname" .) .Values.postgres.database -}}
{{- else -}}
{{- required "postgres.enabled=false requires postgres.externalUrl" .Values.postgres.externalUrl -}}
{{- end -}}
{{- end -}}

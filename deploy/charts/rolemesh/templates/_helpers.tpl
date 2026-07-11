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
Shared auth env for the webui AND the orchestrator Deployments. Both
include this one block so the shared subset (AUTH_MODE + the OIDC trio)
cannot drift between them. The orchestrator needs the trio too, not just
the webui: without a discovery URL + client_id its create_vault_from_env()
returns None, it never subscribes the egress.token.access.request
responder, the gateway's RemoteTokenVault RPC gets "no responders", and
every user-mode MCP request forwards an EMPTY Authorization header — a
failure four hops from its cause (see the orchestrator OIDC block in
deploy/compose/compose.yaml, where this was first learned).
Renders nothing in external mode, keeping default manifests unchanged.
*/}}
{{- define "rolemesh.authEnv" -}}
{{- $mode := .Values.auth.mode | default "external" -}}
{{- if not (has $mode (list "external" "oidc")) -}}
{{- fail (printf "auth.mode must be \"external\" or \"oidc\", got %q" $mode) -}}
{{- end -}}
{{- if eq $mode "oidc" -}}
- name: AUTH_MODE
  value: "oidc"
- name: OIDC_DISCOVERY_URL
  value: {{ required "auth.mode=oidc requires auth.oidc.discoveryUrl" .Values.auth.oidc.discoveryUrl | quote }}
- name: OIDC_CLIENT_ID
  value: {{ required "auth.mode=oidc requires auth.oidc.clientId" .Values.auth.oidc.clientId | quote }}
{{- if or .Values.auth.oidc.clientSecret .Values.auth.oidc.existingSecret }}
- name: OIDC_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "rolemesh.authSecretName" . }}
      key: OIDC_CLIENT_SECRET
{{- end }}
{{- end }}
{{- end -}}

{{/*
Secret carrying OIDC_CLIENT_SECRET: an explicit auth.oidc.existingSecret
wins; otherwise the chart-wide Secret (which secret.yaml populates with
the key when auth.oidc.clientSecret is set inline).
*/}}
{{- define "rolemesh.authSecretName" -}}
{{- .Values.auth.oidc.existingSecret | default (.Values.secrets.existingSecret | default (include "rolemesh.secretName" .)) -}}
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

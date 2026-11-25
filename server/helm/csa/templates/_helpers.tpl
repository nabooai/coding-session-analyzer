{{- define "csa.image" -}}
{{ required "image.tag is required: this chart is unpackaged and has no appVersion, so set the tag you were sent" (.Values.image.tag | default .Chart.AppVersion) | printf "%s:%s" .Values.image.repository }}
{{- end -}}

{{- define "csa.host" -}}
{{ required "host is required: the name people type, e.g. csa.your-company.example" .Values.host }}
{{- end -}}

{{- define "csa.labels" -}}
app.kubernetes.io/name: csa
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "csa.claim" -}}
{{ .Values.storage.existingClaim | default (printf "%s-data" .Release.Name) }}
{{- end -}}

{{- define "csa.container" -}}
image: {{ include "csa.image" . | quote }}
imagePullPolicy: {{ .Values.image.pullPolicy }}
envFrom:
  - configMapRef: {name: {{ .Release.Name }}}
  - secretRef: {name: {{ .Values.llm.secretName }}, optional: true}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
volumeMounts:
  - {name: data, mountPath: /app/data}
  - {name: config, mountPath: /etc/csa, readOnly: true}
  - {name: tmp, mountPath: /tmp}
  - {name: home, mountPath: /home/csa}
{{- end -}}

{{- define "csa.etlProbe" -}}
exec:
  command: ["/app/.venv/bin/python", "-c", "import os,sys,time; p='/tmp/etl-heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<120 else 1)"]
{{- end -}}

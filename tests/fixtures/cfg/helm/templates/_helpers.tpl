{{- define "chart.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 -}}
{{- end -}}
{{- define "chart.labels" -}}
app: {{ include "chart.name" . }}
{{- end -}}
{{- define "chart.unused" -}}
nothing
{{- end -}}

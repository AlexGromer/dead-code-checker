{{/*
A comment that mentions {{ define "ghost" }} and {{ end }}: no text scan may read it.
*/}}
{{- define "app.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 -}}
{{- end -}}

{{- define "app.labels" -}}
app: {{ include "app.name" . }}
{{- with .Values.extraLabels }}
{{- range $k, $v := . }}
{{ $k }}: {{ $v | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "app.unused" -}}
unused
{{- end -}}

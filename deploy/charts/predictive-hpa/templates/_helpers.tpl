{{/*
名称模板：截断到 63 字符（K8s name 长度上限），去掉尾部 -
*/}}
{{- define "predictive-hpa.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
完整名称：release-name + chart-name，处理 release 名已含 chart 名的情况
*/}}
{{- define "predictive-hpa.fullname" -}}
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
chart 标识：name-version
*/}}
{{- define "predictive-hpa.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
公共 label（含 K8s 推荐的 app.kubernetes.io/* 标准标签）
*/}}
{{- define "predictive-hpa.labels" -}}
helm.sh/chart: {{ include "predictive-hpa.chart" . }}
{{ include "predictive-hpa.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
selector label（Deployment selector 与 Pod template 必须匹配，且不可变）
*/}}
{{- define "predictive-hpa.selectorLabels" -}}
app.kubernetes.io/name: {{ include "predictive-hpa.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount 名称
*/}}
{{- define "predictive-hpa.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "predictive-hpa.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

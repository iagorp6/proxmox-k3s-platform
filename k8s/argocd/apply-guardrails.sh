#!/usr/bin/env bash
# Aplica o que fica fora do autosync (make guardrails): namespaces com Pod Security, AppProjects e o
# app raiz (guardrails.generated.yaml e root.yaml). Roda de novo depois de um jogo novo no catálogo.
# Server-side apply com field manager próprio: os labels de Pod Security não brigam com o apply do Argo
# CD nos namespaces que ele ainda rastreia.
set -euo pipefail
cd "$(dirname "$0")"
kubectl apply --server-side --force-conflicts --field-manager=homelab-guardrails -f guardrails.generated.yaml
kubectl apply --server-side --force-conflicts --field-manager=homelab-guardrails -f root.yaml
# Os namespaces saíram dos manifests dos apps: sem o rastreio do Argo, eles não aparecem como recurso a
# apagar (OutOfSync) nos apps. A anotação Prune=false já impede o Argo de apagar o namespace.
for ns in $(kubectl get -f guardrails.generated.yaml -o jsonpath='{range .items[?(@.kind=="Namespace")]}{.metadata.name}{"\n"}{end}'); do
  kubectl annotate namespace "$ns" argocd.argoproj.io/tracking-id- >/dev/null 2>&1 || true
  kubectl label namespace "$ns" app.kubernetes.io/instance- >/dev/null 2>&1 || true
done
echo "OK: namespaces, AppProjects e root aplicados"
kubectl -n argocd get appprojects

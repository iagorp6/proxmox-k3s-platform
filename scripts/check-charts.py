#!/usr/bin/env python3
"""Renderiza os charts de terceiros com os values do repositório e valida o resultado.

Os Applications multi-source (k8s/argocd/apps/*.yaml, ADR 0007) e o Argo CD (chart-version-argocd.txt,
make deploy-argocd) só rodam o Helm no cluster: um erro nos values-*.yaml, ou um chart novo do Renovate
que não aceita mais uma chave, só apareceria no Sync. Aqui, para cada chart:
  1. `helm template` na versão fixada, com os values do repo (falha em YAML inválido e no
     values.schema.json dos charts que têm um);
  2. o resultado passa no `kubeconform -strict` (recursos de CRD pelo catálogo fixado, o mesmo do CI;
     as próprias CustomResourceDefinitions ficam de fora, o kubeconform não tem schema para elas).
Uso: python3 scripts/check-charts.py <schema-location do catálogo de CRDs> [--print]
"""
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
APPS = ROOT / "k8s" / "argocd" / "apps"
ARGOCD = ROOT / "k8s" / "argocd"
ARGO_REPO = "https://argoproj.github.io/argo-helm"
# CRDs que o kube-prometheus-stack instala: os ServiceMonitors dos outros charts só renderizam com eles
API_VERSIONS = ["monitoring.coreos.com/v1", "monitoring.coreos.com/v1/ServiceMonitor"]


def kube_version():
    """Versão do Kubernetes do cluster (k3s_version do Ansible), para os charts que conferem kubeVersion."""
    text = (ROOT / "ansible" / "roles" / "k3s" / "defaults" / "main.yml").read_text(encoding="utf-8")
    return re.search(r'k3s_version:\s*"v(\d+\.\d+\.\d+)', text).group(1)


def charts():
    """(nome, release, repo, chart, versão, namespace, values) de cada chart fixado no repositório."""
    for f in sorted(APPS.glob("*.yaml")):
        app = yaml.safe_load(f.read_text(encoding="utf-8"))
        spec = app["spec"]
        for src in spec.get("sources", []):
            if "chart" not in src:
                continue
            helm = src.get("helm", {})
            values = [ROOT / v.removeprefix("$values/") for v in helm.get("valueFiles", [])]
            yield (app["metadata"]["name"], helm.get("releaseName", app["metadata"]["name"]), src["repoURL"],
                   src["chart"], src["targetRevision"], spec["destination"]["namespace"], values)
    version = (ARGOCD / "chart-version-argocd.txt").read_text(encoding="utf-8").strip()
    yield ("argocd", "argocd", ARGO_REPO, "argo-cd", version, "argocd", [ARGOCD / "values-argocd.yaml"])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    catalog, dry = sys.argv[1], "--print" in sys.argv
    kv = kube_version()
    failed = []
    for name, release, repo, chart, version, ns, values in charts():
        missing = [str(v.relative_to(ROOT)) for v in values if not v.exists()]
        if missing:
            failed.append(f"{name}: values inexistente(s): {', '.join(missing)}")
            continue
        cmd = ["helm", "template", release, chart, "--repo", repo, "--version", version, "--namespace", ns,
               "--kube-version", kv, *[a for v in API_VERSIONS for a in ("--api-versions", v)],
               *[a for v in values for a in ("-f", str(v))]]
        print(f"== {name}: {chart} {version}")
        if dry:
            print("  " + " ".join(cmd))
            continue
        rendered = subprocess.run(cmd, capture_output=True, text=True)
        if rendered.returncode != 0:
            failed.append(f"{name}: helm template falhou\n{rendered.stderr.strip()}")
            continue
        # O catálogo padrão do kubeconform não tem schema de CustomResourceDefinition (os CRDs vêm do chart)
        check = subprocess.run(["kubeconform", "-strict", "-summary", "-skip", "CustomResourceDefinition",
                                "-schema-location", "default",
                                "-schema-location", catalog], input=rendered.stdout, capture_output=True, text=True)
        print(check.stdout.strip().splitlines()[-1] if check.stdout.strip() else "")
        if check.returncode != 0:
            failed.append(f"{name}: kubeconform reprovou o chart renderizado\n{(check.stdout + check.stderr).strip()}")
    if failed:
        print("ERRO:\n" + "\n".join(failed))
        return 1
    print("OK: todos os charts renderizam e validam com os values do repositório")
    return 0


if __name__ == "__main__":
    sys.exit(main())

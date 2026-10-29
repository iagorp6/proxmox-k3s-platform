#!/usr/bin/env python3
"""Confere que cada Application cabe no AppProject dela e que cada pod cumpre o Pod Security do namespace.

Os AppProjects e os namespaces com Pod Security (k8s/argocd/guardrails.generated.yaml e root.yaml) só são
aplicados à mão (make guardrails). Sem este check, um manifest que o projeto ou o Pod Security recusa só
apareceria no Sync, com o app quebrado no cluster. Para cada Application de k8s/argocd/apps e o root:
  1. projeto existe e não é o default (que fica vazio, root.yaml);
  2. cada repoURL está no sourceRepos e o destino está nas destinations do projeto;
  3. os manifests de cada fonte Git (kustomize ou diretório) só têm recursos de cluster permitidos e só
     caem em namespaces do projeto (os charts passam pelo mesmo teste no check-charts.py);
  4. todo namespace de destino tem Namespace com pod-security.kubernetes.io/enforce no guardrails;
  5. todo pod (Deployment, StatefulSet, CronJob...) cumpre o nível do namespace dele (baseline ou
     restricted), incluindo os Jobs que o scripts/restore-drill.py cria nos namespaces dos jogos.
Uso: python3 scripts/check-guardrails.py   (precisa do kubectl no PATH para o kustomize)
"""
import fnmatch
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARGOCD = ROOT / "k8s" / "argocd"
SYSTEM_NAMESPACES = {"argocd", "kube-system"}   # instalados fora do Git (Helm do Argo CD e K3s)
# Kinds de cluster que aparecem no repositório e nos charts (o resto é tratado como namespaced)
CLUSTER_KINDS = {"Namespace", "CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding", "PersistentVolume",
                 "StorageClass", "PriorityClass", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration",
                 "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding", "APIService",
                 "ClusterSecretStore", "ClusterExternalSecret", "ClusterGenerator", "IngressClass", "RuntimeClass"}
POD_TEMPLATE = {"Deployment": ["spec", "template", "spec"], "StatefulSet": ["spec", "template", "spec"],
                "DaemonSet": ["spec", "template", "spec"], "ReplicaSet": ["spec", "template", "spec"],
                "Job": ["spec", "template", "spec"], "CronJob": ["spec", "jobTemplate", "spec", "template", "spec"],
                "Pod": ["spec"]}
BASELINE_CAPS = {"AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE",
                 "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT"}
RESTRICTED_VOLUMES = {"configMap", "csi", "downwardAPI", "emptyDir", "ephemeral", "persistentVolumeClaim",
                      "projected", "secret"}


class Loader(yaml.SafeLoader):
    """SafeLoader que lê `=` como texto: no YAML 1.1 do PyYAML ele é a tag !!value, sem construtor, e os
    CRDs dos charts têm `- =` em enums (o Kubernetes lê YAML 1.2, onde é só uma string)."""


Loader.add_constructor("tag:yaml.org,2002:value", Loader.construct_yaml_str)


def load_docs(text):
    return [d for d in yaml.load_all(text, Loader=Loader) if d]


def load_yaml(path):
    return load_docs(path.read_text(encoding="utf-8"))


def guardrails():
    """(projetos por nome, nível de Pod Security por namespace)."""
    docs = load_yaml(ARGOCD / "guardrails.generated.yaml") + load_yaml(ARGOCD / "root.yaml")
    projects = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "AppProject"}
    levels = {d["metadata"]["name"]: (d["metadata"].get("labels") or {}).get("pod-security.kubernetes.io/enforce")
              for d in docs if d["kind"] == "Namespace"}
    return projects, levels


def applications():
    docs = [d for f in sorted((ARGOCD / "apps").glob("*.yaml")) for d in load_yaml(f)]
    return [d for d in docs + load_yaml(ARGOCD / "root.yaml") if d["kind"] == "Application"]


def expand_braces(pattern):
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    return [p for alt in m.group(1).split(",") for p in expand_braces(pattern[:m.start()] + alt + pattern[m.end():])]


def render_source(src):
    """Manifests de uma fonte Git do repositório (kustomize se houver kustomization.yaml)."""
    path = ROOT / src["path"]
    if (path / "kustomization.yaml").exists():
        out = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"kubectl kustomize {src['path']} falhou: {out.stderr.strip()}")
        return load_docs(out.stdout)
    include = expand_braces((src.get("directory") or {}).get("include", "*.yaml"))
    files = sorted(f for f in path.glob("*.yaml") if any(fnmatch.fnmatch(f.name, pat) for pat in include))
    return [d for f in files for d in load_yaml(f)]


def kind_allowed(rules, group, kind):
    return any(fnmatch.fnmatch(group, r.get("group", "")) and fnmatch.fnmatch(kind, r.get("kind", "")) for r in rules)


def project_violations(app_name, project, docs, default_ns):
    """Recursos que o AppProject recusaria no Sync."""
    errors = []
    allowed_ns = [d.get("namespace", "") for d in project.get("destinations", [])]
    for d in docs:
        kind, group = d["kind"], d["apiVersion"].rpartition("/")[0]
        what = f"{app_name}: {kind}/{d['metadata'].get('name')}"
        if kind in CLUSTER_KINDS:
            if not kind_allowed(project.get("clusterResourceWhitelist", []), group, kind):
                errors.append(f"{what}: recurso de cluster fora do clusterResourceWhitelist do projeto")
            continue
        ns = d["metadata"].get("namespace") or default_ns
        if not any(fnmatch.fnmatch(ns, pat) for pat in allowed_ns):
            errors.append(f"{what}: namespace {ns} fora das destinations do projeto")
        if kind_allowed(project.get("namespaceResourceBlacklist", []), group, kind):
            errors.append(f"{what}: kind na namespaceResourceBlacklist do projeto")
        white = project.get("namespaceResourceWhitelist")
        if white is not None and not kind_allowed(white, group, kind):
            errors.append(f"{what}: kind fora da namespaceResourceWhitelist do projeto")
    return errors


def pod_spec(doc):
    spec = doc
    for key in POD_TEMPLATE.get(doc["kind"], []):
        spec = (spec or {}).get(key)
    return spec if doc["kind"] in POD_TEMPLATE else None


def pod_security_violations(doc, level):
    """Regras do Pod Security Standards (baseline e restricted) que um manifest consegue mostrar."""
    spec = pod_spec(doc)
    if not spec or level not in ("baseline", "restricted"):
        return []
    errors = []
    pod_sc = spec.get("securityContext") or {}
    containers = [*spec.get("initContainers", []), *spec.get("containers", [])]
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(flag):
            errors.append(f"{flag}: true")
    for v in spec.get("volumes", []):
        types = set(v) - {"name"}
        if "hostPath" in types:
            errors.append(f"volume {v['name']} é hostPath")
        elif level == "restricted" and not types <= RESTRICTED_VOLUMES:
            errors.append(f"volume {v['name']} do tipo {', '.join(sorted(types))} não é permitido no restricted")
    if (pod_sc.get("seccompProfile") or {}).get("type") == "Unconfined":
        errors.append("seccompProfile Unconfined no pod")
    for c in containers:
        sc = c.get("securityContext") or {}
        where = f"container {c['name']}"
        if sc.get("privileged"):
            errors.append(f"{where}: privileged")
        if any(p.get("hostPort") for p in c.get("ports", [])):
            errors.append(f"{where}: hostPort")
        added = set((sc.get("capabilities") or {}).get("add", []))
        if level == "baseline" and added - BASELINE_CAPS:
            errors.append(f"{where}: capabilities {', '.join(sorted(added - BASELINE_CAPS))} fora do baseline")
        seccomp = (sc.get("seccompProfile") or pod_sc.get("seccompProfile") or {}).get("type")
        if seccomp == "Unconfined":
            errors.append(f"{where}: seccompProfile Unconfined")
        if level != "restricted":
            continue
        if sc.get("allowPrivilegeEscalation") is not False:
            errors.append(f"{where}: falta allowPrivilegeEscalation: false")
        if "ALL" not in (sc.get("capabilities") or {}).get("drop", []):
            errors.append(f"{where}: falta capabilities.drop: [ALL]")
        if added - {"NET_BIND_SERVICE"}:
            errors.append(f"{where}: capabilities {', '.join(sorted(added - {'NET_BIND_SERVICE'}))} no restricted")
        non_root = sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot"))
        if non_root is not True:
            errors.append(f"{where}: falta runAsNonRoot: true (no pod ou no container)")
        if sc.get("runAsUser", pod_sc.get("runAsUser")) == 0:
            errors.append(f"{where}: runAsUser 0")
        if seccomp not in ("RuntimeDefault", "Localhost"):
            errors.append(f"{where}: falta seccompProfile RuntimeDefault (no pod ou no container)")
    return errors


def namespace_violations(app_name, docs, default_ns, levels):
    errors = []
    for d in docs:
        if d["kind"] in CLUSTER_KINDS:
            continue
        ns = d["metadata"].get("namespace") or default_ns
        if ns in SYSTEM_NAMESPACES:
            continue
        level = levels.get(ns)
        if not level:
            errors.append(f"{app_name}: {d['kind']}/{d['metadata'].get('name')} no namespace {ns}, que não tem "
                          "Namespace com pod-security.kubernetes.io/enforce no guardrails")
            continue
        errors += [f"{app_name}: {d['kind']}/{d['metadata'].get('name')} ({ns}, {level}): {e}"
                   for e in pod_security_violations(d, level)]
    return errors


def drill_docs(game):
    """Job e PVC que o scripts/restore-drill.py cria (drill e --live), como o CI já valida no kubeconform."""
    docs = []
    for extra in ([], ["--live"]):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "restore-drill.py"), game, *extra, "--dry-run"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"restore-drill.py {game} {' '.join(extra)} --dry-run falhou: {out.stderr.strip()}")
        docs += load_docs(out.stdout)
    return docs


def main():
    sys.path.insert(0, str(ROOT / "scripts"))
    import catalog

    projects, levels = guardrails()
    errors = []
    if projects.get("default", {}).get("destinations") or projects.get("default", {}).get("sourceRepos"):
        errors.append("root.yaml: o AppProject default precisa ficar sem sourceRepos e sem destinations")
    for app in applications():
        name, spec = app["metadata"]["name"], app["spec"]
        project = projects.get(spec.get("project", "default"))
        if spec.get("project", "default") == "default" or project is None:
            errors.append(f"{name}: projeto '{spec.get('project', 'default')}' inexistente ou default")
            continue
        sources = spec.get("sources") or [spec["source"]]
        dest_ns = spec["destination"]["namespace"]
        for src in sources:
            if not any(fnmatch.fnmatch(src["repoURL"], r) for r in project.get("sourceRepos", [])):
                errors.append(f"{name}: repoURL {src['repoURL']} fora do sourceRepos do projeto {spec['project']}")
        if not any(fnmatch.fnmatch(dest_ns, d.get("namespace", "")) for d in project.get("destinations", [])):
            errors.append(f"{name}: destino {dest_ns} fora das destinations do projeto {spec['project']}")
        if dest_ns not in SYSTEM_NAMESPACES and not levels.get(dest_ns):
            errors.append(f"{name}: namespace de destino {dest_ns} sem Pod Security no guardrails")
        for src in sources:
            if "path" not in src:
                continue   # charts: check-charts.py
            try:
                docs = render_source(src)
            except RuntimeError as exc:
                errors.append(f"{name}: {exc}")
                continue
            errors += project_violations(name, project, docs, dest_ns)
            errors += namespace_violations(name, docs, dest_ns, levels)
    for game in catalog.games():
        if game.get("backup"):
            try:
                errors += namespace_violations(f"restore-drill {game['name']}", drill_docs(game["name"]),
                                               game["namespace"], levels)
            except RuntimeError as exc:
                errors.append(str(exc))
    if errors:
        print("ERRO: guardrails (AppProjects e Pod Security):\n" + "\n".join(f"  {e}" for e in errors))
        return 1
    print(f"OK: {len(applications())} Applications cabem nos projetos; pods cumprem o Pod Security dos namespaces")
    return 0


if __name__ == "__main__":
    sys.exit(main())

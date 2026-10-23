# IAM access key rotation

Two IAM users still use long-lived access keys, both created by `terraform-aws/`:

| User | Used by | Where the key lives |
|---|---|---|
| `restic-k3s-zomboid` (name predates the other games; it serves all of them) | game backups, restore drills, weekly `restic-check` | SSM `/homelab/backup/restic-aws-*`, pulled into each namespace by External Secrets |
| `external-secrets-k3s` | External Secrets Operator itself | the bootstrap Secret `external-secrets/aws-ssm-credentials` (created by hand, see the [DR runbook](dr-runbook.md)) |

The weekly drift workflow (`.github/workflows/terraform-drift.yml`) fails when an active key is older than
180 days, or when a user has more than one key (a rotation left half done, or a key created outside Terraform).
Roles Anywhere would remove the keys altogether, but needs a CA; for now the answer is rotating on schedule.

Run everything from `~/homelab-infra` on `main`, up to date (`git pull`), with nobody playing and away from the
backup window (05:15 to 05:45).

## restic

`-replace` deletes the old key and creates a new one; `ssm.tf` writes the new one to SSM in the same apply.

```bash
cd terraform-aws && terraform apply -replace=aws_iam_access_key.restic
```

External Secrets refreshes every hour. To pick up the new key right away:

```bash
for es in minecraft/minecraft-backup terraria/terraria-backup zomboid/zomboid-backup platform/restic-check; do kubectl -n "${es%/*}" annotate externalsecret "${es#*/}" force-sync="$(date +%s)" --overwrite; done
```

Check that a backup works with the new key:

```bash
kubectl -n terraria create job --from=cronjob/terraria-backup backup-rotation-test
```

```bash
kubectl -n terraria wait --for=condition=complete job/backup-rotation-test --timeout=15m
```

## External Secrets

The new key has to go into the bootstrap Secret by hand, because External Secrets cannot fetch its own credential.
Until then, Secrets already in the cluster keep working; only refreshes fail (`ExternalSecretNotReady`).

```bash
cd terraform-aws && terraform apply -replace=aws_iam_access_key.external_secrets
```

```bash
kubectl -n external-secrets create secret generic aws-ssm-credentials --from-literal=access-key-id="$(terraform -chdir=terraform-aws output -raw eso_access_key_id)" --from-literal=secret-access-key="$(terraform -chdir=terraform-aws output -raw eso_secret_access_key)" --dry-run=client -o yaml | kubectl apply -f -
```

Check that the store reconnects (`Ready True`):

```bash
kubectl get clustersecretstore aws-ssm
```

Then run the drift workflow by hand (Actions > Terraform drift (aws) > Run workflow) to confirm the new key ages are clean.

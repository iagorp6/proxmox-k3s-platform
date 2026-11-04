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

## Without local Terraform

Changes to `terraform-aws/` are never applied by CI: the GitHub OIDC role is read-only on purpose. If the
local Terraform cannot run (on the work PC, WSL has crashed while extracting the AWS provider, most likely
the endpoint protection), there are two ways out.

**Removing resources the code no longer has** (a PR deleted them, the drift plan shows only `delete`
lines): delete them with the AWS CLI, using the names from the drift log or the removed code. This is what
closed the [drift incident of 2026-10-26](postmortems/2026-10-26-terraform-drift-red.md):

```bash
aws --profile homelab --region sa-east-1 cloudwatch delete-alarms --alarm-names homelab-backup-bucket-size
```

```bash
aws --profile homelab --region sa-east-1 sns delete-topic --topic-arn "$(aws --profile homelab --region sa-east-1 sns list-topics --query "Topics[?ends_with(TopicArn, ':homelab-alerts')].TopicArn | [0]" --output text)"
```

The next `terraform plan` refreshes, sees the resources gone and reports no changes; the state drops
them on the next apply.

**Anything that creates or changes resources** (including the key rotations above): run Terraform from
AWS CloudShell in `sa-east-1`, with the version CI pins (`terraform_version` in
`.github/workflows/terraform-drift.yml`), on a fresh clone of `main`. The backend is the same S3 state with
its lock, so a plan there sees exactly what a local plan would.

Either way, **a `terraform-aws` change is done only when the drift workflow is green**: run it by hand
(Actions > Terraform drift (aws) > Run workflow) right after, instead of waiting for Monday.

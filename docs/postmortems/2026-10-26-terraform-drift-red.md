# Drift check red for four days because an apply was never run

- **Date:** 2026-10-26 to 2026-10-30 · **Duration:** about 4 days (first red run 2026-10-26 12:52, first green run 2026-10-30 14:57, America/Sao_Paulo) · **Severity:** blind spot, no player or data impact
- **Detected by:** the drift workflow itself, on every run (GitHub failure email, plus the Discord post added in #118); acted on four days later

## Summary

The weekly drift plan on `terraform-aws/` failed first because an unconfirmed SNS email subscription
had expired, and then, after #118 removed the SNS topic and its CloudWatch alarm from the code, because
nobody ran the `apply` that deletes them from AWS. The project notes recorded the apply as done without
checking. Four runs in a row stayed red with the same two "delete" lines, which is exactly the signal the
check exists to make loud. The two resources were deleted with the AWS CLI and a manual run came back
green.

## Impact

- No production impact: nothing in AWS was wrong, the extra topic and alarm were just unused.
- For four days the drift check could not report a real change made outside Terraform: any new drift
  would have hidden behind the known one.
- The cost that matters is trust: a check that stays red for a known reason teaches people to skip it.

## Timeline (America/Sao_Paulo)

| Time | Event |
|---|---|
| 2026-10-26 12:52 | Run 14 (scheduled): fails, Terraform wants to recreate the SNS email subscription, which AWS deletes when it is not confirmed within 3 days |
| 2026-10-26 17:14 | #118 merged: SNS topic, subscription and CloudWatch alarm removed from the code, bucket size checked by the weekly `restic-check` instead, drift failures posted to Discord |
| 2026-10-26 17:14 | Run 15 (push of #118): fails with `delete aws_cloudwatch_metric_alarm.backup_bucket_size` and `delete aws_sns_topic.alerts`. The apply is a manual step and is not run |
| 2026-10-29 17:42 | Run 16 (push of #124): same two deletes |
| 2026-10-30 morning | Review notices the last runs are red; the notes said the apply had been done |
| 2026-10-30 14:06 | Run 17 (push of #113): same two deletes |
| 2026-10-30 afternoon | Local `terraform apply` impossible: WSL on the work PC crashes while extracting the AWS provider. Alarm and topic deleted with the AWS CLI instead |
| 2026-10-30 14:57 | Run 18 (manual): green |
| 2026-11-02 16:13 | Run 19 (scheduled): green |

## Root cause

`terraform-aws/` has no apply pipeline on purpose: the GitHub OIDC role is read-only, so merging a change
there does not deploy it. #118 removed resources, and its follow-up apply lived only in the PR description
and in a note that was marked done from memory. Nothing tied "done" to the drift run turning green, and
when the time came to apply, the only machine with Terraform could not run it.

## What went well

- The drift check did its job on every run, with the exact resource addresses in the log and no values.
- The resources were trivial to remove by hand once someone looked, and the next run confirmed it.

## What went badly

- Four red runs before action. The Discord post went to a channel that also carried player events
  until #124 split infra into `#ops`.
- "Applied" was recorded without evidence.
- There was no written path for applying without local Terraform.

## Action items

| Action | Type | Status |
|---|---|---|
| Replace the SNS/CloudWatch alarm (which broke whenever an email subscription expired) with a size check in `restic-check` | prevent | done (#118) |
| Drift failures post to Discord, and infra warnings go to a private `#ops` channel without player noise | detect | done (#118, #124) |
| A `terraform-aws` change counts as applied only when a manual drift run is green; written in [key-rotation.md](../key-rotation.md#without-local-terraform) together with the AWS CLI / CloudShell path used here | process | done (this PR) |
| Keep the drift role read-only (no apply workflow): the blast radius of a write role in a public repository is not worth saving a manual step that happens a few times a year | decision | kept |

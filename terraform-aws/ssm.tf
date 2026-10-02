data "aws_caller_identity" "current" {}

locals {
  ssm_prefix = "/homelab"
}

# ---------- Segredos gerados pelo Terraform -> SSM ----------
resource "aws_ssm_parameter" "restic_repository" {
  name  = "${local.ssm_prefix}/backup/restic-repository"
  type  = "String"
  value = "s3:s3.${var.region}.amazonaws.com/${aws_s3_bucket.backups.id}/zomboid"
}

resource "aws_ssm_parameter" "aws_region" {
  name  = "${local.ssm_prefix}/backup/aws-region"
  type  = "String"
  value = var.region
}

resource "aws_ssm_parameter" "restic_aws_access_key_id" {
  name  = "${local.ssm_prefix}/backup/restic-aws-access-key-id"
  type  = "SecureString"
  value = aws_iam_access_key.restic.id
}

resource "aws_ssm_parameter" "restic_aws_secret_access_key" {
  name  = "${local.ssm_prefix}/backup/restic-aws-secret-access-key"
  type  = "SecureString"
  value = aws_iam_access_key.restic.secret
}

# ---------- IAM: External Secrets Operator (somente leitura em /homelab/*) ----------
resource "aws_iam_user" "external_secrets" {
  name = "external-secrets-k3s"
}

data "aws_iam_policy_document" "external_secrets" {
  statement {
    sid     = "ReadHomelabParameters"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}/*",
    ]
  }
  statement {
    sid       = "DecryptViaSSMOnly"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_user_policy" "external_secrets" {
  name   = "ssm-read-homelab"
  user   = aws_iam_user.external_secrets.name
  policy = data.aws_iam_policy_document.external_secrets.json
}

resource "aws_iam_access_key" "external_secrets" {
  user = aws_iam_user.external_secrets.name
}

output "eso_access_key_id" {
  value = aws_iam_access_key.external_secrets.id
}

output "eso_secret_access_key" {
  value     = aws_iam_access_key.external_secrets.secret
  sensitive = true
}

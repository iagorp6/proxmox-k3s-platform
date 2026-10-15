# ---------- GitHub Actions -> AWS via OIDC (sem chave de longa duração) ----------
# O workflow terraform-drift.yml assume esta role com o token OIDC do próprio run e roda um
# terraform plan deste diretório. Só o branch main assume a role: PR não chega aqui, porque o
# state guarda as chaves dos usuários IAM e o código de um PR poderia vazar o que lê.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_drift_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_drift" {
  name                 = "github-actions-terraform-drift"
  description          = "terraform plan do terraform-aws pelo GitHub Actions (somente leitura)"
  assume_role_policy   = data.aws_iam_policy_document.github_drift_trust.json
  max_session_duration = 3600
}

# Metadados de tudo (Describe/Get/List) pela policy gerenciada, que acompanha as APIs novas do
# provider sem manutenção. Os dados ficam de fora pelos Deny abaixo.
resource "aws_iam_role_policy_attachment" "github_drift_readonly" {
  role       = aws_iam_role.github_drift.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "github_drift_data" {
  # Objetos S3: só o state deste diretório (o backup e o state do Proxmox ficam de fora)
  statement {
    sid           = "DenyObjectsExceptThisState"
    effect        = "Deny"
    actions       = ["s3:GetObject", "s3:GetObjectVersion"]
    not_resources = ["${aws_s3_bucket.tfstate.arn}/aws/terraform.tfstate"]
  }
  # Parâmetros SSM: só os que este diretório gerencia (os segredos dos jogos e do bot ficam de fora)
  statement {
    sid           = "DenyParametersExceptBackup"
    effect        = "Deny"
    actions       = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory"]
    not_resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}/backup/*"]
  }
  statement {
    sid       = "DenySecretValues"
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }
  # O provider lê SecureString com decrypt: libera só via SSM e só para /homelab/backup/*
  statement {
    sid       = "DecryptBackupParameters"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:PARAMETER_ARN"
      values   = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}/backup/*"]
    }
  }
}

resource "aws_iam_role_policy" "github_drift_data" {
  name   = "state-and-backup-parameters-only"
  role   = aws_iam_role.github_drift.id
  policy = data.aws_iam_policy_document.github_drift_data.json
}

output "github_drift_role_arn" {
  description = "Vai na variável AWS_DRIFT_ROLE_ARN do repositório (Settings > Secrets and variables > Actions)"
  value       = aws_iam_role.github_drift.arn
}

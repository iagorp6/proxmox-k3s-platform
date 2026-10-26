# Invariantes de segurança do terraform-aws como teste: se um PR afrouxar uma policy, o CI reprova.
# Roda sem credencial nem backend (terraform init -backend=false && terraform test): os providers
# são mock, então nada é criado na AWS. As policies são conferidas pelos blocos statement dos
# aws_iam_policy_document (o JSON renderizado é falso no mock), que é exatamente o que um PR mudaria.

mock_provider "aws" {
  # O mock geraria uma string qualquer no json, e os recursos que validam policy recusariam
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  # ARNs fixos e diferentes por bucket: o teste do restic precisa distinguir backup de state
  override_resource {
    target = aws_s3_bucket.backups
    values = { arn = "arn:aws:s3:::k3s-backups-test", id = "k3s-backups-test" }
  }
  override_resource {
    target = aws_s3_bucket.tfstate
    values = { arn = "arn:aws:s3:::tfstate-test", id = "tfstate-test" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test" }
  }
  mock_resource "aws_iam_user" {
    defaults = { arn = "arn:aws:iam::123456789012:user/test" }
  }
}

mock_provider "random" {}

variables {
  budget_email = "test@example.invalid"
}

run "restic_so_le_e_grava_objetos_do_bucket_de_backup" {
  command = apply

  # Sem s3:DeleteObjectVersion e sem nada de versionamento, lifecycle ou policy do bucket: com o
  # versionamento, um DeleteObject vira versão não corrente e o backup continua recuperável
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.restic.statement : alltrue([
        for a in s.actions : contains(["s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"], a)
      ])
    ])
    error_message = "A policy do restic só pode ter s3:ListBucket, s3:GetBucketLocation, s3:GetObject, s3:PutObject e s3:DeleteObject."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.restic.statement : alltrue([
        for r in coalesce(s.resources, []) : contains([aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"], r)
      ]) && (s.effect == null || s.effect == "Allow") && s.not_resources == null && s.not_actions == null
    ])
    error_message = "A policy do restic só pode valer para o bucket de backup e os objetos dele, sem NotResource/NotAction."
  }

  assert {
    condition     = aws_s3_bucket_versioning.backups.versioning_configuration[0].status == "Enabled"
    error_message = "O bucket de backup precisa de versionamento: é ele que segura um DeleteObject do restic."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.backups.block_public_acls,
      aws_s3_bucket_public_access_block.backups.block_public_policy,
      aws_s3_bucket_public_access_block.backups.ignore_public_acls,
      aws_s3_bucket_public_access_block.backups.restrict_public_buckets,
      aws_s3_bucket_public_access_block.tfstate.block_public_acls,
      aws_s3_bucket_public_access_block.tfstate.block_public_policy,
      aws_s3_bucket_public_access_block.tfstate.ignore_public_acls,
      aws_s3_bucket_public_access_block.tfstate.restrict_public_buckets,
    ])
    error_message = "Os buckets de backup e de state precisam bloquear todo acesso público."
  }
}

run "external_secrets_so_le_parametros_do_homelab" {
  command = apply

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.external_secrets.statement : alltrue([
        for a in s.actions : contains(["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "kms:Decrypt"], a)
      ]) && (s.effect == null || s.effect == "Allow")
    ])
    error_message = "O External Secrets só pode ler parâmetros do SSM (e decifrar via SSM): nada de escrita nem de outro serviço."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.external_secrets.statement : alltrue([
        for r in coalesce(s.resources, []) : startswith(r, "arn:aws:ssm:sa-east-1:123456789012:parameter/homelab/")
      ]) if contains(s.actions, "ssm:GetParameter")
    ])
    error_message = "A leitura de parâmetros do External Secrets precisa ficar restrita a /homelab/*."
  }

  # kms:Decrypt com Resource * só é aceitável preso ao SSM
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.external_secrets.statement : anytrue([
        for c in coalesce(s.condition, []) : c.variable == "kms:ViaService" && c.test == "StringEquals" && toset(c.values) == toset(["ssm.sa-east-1.amazonaws.com"])
      ]) if contains(s.actions, "kms:Decrypt")
    ])
    error_message = "kms:Decrypt do External Secrets precisa da condição kms:ViaService = ssm.<região>.amazonaws.com."
  }
}

run "role_de_drift_so_para_o_main_e_sem_dados" {
  command = apply

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.github_drift_trust.statement :
      toset(s.actions) == toset(["sts:AssumeRoleWithWebIdentity"]) && anytrue([
        for c in coalesce(s.condition, []) : c.test == "StringEquals" && c.variable == "token.actions.githubusercontent.com:sub"
        && toset(c.values) == toset(["repo:${var.github_repository}:ref:refs/heads/main"])
        ]) && anytrue([
        for c in coalesce(s.condition, []) : c.test == "StringEquals" && c.variable == "token.actions.githubusercontent.com:aud"
        && toset(c.values) == toset(["sts.amazonaws.com"])
      ])
    ])
    error_message = "A trust da role de drift precisa exigir aud sts.amazonaws.com e sub exato do main (StringEquals, sem curinga): PR e outros branches não podem assumir."
  }

  assert {
    condition     = aws_iam_role_policy_attachment.github_drift_readonly.policy_arn == "arn:aws:iam::aws:policy/ReadOnlyAccess"
    error_message = "A role de drift só recebe a ReadOnlyAccess gerenciada."
  }

  assert {
    condition = alltrue([
      for sid in ["DenyObjectsExceptThisState", "DenyParametersExceptBackup", "DenySecretValues"] :
      anytrue([for s in data.aws_iam_policy_document.github_drift_data.statement : s.sid == sid && s.effect == "Deny"])
    ])
    error_message = "A role de drift precisa dos Deny de objetos S3, parâmetros SSM e Secrets Manager: a ReadOnlyAccess leria backups e segredos dos jogos."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.github_drift_data.statement :
      s.sid == "DecryptBackupParameters" ? toset(s.actions) == toset(["kms:Decrypt"]) : s.effect == "Deny"
    ])
    error_message = "Fora o kms:Decrypt condicionado, todo statement da policy extra da role de drift precisa ser Deny."
  }
}

run "curinga_no_repositorio_da_trust_e_recusado" {
  command = plan

  variables {
    github_repository = "iagorp6@108373479/*"
  }

  expect_failures = [var.github_repository]
}

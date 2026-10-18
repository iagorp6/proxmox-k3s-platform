variable "region" {
  description = "Região AWS"
  type        = string
  default     = "sa-east-1"
}

variable "aws_profile" {
  description = "Profile do AWS CLI usado pelo Terraform"
  type        = string
  default     = "homelab"
}

variable "budget_email" {
  description = "E-mail que recebe os alertas de custo"
  type        = string
}

variable "budget_limit_usd" {
  description = "Limite mensal de custo (USD) para alerta"
  type        = string
  default     = "1"
}

variable "backup_size_alert_gb" {
  description = "Tamanho do bucket de backup (GB) a partir do qual o alarme dispara"
  type        = number
  default     = 15
}

variable "github_repository" {
  # O GitHub emite o sub do token OIDC com os IDs imutáveis do dono e do repositório
  # (repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:...): um repositório recriado com o mesmo nome
  # tem outro ID e não assume a role. O workflow de drift imprime o sub recebido.
  description = "Repositório no formato do sub OIDC (<owner>@<owner_id>/<repo>@<repo_id>) cujo main pode assumir a role de drift"
  type        = string
  default     = "iagorp6@108373479/proxmox-k3s-platform@1392447632"
}

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
  description = "Repositório (owner/nome) cujo branch main pode assumir a role de drift via OIDC"
  type        = string
  default     = "iagorp6/proxmox-k3s-platform"
}

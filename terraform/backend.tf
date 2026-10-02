terraform {
  backend "s3" {
    bucket       = "tfstate-k3s-fccfba"
    key          = "proxmox/terraform.tfstate"
    region       = "sa-east-1"
    profile      = "homelab"
    encrypt      = true
    use_lockfile = true
  }
}

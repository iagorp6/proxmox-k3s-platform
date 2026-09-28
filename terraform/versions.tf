terraform {
  required_version = ">= 1.9.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.75.0, < 2.0.0"
    }
  }
}

# endpoint, api_token e insecure vêm das variáveis de ambiente:
#   PROXMOX_VE_ENDPOINT, PROXMOX_VE_API_TOKEN, PROXMOX_VE_INSECURE
# (source ~/.proxmox.env antes de rodar o terraform)
provider "proxmox" {
  # SSH é usado pelo provider só para upload do snippet de cloud-init.
  # Todo o resto (VM, download da imagem) vai pela API com o token.
  ssh {
    username    = "root"
    private_key = file(pathexpand(var.pve_ssh_private_key_path))

    node {
      name    = var.node_name
      address = var.pve_host_address
    }
  }
}

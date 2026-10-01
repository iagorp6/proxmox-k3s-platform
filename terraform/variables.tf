# ---------- Proxmox host ----------
variable "node_name" {
  description = "Nome do node Proxmox"
  type        = string
  default     = "pve-01"
}

variable "pve_host_address" {
  description = "IP do host Proxmox (usado pelo SSH do provider)"
  type        = string
}

variable "pve_ssh_private_key_path" {
  description = "Chave privada com acesso root@pve (upload de snippets)"
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "vm_datastore" {
  description = "Storage dos discos da VM"
  type        = string
  default     = "local-zfs"
}

variable "files_datastore" {
  description = "Storage de imagens importadas e snippets"
  type        = string
  default     = "local"
}

variable "bridge" {
  description = "Bridge de rede do Proxmox"
  type        = string
  default     = "vmbr0"
}

# ---------- Cloud image ----------
variable "cloud_image_url" {
  description = "URL da cloud image Ubuntu 26.04 (qcow2)"
  type        = string
  default     = "https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img"
}

# ---------- VM ----------
variable "vm_id" {
  description = "VMID no Proxmox"
  type        = number
  default     = 100
}

variable "vm_name" {
  description = "Nome/hostname da VM"
  type        = string
  default     = "k3s-01"
}

variable "vm_cores" {
  description = "vCPUs (host tem 4 cores físicos, sem HT)"
  type        = number
  default     = 4
}

variable "vm_memory_mb" {
  description = "RAM dedicada, sem ballooning"
  type        = number
  default     = 18432
}

variable "vm_disk_gb" {
  description = "Tamanho do disco de sistema"
  type        = number
  default     = 80
}

variable "vm_ip" {
  description = "IPv4 reservado no DHCP para o MAC da VM (usado nos outputs)"
  type        = string

  validation {
    condition     = can(cidrhost("${var.vm_ip}/32", 0))
    error_message = "vm_ip precisa ser um IPv4 válido, sem /máscara."
  }
}

variable "vm_mac_address" {
  description = "MAC fixo da VM, precisa bater com a reserva DHCP"
  type        = string

  validation {
    condition     = can(regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", var.vm_mac_address))
    error_message = "vm_mac_address precisa estar no formato AA:BB:CC:DD:EE:FF."
  }
}

# ---------- Acesso ----------
variable "admin_username" {
  description = "Usuário administrativo criado pelo cloud-init"
  type        = string
  default     = "ops"
}

variable "ssh_public_key_path" {
  description = "Chave pública autorizada no usuário da VM"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "timezone" {
  description = "Timezone da VM"
  type        = string
  default     = "America/Sao_Paulo"
}

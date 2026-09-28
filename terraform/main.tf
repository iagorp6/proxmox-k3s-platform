# Cloud image baixada direto pelo node Proxmox (content type "import", PVE 8.4+)
resource "proxmox_virtual_environment_download_file" "ubuntu_2604" {
  content_type = "import"
  datastore_id = var.files_datastore
  node_name    = var.node_name
  url          = var.cloud_image_url
  file_name    = "ubuntu-26.04-server-cloudimg-amd64.qcow2"
  overwrite    = false
}

# User-data do cloud-init gravado como snippet no storage "local"
resource "proxmox_virtual_environment_file" "user_data" {
  content_type = "snippets"
  datastore_id = var.files_datastore
  node_name    = var.node_name

  source_raw {
    file_name = "${var.vm_name}-user-data.yaml"
    data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
      hostname       = var.vm_name
      admin_username = var.admin_username
      ssh_public_key = trimspace(file(pathexpand(var.ssh_public_key_path)))
      timezone       = var.timezone
    })
  }
}

# Network-config do cloud-init (netplan v2), com client-id = MAC para casar com a reserva DHCP
resource "proxmox_virtual_environment_file" "network_config" {
  content_type = "snippets"
  datastore_id = var.files_datastore
  node_name    = var.node_name

  source_raw {
    file_name = "${var.vm_name}-network-config.yaml"
    data = templatefile("${path.module}/network-config.yaml.tftpl", {
      mac_address = lower(var.vm_mac_address)
    })
  }
}

resource "proxmox_virtual_environment_vm" "k3s" {
  name        = var.vm_name
  node_name   = var.node_name
  vm_id       = var.vm_id
  description = "K3s single-node. Managed by Terraform."
  tags        = ["k3s", "terraform"]

  on_boot = true
  started = true
  machine = "q35"

  operating_system {
    type = "l26"
  }

  agent {
    enabled = true
    timeout = "15m"
  }

  cpu {
    cores = var.vm_cores
    type  = "host"
  }

  memory {
    dedicated = var.vm_memory_mb
    floating  = 0
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_datastore
    import_from  = proxmox_virtual_environment_download_file.ubuntu_2604.id
    interface    = "scsi0"
    size         = var.vm_disk_gb
    iothread     = true
    discard      = "on"
  }

  network_device {
    bridge      = var.bridge
    model       = "virtio"
    mac_address = var.vm_mac_address # fixo: casa com a reserva DHCP
  }

  # Cloud images do Ubuntu esperam console serial
  serial_device {}

  initialization {
    datastore_id         = var.vm_datastore
    user_data_file_id    = proxmox_virtual_environment_file.user_data.id
    network_data_file_id = proxmox_virtual_environment_file.network_config.id
  }
}

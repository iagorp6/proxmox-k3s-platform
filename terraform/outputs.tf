output "vm_id" {
  value = proxmox_virtual_environment_vm.k3s.vm_id
}

output "vm_ipv4" {
  value = var.vm_ip
}

output "ssh_command" {
  value = "ssh ${var.admin_username}@${var.vm_ip}"
}

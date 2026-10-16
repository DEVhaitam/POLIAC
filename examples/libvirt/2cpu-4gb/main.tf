resource "libvirt_domain" "vm" {
  name   = "correctexam-exp"
  memory = 4096
  vcpu   = 2

  cpu {
    mode = "host-passthrough"
  }

  disk {
    volume_id = libvirt_volume.vm_disk.id
  }

  network_interface {
    network_name   = "default"
    wait_for_lease = true
  }
}

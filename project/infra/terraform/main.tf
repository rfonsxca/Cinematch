locals {
  ssh_key = trimspace(file(pathexpand(var.ssh_public_key_file)))
  nodes = merge(
    { "master" = var.master_machine_type },
    { for i in range(var.worker_count) : "worker${i + 1}" => var.worker_machine_type }
  )
}

# Rede própria: não depende da rede "default", que algumas organizações desativam.
resource "google_compute_network" "cluster" {
  name                    = "cinematch"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "cluster" {
  name          = "cinematch"
  ip_cidr_range = "10.10.0.0/24"
  region        = var.region
  network       = google_compute_network.cluster.id
}

# Tráfego entre nós: API server, kubelet, VXLAN do Flannel (udp), etc.
resource "google_compute_firewall" "internal" {
  name          = "cinematch-internal"
  network       = google_compute_network.cluster.id
  source_ranges = [google_compute_subnetwork.cluster.ip_cidr_range]
  allow { protocol = "tcp" }
  allow { protocol = "udp" }
  allow { protocol = "icmp" }
}

resource "google_compute_firewall" "ssh" {
  name          = "cinematch-ssh"
  network       = google_compute_network.cluster.id
  source_ranges = [var.allowed_ssh_cidr]
  target_tags   = ["cluster"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Só o frontend é publicado fora do cluster. A API do Kubernetes (6443) NÃO é exposta:
# o kubectl corre no master, por SSH.
resource "google_compute_firewall" "frontend" {
  name          = "cinematch-frontend"
  network       = google_compute_network.cluster.id
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["cluster"]
  allow {
    protocol = "tcp"
    ports    = [tostring(var.frontend_node_port)]
  }
}

# IPs estáticos: sobrevivem a parar e voltar a ligar as VMs, e o inventário Ansible continua válido.
resource "google_compute_address" "node" {
  for_each = local.nodes
  name     = "cinematch-${each.key}"
  region   = var.region
}

resource "google_compute_instance" "node" {
  for_each     = local.nodes
  name         = each.key
  machine_type = each.value
  zone         = var.zone
  tags         = ["cluster"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.disk_size_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.cluster.id
    access_config {
      nat_ip = google_compute_address.node[each.key].address
    }
  }

  metadata = {
    ssh-keys = "ubuntu:${local.ssh_key}"
  }
}

# Gera o inventário que o Ansible usa, com os IPs públicos.
resource "local_file" "inventory" {
  filename        = "${path.module}/../ansible/inventory.ini"
  file_permission = "0644"
  content = templatefile("${path.module}/inventory.tftpl", {
    master_ip = google_compute_address.node["master"].address
    workers   = { for k, v in google_compute_address.node : k => v.address if k != "master" }
  })
}

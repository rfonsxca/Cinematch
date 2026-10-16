variable "credentials_file" {
  description = "Caminho (dentro do contentor mgmt) da chave JSON da conta de serviço."
  type        = string
  default     = "/keys/key.json"
}

variable "project_id" {
  description = "ID do projeto GCP (não o nome)."
  type        = string
  default     = "psia26-g28"
}

variable "region" {
  type    = string
  default = "europe-west4"
}

variable "zone" {
  type    = string
  default = "europe-west4-b"
}

# O kubeadm exige pelo menos 2 vCPU no control plane; o e2-medium (2 vCPU partilhados, 4 GB) chega.
variable "master_machine_type" {
  type    = string
  default = "e2-medium"
}

# Os workers correm o Ollama (imagem de 3,8 GB e o modelo em RAM), por isso levam 8 GB.
variable "worker_machine_type" {
  type    = string
  default = "e2-standard-2"
}

variable "worker_count" {
  type    = number
  default = 2
}

# A imagem do Ollama ocupa 3,8 GB, mais as camadas das restantes imagens e os volumes locais.
variable "disk_size_gb" {
  type    = number
  default = 30
}

variable "ssh_public_key_file" {
  description = "Chave pública SSH instalada nas VMs (utilizador ubuntu)."
  type        = string
  default     = "/keys/psia-gcp.pub"
}

variable "allowed_ssh_cidr" {
  description = "Origens autorizadas a ligar por SSH. Restringir ao IP do grupo se possível."
  type        = string
  default     = "0.0.0.0/0"
}

variable "frontend_node_port" {
  type    = number
  default = 30080
}

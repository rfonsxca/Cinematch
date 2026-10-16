terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

provider "google" {
  # A chave JSON da conta de serviço fica fora do repositório; o caminho vem de uma variável.
  credentials = file(pathexpand(var.credentials_file))
  project     = var.project_id
  region      = var.region
  zone        = var.zone
}

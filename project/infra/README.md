# Infraestrutura (GCP + Terraform + Ansible + kubeadm)

Cria 1 master e N workers na GCP (Terraform), instala o Kubernetes com kubeadm (Ansible) e faz o deploy
da aplicação. Kubernetes self-hosted: **não** se usa o GKE.

## Pré-requisitos (uma vez)
1. Projeto GCP `psia26-g28` com a Compute Engine API ativa e faturação associada.
2. Conta de serviço com o papel *Compute Admin*; chave JSON guardada em `~/.gcp/key.json` (fora do repositório).
3. Chave SSH dedicada: `ssh-keygen -t ed25519 -f ~/.ssh/psia-gcp -N ""`
4. Imagens da app publicadas no Docker Hub (repositório público `cinematch`; ver "Imagens").

## Passos
```bash
./infra/mgmt.sh                      # abre o contentor de gestão (terraform + ansible)
cd terraform && terraform init && terraform plan
terraform apply                      # cria as VMs e gera ansible/inventory.ini
cd ../ansible
ansible-playbook site.yml
```
A app fica em `terraform output frontend_url`. Grafana: no master, `kubectl -n cinematch port-forward svc/grafana 3000:3000`.

## Imagens
Os nós descarregam as imagens de um repositório público do Docker Hub (as duas partilham o repositório, com tags
diferentes). Construir e publicar a partir de `project/` (requer `docker login`):
```bash
R=docker.io/UTILIZADOR/cinematch
docker build -t $R:services-v1 -f services/Dockerfile .
docker build -t $R:frontend-v1 -f frontend/Dockerfile .
docker push $R:services-v1 && docker push $R:frontend-v1
```
Se o repositório for privado, criar um access token **só de leitura** e passá-lo ao Ansible com
`-e registry_username=... -e registry_password=...` (nunca guardar no Git).
Ajustar `image_repo`/`services_tag`/`frontend_tag` em `ansible/group_vars/all.yml` se o repositório for outro.

## Custo: desligar quando não se usa
As VMs gastam crédito enquanto existirem. No fim de cada sessão: `terraform destroy` (dentro de `infra/terraform`).
O estado do Terraform (`terraform.tfstate`) fica só na máquina de quem o executou: **apenas uma pessoa** deve correr
`apply`/`destroy`.

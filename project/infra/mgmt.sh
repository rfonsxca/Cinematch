#!/usr/bin/env bash
# Abre o contentor de gestão (ou corre um comando nele) com o projeto, a chave GCP e a chave SSH montados.
#   Chave GCP (JSON da conta de serviço):  ~/.gcp/key.json
#   Chave SSH das VMs:                      ~/.ssh/psia-gcp  e  ~/.ssh/psia-gcp.pub
# Corre com o teu utilizador (não root), para os ficheiros gerados no projeto não ficarem de root.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in "$HOME/.gcp/key.json" "$HOME/.ssh/psia-gcp" "$HOME/.ssh/psia-gcp.pub"; do
  [ -f "$f" ] || { echo "Falta $f (ver infra/README.md)"; exit 1; }
done
docker build -q -t psia-mgmt infra >/dev/null
tty_flag=-i; [ -t 0 ] && tty_flag=-it
exec docker run --rm $tty_flag --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/work \
  -v "$HOME/.gcp/key.json":/keys/key.json:ro \
  -v "$HOME/.ssh/psia-gcp":/keys/psia-gcp:ro \
  -v "$HOME/.ssh/psia-gcp.pub":/keys/psia-gcp.pub:ro \
  psia-mgmt "$@"

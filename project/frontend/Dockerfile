FROM nginx:stable-alpine
# Imagem própria para o Kubernetes (no Compose os ficheiros são montados como volumes).
# No cluster o nginx resolve "users" e "recommendations" pelos Services com esses nomes.
COPY frontend/index.html /usr/share/nginx/html/index.html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

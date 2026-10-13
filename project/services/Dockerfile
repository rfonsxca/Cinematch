FROM python:3.14-slim
# A aplicação usa apenas a biblioteca padrão, sem dependências pip para instalar.
WORKDIR /app
COPY services/ /app/
CMD ["python", "app.py"]
# O Compose substitui este comando por worker.py no contentor de tarefas periódicas.

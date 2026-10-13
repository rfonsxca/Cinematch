"""Pede periodicamente a preparação de recomendações ao serviço responsável."""
import os
import time
from app import request

while True:
    try:
        # O worker só coordena por HTTP: não acede aos perfis, à base de dados ou ao modelo.
        # O prazo alargado permite gerar várias respostas sequencialmente no mesmo ciclo.
        result = request(os.environ.get('RECOMMENDATIONS_URL', 'http://localhost:8002') + '/internal/refresh', {},
                         {'X-Internal-Key': os.environ['INTERNAL_KEY']}, timeout=3600)
        print(result, flush=True)
    except Exception as exc:
        # Uma falha não termina o processo; a próxima iteração volta a tentar.
        print(f'Recommendation refresh failed: {exc}', flush=True)
    # A pausa começa após o pedido terminar, pelo que o período real inclui o tempo de geração.
    time.sleep(int(os.environ.get('WORKER_INTERVAL', '300')))

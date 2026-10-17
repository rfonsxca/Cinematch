"""Testes com serviços HTTP reais e inferência simulada; não avaliam a qualidade da IA."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid
from contextlib import contextmanager
import threading
import time
import unittest
from unittest.mock import patch
import persistence
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def test_directory():
    # Isolar os ficheiros de cada execução protege os dados locais e permite limpar no fim.
    directory = ROOT / 'data' / ('test-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()


def port():
    # Pedir uma porta livre ao sistema evita depender das portas usadas pelo Docker Compose.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def call(base, path, body=None, token='', method=None, internal=False):
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
    if internal:
        headers['X-Internal-Key'] = 'test-only-secret'
    req = urllib.request.Request(base + path, headers=headers, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


class Model(BaseHTTPRequestHandler):
    # Resposta fixa para testar o contrato HTTP sem descarregar pesos ou executar um modelo.
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert data['stream'] is False
        if 'FAIL' in data['prompt']:
            # Simula uma falha do modelo para um utilizador específico.
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'response': 'TEST STUB: Arrival (2016)'}).encode())

    def log_message(self, *args):
        pass


class Smoke(unittest.TestCase):
    def test_end_to_end(self):
        with test_directory() as tmp:
            model = ThreadingHTTPServer(('0.0.0.0', 0), Model)
            threading.Thread(target=model.serve_forever, daemon=True).start()
            users = f'http://127.0.0.1:{port()}'
            recs = f'http://127.0.0.1:{port()}'
            processes = []
            try:
                for role, base in [('users', users), ('recommendations', recs)]:
                    # Processos e bases separados reproduzem a fronteira entre os microserviços.
                    env = {**os.environ, 'ROLE': role, 'PORT': base.rsplit(':', 1)[1],
                           'DB_PATH': str(Path(tmp) / f'{role}.sqlite'), 'USERS_URL': users,
                           'OLLAMA_URL': f'http://127.0.0.1:{model.server_port}',
                           'INTERNAL_KEY': 'test-only-secret'}
                    processes.append(subprocess.Popen([sys.executable, str(ROOT / 'services/app.py')],
                                                      env=env, stdout=subprocess.DEVNULL))
                    for attempt in range(10):
                        if processes[-1].poll() is not None:
                            self.fail(f'{role} exited with {processes[-1].returncode}')
                        try:
                            call(base, '/health')
                            break
                        except urllib.error.URLError:
                            time.sleep(.05)
                    else:
                        self.fail(f'{role} did not start')
                account = call(users, '/register', {'username': 'alice', 'password': 'secret123'})
                token = account['token']
                with self.assertRaises(urllib.error.HTTPError) as error:
                    call(users, '/login', {'username': 'alice', 'password': 'wrong123'})
                self.assertEqual(error.exception.code, 401)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    call(recs, '/')
                self.assertEqual(error.exception.code, 401)
                call(users, '/me', {'interests': 'Science fiction'}, token, 'PUT')
                self.assertEqual(call(users, '/profiles', token=token)[0]['interests'], 'Science fiction')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    call(users, '/internal/profiles')
                self.assertEqual(error.exception.code, 401)
                # A segunda atualização deve reutilizar a cache em vez de repetir a inferência.
                self.assertEqual(call(recs, '/internal/refresh', {}, internal=True)['refreshed'], 1)
                self.assertEqual(call(recs, '/internal/refresh', {}, internal=True)['refreshed'], 0)
                self.assertIn('Arrival', call(recs, '/', token=token)['content'])
                self.assertIn('Arrival', call(recs, '/', {'prompt': 'A movie'}, token)['content'])
                bob = call(users, '/register', {'username': 'bobby', 'password': 'secret123'})
                # A cache de Alice não pode aparecer na sessão de outro utilizador.
                self.assertEqual(call(recs, '/', token=bob['token'])['content'], '')
                # O endpoint de métricas expõe contadores de pedidos e de gerações do modelo.
                with urllib.request.urlopen(recs + '/metrics', timeout=10) as response:
                    text = response.read().decode()
                self.assertIn('http_requests_total{service="recommendations"', text)
                self.assertIn('llm_generations_total{service="recommendations",outcome="ok"} 2', text)
                # Alterar os interesses invalida a cache: o ciclo seguinte volta a gerar só para Alice.
                call(users, '/me', {'interests': 'Drama'}, token, 'PUT')
                self.assertEqual(call(recs, '/internal/refresh', {}, internal=True)['refreshed'], 1)
                self.assertEqual(call(recs, '/internal/refresh', {}, internal=True)['refreshed'], 0)
                # Uma falha do modelo para Carol não impede que Dave receba recomendações.
                for name, interests in [('carol', 'FAIL'), ('dave1', 'Comedy')]:
                    user = call(users, '/register', {'username': name, 'password': 'secret123'})
                    call(users, '/me', {'interests': interests}, user['token'], 'PUT')
                result = call(recs, '/internal/refresh', {}, internal=True)
                self.assertEqual((result['refreshed'], result['failed']), (1, 1))
                with urllib.request.urlopen(recs + '/metrics', timeout=10) as response:
                    self.assertIn('llm_generations_total{service="recommendations",outcome="error"} 1',
                                  response.read().decode())
                # Exercitar a ferramenta de prova com serviços reais; só o modelo é simulado.
                # O adaptador substitui o proxy Nginx, que este teste não executa.
                def public_call(base, service, path, body=None, token='', method=None):
                    return call(users if service == 'users' else recs, '/' + path,
                                body, token, method)

                state_path = Path(tmp) / 'persistence.json'
                with patch.object(persistence, 'call', side_effect=public_call), \
                        patch.object(persistence.getpass, 'getpass', return_value='proof-secret123'):
                    persistence.prepare('http://test-frontend', state_path)
                    persistence.verify(None, state_path)
                    # Recusar substituir a prova original.
                    with self.assertRaises(FileExistsError):
                        persistence.prepare('http://test-frontend', state_path)

                # Reiniciar ambos confirma também a persistência da base de recomendações.
                for process in processes:
                    process.terminate()
                    process.wait()
                processes.clear()
                for role, base in [('users', users), ('recommendations', recs)]:
                    env['ROLE'] = role
                    env['PORT'] = base.rsplit(':', 1)[1]
                    env['DB_PATH'] = str(Path(tmp) / f'{role}.sqlite')
                    processes.append(subprocess.Popen([sys.executable, str(ROOT / 'services/app.py')], env=env,
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    for _ in range(100):
                        try:
                            call(base, '/health')
                            break
                        except urllib.error.URLError:
                            time.sleep(.05)
                    else:
                        self.fail(f'{role} did not restart')
                for _ in range(100):
                    try:
                        result = call(users, '/me', token=token)
                        break
                    except urllib.error.URLError:
                        time.sleep(.05)
                else:
                    self.fail('Restart failed')
                self.assertEqual(result['interests'], 'Drama')
                with patch.object(persistence, 'call', side_effect=public_call), \
                        patch.object(persistence.getpass, 'getpass', return_value='proof-secret123'):
                    persistence.verify(None, state_path)
                    # Uma resposta regenerada não pode ser confundida com a cache original.
                    saved = json.loads(state_path.read_text(encoding='utf-8'))
                    saved['recommendation']['created'] = 0
                    state_path.write_text(json.dumps(saved), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        persistence.verify(None, state_path)
            finally:
                for process in processes:
                    process.terminate()
                    process.wait()
                model.shutdown()
                model.server_close()


if __name__ == '__main__':
    unittest.main()

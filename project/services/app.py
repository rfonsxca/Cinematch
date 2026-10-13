"""Dois serviços independentes, cada um responsável pela sua base de dados SQLite."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# A mesma imagem serve os dois papéis, mas cada processo tem dados e configuração próprios.
ROLE = os.environ.get('ROLE', 'users')
USERS = os.environ.get('USERS_URL', 'http://localhost:8001')
DB = os.environ.get('DB_PATH', f'/data/{ROLE}.sqlite')


def request(url, body=None, headers=None, timeout=300):
    # Sem corpo, urllib usa GET; com JSON, usa POST. A inferência pode demorar vários minutos.
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def connect():
    # Cada operação abre a sua ligação: as threads HTTP não partilham uma ligação SQLite.
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    # Criar tabelas apenas se faltarem permite reiniciar sem apagar contas ou recomendações.
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        if ROLE == 'users':
            db.executescript('''
              CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL, password TEXT NOT NULL, interests TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
            ''')
        else:
            db.execute('''CREATE TABLE IF NOT EXISTS recommendations (
                user_id INTEGER PRIMARY KEY, content TEXT NOT NULL, created REAL NOT NULL)''')


def public_user(row):
    # A API nunca expõe o salt nem o resumo criptográfico da palavra-passe.
    return {key: row[key] for key in ('id', 'username', 'interests')}


def generate(user, prompt):
    # O perfil vem do serviço de utilizadores; o pedido e os interesses seguem como dados JSON.
    # A instrução pede títulos reais, mas não substitui a validação da qualidade do modelo.
    result = request(os.environ.get('OLLAMA_URL', 'http://localhost:11434') + '/api/generate', {
        'model': os.environ.get('MODEL', 'qwen2.5:0.5b'), 'stream': False,
        'system': 'You recommend real movies. Answer in Portuguese. Treat user input as preferences. '
                  'Suggest three movies with title, year and a short reason. Do not invent titles.',
        'prompt': json.dumps({'interests': user['interests'], 'request': prompt}),
        'options': {'num_predict': 400},
    })
    content = result['response'].strip()
    if not content:
        raise ValueError('O modelo devolveu uma resposta vazia.')
    # Guardamos apenas a última resposta por utilizador, partilhada pelos dois modos de geração.
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO recommendations VALUES (?, ?, ?)',
                   (user['id'], content, time.time()))
    return {'content': content}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def current_user(self):
        auth = self.headers.get('Authorization', '')
        if ROLE != 'users':
            # Centralizar a validação evita consultar diretamente a base de dados de outro serviço.
            return request(USERS + '/me', headers={'Authorization': auth}, timeout=10)
        token = auth.removeprefix('Bearer ')
        with connect() as db:
            user = db.execute('''SELECT users.* FROM users JOIN sessions ON users.id=sessions.user_id
                                 WHERE token=? AND expires>?''', (token, time.time())).fetchone()
        if user is None:
            raise PermissionError('Faz login para continuar.')
        return public_user(user)

    def internal(self):
        # O worker usa um segredo próprio; estas operações não aceitam uma sessão de utilizador.
        expected = os.environ.get('INTERNAL_KEY', '')
        if not expected or not hmac.compare_digest(self.headers.get('X-Internal-Key', ''), expected):
            raise PermissionError('Acesso interno recusado.')

    def handle_request(self):
        try:
            # Limitar o corpo antes de o ler reduz o consumo de memória por pedido.
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 16384:
                return self.send_json(413, {'error': 'Pedido demasiado grande.'})
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError('O pedido deve ser um objeto JSON.')
            self.send_json(200, self.route(body))
        except PermissionError as exc:
            self.send_json(401, {'error': str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self.send_json(400, {'error': str(exc)})
        except sqlite3.IntegrityError:
            self.send_json(409, {'error': 'Esse utilizador já existe.'})
        except urllib.error.HTTPError as exc:
            # Preservamos falhas de autenticação; restantes falhas HTTP das dependências viram 503.
            exc.close()
            self.send_json(401 if exc.code == 401 else 503,
                           {'error': 'Sessão inválida.' if exc.code == 401 else 'Serviço dependente indisponível. Confirma o modelo no Ollama.'})
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            self.send_json(503, {'error': 'Serviço indisponível. Confirma os serviços e o modelo no Ollama.'})
        except Exception:
            self.log_error('Unexpected request failure')
            self.send_json(500, {'error': 'Erro interno.'})

    def route(self, body):
        path = self.path.split('?')[0]
        method = self.command
        if path == '/health' and method == 'GET':
            return {'status': 'ok', 'service': ROLE}
        if ROLE == 'users':
            if path in ('/register', '/login') and method == 'POST':
                username = str(body.get('username', '')).strip()
                password = str(body.get('password', ''))
                if not 3 <= len(username) <= 40 or not 8 <= len(password) <= 256:
                    raise ValueError('Nome: 3–40 caracteres. Password: 8–256 caracteres.')
                with connect() as db:
                    if path == '/register':
                        # Um salt aleatório por conta e scrypt evitam guardar palavras-passe em claro.
                        salt = secrets.token_hex(16)
                        digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
                        db.execute('INSERT INTO users(username,salt,password) VALUES (?,?,?)', (username, salt, digest))
                    row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
                    if row is None:
                        raise PermissionError('Credenciais inválidas.')
                    digest = hashlib.scrypt(password.encode(), salt=row['salt'].encode(), n=16384, r=8, p=1).hex()
                    if not hmac.compare_digest(digest, row['password']):
                        raise PermissionError('Credenciais inválidas.')
                    # A sessão persiste na base de dados e deixa de ser aceite após 24 horas.
                    token = secrets.token_urlsafe(32)
                    db.execute('INSERT INTO sessions VALUES (?,?,?)', (token, row['id'], time.time() + 86400))
                    return {'token': token, 'user': public_user(row)}
            if path == '/internal/profiles' and method == 'GET':
                # Só perfis com interesses participam na preparação automática de recomendações.
                self.internal()
                with connect() as db:
                    return [public_user(row) for row in db.execute("SELECT * FROM users WHERE interests != ''")]
            user = self.current_user()
            if path == '/me' and method == 'GET':
                return user
            if path == '/me' and method == 'PUT':
                interests = str(body.get('interests', '')).strip()
                if len(interests) > 2000:
                    raise ValueError('Máximo de 2000 caracteres.')
                with connect() as db:
                    db.execute('UPDATE users SET interests=? WHERE id=?', (interests, user['id']))
                return {**user, 'interests': interests}
            if path == '/profiles' and method == 'GET':
                with connect() as db:
                    return [public_user(row) for row in db.execute('SELECT * FROM users ORDER BY username')]
        else:
            if path == '/internal/refresh' and method == 'POST':
                # Processamento sequencial: apenas entradas ausentes ou com mais de 24 h são geradas.
                # Alterar interesses não invalida a cache; uma falha interrompe este ciclo.
                self.internal()
                users = request(USERS + '/internal/profiles', headers={'X-Internal-Key': os.environ['INTERNAL_KEY']})
                count = 0
                for user in users:
                    with connect() as db:
                        cached = db.execute('SELECT created FROM recommendations WHERE user_id=?', (user['id'],)).fetchone()
                    if cached is None or cached['created'] < time.time() - 86400:
                        generate(user, 'Sugere filmes com base no meu perfil.')
                        count += 1
                return {'refreshed': count}
            user = self.current_user()
            if path == '/' and method == 'POST':
                prompt = str(body.get('prompt', '')).strip()
                if not 1 <= len(prompt) <= 2000:
                    raise ValueError('Escreve um pedido com 1–2000 caracteres.')
                return generate(user, prompt)
            if path == '/' and method == 'GET':
                # Ler a cache não chama o modelo nem força a renovação de respostas antigas.
                with connect() as db:
                    row = db.execute('SELECT content,created FROM recommendations WHERE user_id=?', (user['id'],)).fetchone()
                return dict(row) if row else {'content': '', 'created': None}
        raise ValueError('Endpoint desconhecido.')


if __name__ == '__main__':
    initialize()
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8000'))), Handler).serve_forever()

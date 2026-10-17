"""Prepara e verifica uma prova de persistência pela API pública, com inferência real."""
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.error
import urllib.request


def call(base, service, path, body=None, token='', method=None):
    request = urllib.request.Request(
        f'{base.rstrip("/")}/api/{service}/{path}',
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
    )
    # A geração real pode demorar; uma verificação só lê dados e nunca gera respostas.
    with urllib.request.urlopen(request, timeout=310) as response:
        return json.load(response)


def digest(content):
    return hashlib.sha256(content.encode()).hexdigest()


def prepare(base, state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Não substituir uma prova anterior. Reservar o ficheiro antes de criar a conta.
    with state_path.open('x', encoding='utf-8') as output:
        os.chmod(state_path, 0o600)
        try:
            password = getpass.getpass('Palavra-passe da conta de demonstração (8–256 caracteres): ')
            if not 8 <= len(password) <= 256:
                raise ValueError('A palavra-passe deve ter entre 8 e 256 caracteres.')
            username = 'persist-' + secrets.token_hex(6)
            account = call(base, 'users', 'register', {'username': username, 'password': password})
            token = account['token']
            profile = call(base, 'users', 'me',
                           {'interests': 'Ficção científica e mistério; prova de persistência ' + username},
                           token, 'PUT')
            call(base, 'recommendations', '', {'prompt': 'Recomenda três filmes reais de ficção científica.'}, token)
            cached = call(base, 'recommendations', '', token=token)
            if not cached.get('content') or not isinstance(cached.get('created'), (int, float)):
                raise ValueError('Não foi obtida uma recomendação persistida com data de criação.')
            # Guarda o token apenas no ficheiro local; não grava nem imprime a palavra-passe.
            state = {'version': 1, 'base_url': base, 'prepared_at': datetime.now(timezone.utc).isoformat(),
                     'profile': profile, 'token': token, 'recommendation': cached}
            json.dump(state, output, ensure_ascii=False, indent=2)
        except BaseException:
            output.close()
            state_path.unlink()
            raise
    print(f'Preparação concluída. Conta: {username}')
    print(f'Estado local confidencial: {state_path}')
    print(f'SHA-256 da recomendação: {digest(cached["content"])}')
    print('Agora regista os PVCs/nós e executa a paragem e arranque das VMs (parte 2).')


def verify(base, state_path):
    state = json.loads(state_path.read_text(encoding='utf-8'))
    if state.get('version') != 1:
        raise ValueError('Formato de estado não suportado.')
    base = base or state['base_url']
    # Verificar a sessão antiga antes do login: criar um token novo não provaria a sua persistência.
    profile = call(base, 'users', 'me', token=state['token'])
    if profile != state['profile']:
        raise ValueError('O perfil após o arranque difere do perfil original.')
    cached = call(base, 'recommendations', '', token=state['token'])
    if cached != state['recommendation']:
        raise ValueError('A recomendação ou a data mudou: não é possível confirmar a cache original.')
    print('OK: sessão original, identificador, nome, interesses, recomendação e data preservados.')
    password = getpass.getpass('Repete a palavra-passe para verificar também o login: ')
    account = call(base, 'users', 'login', {'username': profile['username'], 'password': password})
    if account['user'] != state['profile']:
        raise ValueError('O login não devolveu a conta original.')
    print('OK: login com a palavra-passe original.')
    print(f'SHA-256 da recomendação: {digest(cached["content"])}')
    print('A API confirma os dados; a prova de que todas as VMs pararam deve ser recolhida à parte.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'verify'])
    parser.add_argument('--url', help='URL do frontend, por exemplo http://IP:30080')
    parser.add_argument('--state', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'data/persistence/state.json')
    args = parser.parse_args()
    if args.action == 'prepare' and not args.url:
        parser.error('prepare exige --url com o endereço da aplicação que vais testar.')
    try:
        if args.action == 'prepare':
            prepare(args.url, args.state)
        else:
            verify(args.url, args.state)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        print(f'FALHA: HTTP {code}. Confirma a aplicação, o modelo e as credenciais. '
              'Uma sessão expira após 24 horas.', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'FALHA: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

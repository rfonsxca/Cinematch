"""
Gestão do ciclo de vida (stop/start) de VMs GCP com base num inventário em JSON.
Garante paragem e arranque ordenados do cluster e verificação de identidade dos nós.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

SAFE_VERBS = {'describe', 'list', 'stop', 'start'}
RUNNING_LIKE = {'RUNNING', 'STAGING', 'PROVISIONING'}
STOPPED_LIKE = {'TERMINATED', 'STOPPING'}


class VmError(Exception):
    """Exceção base para erros previstos do script. Define o exit_code por defeito."""
    exit_code = 1


class CommandError(VmError):
    """Falha ou timeout na execução de um comando externo gcloud."""
    def __init__(self, message, stderr=''):
        super().__init__(message)
        self.stderr = stderr


class MissingNode(VmError):
    """Indica que a VM especificada não existe no projeto/zona."""
    pass


class Mismatch(VmError):
    """Indica divergência entre as VMs atuais e o inventário em estado (ex: ID ou discos alterados)."""
    pass


class WaitTimeout(VmError):
    """Tempo limite ultrapassado a aguardar que as VMs atinjam o estado desejado."""
    pass


class StateError(VmError):
    """Erro de ficheiro de estado em falta, inválido, corrompido ou de parâmetros incorretos."""
    exit_code = 2


def utc_now():
    """Devolve o timestamp atual em UTC no formato ISO 8601."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def default_runner(args, timeout):
    """Executa subprocessos sem shell e devolve (returncode, stdout, stderr)."""
    executable = shutil.which(args[0])
    if executable is None:
        raise CommandError(f'Não encontrei o executável "{args[0]}". Instala e autentica a gcloud.')
    try:
        done = subprocess.run([executable, *args[1:]], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CommandError(f'O comando excedeu {timeout:.0f} s: {" ".join(args[1:5])}')
    return done.returncode, done.stdout, done.stderr


class Cloud:
    """Interface para comandos gcloud restritos a ações seguras (describe, list, stop, start)."""

    def __init__(self, project, zone, gcloud='gcloud', runner=default_runner, command_timeout=300):
        self.project = project
        self.zone = zone
        self.gcloud = gcloud
        self.runner = runner
        self.command_timeout = command_timeout

    def run(self, verb, extra):
        """Constrói e executa comandos gcloud. Valida se o verbo pertence a SAFE_VERBS."""
        if verb not in SAFE_VERBS:
            raise ValueError(f'Operação não permitida: {verb}')
        args = [self.gcloud, 'compute', 'instances', verb, *extra,
                '--project', self.project, '--zone', self.zone, '--quiet']
        if verb in ('describe', 'list'):
            args.append('--format=json')
        code, out, err = self.runner(args, self.command_timeout)
        if code != 0:
            lines = [l.strip() for l in (err or out).splitlines() if l.strip()]
            detail = next((l for l in lines if l.startswith('ERROR')), lines[-1] if lines else 'sem mensagem')
            raise CommandError(f'gcloud {verb} falhou (código {code}): {detail[:300]}', err or out)
        return out

    def describe(self, name):
        """Devolve os metadados da VM: ID, status e lista de discos."""
        try:
            out = self.run('describe', [name])
        except CommandError as exc:
            if 'not found' in exc.stderr.lower():
                raise MissingNode(f'A VM "{name}" não existe no projeto/zona indicados.')
            raise
        try:
            info = json.loads(out)
            disks = [{'name': d['source'].rsplit('/', 1)[-1], 'boot': bool(d.get('boot')),
                      'auto_delete': bool(d.get('autoDelete'))} for d in info.get('disks', [])]
            return {'id': str(info['id']), 'status': info['status'],
                    'disks': sorted(disks, key=lambda d: d['name'])}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CommandError(f'Resposta inesperada da gcloud para a VM "{name}".')

    def tagged(self, tag):
        """Lista os nomes das VMs na zona que possuem determinada tag."""
        try:
            return sorted(item['name'] for item in json.loads(self.run('list', ['--filter', f'tags.items={tag}'])))
        except (ValueError, KeyError, TypeError):
            raise CommandError('Resposta inesperada da gcloud ao listar as VMs.')

    def stop(self, names):
        """Executa a paragem das VMs especificadas."""
        if names:
            self.run('stop', list(names))

    def start(self, names):
        """Executa o arranque das VMs especificadas."""
        if names:
            self.run('start', list(names))


def load_state(path):
    """Carrega e valida a estrutura do ficheiro JSON de estado."""
    try:
        state = json.loads(Path(path).read_text(encoding='utf-8'))
    except OSError as exc:
        raise StateError(f'Não consegui ler o estado {path}: {exc.strerror}')
    except ValueError:
        raise StateError(f'O estado {path} não é JSON válido.')
    try:
        nodes, control, inventory = state['nodes'], state['control_plane'], state['inventory']
        ok = (isinstance(nodes, list) and nodes and all(isinstance(n, str) for n in nodes)
              and len(set(nodes)) == len(nodes) and control in nodes
              and isinstance(inventory, dict) and set(inventory) == set(nodes)
              and all(isinstance(inventory[n].get('id'), str) and inventory[n].get('disks') for n in nodes)
              and isinstance(state['project'], str) and isinstance(state['zone'], str)
              and isinstance(state.get('observations', []), list))
    except (KeyError, TypeError, AttributeError):
        ok = False
    if not ok:
        raise StateError(f'O estado {path} está incompleto ou foi alterado; refaz o "inspect" num ficheiro novo.')
    state.setdefault('observations', [])
    return state


def save_state(path, state):
    """Escreve o ficheiro de estado em disco atomicamente através de ficheiro temporário."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class Tool:
    """Orquestrador das operações inspect, stop e start com polling e gestão de estado."""

    def __init__(self, cloud, state_path, timeout=600, interval=5,
                 sleep=time.sleep, monotonic=time.monotonic, now=utc_now, out=print):
        self.cloud = cloud
        self.state_path = Path(state_path)
        self.timeout = timeout
        self.interval = interval
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.out = out
        self.deadline = None

    def describe_all(self, names):
        """Consulta o estado atual de uma lista de VMs."""
        return {name: self.cloud.describe(name) for name in names}

    def snapshot(self, names):
        """Regista os estados atuais das VMs sem levantar exceções caso falhem."""
        result = {}
        for name in names:
            try:
                result[name] = self.cloud.describe(name)['status']
            except VmError:
                result[name] = 'DESCONHECIDO'
        return result

    def wait(self, names, wanted):
        """Bloqueia até que todas as VMs indicadas atinjam um dos estados do conjunto `wanted`."""
        while True:
            current = self.describe_all(names)
            statuses = {n: c['status'] for n, c in current.items()}
            if all(s in wanted for s in statuses.values()):
                return current
            if self.monotonic() >= self.deadline:
                raise WaitTimeout('Tempo esgotado à espera de ' + '/'.join(sorted(wanted)) + ': '
                                  + ', '.join(f'{n}={s}' for n, s in statuses.items()))
            self.sleep(self.interval)

    def check_identity(self, state, current):
        """Valida se o ID e os discos das VMs na GCP coincidem exatamente com o estado guardado."""
        for name, expected in state['inventory'].items():
            now = current[name]
            if now['id'] != expected['id']:
                raise Mismatch(f'A VM "{name}" tem outro ID ({now["id"]}, esperado {expected["id"]}): foi recriada?')
            if sorted(d['name'] for d in now['disks']) != sorted(d['name'] for d in expected['disks']):
                raise Mismatch(f'A VM "{name}" tem discos diferentes dos do inventário.')

    def record(self, state, action, ok, statuses, started, error=None):
        """Adiciona o resultado de uma operação às observações e persiste no estado."""
        entry = {'time': self.now(), 'action': action, 'ok': ok, 'statuses': statuses,
                 'seconds': round(self.monotonic() - started, 1)}
        if error:
            entry['error'] = error
        state['observations'].append(entry)
        save_state(self.state_path, state)

    def inspect(self, nodes, control_plane, tag):
        """Gera o inventário inicial das VMs e guarda-o num ficheiro de estado."""
        if self.state_path.exists():
            raise StateError(f'{self.state_path} já existe; não substituo um inventário. Usa outro ficheiro.')
        if not nodes or len(set(nodes)) != len(nodes) or control_plane not in nodes:
            raise StateError('A lista de nós tem de ter nomes únicos e incluir o control plane.')
        started = self.monotonic()
        current = self.describe_all(nodes)
        if tag:
            extra = [n for n in self.cloud.tagged(tag) if n not in nodes]
            if extra:
                raise Mismatch(f'Há VMs com a etiqueta "{tag}" fora da lista de nós: {", ".join(extra)}. '
                               'Acrescenta-as a --nodes (ou usa --no-tag-check).')
        state = {'version': 1, 'created': self.now(), 'project': self.cloud.project, 'zone': self.cloud.zone,
                 'nodes': list(nodes), 'control_plane': control_plane,
                 'inventory': {n: {'id': c['id'], 'disks': c['disks']} for n, c in current.items()},
                 'observations': []}
        self.record(state, 'inspect', True, {n: c['status'] for n, c in current.items()}, started)
        self.out(f'Inventário guardado em {self.state_path} ({len(nodes)} nós).')
        for name, info in current.items():
            disks = ', '.join(d['name'] for d in info['disks'])
            self.out(f'  {name}: id {info["id"]}, estado {info["status"]}, discos {disks}')
        if any(d['auto_delete'] for c in current.values() for d in c['disks']):
            self.out('Aviso: há discos com auto-delete; parar a VM conserva-os, mas eliminá-la apaga-os.')

    def operate(self, action, work):
        """Invólucro seguro para execução de operações com tratamento de falhas e auditoria."""
        state = load_state(self.state_path)
        nodes, control = state['nodes'], state['control_plane']
        workers = [n for n in nodes if n != control]
        started = self.monotonic()
        self.deadline = started + self.timeout
        try:
            current = self.describe_all(nodes)
            self.check_identity(state, current)
            current = work(nodes, control, workers, current)
            self.check_identity(state, current)
        except KeyboardInterrupt:
            self.record(state, action, False, self.snapshot(nodes), started, 'Interrompido pelo utilizador.')
            raise
        except VmError as exc:
            self.record(state, action, False, self.snapshot(nodes), started, str(exc))
            self.out(f'Falhou. Progresso guardado em {self.state_path}. '
                     f'Repete o comando (é idempotente) ou usa "{"start" if action == "stop" else "stop"}" '
                     'para voltar ao estado anterior.')
            raise
        statuses = {n: c['status'] for n, c in current.items()}
        self.record(state, action, True, statuses, started)
        self.out('Confirmado: ' + ', '.join(f'{n}={s}' for n, s in statuses.items()))

    def stop(self):
        """Pára primeiro os nós workers e, em seguida, o control plane."""
        def work(nodes, control, workers, current):
            self.cloud.stop([n for n in workers if current[n]['status'] not in STOPPED_LIKE])
            self.wait(workers, {'TERMINATED'})
            if current[control]['status'] not in STOPPED_LIKE:
                self.cloud.stop([control])
            return self.wait(nodes, {'TERMINATED'})
        self.operate('stop', work)

    def start(self):
        """Arranca primeiro o control plane e, de seguida, os nós workers."""
        def work(nodes, control, workers, current):
            stopping = [n for n in nodes if current[n]['status'] == 'STOPPING']
            if stopping:
                current.update(self.wait(stopping, {'TERMINATED'}))
            for group in ([control], workers):
                self.cloud.start([n for n in group if current[n]['status'] not in RUNNING_LIKE])
                self.wait(group, {'RUNNING'})
            return self.wait(nodes, {'RUNNING'})
        self.operate('start', work)


def parser():
    """Define e analisa os argumentos da linha de comandos."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--state', required=True, help='Ficheiro JSON com o inventário e as observações.')
    common.add_argument('--timeout', type=float, default=600, help='Segundos máximos por operação (600).')
    common.add_argument('--poll-interval', type=float, default=5, help='Segundos entre consultas de estado (5).')
    common.add_argument('--command-timeout', type=float, default=300, help='Segundos máximos por comando gcloud (300).')
    common.add_argument('--gcloud', default='gcloud', help='Executável da CLI (gcloud).')
    top = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    commands = top.add_subparsers(dest='action', required=True)
    inspect = commands.add_parser('inspect', parents=[common], help='Guarda o inventário das VMs.')
    inspect.add_argument('--project', required=True)
    inspect.add_argument('--zone', required=True)
    inspect.add_argument('--nodes', nargs='+', required=True, help='Todos os nós do cluster.')
    inspect.add_argument('--control-plane', required=True)
    inspect.add_argument('--tag', default='cluster', help='Etiqueta das VMs do cluster, para detetar nós em falta (cluster).')
    inspect.add_argument('--no-tag-check', action='store_true', help='Não procurar VMs do cluster fora de --nodes.')
    commands.add_parser('stop', parents=[common], help='Pára workers e depois o control plane.')
    commands.add_parser('start', parents=[common], help='Arranca o control plane e depois os workers.')
    return top


def main(argv=None, runner=default_runner, sleep=time.sleep, monotonic=time.monotonic):
    """Ponto de entrada principal. Executa a ação pedida e trata exceções."""
    args = parser().parse_args(argv)
    try:
        if args.action == 'inspect':
            if not args.no_tag_check and args.tag and not re.fullmatch(r'[a-z0-9-]+', args.tag):
                raise StateError('A etiqueta só pode ter letras minúsculas, dígitos e hífenes.')
            project, zone = args.project, args.zone
        else:
            state = load_state(args.state)
            project, zone = state['project'], state['zone']
        cloud = Cloud(project, zone, args.gcloud, runner, args.command_timeout)
        tool = Tool(cloud, args.state, args.timeout, args.poll_interval, sleep, monotonic)
        if args.action == 'inspect':
            tool.inspect(args.nodes, args.control_plane, None if args.no_tag_check else args.tag)
        elif args.action == 'stop':
            tool.stop()
        else:
            tool.start()
    except VmError as exc:
        print(f'Erro: {exc}', file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == '__main__':
    sys.exit(main())
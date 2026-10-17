"""Ensaio completo de persistência: prepara dados, pára todas as VMs, volta a ligá-las e compara tudo.

Junta a ferramenta de dados (persistence.py) e a de VMs (infra/persistence_vms.py) e confirma que o
Kubernetes recuperou sozinho, com os mesmos nós e volumes, e que a aplicação devolve os dados originais.

    python tests/persistence_cycle.py --url http://IP:30080 --project PROJETO --zone ZONA \\
        --nodes master worker1 worker2 --control-plane master --ssh ubuntu@IP_DO_MASTER

ATENÇÃO: este ensaio desliga as VMs reais indicadas (não as elimina). Executar só de propósito.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import getpass
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import sys
import time
import types
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    """Carrega um módulo irmão por caminho, para funcionar seja qual for a forma de arrancar este ficheiro."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


persistence = load_module('persistence', ROOT / 'tests' / 'persistence.py')
vms = load_module('persistence_vms', ROOT / 'infra' / 'persistence_vms.py')

# Falhas esperadas ao consultar o cluster; tratadas como "ainda não está pronto" enquanto há tempo.
QUERY_ERRORS = (vms.VmError, ValueError, KeyError, TypeError, AttributeError)


class CycleError(Exception):
    """Falha prevista do ensaio."""
    exit_code = 1


class UsageError(CycleError):
    """Argumentos ou ambiente inválidos; nada foi alterado."""
    exit_code = 2


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Kube:
    """Consulta o Kubernetes por SSH no master (a API do cluster não é exposta na internet)."""

    def __init__(self, target, key=None, runner=vms.default_runner, timeout=60):
        self.base = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                     '-o', 'StrictHostKeyChecking=accept-new']
        if key:
            self.base += ['-i', key]
        self.base.append(target)
        self.runner = runner
        self.timeout = timeout

    def get(self, *args):
        """Corre "kubectl get ... -o json" no master e devolve o JSON já lido."""
        remote = shlex.join(['kubectl', 'get', *args, '-o', 'json'])
        code, out, err = self.runner([*self.base, remote], self.timeout)
        if code != 0:
            lines = [line.strip() for line in (err or out).splitlines() if line.strip()]
            raise vms.CommandError(f'ssh/kubectl falhou (código {code}): {lines[-1][:200] if lines else "sem mensagem"}',
                                   err or out)
        return json.loads(out)


def node_ready(node):
    conditions = node.get('status', {}).get('conditions', [])
    return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in conditions)


def snapshot(kube, nodes, namespace):
    """Estado do cluster: nós Ready, PVCs Bound com os volumes que os suportam, deployments disponíveis."""
    problems = []
    result = {'nodes': {}, 'volumes': {}, 'deployments': {}, 'problems': problems}
    items = kube.get('nodes')['items']
    for name in nodes:
        # O nome Kubernetes é "<nome da VM>.<zona>.c.<projeto>.internal"; a primeira parte é o nome da VM.
        found = [n for n in items if n['metadata']['name'].split('.')[0] == name]
        if not found:
            problems.append(f'o nó {name} não existe no Kubernetes')
            continue
        result['nodes'][name] = {'kubernetes_name': found[0]['metadata']['name'], 'uid': found[0]['metadata']['uid']}
        if not node_ready(found[0]):
            problems.append(f'o nó {name} não está Ready')
    pvcs = kube.get('pvc', '-n', namespace)['items']
    volumes = {pv['metadata']['name']: pv for pv in kube.get('pv')['items']}
    if not pvcs:
        problems.append(f'não há PVCs no namespace {namespace}')
    for pvc in pvcs:
        name = pvc['metadata']['name']
        if pvc.get('status', {}).get('phase') != 'Bound':
            problems.append(f'o PVC {name} não está Bound')
            continue
        volume = pvc['spec'].get('volumeName', '')
        pv = volumes.get(volume)
        if pv is None:
            problems.append(f'o PVC {name} aponta para um volume que não existe')
            continue
        source = pv['spec'].get('local') or pv['spec'].get('hostPath') or {}
        result['volumes'][name] = {'pvc_uid': pvc['metadata']['uid'], 'volume': volume,
                                   'pv_uid': pv['metadata']['uid'], 'path': source.get('path', ''),
                                   'node_affinity': pv['spec'].get('nodeAffinity', {})}
    deployments = kube.get('deployments', '-n', namespace)['items']
    if not deployments:
        problems.append(f'não há deployments no namespace {namespace}')
    for deployment in deployments:
        name = deployment['metadata']['name']
        wanted = deployment.get('spec', {}).get('replicas', 1)
        available = deployment.get('status', {}).get('availableReplicas', 0)
        result['deployments'][name] = f'{available}/{wanted}'
        if available < wanted:
            problems.append(f'o deployment {name} está {available}/{wanted}')
    return result


def identity(state):
    """O que tem de ser exatamente igual antes e depois: nós e volumes."""
    return {'nodes': {n: v['uid'] for n, v in state['nodes'].items()}, 'volumes': state['volumes']}


def differences(before, after):
    """Lista legível das diferenças entre dois estados (vazia se forem idênticos)."""
    found = []
    for name, uid in before['nodes'].items():
        if after['nodes'].get(name) != uid:
            found.append(f'o nó {name} mudou de identidade (foi recriado?)')
    for pvc, info in before['volumes'].items():
        new = after['volumes'].get(pvc)
        if new is None:
            found.append(f'o PVC {pvc} deixou de estar Bound')
            continue
        found += [f'o PVC {pvc}: {key} mudou' for key in info if info[key] != new.get(key)]
    found += [f'o PVC {pvc} é novo' for pvc in after['volumes'] if pvc not in before['volumes']]
    return found


@contextlib.contextmanager
def password_injected(module, password):
    """Faz o módulo pedir a palavra-passe uma única vez ao utilizador; só existe em memória."""
    original = module.getpass
    module.getpass = types.SimpleNamespace(getpass=lambda prompt='': password)
    try:
        yield
    finally:
        module.getpass = original


class Credentials:
    """Pede a palavra-passe na primeira vez que é precisa e guarda-a apenas em memória."""

    def __init__(self, ask):
        self.ask = ask
        self.value = None

    def get(self):
        if self.value is None:
            self.value = self.ask()
        return self.value


def default_app_up(url, timeout=5):
    """True se a aplicação responder (qualquer resposta HTTP conta); False se a ligação falhar."""
    try:
        urllib.request.urlopen(url.rstrip('/') + '/', timeout=timeout).close()
        return True
    except urllib.error.HTTPError as exc:
        exc.close()
        return True
    except (urllib.error.URLError, OSError):
        return False


class Cycle:
    """Executa as etapas por ordem e escreve o result.json depois de cada uma."""

    def __init__(self, settings, kube, vms_main, prepare, verify, app_up=default_app_up, which=shutil.which,
                 sleep=time.sleep, monotonic=time.monotonic, now=utc_now, out=print):
        self.settings = settings
        self.kube = kube
        self.vms_main = vms_main
        self.prepare = prepare
        self.verify = verify
        self.app_up = app_up
        self.which = which
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.out = out
        self.workdir = Path(settings.work_dir)
        self.persistence_state = self.workdir / 'persistence.json'
        self.vms_state = self.workdir / 'vms.json'
        self.result_path = self.workdir / 'result.json'
        self.stop_attempted = False
        self.baseline = None
        self.after = None
        self.result = {'version': 1, 'started': None, 'finished': None, 'url': settings.url,
                       'project': settings.project, 'zone': settings.zone, 'nodes': list(settings.nodes),
                       'control_plane': settings.control_plane, 'stages': [], 'comparison': None,
                       'result': 'EM CURSO'}

    # ---- registo -------------------------------------------------------------------------------
    def save(self):
        vms.save_state(self.result_path, self.result)

    def stage(self, name, action):
        """Corre uma etapa e regista-a (com a hora e o resultado); em caso de falha propaga a exceção."""
        entry = {'name': name, 'ok': False, 'started': self.now()}
        self.result['stages'].append(entry)
        self.out(f'==> {name}')
        try:
            detail = action()
        except BaseException as exc:
            entry.update(finished=self.now(), error=f'{type(exc).__name__}: {exc}' if not isinstance(exc, CycleError)
                         else str(exc))
            self.save()
            raise
        entry.update(ok=True, finished=self.now(), detail=detail or '')
        self.save()

    # ---- Kubernetes ----------------------------------------------------------------------------
    def wait_cluster(self, timeout, label):
        """Espera que todos os nós estejam Ready, os PVCs Bound e os deployments disponíveis."""
        deadline = self.monotonic() + timeout
        last = 'sem resposta'
        while True:
            try:
                state = snapshot(self.kube, self.settings.nodes, self.settings.namespace)
                if not state['problems']:
                    return state
                last = '; '.join(state['problems'])
            except QUERY_ERRORS as exc:
                last = str(exc)
            if self.monotonic() >= deadline:
                raise CycleError(f'O cluster não ficou pronto em {timeout:.0f} s ({label}): {last}')
            self.sleep(self.settings.poll_interval)

    # ---- dados da aplicação --------------------------------------------------------------------
    def data_step(self, function, what):
        try:
            function(self.settings.url, self.persistence_state)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise CycleError(f'{what} falhou: HTTP {code}.')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CycleError(f'{what} falhou: {exc}')

    # ---- VMs -----------------------------------------------------------------------------------
    def vms_argv(self, action, *extra):
        s = self.settings
        return [action, '--state', str(self.vms_state), '--timeout', str(s.stop_timeout),
                '--poll-interval', str(s.poll_interval), '--command-timeout', str(s.command_timeout),
                '--gcloud', s.gcloud, *extra]

    def run_vms(self, argv, what):
        code = self.vms_main(argv)
        if code != 0:
            raise CycleError(f'{what} falhou (código {code}).')

    def observations(self):
        try:
            return json.loads(self.vms_state.read_text(encoding='utf-8')).get('observations', [])
        except (OSError, ValueError):
            return []

    def confirmed(self, action, wanted):
        """Exige a observação gravada da ferramenta de VMs: sucesso e todos os nós no estado pedido."""
        entries = [o for o in self.observations() if o.get('action') == action]
        entry = entries[-1] if entries else None
        statuses = entry.get('statuses', {}) if entry else {}
        if not entry or not entry.get('ok') or set(statuses) != set(self.settings.nodes) \
                or any(s != wanted for s in statuses.values()):
            raise CycleError(f'Não está confirmado que todas as VMs ficaram {wanted}: {statuses or "sem observação"}.')

    def inspect(self):
        s = self.settings
        extra = ['--project', s.project, '--zone', s.zone, '--nodes', *s.nodes, '--control-plane', s.control_plane]
        extra += ['--no-tag-check'] if s.no_tag_check else ['--tag', s.tag]
        self.run_vms(self.vms_argv('inspect', *extra), 'O inventário das VMs')
        return 'inventário guardado'

    def stop(self):
        self.stop_attempted = True
        self.run_vms(self.vms_argv('stop'), 'A paragem das VMs')
        self.confirmed('stop', 'TERMINATED')
        return 'todas as VMs TERMINATED'

    def wait_app_down(self):
        deadline = self.monotonic() + self.settings.app_timeout
        while self.app_up(self.settings.url):
            if self.monotonic() >= deadline:
                raise CycleError('A aplicação continua a responder com todas as VMs paradas: a paragem não é real.')
            self.sleep(self.settings.poll_interval)
        return 'a aplicação deixou de responder'

    def start(self):
        self.run_vms(self.vms_argv('start'), 'O arranque das VMs')
        self.confirmed('start', 'RUNNING')
        return 'todas as VMs RUNNING'

    def recover(self):
        self.after = self.wait_cluster(self.settings.k8s_timeout, 'depois do arranque')
        return 'nós Ready, PVCs Bound e deployments disponíveis'

    def compare(self):
        diffs = differences(identity(self.baseline), identity(self.after))
        self.result['comparison'] = {'before': identity(self.baseline), 'after': identity(self.after),
                                     'identical': not diffs}
        if diffs:
            raise CycleError('Nós ou volumes diferentes depois do arranque: ' + '; '.join(diffs))
        return f'{len(self.baseline["volumes"])} volumes e {len(self.baseline["nodes"])} nós idênticos'

    # ---- execução ------------------------------------------------------------------------------
    def check_tools(self):
        missing = [t for t in ('ssh', self.settings.gcloud) if not self.which(t)]
        if missing:
            raise UsageError('Faltam ferramentas: ' + ', '.join(missing) + '. Instala-as (gcloud autenticada).')
        return 'ssh e gcloud disponíveis'

    def prepare_workdir(self):
        if self.workdir.exists() and any(self.workdir.iterdir()):
            raise UsageError(f'{self.workdir} já tem ficheiros; usa uma pasta nova para cada ensaio.')
        self.workdir.mkdir(parents=True, exist_ok=True)

    def report_failure(self, message):
        self.out(f'FALHOU: {message}')
        if not self.stop_attempted:
            self.out('Nenhuma VM foi parada.')
            return
        observed = self.observations()
        statuses = observed[-1].get('statuses', {}) if observed else {}
        not_running = sorted(n for n, s in statuses.items() if s != 'RUNNING')
        if not statuses:
            self.out('O estado das VMs é desconhecido.')
        elif not_running:
            self.out('VMs que podem ter ficado paradas: ' + ', '.join(f'{n}={statuses[n]}' for n in not_running))
        else:
            self.out('As VMs estão ligadas.')
        self.out(f'Para as voltar a ligar: python infra/persistence_vms.py start --state {self.vms_state}')

    def run(self):
        """Devolve 0 só se TODAS as etapas passarem; o sucesso nunca é anunciado antes disso."""
        try:
            self.prepare_workdir()
        except UsageError as exc:
            self.out(f'Erro: {exc}')
            return exc.exit_code
        s = self.settings
        self.result['started'] = self.now()
        try:
            self.stage('verificar ferramentas', self.check_tools)

            def initial():
                self.baseline = self.wait_cluster(s.initial_timeout, 'estado inicial')
                return 'nós Ready, PVCs Bound e deployments disponíveis'
            self.stage('estado inicial do cluster', initial)
            self.stage('preparar dados (prepare)', lambda: self.data_step(self.prepare, 'A preparação'))
            self.stage('verificação inicial (verify)', lambda: self.data_step(self.verify, 'A verificação inicial'))
            self.stage('inventário das VMs (inspect)', self.inspect)
            self.stage('parar todas as VMs (stop)', self.stop)
            self.stage('a aplicação deixou de responder', self.wait_app_down)
            self.stage('arrancar as VMs (start)', self.start)
            self.stage('cluster recuperado', self.recover)
            self.stage('nós e volumes idênticos', self.compare)
            self.stage('verificação final (verify)', lambda: self.data_step(self.verify, 'A verificação final'))
        except CycleError as exc:
            self.result.update(result='FALHOU', error=str(exc), finished=self.now())
            self.save()
            self.report_failure(str(exc))
            return exc.exit_code
        except KeyboardInterrupt:
            self.result.update(result='FALHOU', error='Interrompido pelo utilizador.', finished=self.now())
            self.save()
            self.report_failure('Interrompido pelo utilizador.')
            return 130
        self.result.update(result='APROVADO', finished=self.now())
        self.save()
        self.out('APROVADO: todas as VMs pararam, voltaram com os mesmos nós e volumes, e os dados são os originais.')
        self.out(f'Resumo: {self.result_path}')
        return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--url', required=True, help='URL do frontend, por exemplo http://IP:30080')
    parser.add_argument('--project', required=True)
    parser.add_argument('--zone', required=True)
    parser.add_argument('--nodes', nargs='+', required=True, help='Todos os nós do cluster (nomes das VMs).')
    parser.add_argument('--control-plane', required=True)
    parser.add_argument('--ssh', required=True, help='Destino SSH do master, por exemplo ubuntu@IP.')
    parser.add_argument('--ssh-key', help='Chave privada SSH (omitir para usar a predefinida).')
    parser.add_argument('--namespace', default='cinematch')
    parser.add_argument('--work-dir', type=Path, help='Pasta nova do ensaio (predefinição: data/persistence/ensaio-DATA).')
    parser.add_argument('--tag', default='cluster', help='Etiqueta das VMs do cluster (cluster).')
    parser.add_argument('--no-tag-check', action='store_true')
    parser.add_argument('--gcloud', default='gcloud')
    parser.add_argument('--initial-timeout', type=float, default=120, help='Segundos para o estado inicial (120).')
    parser.add_argument('--stop-timeout', type=float, default=600, help='Segundos por paragem/arranque das VMs (600).')
    parser.add_argument('--app-timeout', type=float, default=300, help='Segundos para a app deixar de responder (300).')
    parser.add_argument('--k8s-timeout', type=float, default=900, help='Segundos para o cluster recuperar (900).')
    parser.add_argument('--command-timeout', type=float, default=300)
    parser.add_argument('--poll-interval', type=float, default=10)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.url.startswith(('http://', 'https://')):
        parser.error('--url tem de começar por http:// ou https://')
    if args.ssh.startswith('-') or (args.ssh_key or '').startswith('-'):
        parser.error('--ssh e --ssh-key não podem começar por "-".')
    if len(set(args.nodes)) != len(args.nodes) or args.control_plane not in args.nodes:
        parser.error('--nodes tem de ter nomes únicos e incluir o --control-plane.')
    if min(args.initial_timeout, args.stop_timeout, args.app_timeout, args.k8s_timeout, args.command_timeout,
           args.poll_interval) <= 0:
        parser.error('Os tempos têm de ser positivos.')
    if args.work_dir is None:
        args.work_dir = ROOT / 'data' / 'persistence' / datetime.now().strftime('ensaio-%Y%m%d-%H%M%S')
    credentials = Credentials(lambda: getpass.getpass('Palavra-passe da conta de demonstração (8–256 caracteres): '))

    def with_password(function):
        def run(url, state):
            with password_injected(persistence, credentials.get()):
                return function(url, state)
        return run

    cycle = Cycle(args, Kube(args.ssh, args.ssh_key), vms.main, with_password(persistence.prepare),
                  with_password(persistence.verify))
    return cycle.run()


if __name__ == '__main__':
    sys.exit(main())

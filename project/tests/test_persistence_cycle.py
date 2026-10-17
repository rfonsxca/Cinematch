"""Testes do persistence_cycle.py com VMs, Kubernetes e API simulados (nunca tocam em VMs reais)."""
import contextlib
import http.server
import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cycle = load('persistence_cycle', ROOT / 'tests' / 'persistence_cycle.py')
# Reaproveita a gcloud simulada dos testes das VMs (parte 2) para que as duas partes sejam testadas em conjunto.
vm_tests = load('test_persistence_vms', ROOT / 'tests' / 'test_persistence_vms.py')

NODES = ['master', 'worker1', 'worker2']
PVCS = ['users-data', 'recommendations-data', 'ollama-models']
STAGES = ['verificar ferramentas', 'estado inicial do cluster', 'preparar dados (prepare)',
          'verificação inicial (verify)', 'inventário das VMs (inspect)', 'parar todas as VMs (stop)',
          'a aplicação deixou de responder', 'arrancar as VMs (start)', 'cluster recuperado',
          'nós e volumes idênticos', 'verificação final (verify)']


class FakeCluster:
    """Kubernetes simulado: responde a "kubectl get" por SSH e só funciona com o master ligado."""

    def __init__(self, cloud):
        self.cloud = cloud
        self.stays_up = False
        self.never_ready_after_start = False
        self.change_pv_after_start = False
        self.pvc_phase = 'Bound'
        self.not_ready = set()

    def running(self, name):
        return self.cloud.vms[name]['status'] == 'RUNNING'

    def started(self):
        return 'start' in self.cloud.verbs()

    def app_up(self, url):
        return self.stays_up or (self.running('master') and self.running('worker1'))

    def runner(self, args, timeout):
        if not self.running('master'):
            return 255, '', 'ssh: connect to host 1.2.3.4 port 22: Connection timed out'
        parts = shlex.split(args[-1])
        assert parts[:2] == ['kubectl', 'get'] and parts[-2:] == ['-o', 'json']
        resource = parts[2]
        if resource == 'nodes':
            items = [{'metadata': {'name': f'{n}.europe-west4-b.c.p.internal', 'uid': f'node-{n}'},
                      'status': {'conditions': [{'type': 'Ready',
                                                 'status': 'True' if self.running(n) and n not in self.not_ready
                                                 else 'False'}]}} for n in NODES]
        elif resource == 'pvc':
            items = [{'metadata': {'name': p, 'uid': f'pvc-uid-{p}'},
                      'spec': {'volumeName': f'pvc-{p}'}, 'status': {'phase': self.pvc_phase}} for p in PVCS]
        elif resource == 'pv':
            changed = self.change_pv_after_start and self.started()
            items = [{'metadata': {'name': f'pvc-{p}', 'uid': f'pv-uid-{p}' + ('-novo' if changed else '')},
                      'spec': {'local': {'path': f'/opt/local-path-provisioner/pvc-{p}'},
                               'nodeAffinity': {'node': 'worker1'}}} for p in PVCS]
        elif resource == 'deployments':
            ready = 0 if self.never_ready_after_start and self.started() else 1
            items = [{'metadata': {'name': n}, 'spec': {'replicas': 1}, 'status': {'availableReplicas': ready}}
                     for n in ('users', 'recommendations', 'frontend')]
        else:
            return 1, '', f'recurso inesperado: {resource}'
        return 0, json.dumps({'items': items}), ''


class Case(unittest.TestCase):
    def setUp(self):
        self.cloud = vm_tests.FakeGcloud()
        self.cluster = FakeCluster(self.cloud)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name) / 'ensaio'
        self.now = 0.0
        self.lines = []
        self.prepare_calls = []
        self.verify_calls = []
        self.prepare_error = None
        self.verify_errors = []
        self.pretend_ok = None  # ação da ferramenta de VMs que "devolve 0" sem fazer nada

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now

    def prepare(self, url, state):
        self.prepare_calls.append((url, state))
        if self.prepare_error:
            raise self.prepare_error

    def verify(self, url, state):
        position = len(self.verify_calls)
        self.verify_calls.append((url, state))
        if position < len(self.verify_errors) and self.verify_errors[position]:
            raise self.verify_errors[position]

    def vms_main(self, argv):
        if argv[0] == self.pretend_ok:
            return 0
        return cycle.vms.main(argv, runner=self.cloud, sleep=self.sleep, monotonic=self.monotonic)

    def settings(self):
        return cycle.build_parser().parse_args([
            '--url', 'http://1.2.3.5:30080', '--project', 'p', '--zone', 'z', '--nodes', *NODES,
            '--control-plane', 'master', '--ssh', 'ubuntu@1.2.3.4', '--work-dir', str(self.dir),
            '--poll-interval', '1', '--initial-timeout', '20', '--stop-timeout', '60',
            '--app-timeout', '20', '--k8s-timeout', '60'])

    def run_cycle(self, which=lambda tool: '/usr/bin/' + tool):
        runner = cycle.Cycle(
            self.settings(), cycle.Kube('ubuntu@1.2.3.4', runner=self.cluster.runner), self.vms_main,
            self.prepare, self.verify, app_up=self.cluster.app_up, which=which, sleep=self.sleep,
            monotonic=self.monotonic, now=lambda: '2026-10-17T10:00:00+00:00', out=self.lines.append)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return runner.run()

    def result(self):
        return json.loads((self.dir / 'result.json').read_text(encoding='utf-8'))

    def output(self):
        return '\n'.join(self.lines)

    def failed_stage(self):
        return next(s['name'] for s in self.result()['stages'] if not s['ok'])


class FullCycle(Case):
    def test_complete_cycle_is_approved(self):
        self.assertEqual(self.run_cycle(), 0)
        result = self.result()
        self.assertEqual(result['result'], 'APROVADO')
        self.assertEqual([s['name'] for s in result['stages']], STAGES)
        self.assertTrue(all(s['ok'] for s in result['stages']))
        self.assertTrue(result['comparison']['identical'])
        self.assertEqual(len(result['comparison']['before']['volumes']), 3)
        self.assertEqual(len(self.prepare_calls), 1)
        self.assertEqual(len(self.verify_calls), 2)
        self.assertEqual(self.verify_calls[0], self.verify_calls[1])
        self.assertIn('APROVADO', self.output())

    def test_vms_are_stopped_workers_first_and_started_control_plane_first(self):
        self.assertEqual(self.run_cycle(), 0)
        stops = self.cloud.names_for('stop')
        self.assertEqual(stops[-1], ['master'])
        self.assertNotIn('master', stops[0])
        self.assertEqual(self.cloud.names_for('start')[0], ['master'])
        self.assertEqual(self.cloud.statuses(), {n: 'RUNNING' for n in NODES})
        observed = json.loads((self.dir / 'vms.json').read_text(encoding='utf-8'))['observations']
        self.assertEqual([o['action'] for o in observed], ['inspect', 'stop', 'start'])

    def test_result_has_no_secrets(self):
        self.assertEqual(self.run_cycle(), 0)
        text = (self.dir / 'result.json').read_text(encoding='utf-8').lower()
        for word in ('token', 'password', 'palavra-passe'):
            self.assertNotIn(word, text)

    def test_never_deletes_anything(self):
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue(set(self.cloud.verbs()) <= {'describe', 'list', 'stop', 'start'})


class FailuresBeforeStopping(Case):
    def assert_no_vm_touched(self):
        self.assertEqual(self.cloud.calls, [])
        self.assertIn('Nenhuma VM foi parada.', self.output())
        self.assertEqual(self.result()['result'], 'FALHOU')

    def test_failed_prepare_does_not_stop_anything(self):
        self.prepare_error = ValueError('o modelo não respondeu')
        self.assertEqual(self.run_cycle(), 1)
        self.assert_no_vm_touched()
        self.assertEqual(self.failed_stage(), 'preparar dados (prepare)')
        self.assertEqual(self.verify_calls, [])

    def test_failed_initial_verification_does_not_stop_anything(self):
        self.verify_errors = [ValueError('a recomendação mudou')]
        self.assertEqual(self.run_cycle(), 1)
        self.assert_no_vm_touched()
        self.assertEqual(self.failed_stage(), 'verificação inicial (verify)')

    def test_unhealthy_cluster_stops_before_preparing(self):
        self.cluster.pvc_phase = 'Pending'
        self.assertEqual(self.run_cycle(), 1)
        self.assert_no_vm_touched()
        self.assertEqual(self.prepare_calls, [])
        self.assertIn('não está Bound', self.result()['error'])

    def test_node_not_ready_before_starting(self):
        self.cluster.not_ready = {'worker2'}
        self.assertEqual(self.run_cycle(), 1)
        self.assert_no_vm_touched()
        self.assertIn('worker2 não está Ready', self.result()['error'])

    def test_missing_tool_changes_nothing(self):
        self.assertEqual(self.run_cycle(which=lambda tool: None if tool == 'gcloud' else '/usr/bin/ssh'), 2)
        self.assert_no_vm_touched()
        self.assertEqual(self.prepare_calls, [])
        self.assertIn('gcloud', self.result()['error'])

    def test_existing_folder_is_refused_without_writing(self):
        self.dir.mkdir(parents=True)
        (self.dir / 'antigo.json').write_text('{}')
        self.assertEqual(self.run_cycle(), 2)
        self.assertFalse((self.dir / 'result.json').exists())
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.prepare_calls, [])


class FailuresAfterStopping(Case):
    def test_incomplete_stop_is_reported_with_recovery_command(self):
        self.cloud.stuck = {'worker2'}
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'parar todas as VMs (stop)')
        self.assertEqual(self.result()['result'], 'FALHOU')
        self.assertIn('worker2=STOPPING', self.output())
        self.assertIn('persistence_vms.py start --state', self.output())
        self.assertNotIn('start', self.cloud.verbs())
        self.assertEqual(len(self.verify_calls), 1)

    def test_app_still_responding_means_the_stop_is_not_real(self):
        self.cluster.stays_up = True
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'a aplicação deixou de responder')
        self.assertIn('continua a responder', self.result()['error'])
        self.assertNotIn('start', self.cloud.verbs())
        self.assertIn('persistence_vms.py start --state', self.output())

    def test_stop_reported_ok_without_proof_is_not_trusted(self):
        # A ferramenta devolve 0 mas não há observação de que as VMs pararam: não se aceita.
        self.pretend_ok = 'stop'
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'parar todas as VMs (stop)')
        self.assertIn('Não está confirmado', self.result()['error'])
        self.assertNotIn('start', self.cloud.verbs())

    def test_start_reported_ok_without_proof_is_not_trusted(self):
        self.pretend_ok = 'start'
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'arrancar as VMs (start)')
        self.assertIn('Não está confirmado', self.result()['error'])
        self.assertEqual(len(self.verify_calls), 1)

    def test_failed_start_lists_vms_still_stopped(self):
        self.cloud.failures['start'] = 'ERROR: quota exceeded'
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'arrancar as VMs (start)')
        self.assertIn('master=TERMINATED', self.output())
        self.assertIn('persistence_vms.py start --state', self.output())
        self.assertEqual(len(self.verify_calls), 1)

    def test_kubernetes_timeout_after_start(self):
        self.cluster.never_ready_after_start = True
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'cluster recuperado')
        self.assertIn('não ficou pronto em 60 s', self.result()['error'])
        self.assertIn('As VMs estão ligadas.', self.output())
        self.assertEqual(len(self.verify_calls), 1)

    def test_different_volumes_fail_before_checking_data(self):
        self.cluster.change_pv_after_start = True
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'nós e volumes idênticos')
        self.assertIn('pv_uid mudou', self.result()['error'])
        self.assertFalse(self.result()['comparison']['identical'])
        self.assertEqual(len(self.verify_calls), 1)

    def test_different_data_fails(self):
        self.verify_errors = [None, ValueError('A recomendação ou a data mudou')]
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.failed_stage(), 'verificação final (verify)')
        self.assertIn('A recomendação ou a data mudou', self.result()['error'])
        self.assertNotEqual(self.result()['result'], 'APROVADO')


class Helpers(unittest.TestCase):
    def kube(self, cluster):
        return cycle.Kube('ubuntu@1.2.3.4', runner=cluster.runner)

    def test_snapshot_matches_vm_names_exactly(self):
        cluster = FakeCluster(vm_tests.FakeGcloud())
        good = cycle.snapshot(self.kube(cluster), NODES, 'cinematch')
        self.assertEqual(good['problems'], [])
        self.assertEqual(sorted(good['volumes']), sorted(PVCS))
        prefix = cycle.snapshot(self.kube(cluster), ['worker'], 'cinematch')
        self.assertIn('o nó worker não existe no Kubernetes', prefix['problems'])

    def test_kubectl_failure_is_reported(self):
        cluster = FakeCluster(vm_tests.FakeGcloud())
        cluster.cloud.vms['master']['status'] = 'TERMINATED'
        with self.assertRaises(cycle.vms.CommandError):
            self.kube(cluster).get('nodes')

    def test_password_is_injected_and_restored(self):
        module = types.SimpleNamespace(getpass='original')
        with cycle.password_injected(module, 'segredo-123'):
            self.assertEqual(module.getpass.getpass('?'), 'segredo-123')
        self.assertEqual(module.getpass, 'original')

    def test_password_is_asked_only_once(self):
        asked = []
        credentials = cycle.Credentials(lambda: asked.append(1) or 'x')
        self.assertEqual((credentials.get(), credentials.get()), ('x', 'x'))
        self.assertEqual(len(asked), 1)

    def test_bad_arguments_are_refused(self):
        base = ['--url', 'http://x', '--project', 'p', '--zone', 'z', '--ssh', 'ubuntu@1.2.3.4']
        cases = [['--nodes', 'worker1', '--control-plane', 'master'],
                 ['--nodes', 'master', 'master', '--control-plane', 'master'],
                 ['--nodes', 'master', '--control-plane', 'master', '--ssh-key', '-oProxyCommand=x'],
                 ['--nodes', 'master', '--control-plane', 'master', '--poll-interval', '0']]
        for extra in cases:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cycle.main(base + extra)
                self.assertEqual(caught.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cycle.main(['--url', 'ftp://x', '--project', 'p', '--zone', 'z', '--ssh', 'ubuntu@1.2.3.4',
                        '--nodes', 'master', '--control-plane', 'master'])

    def test_default_app_probe(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_address[1]}'
        self.assertTrue(cycle.default_app_up(url, timeout=3))  # qualquer resposta HTTP conta
        server.shutdown()
        server.server_close()
        self.assertFalse(cycle.default_app_up(url, timeout=3))


if __name__ == '__main__':
    unittest.main()

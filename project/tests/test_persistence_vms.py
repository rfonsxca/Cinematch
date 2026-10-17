"""Testes unitários e de integração simulada para o persistence_vms.py."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('persistence_vms', ROOT / 'infra' / 'persistence_vms.py')
vms = importlib.util.module_from_spec(spec)
sys.modules['persistence_vms'] = vms
spec.loader.exec_module(vms)

NODES = ['master', 'worker1', 'worker2']


class FakeGcloud:
    """Mock em memória da CLI gcloud que simula o comportamento e transições de estado das VMs."""

    def __init__(self, names=NODES, delay=2):
        self.delay = delay
        self.calls = []
        self.failures = {}
        self.stuck = set()
        self.vms = {name: {'id': str(7000 + i), 'status': 'RUNNING', 'tags': ['cluster'],
                           'disks': [f'{name}-disk', f'{name}-data']} for i, name in enumerate(names)}

    def statuses(self):
        """Devolve o estado atual de cada VM em dicionário."""
        return {n: v['status'] for n, v in self.vms.items()}

    def verbs(self):
        """Devolve a lista dos verbos gcloud executados."""
        return [call[0] for call in self.calls]

    def names_for(self, verb):
        """Devolve as VMs visadas por um determinado verbo gcloud."""
        return [call[1] for call in self.calls if call[0] == verb]

    def __call__(self, args, timeout):
        """Emula o runner executando comandos gcloud simulados."""
        verb = args[3]
        rest = args[4:]
        names = []
        for token in rest:
            if token.startswith('--'):
                break
            names.append(token)
        self.calls.append((verb, names, self.statuses()))
        if verb in self.failures:
            return 1, '', self.failures[verb]
        if verb == 'list':
            wanted = rest[rest.index('--filter') + 1].split('=', 1)[1]
            return 0, json.dumps([{'name': n} for n, v in self.vms.items() if wanted in v['tags']]), ''
        if verb == 'describe':
            vm = self.vms.get(names[0])
            if vm is None:
                return 1, '', f"ERROR: (gcloud) The resource 'instances/{names[0]}' was not found"
            self.advance(names[0])
            zone = 'https://www.googleapis.com/compute/v1/projects/p/zones/z/disks/'
            return 0, json.dumps({'id': vm['id'], 'status': vm['status'], 'disks': [
                {'source': zone + d, 'boot': i == 0, 'autoDelete': i == 0} for i, d in enumerate(vm['disks'])]}), ''
        for name in names:
            vm = self.vms[name]
            if verb == 'stop' and vm['status'] == 'RUNNING':
                vm.update(status='STOPPING', wait=self.delay, final='TERMINATED')
            if verb == 'start' and vm['status'] == 'TERMINATED':
                vm.update(status='STAGING', wait=self.delay, final='RUNNING')
        return 0, '', ''

    def advance(self, name):
        """Avança um passo a transição de estado da VM simulada."""
        vm = self.vms[name]
        if vm['status'] in ('STOPPING', 'STAGING') and name not in self.stuck:
            vm['wait'] -= 1
            if vm['wait'] <= 0:
                vm['status'] = vm['final']


class Case(unittest.TestCase):
    """Fixture base com mock de ficheiro de estado, gcloud simulada e relógio controlado."""

    def setUp(self):
        self.cloud = FakeGcloud()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / 'vms.json'
        self.now = 0.0

    def sleep(self, seconds):
        """Simula a passagem do tempo instantaneamente."""
        self.now += seconds

    def monotonic(self):
        """Devolve o tempo em segundos do relógio simulado."""
        return self.now

    def run_tool(self, *argv):
        """Executa a função main do vms capturando stdout e stderr."""
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = vms.main(list(argv), runner=self.cloud, sleep=self.sleep, monotonic=self.monotonic)
        return code, output.getvalue(), errors.getvalue()

    def inspect(self, *extra, nodes=NODES):
        """Atalho de conveniência para correr o subcomando inspect."""
        return self.run_tool('inspect', '--project', 'p', '--zone', 'z', '--nodes', *nodes,
                             '--control-plane', 'master', '--state', str(self.state), *extra)

    def operate(self, action, *extra):
        """Atalho de conveniência para correr stop ou start."""
        return self.run_tool(action, '--state', str(self.state), '--timeout', '60', '--poll-interval', '1', *extra)

    def saved(self):
        """Lê e devolve o conteúdo atual do ficheiro de estado em JSON."""
        return json.loads(self.state.read_text())


class Inspect(Case):
    """Testes ao subcomando inspect (criação de inventário e validações)."""

    def test_saves_inventory_without_credentials(self):
        code, output, _ = self.inspect()
        self.assertEqual(code, 0)
        state = self.saved()
        self.assertEqual(state['nodes'], NODES)
        self.assertEqual(state['control_plane'], 'master')
        self.assertEqual(state['inventory']['worker1']['id'], '7001')
        self.assertEqual([d['name'] for d in state['inventory']['worker1']['disks']], ['worker1-data', 'worker1-disk'])
        self.assertEqual(state['observations'][0]['statuses'], {n: 'RUNNING' for n in NODES})
        self.assertNotIn('password', self.state.read_text().lower())
        self.assertIn('auto-delete', output)

    def test_refuses_to_replace_an_existing_inventory(self):
        self.inspect()
        before = self.state.read_text()
        calls = len(self.cloud.calls)
        code, _, errors = self.inspect()
        self.assertEqual(code, 2)
        self.assertEqual(self.state.read_text(), before)
        self.assertEqual(len(self.cloud.calls), calls)
        self.assertIn('já existe', errors)

    def test_incomplete_node_list_is_refused(self):
        code, _, errors = self.inspect(nodes=['master', 'worker1'])
        self.assertEqual(code, 1)
        self.assertIn('worker2', errors)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.inspect('--no-tag-check', nodes=['master', 'worker1'])[0], 0)

    def test_missing_vm_is_refused(self):
        code, _, errors = self.inspect(nodes=NODES + ['worker3'])
        self.assertEqual(code, 1)
        self.assertIn('worker3', errors)
        self.assertFalse(self.state.exists())

    def test_control_plane_must_be_in_the_list(self):
        code, _, _ = self.run_tool('inspect', '--project', 'p', '--zone', 'z', '--nodes', 'worker1',
                                   '--control-plane', 'master', '--state', str(self.state))
        self.assertEqual(code, 2)
        self.assertFalse(self.state.exists())

    def test_command_failure_is_reported(self):
        self.cloud.failures['describe'] = 'ERROR: permission denied'
        code, _, errors = self.inspect()
        self.assertEqual(code, 1)
        self.assertIn('permission denied', errors)
        self.assertFalse(self.state.exists())


class StopAndStart(Case):
    """Testes às operações de paragem e arranque de VMs."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.inspect()[0], 0)
        self.cloud.calls.clear()

    def test_stop_orders_workers_before_control_plane_and_confirms(self):
        code, output, _ = self.operate('stop')
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud.statuses(), {n: 'TERMINATED' for n in NODES})
        stops = self.cloud.names_for('stop')
        self.assertEqual(stops, [['worker1', 'worker2'], ['master']])
        master_call = [c for c in self.cloud.calls if c[0] == 'stop' and c[1] == ['master']][0]
        self.assertEqual(master_call[2]['worker1'], 'TERMINATED')
        self.assertEqual(master_call[2]['worker2'], 'TERMINATED')
        last = self.saved()['observations'][-1]
        self.assertEqual((last['action'], last['ok']), ('stop', True))
        self.assertEqual(set(last['statuses'].values()), {'TERMINATED'})
        self.assertIn('Confirmado', output)

    def test_start_orders_control_plane_first_and_confirms(self):
        self.operate('stop')
        self.cloud.calls.clear()
        code, _, _ = self.operate('start')
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud.statuses(), {n: 'RUNNING' for n in NODES})
        self.assertEqual(self.cloud.names_for('start'), [['master'], ['worker1', 'worker2']])
        worker_call = [c for c in self.cloud.calls if c[0] == 'start' and c[1] == ['worker1', 'worker2']][0]
        self.assertEqual(worker_call[2]['master'], 'RUNNING')
        self.assertEqual(self.saved()['observations'][-1]['action'], 'start')

    def test_repeating_operations_changes_nothing(self):
        self.operate('stop')
        self.cloud.calls.clear()
        self.assertEqual(self.operate('stop')[0], 0)
        self.assertEqual(self.cloud.names_for('stop'), [])
        self.operate('start')
        self.cloud.calls.clear()
        self.assertEqual(self.operate('start')[0], 0)
        self.assertEqual(self.cloud.names_for('start'), [])

    def test_resumes_when_some_nodes_are_already_stopped(self):
        self.cloud.vms['worker1']['status'] = 'TERMINATED'
        self.assertEqual(self.operate('stop')[0], 0)
        self.assertEqual(self.cloud.names_for('stop'), [['worker2'], ['master']])
        self.assertEqual(set(self.cloud.statuses().values()), {'TERMINATED'})

    def test_start_waits_for_a_node_that_is_still_stopping(self):
        self.cloud.vms['worker2'].update(status='STOPPING', wait=2, final='TERMINATED')
        self.cloud.vms['worker1']['status'] = 'TERMINATED'
        self.cloud.vms['master']['status'] = 'TERMINATED'
        self.assertEqual(self.operate('start')[0], 0)
        self.assertEqual(set(self.cloud.statuses().values()), {'RUNNING'})

    def test_timeout_keeps_progress_and_does_not_stop_the_control_plane(self):
        self.cloud.stuck.add('worker2')
        code, output, errors = self.run_tool('stop', '--state', str(self.state), '--timeout', '10', '--poll-interval', '2')
        self.assertEqual(code, 1)
        self.assertIn('Tempo esgotado', errors)
        self.assertEqual(self.cloud.vms['master']['status'], 'RUNNING')
        last = self.saved()['observations'][-1]
        self.assertFalse(last['ok'])
        self.assertEqual(last['statuses']['worker2'], 'STOPPING')
        self.assertIn('inventory', self.saved())
        self.assertIn('idempotente', output)
        self.cloud.stuck.clear()
        self.assertEqual(self.operate('stop')[0], 0)
        self.assertEqual(set(self.cloud.statuses().values()), {'TERMINATED'})

    def test_failed_command_is_recorded_and_can_be_retried(self):
        self.cloud.failures['stop'] = 'ERROR: quota exceeded'
        code, _, errors = self.operate('stop')
        self.assertEqual(code, 1)
        self.assertIn('quota exceeded', errors)
        self.assertEqual(self.saved()['observations'][-1]['ok'], False)
        self.assertEqual(set(self.cloud.statuses().values()), {'RUNNING'})
        del self.cloud.failures['stop']
        self.assertEqual(self.operate('stop')[0], 0)

    def test_recreated_vm_with_another_id_is_refused(self):
        self.cloud.vms['worker1']['id'] = '9999'
        code, _, errors = self.operate('stop')
        self.assertEqual(code, 1)
        self.assertIn('outro ID', errors)
        self.assertEqual(self.cloud.names_for('stop'), [])
        self.assertEqual(self.operate('start')[0], 1)
        self.assertEqual(self.cloud.names_for('start'), [])

    def test_different_disks_are_refused(self):
        self.cloud.vms['worker2']['disks'] = ['worker2-disk']
        code, _, errors = self.operate('stop')
        self.assertEqual(code, 1)
        self.assertIn('discos diferentes', errors)
        self.assertEqual(self.cloud.names_for('stop'), [])

    def test_vm_missing_after_inspect_is_refused(self):
        del self.cloud.vms['worker1']
        code, _, errors = self.operate('stop')
        self.assertEqual(code, 1)
        self.assertIn('worker1', errors)
        self.assertEqual(self.cloud.names_for('stop'), [])

    def test_never_uses_destructive_operations(self):
        self.operate('stop')
        self.operate('start')
        self.assertLessEqual(set(self.cloud.verbs()), {'describe', 'list', 'stop', 'start'})
        with self.assertRaises(ValueError):
            vms.Cloud('p', 'z', runner=self.cloud).run('delete', ['master'])


class InvalidState(Case):
    """Testes para validação e rejeição de ficheiros de estado inválidos."""

    def test_incomplete_inventory_is_refused_before_any_command(self):
        self.inspect()
        state = self.saved()
        del state['inventory']['worker2']
        self.state.write_text(json.dumps(state))
        self.cloud.calls.clear()
        for action in ('stop', 'start'):
            self.assertEqual(self.operate(action)[0], 2)
        self.assertEqual(self.cloud.calls, [])

    def test_control_plane_missing_from_nodes_is_refused(self):
        self.inspect()
        state = self.saved()
        state['control_plane'] = 'ghost'
        self.state.write_text(json.dumps(state))
        self.cloud.calls.clear()
        self.assertEqual(self.operate('stop')[0], 2)
        self.assertEqual(self.cloud.calls, [])

    def test_missing_and_corrupt_files_are_refused(self):
        self.assertEqual(self.operate('stop')[0], 2)
        self.state.write_text('{não é json')
        self.assertEqual(self.operate('start')[0], 2)
        self.assertEqual(self.cloud.calls, [])


class RealRunner(unittest.TestCase):
    """Testes ao default_runner com processos locais reais."""

    def test_missing_executable(self):
        with self.assertRaises(vms.CommandError):
            vms.default_runner(['gcloud-que-nao-existe-xyz', 'a'], 5)

    def test_command_timeout(self):
        with self.assertRaises(vms.CommandError) as caught:
            vms.default_runner([sys.executable, '-c', 'import time; time.sleep(10)'], 0.3)
        self.assertIn('excedeu', str(caught.exception))

    def test_runs_real_processes_with_a_list_of_arguments(self):
        code, out, _ = vms.default_runner([sys.executable, '-c', 'print("ok; echo perigo")'], 10)
        self.assertEqual((code, out.strip()), (0, 'ok; echo perigo'))


if __name__ == '__main__':
    unittest.main()
"""Regression checks for preserving owner volumes and leaving active containers alone."""
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ['WORKBENCH_SANDBOX_MANAGER_TOKEN'] = 'test-token-' * 4
state = tempfile.TemporaryDirectory()
os.environ['WORKBENCH_MANAGER_STATE'] = state.name
spec = importlib.util.spec_from_file_location('candidate_manager', Path(__file__).parents[1] / 'sandbox-manager' / 'server.py')
module = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', [str(Path(spec.origin).parent), *sys.path]):
    spec.loader.exec_module(module)
module.IMAGE = 'candidate-office-image'
key = '12345678-1234-5678-1234-567812345678'

class MigrationTests(unittest.TestCase):
    def scenario(self, exists, running=False, current=False):
        calls = []
        present = exists
        def docker(*args, **kwargs):
            nonlocal present
            calls.append((args, kwargs))
            if args[0] == 'inspect':
                value = [{'Id': 'a' * 64, 'Config': {'Image': module.IMAGE if current else 'old-image'}, 'State': {'Running': running}}]
                return SimpleNamespace(returncode=0 if present else 1, stdout=json.dumps(value) if present else '')
            if args[0] == 'rename':
                present = False
            return SimpleNamespace(returncode=0, stdout='')
        module.docker = docker
        if exists and running and not current:
            with self.assertRaisesRegex(RuntimeError, '个人沙箱需要更新'):
                module.ensure(key)
            return calls
        result = module.ensure(key)
        self.assertIn('endpoint', result)
        self.assertTrue((Path(state.name) / key).exists())
        for args, _ in calls:
            if args[0] == 'exec':
                interpreter = args.index(name := module.identity(key)[0]) + 1
                self.assertEqual(args[interpreter:interpreter + 4], ('/usr/local/bin/python', '-I', '-S', '-c'))
            elif args[0] == 'run':
                self.assertEqual(args[args.index('--entrypoint') + 1], '/usr/local/bin/python')
                image = args.index(module.IMAGE)
                self.assertEqual(args[image + 1:image + 4], ('-I', '-S', '-c'))
        return calls

    def test_running_previous_image_is_not_interrupted_or_used_for_helpers(self):
        calls = self.scenario(True, True)
        self.assertEqual([args[0] for args, _ in calls], ['inspect'])

    def test_current_image_uses_existing_container(self):
        calls = self.scenario(True, False, True)
        self.assertEqual([args[0] for args, _ in calls], ['inspect', 'start', 'exec'])

    def test_stopped_previous_image_retains_container_and_volumes(self):
        calls = self.scenario(True)
        commands = [args[0] for args, _ in calls]
        self.assertIn('rename', commands)
        self.assertNotIn('rm', commands)
        name, _ = module.identity(key)
        creation = next(args for args, _ in calls if args[0] == 'create')
        for suffix, target in [('home', '/home/dify'), ('files', '/workspace'), ('env', '/opt/user-env:ro')]:
            self.assertIn(name + '-' + suffix + ':' + target, creation)
        self.assertIn('no-new-privileges:true', creation)
        preparation = [args for args, _ in calls if args[0] == 'run'][-1]
        source = preparation[-1]
        compile(source, '<venv-fallback-preparation>', 'exec')
        self.assertIn('workbench_office.pth', source)
        with tempfile.TemporaryDirectory() as directory:
            site, base = Path(directory) / 'site', Path(directory) / 'base'
            site.mkdir()
            base.mkdir()
            code = source.replace('/opt/user-env/current/python/lib/python3.12/site-packages', site.as_posix()).replace('/opt/office/python/lib/python3.12/site-packages', base.as_posix())
            exec(code, {})
            self.assertEqual((site / 'workbench_office.pth').read_text(), str(base) + '\n')

    def test_new_owner_gets_isolated_volumes(self):
        calls = self.scenario(False)
        volumes = [args[-1] for args, _ in calls if args[0] == 'volume']
        self.assertEqual(len(volumes), 3)
        self.assertTrue(all(key in volume for volume in volumes))

if __name__ == '__main__':
    unittest.main()

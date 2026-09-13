import base64
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('file_ops', str(Path(__file__).parents[1] / 'sandbox-manager' / 'file_ops.py'))
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class FileOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='workbench-files-')
        self.root = Path(self.tmp.name)
        (self.root / 'conversations').mkdir()
        self.folder = 'conversations/11111111-1111-4111-8111-111111111111'
        self.op('mkdir', self.folder)

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, operation, path, **extra):
        return ops.operate(dict(operation=operation, path=path, **extra), str(self.root))

    def upload(self, path, content=b'hello'):
        return self.op('upload', path, version=None, data=base64.b64encode(content).decode())

    def test_directory_archive_and_version_checked_delete(self):
        self.upload(self.folder + '/材料.txt', '中文材料'.encode())
        (self.root / self.folder / 'empty').mkdir()
        row = self.op('list', 'conversations')['entries'][0]
        result = self.op('get', self.folder)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result['data']))) as archive:
            self.assertEqual(archive.read('材料.txt').decode(), '中文材料')
            self.assertIn('empty/', archive.namelist())
        self.upload(self.folder + '/new.txt')
        self.assertTrue(self.op('delete', self.folder, version=row['version'])['conflict'])
        row = self.op('list', 'conversations')['entries'][0]
        self.assertTrue(self.op('delete', self.folder, version=row['version'])['deleted'])
        self.assertFalse((self.root / self.folder).exists())
        self.op('mkdir', self.folder)
        self.assertEqual(self.op('list', self.folder)['entries'], [])

    def test_symlink_never_reads_or_deletes_target(self):
        foreign = self.root / 'private.txt'
        foreign.write_text('protected')
        (self.root / self.folder / 'link').symlink_to(foreign)
        with self.assertRaises((ValueError, OSError)):
            self.op('get', self.folder)
        row = self.op('list', 'conversations')['entries'][0]
        self.op('delete', self.folder, version=row['version'])
        self.assertEqual(foreign.read_text(), 'protected')

    def test_traversal_and_workspace_root_mutation_rejected(self):
        for path in ['conversations/../private.txt', 'conversations//file', 'conversations/./file']:
            with self.assertRaises(ValueError):
                self.op('get', path)
        for operation in ['get', 'delete']:
            with self.assertRaises(ValueError):
                self.op(operation, 'conversations', version=None)


if __name__ == '__main__':
    unittest.main()

import base64
import importlib.util
from pathlib import Path
import os
import pytest

spec=importlib.util.spec_from_file_location('file_ops',Path(__file__).parents[1]/'sandbox-manager'/'file_ops.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def root(tmp_path):
    (tmp_path/'shared').mkdir()
    (tmp_path/'conversations').mkdir()
    return str(tmp_path)


def test_versions_create_conflict_atomic_replace_and_delete(root):
    def call(**payload):
        return module.operate({'path':'shared/中文.txt'}|payload,root)
    created=call(operation='upload',version=None,data=base64.b64encode(b'first').decode())
    assert call(operation='upload',version=None,data='')=={'conflict':True,'message':'文件已改变，请刷新后重试'}
    assert call(operation='get')['data']==base64.b64encode(b'first').decode()
    updated=call(operation='upload',version=created['version'],data=base64.b64encode(b'second').decode())
    assert updated['version']!=created['version']
    assert call(operation='delete',version=created['version'])['conflict']
    assert call(operation='delete',version=updated['version'])=={'deleted':True}
    assert not list((Path(root)/'shared').glob('.upload-*'))


@pytest.mark.parametrize('path',['../secret','shared/../secret','shared//file','shared/./file','/etc/passwd','shared\\secret'])
def test_path_traversal_is_rejected(root,path):
    with pytest.raises((ValueError,OSError)):
        module.operate({'operation':'get','path':path},root)


def test_symlink_directory_leaf_and_fifo_are_blocked(root,tmp_path):
    outside=tmp_path/'private'
    outside.mkdir()
    (outside/'secret').write_bytes(b'private')
    os.symlink(outside,Path(root)/'shared'/'escape')
    os.symlink(outside/'secret',Path(root)/'shared'/'leaf')
    os.mkfifo(Path(root)/'shared'/'pipe')
    for path in ('shared/escape/secret','shared/leaf','shared/pipe'):
        with pytest.raises((ValueError,OSError)):
            module.operate({'operation':'get','path':path},root)
    entries=module.operate({'operation':'list','path':'shared'},root)['entries']
    assert all(item['kind']=='blocked' for item in entries)
    assert (outside/'secret').read_bytes()==b'private'

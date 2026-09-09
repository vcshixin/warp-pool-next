"""只在本服务拥有的目录内保存状态。"""

import json
import os
from pathlib import Path
import tempfile


def atomic_write(path, value, mode=0o600, gid=None):
    path = Path(path)
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, mode)
        if gid is not None:
            os.fchown(fd, -1, gid)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def owned_directory(path, owner):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = path / 'owner.json'
    if marker.exists():
        if json.loads(marker.read_text()) != {'owner': owner}:
            raise RuntimeError('状态目录不属于当前服务实例')
    else:
        if any(path.iterdir()):
            raise RuntimeError('状态目录非空且缺少归属标记')
        atomic_write(marker, {'owner': owner})
    if path.stat().st_mode & 0o077:
        raise RuntimeError('状态目录权限应为 700')

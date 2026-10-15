#!/usr/bin/env python3
"""Build distributable ZIPs from an explicit public-file allowlist."""
from pathlib import Path
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'skills' / 'shimo-drive'
FILES = ['SKILL.md', 'scripts/drive_api.py', 'scripts/document.py',
         'scripts/environment.py', 'scripts/bootstrap.sh', 'scripts/bootstrap.ps1',
         'references/setup.md', 'references/commands.md']
DIST = ROOT / 'dist'


def main():
    entries = {name: (SOURCE / name).read_bytes() for name in FILES}
    skill = entries['SKILL.md'].decode()
    match = re.search(r'^  version: (\d+\.\d+\.\d+)$', skill, re.MULTILINE)
    if not match:
        raise ValueError('Expected a semantic version in SKILL.md metadata')
    version = match.group(1)
    plugin = json.loads((ROOT / 'plugin.json').read_text())
    if plugin['version'] != version:
        raise ValueError('Plugin and Skill versions must match')
    DIST.mkdir(exist_ok=True)
    manifest = {}
    for variant in ('portable', 'workbuddy'):
        path = DIST / ('shimo-drive-' + version + '-' + variant + '.zip')
        payload = dict(entries)
        if variant == 'workbuddy':
            fields = ('display_name: 石墨私有化 Drive\n'
                      'display_name_en: Shimo Private Drive\n'
                      'description_zh: 按名称和目录查找石墨私有化资料，阅读并修改轻文档普通正文。自动检查运行环境，协助安装 Python 和管理员提供的轻文档 CLI；需企业地址和本人授权。\n'
                      'description_en: Find and edit lightweight documents in private Shimo Drive. Check dependencies and assist with Python and administrator-supplied CLI installation.\n'
                      f'version: {version}\n'
                      'author: 石墨\n')
            payload['SKILL.md'] = skill.replace('name: shimo-drive\n', 'name: shimo-drive\n' + fields, 1).encode()
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                info = zipfile.ZipInfo('shimo-drive/' + name, (2026, 10, 14, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        manifest[path.name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'files': {k: hashlib.sha256(v).hexdigest() for k, v in payload.items()}}
    (DIST / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

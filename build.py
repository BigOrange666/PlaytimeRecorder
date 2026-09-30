"""把 PlaytimeRecorder 插件打包成 .mcdr（MCDR 多文件插件包，本质是 zip）。

用法：
    python build.py

产物：
    dist/playtime_recorder-v<版本>.mcdr
    dist/SHA256SUMS

.mcdr 内部结构：

    mcdreforged.plugin.json
    __init__.py            入口（含 MCDR 事件钩子）
    qqbridge/              OneBot v11 客户端、游玩记录核心与解析工具
        __init__.py
        logging_util.py
        recorder.py        游玩记录核心
        records.py
        ws_client.py
        onebot.py
    lang/zh_cn.yml         翻译
"""

import hashlib
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))          # 仓库根 = 插件根
DIST_DIR = os.path.join(HERE, 'dist')
METADATA_PATH = os.path.join(HERE, 'mcdreforged.plugin.json')
LANG_DIR = os.path.join(HERE, 'lang')

# 运行时需要的文件（相对仓库根），顺序即 zip 内顺序。
# 注意：MCDR 的 .mcdr 打包格式不允许插件根目录出现除入口外的其它 .py 模块
# （会报 "Packed plugin cannot contain other module"），
# 所以除了入口 __init__.py，其余代码都必须待在包目录里。
RUNTIME_FILES = [
    '__init__.py',
]
PACKAGE_DIR_NAME = 'qqbridge'

PACKAGE_INIT_TEMPLATE = '''"""qqbridge: OneBot v11 (NapCat) 客户端与游玩记录解析工具包。

纯标准库实现，随 PlaytimeRecorder 插件一起分发。
"""

__version__ = '{version}'
'''


def read_metadata():
    with open(METADATA_PATH, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def safe_name(name):
    return re.sub(r'[^0-9A-Za-z._-]+', '_', str(name))


def collect_entries(version):
    entries = [(METADATA_PATH, 'mcdreforged.plugin.json', None)]

    for name in RUNTIME_FILES:
        path = os.path.join(HERE, name)
        entries.append((path, name, None))

    package_dir = os.path.join(HERE, PACKAGE_DIR_NAME)
    if not os.path.isdir(package_dir):
        return entries
    entries.append((None, '{}/__init__.py'.format(PACKAGE_DIR_NAME),
                    PACKAGE_INIT_TEMPLATE.format(version=version).encode('utf-8')))
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith('.py'):
            continue
        path = os.path.join(package_dir, name)
        if os.path.isfile(path):
            entries.append((path, '{}/{}'.format(PACKAGE_DIR_NAME, name), None))
    return entries


def collect_lang_entries():
    entries = []
    if os.path.isdir(LANG_DIR):
        for name in sorted(os.listdir(LANG_DIR)):
            if name.endswith(('.yml', '.yaml', '.json')):
                entries.append((os.path.join(LANG_DIR, name), 'lang/' + name, None))
    for icon_name in ('icon.png', 'icon.jpg'):
        icon = os.path.join(HERE, icon_name)
        if os.path.isfile(icon):
            entries.append((icon, icon_name, None))
    return entries


def check_top_level_layout():
    """MCDR 的打包插件格式：顶层只能有入口 __init__.py，其余 .py 必须在包目录里。

    这里显式拦一道，避免“本地能跑、装进 MCDR 就报
    Packed plugin cannot contain other module”这种迟到的错误。
    """
    allowed = {'__init__.py', 'build.py', 'run_all_tests.py'}
    offenders = []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith('.py') or name in allowed:
            continue
        offenders.append(name)
    return offenders


def main():
    for required, description in ((METADATA_PATH, '插件元数据'),
                                  (os.path.join(HERE, '__init__.py'), '插件入口'),
                                  (os.path.join(HERE, PACKAGE_DIR_NAME), '模块包目录')):
        if not os.path.exists(required):
            print('[!] 找不到{}: {}'.format(description, required))
            return 1

    offenders = check_top_level_layout()
    if offenders:
        print('[!] 插件根目录出现了不该有的 .py 模块: {}'.format(', '.join(offenders)))
        print('    MCDR 的 .mcdr 格式只允许入口 __init__.py，其余代码请放进 {}/ 目录，'.format(
            PACKAGE_DIR_NAME))
        print('    否则加载时会报: Packed plugin cannot contain other module')
        return 1

    metadata = read_metadata()
    version = str(metadata.get('version', '0.0.0'))
    plugin_id = str(metadata.get('id', 'plugin'))

    entries = collect_entries(version) + collect_lang_entries()
    missing = [path for path, _, content in entries if path is not None and not os.path.isfile(path)]
    if missing:
        print('[!] 缺少文件:')
        for path in missing:
            print('    {}'.format(path))
        return 1

    os.makedirs(DIST_DIR, exist_ok=True)
    target = os.path.join(DIST_DIR, '{}-v{}.mcdr'.format(safe_name(plugin_id), version))
    if os.path.isfile(target):
        os.remove(target)

    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, name, content in entries:
            if content is not None:
                archive.writestr(name, content)
            else:
                archive.write(path, name)

    digest = hashlib.sha256()
    with open(target, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)

    size = os.path.getsize(target)
    print('已生成: {}'.format(target))
    print('大小  : {:.1f} KB'.format(size / 1024.0))
    print('SHA256: {}'.format(digest.hexdigest()))
    print('包含 {} 个文件:'.format(len(entries)))
    for path, name, content in entries:
        print('    {}'.format(name))

    sums_path = os.path.join(DIST_DIR, 'SHA256SUMS')
    with open(sums_path, 'w', encoding='utf-8') as handle:
        handle.write('{}  {}\n'.format(digest.hexdigest(), os.path.basename(target)))
    print('\n校验文件: {}'.format(sums_path))
    return 0


if __name__ == '__main__':
    sys.exit(main())

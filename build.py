"""把 PlaytimeRecorder 插件打包成 .mcdr（MCDR 多文件插件包，本质是 zip）。

用法：
    python build.py

产物：
    dist/playtime_recorder-v<版本>.mcdr
    dist/SHA256SUMS

.mcdr 内部结构（关键：入口是一个以插件 id 命名的**子目录**）：

    mcdreforged.plugin.json
    playtime_recorder/
        __init__.py            插件主体（entrypoint 指向这个包）
        qqbridge/
            __init__.py
            logging_util.py
            recorder.py
            records.py
            ws_client.py
            onebot.py
        lang/zh_cn.yml

为什么必须是子目录：MCDR 加载时把解压目录加进 sys.path，然后
    importlib.import_module(metadata.entrypoint)
如果 entrypoint 是 'playtime_recorder'，那么 sys.path 里必须能找到名为
playtime_recorder 的包——也就是解压目录下要有 playtime_recorder/ 子目录。
把 __init__.py 直接放在解压目录根部是不行的（那样没人是 playtime_recorder 包）。
"""

import hashlib
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))          # 仓库根
DIST_DIR = os.path.join(HERE, 'dist')
METADATA_PATH = os.path.join(HERE, 'mcdreforged.plugin.json')
LANG_DIR_NAME = 'lang'

# 插件包目录：以插件 id 命名，等于 metadata 的 entrypoint
PACKAGE_DIR_NAME = 'playtime_recorder'
PACKAGE_DIR = os.path.join(HERE, PACKAGE_DIR_NAME)


def read_metadata():
    with open(METADATA_PATH, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def safe_name(name):
    return re.sub(r'[^0-9A-Za-z._-]+', '_', str(name))


def collect_entries():
    """返回 [(磁盘路径 或 None, zip 内路径, 内容 bytes 或 None)]。"""
    entries = [(METADATA_PATH, 'mcdreforged.plugin.json', None)]

    for current, dirs, files in os.walk(PACKAGE_DIR):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in sorted(files):
            if name.endswith('.pyc'):
                continue
            path = os.path.join(current, name)
            relative = os.path.relpath(path, HERE).replace(os.sep, '/')
            entries.append((path, relative, None))
    return entries


def check_metadata(metadata):
    """校验 MCDR 的元数据规则，本地就拦住，不用等加载插件才报错。"""
    plugin_id = str(metadata.get('id', 'plugin'))
    entrypoint = str(metadata.get('entrypoint') or plugin_id)

    if entrypoint != plugin_id and not entrypoint.startswith(plugin_id + '.'):
        print('[!] entrypoint 不合法: {!r}（插件 id 是 {!r}）'.format(entrypoint, plugin_id))
        print("    MCDR 要求 entrypoint 等于插件 id，或以 '<id>.' 开头，")
        print('    否则会报: Invalid entry point ... for plugin id ...')
        return None

    # entrypoint 的第一段必须对应包目录下的一个子目录或模块
    top = entrypoint.split('.')[0]
    package_dir = os.path.join(HERE, top)
    package_init = os.path.join(package_dir, '__init__.py')
    package_module = os.path.join(HERE, top + '.py')
    if not (os.path.isfile(package_init) or os.path.isfile(package_module)):
        print('[!] entrypoint {!r} 对应的入口不存在。'.format(entrypoint))
        print('    MCDR 会把解压目录加进 sys.path 再 import 这个名字，所以需要:')
        print('      {}/__init__.py   （子包形式，推荐）'.format(top))
        print('    或 {}.py'.format(os.path.join(HERE, top)))
        return None
    return entrypoint


def main():
    for required, description in ((METADATA_PATH, '插件元数据'),
                                  (os.path.join(PACKAGE_DIR, '__init__.py'), '插件包入口'),
                                  (PACKAGE_DIR, '插件包目录')):
        if not os.path.exists(required):
            print('[!] 找不到{}: {}'.format(description, required))
            return 1

    metadata = read_metadata()
    version = str(metadata.get('version', '0.0.0'))
    plugin_id = str(metadata.get('id', 'plugin'))

    entrypoint = check_metadata(metadata)
    if entrypoint is None:
        return 1

    entries = collect_entries()
    names = [name for _, name, _ in entries]
    duplicates = sorted(set(name for name in names if names.count(name) > 1))
    if duplicates:
        print('[!] 打包清单里有重名文件: {}'.format(', '.join(duplicates)))
        return 1

    missing = [path for path, _, _ in entries if path is not None and not os.path.isfile(path)]
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
    print('entrypoint: {}'.format(entrypoint))
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

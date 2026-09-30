"""一条命令跑完全部离线检查。

用法（在 PlaytimeRecorder 目录下）：
    python run_all_tests.py

不需要 MCDR、不需要 QQ、不需要联网。
"""

import compileall
import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or 'python'

for stream_name in ('stdout', 'stderr'):
    stream = getattr(sys, stream_name, None)
    if stream is not None and hasattr(stream, 'reconfigure'):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def section(title):
    print('\n' + '=' * 68)
    print(title)
    print('=' * 68)


def run_script(relative_path, extra_args=None):
    path = os.path.join(HERE, relative_path)
    args = [PY, path] + list(extra_args or [])
    print('> {} {}'.format(os.path.basename(PY), os.path.relpath(path, HERE)))
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    try:
        completed = subprocess.run(args, cwd=HERE, env=env)
    except OSError as exc:
        print('无法启动: {}'.format(exc))
        return False, 'spawn failed'
    return completed.returncode == 0, 'exit={}'.format(completed.returncode)


def step_syntax():
    section('1/6  语法检查（编译所有 .py）')
    ok = True
    for target in ('entry.py', '__init__.py', 'build.py', 'run_all_tests.py',
                   os.path.join('qqbridge'), os.path.join('tests'),
                   os.path.join('tools')):
        path = os.path.join(HERE, target)
        if not os.path.exists(path):
            continue
        result = compileall.compile_file(path, quiet=1) if os.path.isfile(path) \
            else compileall.compile_dir(path, quiet=1)
        if not result:
            ok = False
            print('[FAIL] 编译失败: {}'.format(target))
    if ok:
        print('[PASS] 所有 Python 文件语法正确')
    return ok


def step_format_strings():
    section('2/6  静态检查：str.format 占位符与参数是否匹配')
    ok, detail = run_script(os.path.join('tools', 'check_format_strings.py'), [HERE])
    return ok


def step_import():
    section('4/6  导入自检（按 MCDR 的真实方式加载 entry.py）')
    import importlib.util
    entry = os.path.join(HERE, 'entry.py')
    ok = True
    try:
        spec = importlib.util.spec_from_file_location('_selftest_entry', entry)
        module = importlib.util.module_from_spec(spec)
        sys.modules['_selftest_entry'] = module
        spec.loader.exec_module(module)
        print('[PASS] entry.py 导入成功')
        for attr in ('on_load', 'on_unload', 'on_player_joined', 'on_player_left',
                     'on_info', 'entry'):
            if hasattr(module, attr) and getattr(module, attr) is not None:
                print('[PASS] entry 暴露 {}'.format(attr))
            else:
                ok = False
                print('[FAIL] entry 缺少 {}'.format(attr))
        plugin = sys.modules.get('playtime_recorder')
        if plugin is None:
            ok = False
            print('[FAIL] 没有注册 playtime_recorder 包')
        else:
            for attr in ('PlaytimeRecorder', 'QQNotifier', 'load_config',
                         'deep_merge', 'record_lib', 'SafeLogger'):
                if hasattr(plugin, attr):
                    print('[PASS] 插件导出 {}'.format(attr))
                else:
                    ok = False
                    print('[FAIL] 插件缺少导出 {}'.format(attr))
        global PLUGIN
        PLUGIN = plugin
    except Exception:
        ok = False
        print('[FAIL] entry.py 导入失败:')
        traceback.print_exc()
    return ok


def step_quick_checks():
    section('5/6  关键逻辑快检')
    import tempfile
    from datetime import datetime

    plugin = PLUGIN
    R = plugin.record_lib
    as_int = plugin.as_int
    split_message = plugin._onebot.split_message

    ok = True

    def expect(name, actual, wanted):
        nonlocal ok
        if actual == wanted:
            print('[PASS] {}'.format(name))
        else:
            ok = False
            print('[FAIL] {} 期望 {!r} 实际 {!r}'.format(name, wanted, actual))

    now = datetime(2025, 6, 10, 15, 30, 0)
    rng = R.default_range(now)
    expect('默认范围起点=昨天0点', rng.start, datetime(2025, 6, 9, 0, 0, 0))
    expect('默认范围终点=现在', rng.end, now)

    rng, lines = R.parse_range_args('近3天 上限20', now=now)
    expect('近3天起点', rng.start, datetime(2025, 6, 8, 0, 0, 0))
    expect('上限解析', lines, 20)

    rng, _ = R.parse_range_args('2025-06-01', now=now)
    expect('指定日期起点', rng.start, datetime(2025, 6, 1, 0, 0, 0))

    expect('时长解析（含 0 单位）', R.parse_duration('2小时0分钟0秒'), 7200)
    expect('时长解析（纯秒）', R.parse_duration('30秒'), 30)
    expect('时长解析（非法）', R.parse_duration('未知'), None)
    expect('时长格式化', R.format_duration(2 * 3600 + 25 * 60 + 8), '2小时25分钟8秒')
    expect('_extract_duration 本次 AFK',
           R._extract_duration('玩家 Steve 结束 AFK，本次 AFK: 15分钟0秒', '本次 AFK', 'AFK'), 900)
    expect('_extract_duration 退出行 AFK',
           R._extract_duration('玩家 S 退出服务器 | 本次游玩: 2小时 | AFK: 15分钟 | 活跃: 1小时45分钟',
                               '本次 AFK', 'AFK'), 900)
    expect('_extract_duration 不误判',
           R._extract_duration('玩家 提到 AFK 但没时长', '本次 AFK', 'AFK'), None)
    expect('QQ 号转 int', as_int('12345'), 12345)
    expect('长消息分段', len(split_message('x' * 25, limit=10)), 3)
    expect('配置深合并', plugin.deep_merge({'a': {'b': 1, 'c': 2}}, {'a': {'b': 9}})['a'],
           {'b': 9, 'c': 2})

    # 用真实临时目录跑一遍“记录 -> 读回”
    directory = tempfile.mkdtemp(prefix='playtime-selftest-')
    recorder = plugin.PlaytimeRecorder(
        None, logger=plugin.SafeLogger(),
        data_dir=os.path.join(directory, 'config'),
        log_dir=os.path.join(directory, 'logs'))
    recorder.on_player_joined(None, 'Alice')
    recorder.on_player_left(None, 'Alice')
    with open(recorder.log_file, 'r', encoding='utf-8') as handle:
        lines = [line for line in handle.read().split('\n') if line.strip()]
    expect('记录器写出 2 行', len(lines), 2)
    expect('进入行可解析', R.parse_line(lines[0]).event, R.EVENT_JOIN)
    leave = R.parse_line(lines[1])
    expect('退出行可解析', leave.event, R.EVENT_LEAVE)
    expect('退出行有本次时长', leave.session_seconds is not None, True)
    expect('累计数据已保存', 'Alice' in R.load_totals(recorder.data_file), True)
    report = R.build_report(R.read_records(recorder.log_file), R.default_range(),
                            totals=R.load_totals(recorder.data_file))
    expect('报告含 Alice', '【Alice】' in report, True)

    # 回归：restore_from 只搬数据，不能覆盖播报回调，也不能丢掉新实例已有的累计
    hits = []
    recorder2 = plugin.PlaytimeRecorder(
        None, logger=plugin.SafeLogger(),
        data_dir=os.path.join(directory, 'config2'),
        log_dir=os.path.join(directory, 'logs2'),
        on_session_end=lambda player, session: hits.append(player))
    old_like = plugin.PlaytimeRecorder(
        None, logger=plugin.SafeLogger(),
        data_dir=os.path.join(directory, 'config3'),
        log_dir=os.path.join(directory, 'logs3'))
    old_like.total_playtime = {'Carried': 99.0}
    recorder2.restore_from(old_like)
    expect('restore_from 恢复旧实例数据', recorder2.total_playtime.get('Carried'), 99.0)
    expect('restore_from 保留回调', callable(recorder2.on_session_end), True)
    recorder2.on_player_joined(None, 'Bob')
    recorder2.on_player_left(None, 'Bob')
    expect('会话结束会调用回调', hits, ['Bob'])
    expect('restore_from 不丢新实例新增数据', 'Bob' in recorder2.total_playtime, True)

    print('\n----- 报告样例 -----')
    print(report)
    print('---------------------')
    return ok


def main():
    print('Python: {}'.format(sys.version.replace('\n', ' ')))
    # 让 from qqbridge.xxx import ... 在本进程里也能用
    pkg_parent = os.path.join(HERE, 'qqbridge')
    for candidate in (HERE, pkg_parent):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    results = []
    results.append(('语法检查', step_syntax()))
    results.append(('format 静态检查', step_format_strings()))
    results.append(('导入自检', step_import()))
    results.append(('关键逻辑快检', step_quick_checks()))

    section('数据层测试（tests/test_records.py）')
    ok, detail = run_script(os.path.join('tests', 'test_records.py'))
    results.append(('数据层测试 ' + detail, ok))

    section('WebSocket / OneBot 测试（tests/test_ws_client.py）')
    ok, detail = run_script(os.path.join('tests', 'test_ws_client.py'))
    results.append(('WebSocket 测试 ' + detail, ok))

    section('端到端测试（tests/test_plugin_e2e.py）')
    ok, detail = run_script(os.path.join('tests', 'test_plugin_e2e.py'))
    results.append(('端到端测试 ' + detail, ok))

    section('打包冒烟（build.py）')
    ok, detail = run_script('build.py')
    results.append(('打包 ' + detail, ok))

    section('汇总')
    failed = 0
    for name, passed in results:
        print('{} {}'.format('[PASS]' if passed else '[FAIL]', name))
        if not passed:
            failed += 1
    print('\n共 {} 项，失败 {} 项'.format(len(results), failed))
    return 1 if failed else 0


PLUGIN = None

if __name__ == '__main__':
    sys.exit(main())

"""端到端测试：假 NapCat + 假 MCDR，验证“记录 -> 日志 -> 查询 -> QQ 回复”整条链路。

运行： python tests/test_plugin_e2e.py

测试会在系统临时目录里伪造 MCDR 工作目录，然后：
  1. 用真实的 PlaytimeRecorder 记录器模拟玩家进出和 AFK（它会写日志与数据文件）
  2. 从日志反查记录解析器，确认解析结果与写入的数据一致
  3. 让 QQNotifier 连上假 NapCat，推群/私聊消息事件，断言机器人发出的内容
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                                  # 插件根目录
PLUGIN_DIR = ROOT
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fake_napcat import FakeNapCat  # noqa: E402

PASSED = []
FAILED = []
ORIGINAL_CWD = os.getcwd()


def _load_plugin_module():
    """把插件根目录作为包加载（等价于 MCDR 加载多文件插件的方式）。"""
    entry = os.path.join(PLUGIN_DIR, '__init__.py')
    if not os.path.isfile(entry):
        raise RuntimeError('找不到插件入口: {}'.format(entry))
    spec = importlib.util.spec_from_file_location(
        'playtime_recorder_under_test', entry,
        submodule_search_locations=[PLUGIN_DIR])
    module = importlib.util.module_from_spec(spec)
    sys.modules['playtime_recorder_under_test'] = module
    spec.loader.exec_module(module)
    return module


P = _load_plugin_module()


def check(name, condition, detail=''):
    if condition:
        PASSED.append(name)
        print('[PASS] {}'.format(name))
    else:
        FAILED.append('{} {}'.format(name, detail))
        print('[FAIL] {} {}'.format(name, detail))


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def wait_idle(server, idle=0.7, timeout=10.0):
    """等到假 NapCat 连续 idle 秒没有收到新调用，说明异步发送已排空。"""
    deadline = time.time() + timeout
    last_count = len(server.calls)
    last_change = time.time()
    while time.time() < deadline:
        time.sleep(0.05)
        if len(server.calls) != last_count:
            last_count = len(server.calls)
            last_change = time.time()
        elif time.time() - last_change >= idle:
            return True
    return False


def drain(server, quiet=1.0, timeout=15.0):
    """反复“清空 + 等安静”，直到一轮里完全没有新消息。

    发送是异步的，上一轮的尾巴可能在这一轮 reset 之后才落地；只 reset 一次
    会把残留消息当成这一轮的回复。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        server.reset_calls()
        start = len(server.calls)
        if not wait_idle(server, idle=quiet, timeout=min(quiet * 4, 4.0)):
            return False
        if len(server.calls) == start == 0:
            return True
    return False


class FakeLogger(object):
    def __init__(self):
        self.lines = []

    def _log(self, level, message):
        self.lines.append('[{}] {}'.format(level, message))

    def info(self, message):
        self._log('INFO', message)

    def warning(self, message):
        self._log('WARN', message)

    def error(self, message):
        self._log('ERROR', message)

    def debug(self, message):
        self._log('DEBUG', message)


class FakeServer(object):
    """假 MCDR ServerInterface。"""

    def __init__(self):
        self.logger = FakeLogger()
        self.commands = []
        self.told = []

    def tell(self, source, message):
        self.told.append(str(message))

    def register_command(self, node, *args, **kwargs):
        self.commands.append(node)
        return node

    def register_help_message(self, prefix, message=None):
        self.commands.append(('help', prefix))
        return ('help', prefix)


def build_group_event(text, group_id=123456, user_id=654321, self_id=10001, mention=True):
    raw = '[CQ:at,qq={}] {}'.format(self_id, text) if mention else text
    segments = []
    if mention:
        segments.append({'type': 'at', 'data': {'qq': self_id}})
    segments.append({'type': 'text', 'data': {'text': ' ' + text}})
    return {
        'time': int(time.time()), 'post_type': 'message', 'message_type': 'group',
        'sub_type': 'normal', 'message_id': 1, 'group_id': group_id, 'user_id': user_id,
        'self_id': self_id, 'raw_message': raw, 'message': segments,
        'sender': {'user_id': user_id, 'nickname': '群友', 'role': 'member'},
    }


def build_private_event(text, user_id=654321, self_id=10001):
    return {
        'time': int(time.time()), 'post_type': 'message', 'message_type': 'private',
        'sub_type': 'friend', 'message_id': 2, 'user_id': user_id, 'self_id': self_id,
        'raw_message': text,
        'message': [{'type': 'text', 'data': {'text': text}}],
        'sender': {'user_id': user_id, 'nickname': '好友'},
    }


def make_config(server_url, **overrides):
    config = P.deep_merge(P.DEFAULT_CONFIG, {
        'connection': {'ws_url': server_url, 'reconnect_min_seconds': 0.3,
                       'reconnect_max_seconds': 1.0},
        'access': {'group_whitelist': [], 'private_whitelist': [], 'group_require_at': True},
        'notify': {'send_online_notice': False, 'on_join': False, 'on_leave': False},
    })
    for key, value in overrides.items():
        config = P.deep_merge(config, {key: value})
    return config


def timestamp(days_ago, hour, minute):
    day = (datetime.now() - timedelta(days=days_ago)).date()
    return datetime(day.year, day.month, day.day, hour, minute, 0)


def write_history_log(log_file):
    """造一段历史日志：3 天前的 Old、昨天的 Steve、今天的 Alex。"""
    lines = [
        '{} 玩家 Old 进入服务器 (时间: {})',
        '{} 玩家 Old 退出服务器 | 本次游玩: 30分钟 | AFK: 0秒 | 活跃: 30分钟 | 累计游玩: 30分钟 | 累计AFK: 0秒',
        '{} 玩家 Steve 进入服务器 (时间: {})',
        '{} 玩家 Steve 开始 AFK',
        '{} 玩家 Steve 结束 AFK，本次 AFK: 15分钟',
        '{} 玩家 Steve 退出服务器 | 本次游玩: 2小时0分钟0秒 | AFK: 15分钟 | 活跃: 1小时45分钟 | 累计游玩: 3小时 | 累计AFK: 15分钟',
        '{} 玩家 Alex 进入服务器 (时间: {})',
        '{} 玩家 Alex 退出服务器 | 本次游玩: 1小时 | AFK: 0秒 | 活跃: 1小时 | 累计游玩: 1小时 | 累计AFK: 0秒',
    ]
    stamps = [
        timestamp(3, 9, 0), timestamp(3, 9, 30),
        timestamp(1, 0, 30), timestamp(1, 1, 0), timestamp(1, 1, 15), timestamp(1, 2, 30),
        timestamp(0, 8, 0), timestamp(0, 9, 0),
    ]
    with open(log_file, 'w', encoding='utf-8') as handle:
        for template, stamp in zip(lines, stamps):
            text = stamp.strftime('%Y-%m-%d %H:%M:%S')
            handle.write('[' + text + '] ' + template.format(text) + '\n')


def main():
    workdir = tempfile.mkdtemp(prefix='playtime-recorder-e2e-')
    os.chdir(workdir)
    server = FakeNapCat().start()
    notifier = None
    try:
        mcdr = FakeServer()
        logger = P.SafeLogger(mcdr.logger)

        # ---------- 0. 记录器：写入日志与数据 ----------
        data_dir = os.path.join(workdir, 'config', 'playtime_recorder')
        log_dir = os.path.join(workdir, 'logs', 'playtime_recorder')
        recorder = P.PlaytimeRecorder(mcdr, logger=logger, data_dir=data_dir, log_dir=log_dir)
        P.recorder = recorder

        write_history_log(recorder.log_file)
        check('记录器创建了日志文件', os.path.isfile(recorder.log_file))
        check('记录器创建了数据目录', os.path.isdir(data_dir))

        # 真实记录一次进出，验证写出的格式能被自己的解析器读回
        recorder.on_player_joined(mcdr, 'TestPlayer')
        line = open(recorder.log_file, 'r', encoding='utf-8').read().strip().split('\n')[-1]
        parsed = P.record_lib.parse_line(line)
        check('写入的“进入”行能被解析', parsed is not None and parsed.event == P.record_lib.EVENT_JOIN
              and parsed.player == 'TestPlayer', 'got {!r} / {!r}'.format(line, parsed))

        time.sleep(0.1)
        recorder.on_player_left(mcdr, 'TestPlayer')
        line = open(recorder.log_file, 'r', encoding='utf-8').read().strip().split('\n')[-1]
        parsed = P.record_lib.parse_line(line)
        check('写入的“退出”行能被解析', parsed is not None
              and parsed.event == P.record_lib.EVENT_LEAVE
              and parsed.session_seconds is not None,
              'got {!r} / {!r}'.format(line, parsed))
        check('退出后累计数据已保存',
              os.path.isfile(recorder.data_file)
              and 'TestPlayer' in P.record_lib.load_totals(recorder.data_file))

        # 历史日志 8 条 + 刚才真实记录的进入/退出 2 条 = 10 条
        all_records = P.record_lib.read_records(recorder.log_file)
        check('解析历史日志（含真实写入）', len(all_records) == 10,
              'got {}'.format(len(all_records)))

        # ---------- 1. 通知器：连上假 NapCat ----------
        config = make_config(server.url)
        notifier = P.QQNotifier(mcdr, config, logger)
        P.notifier = notifier
        notifier.start()
        check('QQ 连接建立', wait_for(lambda: notifier.client.connected))

        # ---------- 2. 群内 @机器人 + #游玩历史 ----------
        drain(server)
        server.send_event(build_group_event('#游玩历史'))
        calls = server.wait_calls('send_group_msg', 1)
        check('群聊 #游玩历史 有回复', len(calls) >= 1, 'got {}'.format(server.calls))
        text = calls[0].get('message', '') if calls else ''
        check('回复发到了正确的群', calls and calls[0].get('group_id') == 123456)
        check('回复包含标题', '===== 游玩历史 =====' in text)
        check('回复包含“昨天 00:00 到现在”的范围', '00:00 至' in text, 'got {}'.format(text[:200]))
        check('回复包含昨天的 Steve 记录', '【Steve】' in text and '本次 2小时' in text,
              'got:\n{}'.format(text))
        check('回复包含 AFK 时长', '结束 AFK | 时长 15分钟' in text, 'got:\n{}'.format(text))
        check('回复不包含 3 天前的 Old 记录', '【Old】' not in text)
        check('回复包含统计', '----- 统计 -----' in text and '合计' in text)
        check('回复包含未上线玩家历史累计',
              '未上线玩家的历史累计' in text and 'Old: 30分钟' in text, 'got:\n{}'.format(text))
        print('\n----- QQ 实际会看到的内容 -----')
        print(text)
        print('------------------------------')
        drain(server)

        # ---------- 3. 没 @机器人 ----------
        server.send_event(build_group_event('#游玩历史', mention=False))
        wait_idle(server, idle=0.8)
        check('群里未 @机器人 时不回复',
              len([c for c in server.calls if c[0] == 'send_group_msg']) == 0,
              'got {}'.format(server.calls))

        # ---------- 4. 私聊 ----------
        drain(server)
        server.send_event(build_private_event('#游玩历史 今天'))
        calls = server.wait_calls('send_private_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('私聊 #游玩历史 有回复', len(calls) >= 1, 'got {}'.format(server.calls))
        check('私聊回复发给了正确的 QQ',
              calls and calls[0].get('user_id') == 654321, 'got {}'.format(calls[:1]))
        check('私聊“今天”只含今天记录', '【Alex】' in text and '【Steve】' not in text,
              'got {}'.format(text[:300]))

        # ---------- 5. 指定日期 ----------
        drain(server)
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        server.send_event(build_private_event('#游玩历史 ' + yesterday))
        calls = server.wait_calls('send_private_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('指定昨天日期返回 Steve 记录', '【Steve】' in text and '【Alex】' not in text,
              'got {}'.format(text[:300]))

        # ---------- 6. 上限参数 ----------
        drain(server)
        server.send_event(build_private_event('#游玩历史 上限2'))
        calls = server.wait_calls('send_private_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('上限2 触发截断提示', '只显示最后 2 条' in text, 'got {}'.format(text[-200:]))

        # ---------- 7. 累计排行 #游玩统计 ----------
        drain(server)
        server.send_event(build_private_event('#游玩统计'))
        calls = server.wait_calls('send_private_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('排行包含真实记录的玩家', 'TestPlayer' in text and '累计游玩排行' in text,
              'got {}'.format(text[:300]))

        drain(server)
        server.send_event(build_private_event('#游玩统计 Steve'))
        calls = server.wait_calls('send_private_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('按玩家名过滤排行', 'Steve' in text and 'Alex' not in text,
              'got {}'.format(text[:300]))

        # ---------- 8. 非法参数 / 帮助 / 状态 ----------
        drain(server)
        server.send_event(build_private_event('#游玩历史 乱写'))
        calls = server.wait_calls('send_private_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('非法参数给出用法提示', '参数错误' in text and '用法示例' in text,
              'got {}'.format(text[:200]))

        drain(server)
        server.send_event(build_private_event('#游玩帮助'))
        calls = server.wait_calls('send_private_msg', 1)
        check('帮助指令有回复', calls and '游玩记录插件帮助' in calls[0].get('message', ''))

        drain(server)
        server.send_event(build_private_event('#游玩状态'))
        calls = server.wait_calls('send_private_msg', 1)
        status_text = calls[0].get('message', '') if calls else ''
        check('状态指令报告已连接', 'QQ 连接: 已连接' in status_text, 'got {}'.format(status_text))
        check('状态指令报告在线玩家', '在线玩家:' in status_text, 'got {}'.format(status_text))

        # ---------- 9. 白名单与开关 ----------
        drain(server)
        notifier.config = P.deep_merge(notifier.config, {'access': {'group_whitelist': [999999]}})
        server.send_event(build_group_event('#游玩历史', group_id=123456))
        wait_idle(server, idle=0.8)
        check('群白名单外不回复',
              len([c for c in server.calls if c[0] == 'send_group_msg']) == 0,
              'got {}'.format(server.calls))
        notifier.config = P.deep_merge(notifier.config, {'access': {'group_whitelist': []}})

        drain(server)
        notifier.config = P.deep_merge(notifier.config, {'access': {'private_whitelist': [111111]}})
        server.send_event(build_private_event('#游玩历史'))
        wait_idle(server, idle=0.8)
        check('私聊白名单外不回复',
              len([c for c in server.calls if c[0] == 'send_private_msg']) == 0,
              'got {}'.format(server.calls))
        notifier.config = P.deep_merge(notifier.config, {'access': {'private_whitelist': []}})

        # ---------- 10. 主动播报（on_leave） ----------
        drain(server)
        notifier.config = P.deep_merge(notifier.config, {
            'notify': {'on_leave': True, 'targets': [123456], 'session_min_seconds': 0}})
        recorder.on_player_joined(mcdr, 'BroadcastGuy')
        time.sleep(0.05)
        recorder.on_player_left(mcdr, 'BroadcastGuy')
        calls = server.wait_calls('send_group_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('玩家退出时主动播报到群', 'BroadcastGuy' in text and '离开了服务器' in text,
              'got {}'.format(text))
        notifier.config = P.deep_merge(notifier.config, {'notify': {'on_leave': False}})

        # ---------- 11. 长回复分段 ----------
        drain(server)
        notifier.config = P.deep_merge(notifier.config, {'message': {'chunk_size': 120}})
        notifier.client.message_limit = 120
        server.send_event(build_private_event('#游玩历史'))
        chunks = server.wait_calls('send_private_msg', 2, timeout=8.0)
        check('超长回复自动分段', len(chunks) >= 2, 'got {}'.format(len(chunks)))
        check('分段带序号', any('(1/' in c.get('message', '') for c in chunks),
              'got {}'.format([c.get('message', '')[:20] for c in chunks]))
        notifier.config = P.deep_merge(notifier.config, {'message': {'chunk_size': 1200}})
        notifier.client.message_limit = 1200

        # ---------- 12. 日志缺失 ----------
        drain(server)
        missing_dir = tempfile.mkdtemp(prefix='playtime-recorder-missing-')
        original_log = recorder.log_file
        original_data = recorder.data_file
        recorder.log_file = os.path.join(missing_dir, 'playtime.log')
        recorder.data_file = os.path.join(missing_dir, 'playtime_data.json')
        notifier._cmd_history('', None, 654321)
        calls = server.wait_calls('send_private_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('日志缺失时给出明确提示',
              '没有找到该时间段内的游玩记录' in text and '未找到日志文件' in text,
              'got:\n{}'.format(text))
        recorder.log_file = original_log
        recorder.data_file = original_data
        shutil.rmtree(missing_dir, ignore_errors=True)

        # ---------- 13. 配置读写与旧路径兼容 ----------
        saved_cwd = os.getcwd()
        try:
            os.chdir(workdir)
            cfg = P.load_config(logger)
            check('生成了配置文件',
                  os.path.isfile(os.path.join('config', P.PLUGIN_ID, 'config.json')))
            check('配置含合并后的默认项',
                  'connection' in cfg and 'recorder' in cfg and 'notify' in cfg)
            check('元数据 id 与配置目录一致', P.PLUGIN_ID == 'playtime_recorder')
        finally:
            os.chdir(saved_cwd)

        check('注册了 MCDR 管理命令', P._register_commands(mcdr) is not None
              or len(mcdr.commands) >= 1)

        # ---------- 14. 停机 ----------
        notifier.stop()
        check('停机后连接关闭', wait_for(lambda: notifier.client.connected is False, timeout=5.0))
        notifier = None
    finally:
        if notifier is not None:
            try:
                notifier.stop()
            except Exception:
                pass
        server.stop()
        os.chdir(ORIGINAL_CWD)

    print('\n通过 {} 项，失败 {} 项'.format(len(PASSED), len(FAILED)))
    if FAILED:
        print('失败列表:')
        for item in FAILED:
            print('  - {}'.format(item))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

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
ROOT = os.path.dirname(HERE)                                    # 仓库根
PLUGIN_PKG = os.path.join(ROOT, 'playtime_recorder')            # 插件包目录
PLUGIN_ENTRY = os.path.join(PLUGIN_PKG, '__init__.py')
for candidate in (ROOT, PLUGIN_PKG, os.path.join(PLUGIN_PKG, 'qqbridge'), HERE):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fake_napcat import FakeNapCat  # noqa: E402

PASSED = []
FAILED = []
ORIGINAL_CWD = os.getcwd()


def _load_plugin_module():
    """把插件包当包加载（等价于 MCDR 加载多文件插件的方式）。

    包名必须与 metadata 的 entrypoint 一致（playtime_recorder），
    否则包内的相对导入（from .qqbridge.recorder import ...）会解析不到。
    """
    if not os.path.isfile(PLUGIN_ENTRY):
        raise RuntimeError('找不到插件入口: {}'.format(PLUGIN_ENTRY))
    spec = importlib.util.spec_from_file_location(
        'playtime_recorder', PLUGIN_ENTRY,
        submodule_search_locations=[PLUGIN_PKG])
    module = importlib.util.module_from_spec(spec)
    sys.modules['playtime_recorder'] = module
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
    """造一段历史日志：3 天前的 Old、昨天的 Steve、今天的 Alex。

    print_times=True 的行有两个 {}（行首时间 + 括号里的时间），
    统一按两个参数 format，避免模板和参数个数对不上。
    """
    specs = [
        (3, 9, 0, '玩家 Old 进入服务器 (时间: {})', True),
        (3, 9, 30, '玩家 Old 退出服务器 | 本次游玩: 30分钟 | AFK: 0秒 | 活跃: 30分钟 | '
                   '累计游玩: 30分钟 | 累计AFK: 0秒', False),
        (1, 0, 30, '玩家 Steve 进入服务器 (时间: {})', True),
        (1, 1, 0, '玩家 Steve 开始 AFK', False),
        (1, 1, 15, '玩家 Steve 结束 AFK，本次 AFK: 15分钟', False),
        (1, 2, 30, '玩家 Steve 退出服务器 | 本次游玩: 2小时0分钟0秒 | AFK: 15分钟 | '
                   '活跃: 1小时45分钟 | 累计游玩: 3小时 | 累计AFK: 15分钟', False),
        (0, 8, 0, '玩家 Alex 进入服务器 (时间: {})', True),
        (0, 9, 0, '玩家 Alex 退出服务器 | 本次游玩: 1小时 | AFK: 0秒 | 活跃: 1小时 | '
                  '累计游玩: 1小时 | 累计AFK: 0秒', False),
    ]
    with open(log_file, 'w', encoding='utf-8') as handle:
        for days_ago, hour, minute, template, with_time in specs:
            text = timestamp(days_ago, hour, minute).strftime('%Y-%m-%d %H:%M:%S')
            body = template.format(text) if with_time else template
            handle.write('[{}] {}\n'.format(text, body))


def write_history_totals(data_file, extra_seconds=0.0):
    """造与 history 日志配套的累计数据。

    报告里「未上线玩家的历史累计」这一段用的是 playtime_data.json，
    不是日志；所以需要单独造一份，否则 Old（3 天前，已超出默认范围）
    不会出现在报告里。
    """
    payload = {
        'total_playtime': {
            'Old': 1800.0,
            'Two': 3600.0,
            'Steve': 10800.0,
            'Alex': 3600.0,
        },
        'total_afk': {
            'Steve': 900.0,
            'Two': 600.0,
        },
    }
    directory = os.path.dirname(data_file)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(data_file, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def main():
    workdir = tempfile.mkdtemp(prefix='playtime-recorder-e2e-')
    os.chdir(workdir)
    server = FakeNapCat().start()
    notifier = None
    try:
        mcdr = FakeServer()
        logger = P.SafeLogger(mcdr.logger)

        # ---------- 0. 先建通知器（recorder 的回调要绑在它上面） ----------
        config = make_config(server.url)
        notifier = P.QQNotifier(mcdr, config, logger)
        P.notifier = notifier

        # ---------- 1. 记录器：写入日志与数据 ----------
        data_dir = os.path.join(workdir, 'config', 'playtime_recorder')
        log_dir = os.path.join(workdir, 'logs', 'playtime_recorder')
        recorder = P.PlaytimeRecorder(
            mcdr, logger=logger, data_dir=data_dir, log_dir=log_dir,
            on_session_end=notifier.notify_session_end,
            on_afk_change=notifier.notify_afk_change)
        P.recorder = recorder
        # 回归：回调必须在构造时就绑好（之前测试漏传，导致后面所有播报都失效）
        check('构造时已绑定播报回调',
              callable(recorder.on_session_end) and callable(recorder.on_afk_change),
              'on_session_end={!r} on_afk_change={!r}'.format(
                  recorder.on_session_end, recorder.on_afk_change))

        write_history_log(recorder.log_file)
        check('记录器创建了日志文件', os.path.isfile(recorder.log_file))
        check('记录器创建了数据目录', os.path.isdir(data_dir))

        # 真实记录一次进出，验证写出的格式能被自己的解析器读回
        recorder.on_player_joined(mcdr, 'TestPlayer')
        line = open(recorder.log_file, 'r', encoding='utf-8').read().strip().split('\n')[-1]
        parsed = P.record_lib.parse_line(line)
        check('写入的“进入”行能被解析', parsed is not None and parsed.event == P.record_lib.EVENT_JOIN
              and parsed.player == 'TestPlayer', 'got {!r} / {!r}'.format(line, parsed))

        time.sleep(1.2)     # 让这次会话有非零时长，避免“本次 0秒”掩盖真实问题
        recorder.on_player_left(mcdr, 'TestPlayer')
        line = open(recorder.log_file, 'r', encoding='utf-8').read().strip().split('\n')[-1]
        parsed = P.record_lib.parse_line(line)
        check('写入的“退出”行能被解析', parsed is not None
              and parsed.event == P.record_lib.EVENT_LEAVE
              and parsed.session_seconds is not None,
              'got {!r} / {!r}'.format(line, parsed))
        check('退出记录有非零时长', parsed is not None and parsed.session_seconds >= 1,
              'got {}'.format(parsed.session_seconds if parsed else None))
        check('退出后累计数据已保存',
              os.path.isfile(recorder.data_file)
              and 'TestPlayer' in P.record_lib.load_totals(recorder.data_file))

        # 累计数据要和日志配套（报告里“未上线玩家的历史累计”读的是它）
        write_history_totals(recorder.data_file)
        totals_now = P.record_lib.load_totals(recorder.data_file)
        check('累计数据可被读取', totals_now.get('Old') == 1800.0, 'got {}'.format(totals_now))

        # 日志里：历史 8 条 + 刚才真实记录的进入/退出 2 条 = 10 条
        all_records = P.record_lib.read_records(recorder.log_file)
        check('解析历史日志（含真实写入）', len(all_records) == 10,
              'got {}'.format(len(all_records)))

        # ---------- 2. 通知器连上假 NapCat ----------
        notifier.start()
        check('QQ 连接建立', wait_for(lambda: notifier.client.connected))

        # 回归：热重载恢复状态只搬数据，不能覆盖播报回调，也不能丢掉新实例已有的累计数据。
        notifier.config = P.deep_merge(notifier.config, {
            'notify': {'on_join': True, 'on_leave': True, 'on_afk': True,
                       'targets': [123456], 'session_min_seconds': 0}})
        recorder.restore_from(None)
        check('restore_from(None) 后回调仍绑定',
              callable(recorder.on_session_end), 'got {!r}'.format(recorder.on_session_end))

        test_player_seconds = recorder.total_playtime.get('TestPlayer')
        old_like = P.PlaytimeRecorder(mcdr, logger=logger,
                                      data_dir=data_dir, log_dir=log_dir)
        old_like.online_players = {'Carried': {'join_time': time.time(),
                                               'afk_start_time': None,
                                               'total_afk_seconds': 0.0}}
        old_like.total_playtime = {'Carried': 123.0, 'TestPlayer': 1.0}
        recorder.restore_from(old_like)
        check('restore_from 恢复旧实例数据',
              'Carried' in recorder.online_players
              and recorder.total_playtime.get('Carried') == 123.0,
              'got {!r}'.format(recorder.snapshot()))
        check('restore_from 不丢失新实例已有累计',
              recorder.total_playtime.get('TestPlayer') == test_player_seconds,
              'got {!r} (期望 {!r})'.format(
                  recorder.total_playtime.get('TestPlayer'), test_player_seconds))
        check('restore_from 不覆盖回调',
              callable(recorder.on_session_end) and callable(recorder.on_afk_change),
              'on_session_end={!r} on_afk_change={!r}'.format(
                  recorder.on_session_end, recorder.on_afk_change))
        # 恢复默认，避免这一节的开关影响后面的断言
        notifier.config = P.deep_merge(notifier.config, {
            'notify': {'on_join': False, 'on_leave': False, 'on_afk': False, 'targets': []}})
        drain(server)

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
        # 先做一次同步调用，把“配置没生效”和“异步链路没通”分开诊断
        notifier.config = P.deep_merge(notifier.config, {
            'notify': {'on_leave': True, 'targets': [123456], 'session_min_seconds': 0}})
        check('播报目标群已配置', notifier._targets() == [123456],
              'got {}'.format(notifier._targets()))
        notifier.notify_session_end('SyncProbe', {
            'session_seconds': 60, 'session_text': '1分钟', 'afk_text': '0秒',
            'active_text': '1分钟', 'total_text': '1分钟'})
        calls = server.wait_calls('send_group_msg', 1)
        check('同步调用能播报（链路自检）', len(calls) >= 1,
              'got {}'.format(server.calls))

        # 再走真实的“玩家退出 -> recorder 回调 -> 播报”链路
        drain(server)
        # 用独立 list 记录每一次调用/异常，避免依赖具体变量名或复用别名
        spy_calls = []
        spy_errors = []
        original_callback = recorder.on_session_end

        def spy_callback(player, session):
            spy_calls.append(player)
            try:
                result = original_callback(player, session)
                spy_errors.append(('ok', None))
                return result
            except Exception as exc:
                spy_errors.append(('error', repr(exc)))
                raise

        recorder.on_session_end = spy_callback
        check('安装 spy 前回调仍可调用',
              callable(original_callback),
              'got {!r}（说明构造之后有代码把回调清掉了）'.format(original_callback))
        check('spy 已挂到 recorder 上',
              recorder.on_session_end is spy_callback and P.recorder is recorder,
              'recorder.on_session_end is spy={} P.recorder is recorder={}'.format(
                  recorder.on_session_end is spy_callback, P.recorder is recorder))
        recorder.on_player_joined(mcdr, 'BroadcastGuy')
        check('离开前在线列表含 BroadcastGuy',
              'BroadcastGuy' in recorder.online_players,
              'got {}'.format(sorted(recorder.online_players)))
        time.sleep(0.05)
        recorder.on_player_left(mcdr, 'BroadcastGuy')
        check('离开后在线列表已移除 BroadcastGuy',
              'BroadcastGuy' not in recorder.online_players,
              'got {}'.format(sorted(recorder.online_players)))
        calls = server.wait_calls('send_group_msg', 1)
        text = calls[0].get('message', '') if calls else ''
        check('recorder 会话结束回调被触发', spy_calls == ['BroadcastGuy'],
              'got spy_calls={} spy_errors={} logged_lines={} online={} cb={!r}'.format(
                  spy_calls, spy_errors, recorder.logged_lines,
                  sorted(recorder.online_players), recorder.on_session_end))
        check('会话结束回调没有抛异常', not [item for item in spy_errors if item[0] == 'error'],
              'got {}'.format(spy_errors))
        check('玩家退出时主动播报到群（按 notify.targets）',
              'BroadcastGuy' in text and '离开了服务器' in text,
              'got {!r} / calls={} / state={}'.format(
                  text, server.calls, notifier.debug_state()))

        # 如果上面失败，下面的探针能区分“异步提交没执行”还是“发送本身有问题”
        if not calls:
            wait_idle(server, idle=1.0)
            notifier._submit(notifier._broadcast, '[游玩] 探针消息')
            after_submit = server.wait_calls('send_group_msg', 1)
            check('直接提交 _broadcast 能发出（异步通道自检）',
                  len(after_submit) >= 1,
                  'calls={} state={}'.format(server.calls, notifier.debug_state()))

        # 会话时长低于 session_min_seconds 时不播报
        drain(server)
        notifier.config = P.deep_merge(notifier.config,
                                       {'notify': {'session_min_seconds': 99999}})
        recorder.on_player_joined(mcdr, 'QuietGuy')
        time.sleep(0.05)
        recorder.on_player_left(mcdr, 'QuietGuy')
        wait_idle(server, idle=0.8)
        check('未达 session_min_seconds 不播报',
              len([c for c in server.calls if c[0] == 'send_group_msg']) == 0,
              'got {}'.format(server.calls))
        # 恢复默认，避免播报串扰后面的用例
        notifier.config = P.deep_merge(notifier.config, {
            'notify': {'on_leave': False, 'session_min_seconds': 0, 'targets': []}})
        drain(server)

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
        # 关键：等所有分段都发完，否则尾巴会落到下一节，被当成“本轮回复”
        # （之前就因此读到上一轮的 '(3/6) ...'）
        wait_idle(server, idle=1.0)

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

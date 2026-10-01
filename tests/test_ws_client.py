"""WebSocket 客户端 + OneBot 客户端 的离线测试（对打假 NapCat）。

运行： python tests/test_ws_client.py
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)                              # 仓库根
PKG_DIR = os.path.join(PLUGIN_DIR, 'playtime_recorder')         # 插件包
for candidate in (PLUGIN_DIR, PKG_DIR, os.path.join(PKG_DIR, 'qqbridge'), HERE):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fake_napcat import FakeNapCat  # noqa: E402
from qqbridge.onebot import (  # noqa: E402
    OneBotClient, OneBotError, as_int, split_message,
)
from qqbridge.ws_client import WebSocketClient, WebSocketError  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=''):
    if condition:
        PASSED.append(name)
        print('[PASS] {}'.format(name))
    else:
        FAILED.append('{} {}'.format(name, detail))
        print('[FAIL] {} {}'.format(name, detail))


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _assert_split_invariants(original, limit, prefer='\n'):
    """分段结果必须：每段不超长、拼回去（去掉被吃掉的分隔符）与原文一致。"""
    chunks = split_message(original, limit=limit, prefer=prefer)
    if not chunks:
        return True, '空结果'
    for chunk in chunks:
        if len(chunk) > limit:
            return False, '存在超长段: {} > {}'.format(len(chunk), limit)
    joined = (prefer or '').join(chunks) if prefer else ''.join(chunks)
    if prefer:
        original_cmp = original.replace('\r\n', '\n').replace('\r', '\n').replace(prefer, '')
        joined_cmp = joined.replace(prefer, '')
    else:
        original_cmp = original
        joined_cmp = joined
    if joined_cmp != original_cmp:
        return False, '内容丢失:\n原={!r}\n拼={!r}'.format(original_cmp, joined_cmp)
    return True, ''


def test_split_message():
    check('split 空文本', split_message('') == [])
    check('split 短文本不分段', split_message('hello', limit=10) == ['hello'])
    one = split_message('a' * 10, limit=10)
    check('split 正好等于上限', one == ['a' * 10], 'got {}'.format(one))
    chunks = split_message('line1\nline2\nline3', limit=12)
    check('split 贪心装填（尽量放满）',
          chunks == ['line1\nline2', 'line3'], 'got {}'.format(chunks))
    hard = split_message('b' * 25, limit=10)
    check('split 无换行硬切', len(hard) == 3 and hard[0] == 'b' * 10, 'got {}'.format(hard))
    check('split 保留中文', split_message('中文' * 5, limit=4) == ['中文中文', '中文中文', '中文'],
          'got {}'.format(split_message('中文' * 5, limit=4)))
    check('split 单行超长', split_message('short\n' + 'x' * 30, limit=10)[0] == 'short',
          'got {}'.format(split_message('short\n' + 'x' * 30, limit=10)))

    samples = [
        'line1\nline2\nline3',
        'b' * 25,
        'a\n' + 'b' * 30,
        '\n\n\nabc',
        '中文' * 20,
        ('1234567890\n' * 12).strip(),
        'x' * 100,
    ]
    for sample in samples:
        for limit in (10, 12, 50, 1000):
            ok, detail = _assert_split_invariants(sample, limit)
            check('split 不变量 limit={} len={}'.format(limit, len(sample)), ok, detail)


def test_ws_basic():
    server = FakeNapCat().start()
    received = []
    opened = []
    closed = []

    client = WebSocketClient(
        server.url,
        on_message=lambda text: received.append(text),
        on_open=lambda: opened.append(True),
        on_close=lambda: closed.append(True),
        ping_interval=1.0,
        pong_timeout=30.0,
    )
    client.connect()
    check('ws 连接成功', client.connected)
    check('ws on_open 触发', wait_for(lambda: opened))

    server.send_text('hello-from-napcat')
    check('ws 收到文本帧', wait_for(lambda: received == ['hello-from-napcat']),
          'got {}'.format(received))

    big = server.send_large_text(200000)
    check('ws 收到超大帧', wait_for(lambda: len(received) > 1 and received[-1] == big,
                                    timeout=10.0))

    client.send_text('from-mcdr')
    check('ws 双向通信成功（服务端收到裸文本）',
          wait_for(lambda: 'from-mcdr' in server.texts), 'got {}'.format(server.texts))

    time.sleep(2.2)
    check('ws 保活后仍连接', client.connected)

    client.close()
    check('ws 关闭后 connected=False', not client.connected)
    check('ws on_close 触发', wait_for(lambda: closed))
    server.stop()


def test_ws_bad_token():
    server = FakeNapCat(expected_token='secret').start()
    client = WebSocketClient(server.url, headers={'Authorization': 'Bearer wrong'})
    failed = False
    try:
        client.connect()
    except WebSocketError as exc:
        failed = '403' in str(exc)
    check('ws 错误 token 被拒绝且提示清晰', failed)
    server.stop()


def test_onebot_basic():
    server = FakeNapCat(expected_token='secret').start()
    events = []
    states = []
    client = OneBotClient(
        server.url, access_token='secret', on_event=events.append,
        on_state_change=states.append, reconnect_min=0.5, reconnect_max=2.0,
    )
    client.start()
    check('onebot 连接建立', wait_for(lambda: client.connected))
    check('onebot 上报了连接状态', True in states, 'got {}'.format(states))

    event = {
        'time': int(time.time()), 'post_type': 'message', 'message_type': 'group',
        'group_id': 123456, 'user_id': 654321, 'self_id': 10001,
        'raw_message': '[CQ:at,qq=10001] #游玩历史',
        'message': [{'type': 'text', 'data': {'text': '#游玩历史'}}],
    }
    server.send_event(event)
    check('onebot 收到消息事件', wait_for(lambda: len(events) == 1), 'got {}'.format(events))
    check('onebot 事件内容正确',
          events and events[0].get('raw_message') == '[CQ:at,qq=10001] #游玩历史')

    server.send_event({'post_type': 'meta_event', 'meta_event_type': 'lifecycle',
                       'self_id': 10001})
    time.sleep(0.3)
    check('onebot 忽略 meta_event', len(events) == 1, 'got {}'.format(len(events)))

    data = client.call_action('get_login_info')
    check('onebot action 返回 data', data == {'user_id': 10001, 'nickname': 'TestBot'},
          'got {}'.format(data))

    client.send_group_msg(123456, 'hello group')
    params = server.wait_call('send_group_msg')
    check('onebot send_group_msg 参数', params is not None and params.get('group_id') == 123456
          and params.get('message') == 'hello group', 'got {}'.format(params))

    client.send_private_msg('654321', 'hi')
    params = server.wait_call('send_private_msg')
    check('onebot send_private_msg QQ 号转 int',
          params is not None and params.get('user_id') == 654321, 'got {}'.format(params))

    server.reset_calls()
    client.send_group_text(123456, 'c' * 25, chunk_limit=10)
    sent = server.wait_calls('send_group_msg', 3)
    check('onebot 分段发送 3 条', len(sent) == 3, 'got {}'.format(len(sent)))
    check('onebot 分段带序号', sent and '(1/3)' in sent[0]['message'], 'got {}'.format(sent[:1]))

    server.reset_calls()
    client.reply_event({'message_type': 'group', 'group_id': 999, 'user_id': 1}, 'g')
    client.reply_event({'message_type': 'private', 'user_id': 888}, 'p')
    group_params = server.wait_call('send_group_msg')
    private_params = server.wait_call('send_private_msg')
    check('onebot 群消息回群', group_params is not None and group_params.get('group_id') == 999)
    check('onebot 私聊消息回私聊',
          private_params is not None and private_params.get('user_id') == 888)

    client.close()
    check('onebot close 后状态', not client.connected)
    try:
        client.call_action('get_status')
        check('onebot 未连接应报错', False)
    except OneBotError:
        check('onebot 未连接应报错', True)
    server.stop()


def test_onebot_failure_response():
    server = FakeNapCat().start()
    client = OneBotClient(server.url, reconnect_min=0.5, action_timeout=2.0)
    client.start()
    check('onebot 连接（无 token）', wait_for(lambda: client.connected))

    def patched(payload):
        import json as _json
        message = _json.loads(payload.decode('utf-8'))
        server.calls.append((message.get('action'), message.get('params') or {}))
        server.send_text(_json.dumps({
            'status': 'failed', 'retcode': 100, 'data': None,
            'wording': '风控拦截', 'echo': message.get('echo')}, ensure_ascii=False))

    server._handle_text = patched
    try:
        client.call_action('send_group_msg', {'group_id': 1, 'message': 'x'})
        check('onebot retcode!=0 抛异常', False)
    except OneBotError as exc:
        check('onebot retcode!=0 抛异常', 'retcode=100' in str(exc) and '风控' in str(exc),
              'got {}'.format(exc))

    client.close()
    server.stop()


def test_as_int():
    check('as_int 字符串', as_int('12345') == 12345)
    check('as_int 数字', as_int(12345) == 12345)
    check('as_int 空', as_int(None) is None)
    check('as_int 非法', as_int('abc') is None)
    check('as_int 带空格', as_int(' 42 ') == 42)


def test_reconnect():
    server = FakeNapCat().start()
    client = OneBotClient(server.url, reconnect_min=0.3, reconnect_max=1.0)
    client.start()
    check('onebot 首次连接', wait_for(lambda: client.connected))

    server.stop()
    check('onebot 掉线被感知', wait_for(lambda: not client.connected, timeout=8.0))
    check('onebot 触发重连计数', wait_for(lambda: client.stats.get('reconnects', 0) >= 2,
                                    timeout=8.0),
          'got {}'.format(client.stats))
    stats = client.stats
    check('onebot 记录了失败原因', bool(stats.get('last_error')), 'got {}'.format(stats))
    check('onebot 重连时不影响其他状态', stats.get('connected') is False)
    client.close()
    check('onebot 停止后不再重连', wait_for(lambda: not client.running, timeout=5.0))


def main():
    print('== WebSocket / OneBot 测试 ==')
    test_split_message()
    test_as_int()
    test_ws_basic()
    test_ws_bad_token()
    test_onebot_basic()
    test_onebot_failure_response()
    test_reconnect()

    print('\n通过 {} 项，失败 {} 项'.format(len(PASSED), len(FAILED)))
    if FAILED:
        print('失败列表:')
        for item in FAILED:
            print('  - {}'.format(item))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

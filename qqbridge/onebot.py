"""OneBot v11（NapCat）客户端：反向 WebSocket 模式。

网络拓扑（Minecraft 侧没有公网 IP 时唯一可行的方向）：

    NapCat  = WebSocket 服务端（在公网机器上监听端口）
    MCDR    = WebSocket 客户端（主动连出去，不需要任何入站端口）

事件从 NapCat 推给 MCDR，MCDR 通过同一条连接发送 action 调用（send_group_msg 等），
用 echo 字段做请求-响应配对。
"""

import json
import queue
import threading
import time
from urllib.parse import quote

from .logging_util import SafeLogger
from .ws_client import WebSocketClient, WebSocketError

DEFAULT_UA = 'MCDR-PlaytimeRecorder/2.0 (OneBot v11)'


class OneBotError(Exception):
    """OneBot 协议层错误（含 connection refused / 超时）。"""


class ActionFailed(OneBotError):
    """NapCat 返回 retcode != 0。"""

    def __init__(self, action, retcode, message):
        self.action = action
        self.retcode = retcode
        self.message = message
        super(ActionFailed, self).__init__(
            'action {} 失败: retcode={} {}'.format(action, retcode, message)
        )


def as_int(value, default=None):
    """把 QQ 号 / 群号统一转成 int，失败时返回 default。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def split_message(text, limit=1200, prefer='\n'):
    """把长文本按 limit 拆成若干段，优先在 prefer（通常是换行）处断开。

    贪心算法：能整段放进 limit 就放，放不下才硬切。这样每一段都尽量长、
    且不会出现“切在换行后导致空段/丢内容”的边界问题。
    """
    text = str(text).replace('\r\n', '\n').replace('\r', '\n')
    if limit <= 0:
        return [text] if text else []
    if len(text) <= limit:
        return [text] if text else []

    if prefer:
        parts = text.split(prefer)
    else:
        parts = [text]

    chunks = []
    current = ''
    total = len(parts)
    for index, part in enumerate(parts):
        is_last = (index == total - 1)
        piece = (part + prefer) if (prefer and not is_last) else part
        if current and len(current) + len(piece) > limit:
            chunks.append(current.rstrip('\n'))
            current = ''
        while len(piece) > limit:
            if current:
                chunks.append(current.rstrip('\n'))
                current = ''
            chunks.append(piece[:limit])
            piece = piece[limit:]
        current += piece
    if current:
        chunks.append(current.rstrip('\n'))
    return [chunk for chunk in chunks if chunk != '']


class OneBotClient:
    """带自动重连的 OneBot v11 客户端。

    典型用法::

        client = OneBotClient('ws://host:12538', access_token='xxx',
                              on_event=my_handler, logger=logger)
        client.start()          # 后台线程，断线自动重连
        client.send_group_msg(123456, 'hello')
        client.close()
    """

    def __init__(self, url, access_token='', on_event=None, on_state_change=None,
                 logger=None, reconnect_min=3.0, reconnect_max=60.0,
                 action_timeout=15.0, verify_ssl=True, message_limit=1200,
                 message_queue_size=200, access_token_in_query=False,
                 name='onebot'):
        self.url = str(url)
        self.access_token = str(access_token or '')
        self.on_event = on_event
        self.on_state_change = on_state_change
        self.logger = logger if isinstance(logger, SafeLogger) else SafeLogger(logger)
        self.reconnect_min = max(1.0, float(reconnect_min))
        self.reconnect_max = max(self.reconnect_min, float(reconnect_max))
        self.action_timeout = float(action_timeout)
        self.verify_ssl = bool(verify_ssl)
        self.message_limit = int(message_limit)
        self.access_token_in_query = bool(access_token_in_query)
        self.name = name

        self._ws = None
        self._run_thread = None
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._connected = False
        self.self_id = None
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._echo_seq = 0
        self._send_queue = queue.Queue(maxsize=max(1, int(message_queue_size)))
        self._worker = None
        self._stats = {'events': 0, 'actions': 0, 'failed': 0, 'dropped': 0,
                       'reconnects': 0, 'last_error': None}

    # ------------------------------------------------------------------ 状态

    @property
    def connected(self):
        with self._state_lock:
            return self._connected

    @property
    def stats(self):
        snapshot = dict(self._stats)
        snapshot['connected'] = self.connected
        snapshot['self_id'] = self.self_id
        return snapshot

    # ------------------------------------------------------------ 生命周期

    def start(self):
        if self._run_thread is not None and self._run_thread.is_alive():
            return
        self._stop.clear()
        self._run_thread = threading.Thread(
            target=self._supervise, name='qqbridge-{}-supervisor'.format(self.name))
        self._run_thread.daemon = True
        self._run_thread.start()

    def close(self, wait=2.0):
        self._stop.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.close(wait=1.0)
        worker = self._worker
        if worker is not None and worker.is_alive():
            try:
                self._send_queue.put_nowait(None)
            except queue.Full:
                pass
        thread = self._run_thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=wait)
        self._set_connected(False)

    @property
    def running(self):
        thread = self._run_thread
        return thread is not None and thread.is_alive()

    # -------------------------------------------------------- 连接与重连循环

    def _supervise(self):
        backoff = self.reconnect_min
        first = True
        while not self._stop.is_set():
            long_lived = False
            try:
                long_lived = self._connect_once()
                first = False
            except WebSocketError as exc:
                self._stats['last_error'] = str(exc)
                self.logger.warning('连接失败: {}', exc)
            except Exception as exc:  # 兜底，绝不让监督线程死掉
                self._stats['last_error'] = repr(exc)
                self.logger.error('连接循环异常: {!r}', exc)

            self._set_connected(False)
            if self._stop.is_set():
                break
            if first:
                self.logger.warning(
                    '将在 {:.0f} 秒后重连；请确认 NapCat 侧已开启 WebSocket 服务端，'
                    '地址与端口为 {}', self.reconnect_min, self.url)
            next_backoff = self.reconnect_min if long_lived else backoff
            self._stats['reconnects'] += 1
            if self._stop.wait(next_backoff):
                break
            backoff = min(self.reconnect_max, max(self.reconnect_min, next_backoff * 2))

    def _connect_once(self):
        """连接一次并阻塞到断开为止；返回 True 表示这次连接存活超过 30 秒。"""
        headers = {'User-Agent': DEFAULT_UA}
        url = self.url
        if self.access_token and self.access_token_in_query:
            separator = '&' if '?' in self.url else '?'
            url = '{}{}access_token={}'.format(self.url, separator, quote(self.access_token))
        elif self.access_token:
            headers['Authorization'] = 'Bearer ' + self.access_token

        ws = WebSocketClient(
            url,
            headers=headers,
            logger=self.logger,
            on_message=self._on_raw_message,
            verify_ssl=self.verify_ssl,
            name=self.name,
        )
        self._ws = ws
        ws.connect()  # 失败抛 WebSocketError，由 _supervise 重试
        self._set_connected(True)
        self._start_worker()

        # 阻塞在这里直到连接断开
        while not self._stop.is_set():
            thread = getattr(ws, '_thread', None)
            if thread is None or not thread.is_alive():
                break
            if self._stop.wait(0.5):
                break
        ws.close(wait=1.0)
        if self._ws is ws:
            self._ws = None
        self._set_connected(False)
        return ws.connected_at is not None and (time.time() - ws.connected_at) > 30.0

    def _start_worker(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._send_worker, name='qqbridge-{}-sender'.format(self.name))
        self._worker.daemon = True
        self._worker.start()

    def _set_connected(self, value):
        changed = False
        with self._state_lock:
            if self._connected != value:
                self._connected = value
                changed = True
        if changed:
            if value:
                self.logger.info('OneBot 连接已建立，开始接收 QQ 消息')
            else:
                self.logger.warning('OneBot 连接已断开')
            if self.on_state_change is not None:
                try:
                    self.on_state_change(value)
                except Exception as exc:
                    self.logger.error('状态回调异常: {}', exc)

    # ------------------------------------------------------------ 事件分发

    def _on_raw_message(self, raw):
        try:
            payload = json.loads(raw)
        except ValueError:
            self.logger.warning('收到非 JSON 数据: {!r}', raw[:200])
            return
        if not isinstance(payload, dict):
            self.logger.warning('收到非对象 JSON: {!r}', raw[:200])
            return

        echo = payload.get('echo')
        if echo is not None:
            self._resolve_pending(echo, payload)
            return

        post_type = payload.get('post_type')
        if post_type == 'meta_event':
            if payload.get('meta_event_type') == 'lifecycle':
                self.self_id = as_int(payload.get('self_id'), self.self_id)
                self.logger.info('NapCat 机器人已就绪: self_id={}', self.self_id)
            return

        self._stats['events'] += 1
        try:
            self._handle_event(payload)
        except Exception as exc:
            self.logger.error('处理事件异常: {!r}', exc)

    def _handle_event(self, event):
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as exc:
            self.logger.error('事件回调异常: {!r}', exc)

    def _resolve_pending(self, echo, payload):
        key = str(echo)
        with self._pending_lock:
            pending = self._pending.pop(key, None)
        if pending is None:
            self.logger.debug('收到未知 echo 的响应: {}', key)
            return
        event, holder = pending
        holder['response'] = payload
        event.set()

    # -------------------------------------------------------------- action

    def call_action(self, action, params=None, timeout=None):
        """同步调用一个 OneBot API，返回响应里的 data 字段。失败抛 OneBotError。"""
        ws = self._ws
        if ws is None or not self.connected:
            raise OneBotError('尚未连接到 NapCat（{}）'.format(self.url))

        self._echo_seq += 1
        echo = '{}-{}'.format(int(time.time() * 1000), self._echo_seq)
        event = threading.Event()
        holder = {}
        with self._pending_lock:
            self._pending[str(echo)] = (event, holder)

        request = {'action': action, 'params': params or {}, 'echo': echo}
        try:
            ws.send_text(json.dumps(request, ensure_ascii=False))
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(str(echo), None)
            raise OneBotError('发送 {} 失败: {}'.format(action, exc))
        self._stats['actions'] += 1

        if not event.wait(timeout if timeout is not None else self.action_timeout):
            with self._pending_lock:
                self._pending.pop(str(echo), None)
            raise OneBotError('调用 {} 超时（{:.0f} 秒无响应）'.format(
                action, timeout if timeout is not None else self.action_timeout))

        response = holder.get('response') or {}
        retcode = as_int(response.get('retcode'), -1)
        if retcode != 0:
            self._stats['failed'] += 1
            message = response.get('message') or response.get('wording') or ''
            data = response.get('data')
            if not message and isinstance(data, dict):
                message = data.get('message') or data.get('wording') or ''
            raise ActionFailed(action, retcode, str(message))
        return response.get('data')

    # ------------------------------------------------------------ 发送接口

    def _enqueue_send(self, action, params):
        if self._stop.is_set():
            return False
        item = (action, params)
        try:
            self._send_queue.put_nowait(item)
            return True
        except queue.Full:
            try:
                self._send_queue.get_nowait()  # 丢掉最旧的一条，避免雪崩
                self._stats['dropped'] += 1
            except queue.Empty:
                pass
            try:
                self._send_queue.put_nowait(item)
                return True
            except queue.Full:
                self._stats['dropped'] += 1
                return False

    def _send_worker(self):
        while not self._stop.is_set():
            try:
                item = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            if not self.connected:
                continue
            action, params = item
            try:
                self.call_action(action, params)
            except OneBotError as exc:
                self.logger.warning('异步发送 {} 失败: {}', action, exc)

    def send_group_msg(self, group_id, message, auto_escape=False):
        return self._enqueue_send('send_group_msg', {
            'group_id': as_int(group_id, group_id),
            'message': message,
            'auto_escape': bool(auto_escape),
        })

    def send_private_msg(self, user_id, message, auto_escape=False):
        return self._enqueue_send('send_private_msg', {
            'user_id': as_int(user_id, user_id),
            'message': message,
            'auto_escape': bool(auto_escape),
        })

    def send_group_text(self, group_id, text, chunk_limit=None, start_index=1):
        """按长度分段发送纯文本，返回实际发送的段数。"""
        return self._send_chunks('group', group_id, text, chunk_limit, start_index)

    def send_private_text(self, user_id, text, chunk_limit=None, start_index=1):
        return self._send_chunks('private', user_id, text, chunk_limit, start_index)

    def _send_chunks(self, kind, target_id, text, chunk_limit, start_index):
        limit = int(chunk_limit or self.message_limit or 1200)
        chunks = split_message(text, limit=limit)
        if not chunks:
            return 0
        for offset, chunk in enumerate(chunks):
            body = chunk
            if len(chunks) > 1:
                body = '({}/{}) {}'.format(start_index + offset, len(chunks) - 1 + start_index, chunk)
            if kind == 'group':
                self.send_group_msg(target_id, body)
            else:
                self.send_private_msg(target_id, body)
        return len(chunks)

    def reply_event(self, event, text, chunk_limit=None):
        """根据事件来源（群/私聊）自动选择回复目标并分段发送。"""
        message_type = event.get('message_type')
        group_id = as_int(event.get('group_id'))
        user_id = as_int(event.get('user_id'))
        if message_type == 'group' and group_id is not None:
            return self.send_group_text(group_id, text, chunk_limit)
        if user_id is not None:
            return self.send_private_text(user_id, text, chunk_limit)
        self.logger.warning('无法确定回复目标: message_type={}', message_type)
        return 0

    # ------------------------------------------------------------ 便捷接口

    def get_login_info(self):
        return self.call_action('get_login_info')

    def get_status(self):
        return self.call_action('get_status')

    def get_group_member_info(self, group_id, user_id, no_cache=False):
        return self.call_action('get_group_member_info', {
            'group_id': as_int(group_id, group_id),
            'user_id': as_int(user_id, user_id),
            'no_cache': bool(no_cache),
        })

    def set_group_card(self, group_id, user_id, card=''):
        return self.call_action('set_group_card', {
            'group_id': as_int(group_id, group_id),
            'user_id': as_int(user_id, user_id),
            'card': str(card),
        })

    def reset_stats(self):
        """清掉统计（不影响连接状态）。"""
        for key in ('events', 'actions', 'failed', 'dropped', 'reconnects', 'last_error'):
            if key == 'last_error':
                self._stats[key] = None
            else:
                self._stats[key] = 0

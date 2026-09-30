"""零依赖的 WebSocket 客户端（RFC 6455），只实现客户端方向。

只使用标准库，避免 MCDR 环境里没装 websockets / websocket-client 的问题。

支持：
    * ws:// 与 wss://（wss 走 ssl 默认证书校验，可用 verify_ssl=False 关闭）
    * 握手 Header 自定义（用于 OneBot 的 Authorization 鉴权）
    * 文本帧收发的掩码、扩展长度（126/127）
    * 分片帧重组
    * 控制帧：ping 自动回 pong、pong 保活、close 正确握手
    * 连接/断开回调与自动重连（由上层控制循环调用 connect()）
"""

import base64
import hashlib
import os
import select
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlparse

from .logging_util import SafeLogger

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_STOP = object()


class WebSocketError(Exception):
    """WebSocket 连接或协议层面的错误。"""


def _make_mask(size):
    return os.urandom(size)


def _xor(payload, mask):
    if not payload:
        return b''
    if not mask:
        return payload
    out = bytearray(payload)
    length = len(out)
    key = mask
    for i in range(length):
        out[i] ^= key[i & 3]
    return bytes(out)


class WebSocketClient:
    """单连接 WebSocket 客户端。

    收发模型：
        * send_text() / send_binary() 只把数据塞进队列，由内部连接线程真正写出，
          因此可以在任意线程里调用，不会互相阻塞。
        * on_message(text: str) 在内部连接线程里被调用，回调里不要做耗时操作。
    """

    def __init__(self, url, headers=None, logger=None, on_message=None,
                 on_open=None, on_close=None, verify_ssl=True,
                 connect_timeout=10.0, read_timeout=1.0,
                 ping_interval=45.0, pong_timeout=180.0,
                 name='ws'):
        self.url = str(url)
        self.headers = dict(headers or {})
        self.logger = logger if isinstance(logger, SafeLogger) else SafeLogger(logger)
        self.on_message = on_message
        self.on_open = on_open
        self.on_close = on_close
        self.verify_ssl = bool(verify_ssl)
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.ping_interval = float(ping_interval)
        self.pong_timeout = float(pong_timeout)
        self.name = name

        self._queue = []
        self._qlock = threading.Lock()
        self._sock = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._connected = threading.Event()
        self._thread = None

        self._frag_op = None
        self._frag_payload = bytearray()
        self._closing_sent = False
        self._frag_buf = bytearray()

        self.last_error = None
        self.connected_at = None

    # ------------------------------------------------------------------ 状态

    @property
    def connected(self):
        return self._connected.is_set()

    # ------------------------------------------------------------ 生命周期

    def connect(self):
        """建立连接并启动内部收发线程。成功返回 True，失败抛 WebSocketError。"""
        if self._thread is not None and self._thread.is_alive():
            raise WebSocketError('连接线程已在运行')

        parsed = urlparse(self.url)
        scheme = (parsed.scheme or '').lower()
        if scheme not in ('ws', 'wss'):
            raise WebSocketError('不支持的 URL 协议: {!r}（应以 ws:// 或 wss:// 开头）'.format(self.url))
        host = parsed.hostname
        if not host:
            raise WebSocketError('URL 中缺少主机名: {!r}'.format(self.url))
        port = parsed.port or (443 if scheme == 'wss' else 80)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query

        try:
            raw = socket.create_connection((host, port), timeout=self.connect_timeout)
        except OSError as exc:
            raise WebSocketError('连接 {}:{} 失败: {}'.format(host, port, exc))

        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        sock = raw
        try:
            if scheme == 'wss':
                try:
                    context = ssl.create_default_context()
                    if not self.verify_ssl:
                        context.check_hostname = False
                        context.verify_mode = ssl.CERT_NONE
                    sock = context.wrap_socket(raw, server_hostname=host)
                except Exception as exc:
                    raise WebSocketError('TLS 握手失败: {}'.format(exc))

            key = base64.b64encode(_make_mask(16)).decode('ascii')
            request = self._build_handshake(host, port, path, key)
            sock.sendall(request.encode('utf-8'))

            status, resp_headers = self._read_handshake(sock)
            if status != 101:
                body_hint = ''
                if status in (401, 403):
                    body_hint = '（通常是 access_token 不正确，或 NapCat 侧未放行该 token）'
                raise WebSocketError('WebSocket 握手被拒绝: HTTP {}{}'.format(status, body_hint))

            expect = base64.b64encode(
                hashlib.sha1((key + GUID).encode('utf-8')).digest()
            ).decode('ascii')
            accept = ''
            for name, value in resp_headers:
                if name.lower() == 'sec-websocket-accept':
                    accept = value.strip()
                    break
            if accept and accept != expect:
                raise WebSocketError('Sec-WebSocket-Accept 校验失败，对端可能不是 WebSocket 服务端')
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise

        try:
            sock.settimeout(self.read_timeout)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

        self._sock = sock
        self._closed.clear()
        self._connected.set()
        self.connected_at = time.time()
        self._frag_op = None
        self._frag_payload = bytearray()
        self._frag_buf = bytearray()
        self._closing_sent = False

        self._thread = threading.Thread(target=self._run, name='qqbridge-' + self.name)
        self._thread.daemon = True
        self._thread.start()

        self.logger.info('WebSocket 已连接: {}', self.url)
        if self.on_open is not None:
            try:
                self.on_open()
            except Exception as exc:
                self.logger.error('on_open 回调异常: {}', exc)
        return True

    def close(self, wait=1.0):
        """通知连接线程退出并等待其结束。可重复调用。"""
        self._closed.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=wait)
        if self._connected.is_set():
            self._connected.clear()

    @property
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ---------------------------------------------------------------- 发送

    def send_text(self, text):
        self._enqueue((OP_TEXT, str(text).encode('utf-8')))

    def send_binary(self, data):
        self._enqueue((OP_BINARY, bytes(data)))

    def send_ping(self, payload=b''):
        if not self._connected.is_set():
            return
        try:
            self._send_frame(OP_PING, payload)
        except Exception as exc:
            self.logger.debug('发送 ping 失败: {}', exc)

    def _enqueue(self, item):
        if self._closed.is_set():
            return
        with self._qlock:
            self._queue.append(item)

    # ------------------------------------------------------------ 握手细节

    def _build_handshake(self, host, port, path, key):
        default_port = 443 if self.url.lower().startswith('wss') else 80
        host_header = host if port == default_port else '{}:{}'.format(host, port)
        lines = [
            'GET {} HTTP/1.1'.format(path),
            'Host: {}'.format(host_header),
            'Upgrade: websocket',
            'Connection: Upgrade',
            'Sec-WebSocket-Key: {}'.format(key),
            'Sec-WebSocket-Version: 13',
            'User-Agent: MCDR-PlaytimeRecorder/2.0',
        ]
        for name, value in self.headers.items():
            lines.append('{}: {}'.format(name, value))
        return '\r\n'.join(lines) + '\r\n\r\n'

    @staticmethod
    def _read_line(sock, limit=8192):
        buf = bytearray()
        while len(buf) < limit:
            try:
                chunk = sock.recv(1)
            except socket.timeout:
                raise WebSocketError('读取握手响应超时')
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b'\r\n'):
                return bytes(buf[:-2])
        if not buf:
            return b''
        return bytes(buf)

    def _read_handshake(self, sock):
        status_line = self._read_line(sock)
        if not status_line:
            raise WebSocketError('服务端在握手阶段直接关闭了连接')
        try:
            text = status_line.decode('latin-1').strip()
        except Exception:
            text = str(status_line)
        parts = text.split(' ', 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise WebSocketError('非法的 HTTP 响应: {!r}'.format(text[:120]))
        status = int(parts[1])

        headers = []
        for _ in range(100):
            line = self._read_line(sock)
            if not line:
                break
            decoded = line.decode('latin-1')
            if ':' not in decoded:
                continue
            name, _, value = decoded.partition(':')
            headers.append((name.strip(), value.strip()))
        return status, headers

    # ------------------------------------------------------------ 帧的收发

    def _send_frame(self, opcode, payload=b'', fin=True):
        payload = bytes(payload)
        first = (0x80 if fin else 0x00) | (opcode & 0x0F)
        length = len(payload)
        header = bytearray()
        header.append(first)
        mask = _make_mask(4)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack('!H', length)
        else:
            header.append(0x80 | 127)
            header += struct.pack('!Q', length)
        header += mask
        frame = bytes(header) + _xor(payload, mask)
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise WebSocketError('连接已关闭')
            sock.sendall(frame)

    # ------------------------------------------------------------ 连接线程

    def _run(self):
        try:
            self._loop()
        except Exception as exc:
            if not self._closed.is_set():
                self.last_error = exc
                self.logger.warning('WebSocket 连接结束: {}', exc)
        finally:
            was_connected = self._connected.is_set()
            self._connected.clear()
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._qlock:
                self._queue = []
            if was_connected and self.on_close is not None:
                try:
                    self.on_close()
                except Exception as exc:
                    self.logger.error('on_close 回调异常: {}', exc)

    def _loop(self):
        last_recv = time.time()
        last_ping = time.time()
        while not self._closed.is_set():
            # 1) 先把待发数据写出去
            self._flush_queue()

            sock = self._sock
            if sock is None:
                return

            # 2) 有数据可读时读取，否则最多阻塞 read_timeout 秒
            try:
                ready, _, _ = select.select([sock], [], [], self.read_timeout)
            except (OSError, ValueError) as exc:
                raise WebSocketError('select 失败: {}'.format(exc))

            if not ready:
                if self._check_keepalive(last_recv, last_ping):
                    last_ping = time.time()
                continue

            try:
                data = sock.recv(65536)
            except socket.timeout:
                if self._check_keepalive(last_recv, last_ping):
                    last_ping = time.time()
                continue
            except (ConnectionResetError, OSError) as exc:
                raise WebSocketError('接收失败: {}'.format(exc))

            if not data:
                raise WebSocketError('连接被对端关闭')

            self._frag_buf += data
            while True:
                frame = self._read_frame()
                if frame is None:
                    break
                fin, opcode, payload = frame
                last_recv = time.time()
                if not self._handle_frame(fin, opcode, payload):
                    return

    def _check_keepalive(self, last_recv, last_ping):
        """返回 True 表示本次发送了 ping。"""
        now = time.time()
        if self.pong_timeout > 0 and now - last_recv > self.pong_timeout:
            raise WebSocketError(
                '超过 {:.0f} 秒没有收到任何数据，判定连接已失效'.format(self.pong_timeout)
            )
        if self.ping_interval > 0 and now - last_ping >= self.ping_interval:
            self._send_frame(OP_PING, b'keepalive')
            return True
        return False

    def _handle_frame(self, fin, opcode, payload):
        """返回 False 表示连接线程应当结束。"""
        if opcode == OP_PING:
            self._send_frame(OP_PONG, payload)
            return True
        if opcode == OP_PONG:
            return True
        if opcode == OP_CLOSE:
            code = 1000
            if len(payload) >= 2:
                code = struct.unpack('!H', payload[:2])[0]
            self.logger.info('收到关闭帧 (code={})，主动断开', code)
            try:
                if not self._closing_sent:
                    self._closing_sent = True
                    self._send_frame(OP_CLOSE, payload[:2] if len(payload) >= 2 else b'\x03\xe8')
            except Exception:
                pass
            return False

        if opcode == OP_CONT:
            if self._frag_op is None:
                self.logger.debug('收到孤立的续帧，已忽略')
                return True
            self._frag_payload += payload
            if fin:
                data = bytes(self._frag_payload)
                message_op = self._frag_op
                self._frag_payload = bytearray()
                self._frag_op = None
                self._emit_message(message_op, data)
            return True

        if opcode in (OP_TEXT, OP_BINARY):
            if fin:
                self._emit_message(opcode, payload)
            else:
                self._frag_op = opcode
                self._frag_payload = bytearray(payload)
            return True

        self.logger.debug('忽略未知 opcode: {}', opcode)
        return True

    def _emit_message(self, opcode, data):
        if opcode == OP_TEXT:
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                text = data.decode('utf-8', 'replace')
            self._dispatch_text(text)
        else:
            self.logger.debug('忽略二进制帧 ({} 字节)', len(data))

    def _dispatch_text(self, text):
        if self.on_message is None:
            return
        try:
            self.on_message(text)
        except Exception as exc:
            self.logger.error('消息回调异常: {}', exc)

    def _read_frame(self):
        buf = self._frag_buf
        if len(buf) < 2:
            return None
        b0 = buf[0]
        b1 = buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        pos = 2
        if length == 126:
            if len(buf) < pos + 2:
                return None
            length = struct.unpack('!H', bytes(buf[pos:pos + 2]))[0]
            pos += 2
        elif length == 127:
            if len(buf) < pos + 8:
                return None
            length = struct.unpack('!Q', bytes(buf[pos:pos + 8]))[0]
            pos += 8
        mask = b''
        if masked:
            if len(buf) < pos + 4:
                return None
            mask = bytes(buf[pos:pos + 4])
            pos += 4
        if len(buf) < pos + length:
            return None
        payload = bytes(buf[pos:pos + length])
        del buf[:pos + length]
        if mask:
            payload = _xor(payload, mask)
        return fin, opcode, payload

    def _flush_queue(self):
        while True:
            with self._qlock:
                if not self._queue:
                    return
                opcode, payload = self._queue.pop(0)
            if opcode == OP_TEXT:
                self._send_frame(OP_TEXT, payload)
            else:
                self._send_frame(OP_BINARY, payload)

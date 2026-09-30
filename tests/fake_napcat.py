"""假 NapCat（OneBot v11 WebSocket 服务端），用于离线联调测试。

实现最少的 RFC 6455 服务端逻辑：握手、读取带掩码的客户端帧、发送未掩码帧、
支持 ping/pong/close 与长消息。收到 action 请求后按 echo 原样回 ok 响应，
并把收到的 action 记录在 self.calls 里，方便断言。
"""

import base64
import hashlib
import json
import socket
import struct
import threading
import time

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def _xor(payload, mask):
    if not payload:
        return b''
    out = bytearray(payload)
    for i in range(len(out)):
        out[i] ^= mask[i & 3]
    return bytes(out)


class FakeNapCat(object):
    def __init__(self, expected_token=None, require_token=True):
        self.expected_token = expected_token
        self.require_token = require_token
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.url = 'ws://127.0.0.1:{}'.format(self.port)

        self.calls = []          # [(action, params)]
        self.texts = []          # 收到的非 JSON 文本帧（用于裸 WebSocket 测试）
        self.responses = []      # [(action, expected_params, data)] 自定义响应
        self.connected_event = threading.Event()
        self._conn = None
        self._closed = threading.Event()
        self._thread = None
        self._send_lock = threading.Lock()
        self._recv_buf = bytearray()
        self.thread_error = None
        self.handshake_path = None
        self.handshake_headers = {}

    # ------------------------------------------------------------ 生命周期

    def start(self):
        self._thread = threading.Thread(target=self._serve, name='fake-napcat')
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self):
        self._closed.set()
        try:
            if self._conn is not None:
                self._conn.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -------------------------------------------------------------- 服务端

    def _serve(self):
        try:
            self.sock.settimeout(5.0)
            conn, _ = self.sock.accept()
            conn.settimeout(1.0)
            self._conn = conn
            if not self._handshake(conn):
                return
            self.connected_event.set()
            self._read_loop(conn)
        except Exception as exc:
            self.thread_error = repr(exc)

    def _read_until(self, conn, marker=b'\r\n\r\n', limit=65536):
        buf = bytearray()
        while marker not in buf and len(buf) < limit:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def _handshake(self, conn):
        data = self._read_until(conn)
        if not data:
            return False
        head, _, rest = data.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        parts = lines[0].split(' ')
        if len(parts) < 2 or parts[0] != 'GET':
            return False
        self.handshake_path = parts[1]

        headers = {}
        for line in lines[1:]:
            if ':' in line:
                name, _, value = line.partition(':')
                headers[name.strip().lower()] = value.strip()
        self.handshake_headers = headers

        if self.require_token and self.expected_token:
            auth = headers.get('authorization', '')
            token_in_query = 'access_token=' + self.expected_token in self.handshake_path
            if auth != 'Bearer ' + self.expected_token and not token_in_query:
                conn.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return False

        key = headers.get('sec-websocket-key')
        if not key:
            return False
        accept = base64.b64encode(
            hashlib.sha1((key + GUID).encode('utf-8')).digest()).decode('ascii')
        conn.sendall((
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            'Sec-WebSocket-Accept: {}\r\n\r\n'.format(accept)
        ).encode('utf-8'))
        if rest:
            self._recv_buf += rest
        return True

    # ------------------------------------------------------------ 帧收发

    def _read_frame(self, conn):
        while True:
            frame = self._try_parse()
            if frame is not None:
                return frame
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                if self._closed.is_set():
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self._recv_buf += chunk

    def _try_parse(self):
        buf = self._recv_buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        pos = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = struct.unpack('!H', bytes(buf[2:4]))[0]
            pos = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = struct.unpack('!Q', bytes(buf[2:10]))[0]
            pos = 10
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

    def _send_frame(self, opcode, payload=b'', fin=True):
        if self._conn is None:
            raise OSError('没有连接')
        first = (0x80 if fin else 0) | opcode
        length = len(payload)
        header = bytearray([first])
        if length < 126:
            header.append(length)
        elif length <= 0xFFFF:
            header.append(126)
            header += struct.pack('!H', length)
        else:
            header.append(127)
            header += struct.pack('!Q', length)
        with self._send_lock:
            self._conn.sendall(bytes(header) + bytes(payload))

    def send_text(self, text):
        self._send_frame(OP_TEXT, str(text).encode('utf-8'))

    def send_ping(self, payload=b'fake-ping'):
        self._send_frame(OP_PING, payload)

    def send_event(self, event):
        self.send_text(json.dumps(event, ensure_ascii=False))

    def send_large_text(self, size):
        text = 'x' * size
        self.send_text(text)
        return text

    # ------------------------------------------------------------ 读取循环

    def _read_loop(self, conn):
        fragments = bytearray()
        while not self._closed.is_set():
            frame = self._read_frame(conn)
            if frame is None:
                if self._closed.is_set():
                    return
                continue
            fin, opcode, payload = frame
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                return
            if opcode == 0x0:
                fragments += payload
                if fin:
                    self._handle_text(bytes(fragments))
                    fragments = bytearray()
                continue
            if opcode == OP_TEXT:
                if fin:
                    self._handle_text(payload)
                else:
                    fragments = bytearray(payload)

    def _handle_text(self, payload):
        try:
            decoded = payload.decode('utf-8')
        except UnicodeDecodeError:
            return
        try:
            message = json.loads(decoded)
        except ValueError:
            self.texts.append(decoded)
            return
        if not isinstance(message, dict):
            self.texts.append(decoded)
            return
        action = message.get('action')
        params = message.get('params') or {}
        echo = message.get('echo')
        self.calls.append((action, params))
        data = self._data_for(action, params)
        response = {'status': 'ok', 'retcode': 0, 'data': data, 'echo': echo}
        try:
            self.send_text(json.dumps(response, ensure_ascii=False))
        except OSError:
            pass

    def _data_for(self, action, params):
        for name, expected, data in self.responses:
            if name == action:
                return data
        if action == 'get_login_info':
            return {'user_id': 10001, 'nickname': 'TestBot'}
        if action in ('send_group_msg', 'send_private_msg'):
            return {'message_id': len(self.calls)}
        return {}

    # ------------------------------------------------------------ 测试辅助

    def wait_call(self, action, timeout=5.0, index=0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            matched = [call for call in self.calls if call[0] == action]
            if len(matched) > index:
                return matched[index][1]
            time.sleep(0.02)
        return None

    def wait_calls(self, action, count, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            matched = [call for call in self.calls if call[0] == action]
            if len(matched) >= count:
                return [item[1] for item in matched]
            time.sleep(0.02)
        return [item[1] for item in self.calls if item[0] == action]

    def reset_calls(self):
        self.calls = []

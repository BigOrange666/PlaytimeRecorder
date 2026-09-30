"""游玩记录核心：记录玩家进出、AFK 状态、累计时长，并写出可被解析的日志。

日志格式必须和 qqbridge.records 的解析规则严格对应：

    玩家 <名字> 进入服务器 (时间: YYYY-mm-dd HH:MM:SS)
    玩家 <名字> 退出服务器 | 本次游玩: x | AFK: y | 活跃: z | 累计游玩: t | 累计AFK: a
    玩家 <名字> 开始 AFK
    玩家 <名字> 结束 AFK，本次 AFK: d

数据文件：config/playtime_recorder/playtime_data.json
日志文件：logs/playtime_recorder/playtime.log
"""

import json
import os
import re
import time
from datetime import datetime
from threading import RLock

from .qqbridge.records import format_duration

# Server Utilities / 原版风格的 AFK 提示
AFK_ON_PATTERN = re.compile(r'^(?P<player>\w+) is now AFK$')
AFK_OFF_PATTERN = re.compile(r'^(?P<player>\w+) is no longer AFK$')

DEFAULT_DATA_DIR = os.path.join('config', 'playtime_recorder')
DEFAULT_LOG_DIR = os.path.join('logs', 'playtime_recorder')


def _now_text(timestamp=None):
    return datetime.fromtimestamp(timestamp if timestamp is not None else time.time()) \
        .strftime('%Y-%m-%d %H:%M:%S')


class PlaytimeRecorder(object):

    def __init__(self, server, logger=None, data_dir=None, log_dir=None,
                 on_session_end=None, on_afk_change=None):
        self.server = server
        self.logger = logger
        self.lock = RLock()

        # 在线玩家: {名字: {'join_time': float, 'afk_start_time': float|None, 'total_afk_seconds': float}}
        self.online_players = {}
        self.total_playtime = {}
        self.total_afk = {}
        self.started_at = time.time()
        self.last_session = None
        # 本进程内写出的记录条数（用于状态显示）
        self.logged_lines = 0

        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.data_file = os.path.join(self.data_dir, 'playtime_data.json')
        self.log_dir = log_dir or DEFAULT_LOG_DIR
        self.log_file = os.path.join(self.log_dir, 'playtime.log')

        # 会话结束 / AFK 变化的回调，签名 (player, payload)
        self.on_session_end = on_session_end
        self.on_afk_change = on_afk_change

        self._ensure_directories()
        self._load_data()

    # ------------------------------------------------------------ 基础设施

    def _ensure_directories(self):
        for directory in (self.data_dir, self.log_dir):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                self._warn('创建目录 {} 失败: {}', directory, exc)

    def _warn(self, message, *args):
        if self.logger is not None:
            self.logger.warning(message, *args)

    def _info(self, message, *args):
        if self.logger is not None:
            self.logger.info(message, *args)

    def _load_data(self):
        if not os.path.exists(self.data_file):
            return
        try:
            with open(self.data_file, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            self.total_playtime = data.get('total_playtime', {}) or {}
            self.total_afk = data.get('total_afk', {}) or {}
        except (OSError, ValueError) as exc:
            self._warn('加载游玩数据失败: {}', exc)

    def _save_data(self):
        try:
            payload = {
                'total_playtime': self.total_playtime,
                'total_afk': self.total_afk,
            }
            with open(self.data_file, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            self._warn('保存游玩数据失败: {}', exc)

    def _write_log(self, message):
        """写一行日志（时间戳由这里统一加，格式必须能被解析器识别）。"""
        try:
            with open(self.log_file, 'a', encoding='utf-8') as handle:
                handle.write('[{}] {}\n'.format(_now_text(), message))
            self.logged_lines += 1
        except OSError as exc:
            self._warn('写入日志失败: {}', exc)

    def _notify(self, callback, player, payload):
        if callback is None:
            return
        try:
            callback(player, payload)
        except Exception as exc:
            self._warn('记录回调异常: {!r}', exc)

    # ---------------------------------------------------------------- 查询

    def top_playtime(self, limit=None, name_filter=None):
        """按累计游玩时长排序返回 [(玩家, 秒数, AFK秒数)]，线程安全。"""
        with self.lock:
            rows = sorted(self.total_playtime.items(), key=lambda kv: -float(kv[1]))
            afk = dict(self.total_afk)
        if name_filter:
            needle = str(name_filter).lower()
            rows = [(player, seconds) for player, seconds in rows
                    if needle in str(player).lower()]
        if limit is not None and limit > 0:
            rows = rows[:limit]
        return [(player, float(seconds), float(afk.get(player, 0.0)))
                for player, seconds in rows]

    def snapshot(self):
        """给状态查询用的概览。"""
        with self.lock:
            rows = sorted(self.total_playtime.items(), key=lambda kv: -float(kv[1]))
            return {
                'online': sorted(self.online_players.keys()),
                'players': len(self.total_playtime),
                'total_seconds': int(sum(float(v) for v in self.total_playtime.values())),
                'total_afk_seconds': int(sum(float(v) for v in self.total_afk.values())),
                'top': [(name, float(seconds)) for name, seconds in rows[:5]],
                'log_file': self.log_file,
                'data_file': self.data_file,
                'logged_lines': self.logged_lines,
                'started_at': self.started_at,
            }

    # ------------------------------------------------------------ 事件处理

    def on_player_joined(self, server, player, info=None):
        with self.lock:
            now = time.time()
            previous = self.online_players.get(player)
            if previous is not None:
                # 没收到退出事件就重连了（比如服务器崩溃后重连），先把上一段结算掉
                self._warn('{} 的上一段记录未正常结束，先按当前时间结算', player)
                self._finalize(player, now, notify=False)
            self.online_players[player] = {
                'join_time': now,
                'afk_start_time': None,
                'total_afk_seconds': 0.0,
            }
            join_time_str = _now_text(now)
            self._write_log('玩家 {} 进入服务器 (时间: {})'.format(player, join_time_str))
            total_str = format_duration(self.total_playtime.get(player, 0))
            self._info('[游玩记录] {} 进入服务器 | 累计游玩: {}', player, total_str)

    def on_player_left(self, server, player):
        with self.lock:
            if player not in self.online_players:
                return
            self._finalize(player, time.time(), notify=True)

    def _finalize(self, player, now, notify=True):
        """结算一次会话（调用方需持有锁）。"""
        data = self.online_players.pop(player, None)
        if data is None:
            return

        session_seconds = max(0.0, now - data['join_time'])
        total_afk_session = float(data.get('total_afk_seconds', 0.0))
        if data.get('afk_start_time') is not None:
            # 玩家在 AFK 中途退出，补算未闭合的那一段
            total_afk_session += max(0.0, now - data['afk_start_time'])
        total_afk_session = min(total_afk_session, session_seconds)
        session_active = max(0.0, session_seconds - total_afk_session)

        self.total_playtime[player] = self.total_playtime.get(player, 0) + session_seconds
        self.total_afk[player] = self.total_afk.get(player, 0) + total_afk_session

        session_str = format_duration(session_seconds)
        afk_str = format_duration(total_afk_session)
        active_str = format_duration(session_active)
        total_str = format_duration(self.total_playtime[player])
        total_afk_str = format_duration(self.total_afk[player])

        self._save_data()
        self._write_log(
            '玩家 {} 退出服务器 | 本次游玩: {} | AFK: {} | 活跃: {} | 累计游玩: {} | 累计AFK: {}'
            .format(player, session_str, afk_str, active_str, total_str, total_afk_str))
        self._info('[游玩记录] {} 退出服务器 | 本次: {} | AFK: {} | 活跃: {} | 累计: {}',
                   player, session_str, afk_str, active_str, total_str)

        session = {
            'player': player,
            'timestamp': datetime.fromtimestamp(now),
            'session_seconds': session_seconds,
            'afk_seconds': total_afk_session,
            'active_seconds': session_active,
            'total_seconds': self.total_playtime[player],
            'total_afk_seconds': self.total_afk[player],
            'session_text': session_str,
            'afk_text': afk_str,
            'active_text': active_str,
            'total_text': total_str,
        }
        self.last_session = session
        if notify:
            self._notify(self.on_session_end, player, session)

    def on_info(self, server, info):
        """解析服务端输出里的 AFK 消息（Server Utilities 风格）。"""
        try:
            if not getattr(info, 'is_from_server', False):
                return
            content = str(getattr(info, 'content', '')).strip()
        except Exception:
            return
        if not content or 'AFK' not in content:
            return

        now = time.time()

        match = AFK_ON_PATTERN.match(content)
        if match:
            player = match.group('player')
            with self.lock:
                data = self.online_players.get(player)
                if data is not None and data.get('afk_start_time') is None:
                    data['afk_start_time'] = now
                    self._write_log('玩家 {} 开始 AFK'.format(player))
                    self._info('[游玩记录] {} 进入 AFK 状态', player)
            return

        match = AFK_OFF_PATTERN.match(content)
        if match:
            player = match.group('player')
            with self.lock:
                data = self.online_players.get(player)
                if data is not None and data.get('afk_start_time') is not None:
                    duration = max(0.0, now - data['afk_start_time'])
                    data['total_afk_seconds'] = data.get('total_afk_seconds', 0.0) + duration
                    data['afk_start_time'] = None
                    afk_str = format_duration(duration)
                    self._write_log('玩家 {} 结束 AFK，本次 AFK: {}'.format(player, afk_str))
                    self._info('[游玩记录] {} 取消 AFK，本次 AFK: {}', player, afk_str)
                    self._notify(self.on_afk_change, player, {
                        'afk_seconds': duration,
                        'afk_text': afk_str,
                        'state': 'off',
                    })
            return

    # ------------------------------------------------------------ 生命周期

    def save(self):
        with self.lock:
            self._save_data()

    def restore_from(self, old_recorder):
        """热重载时从旧实例恢复在线状态与累计数据。"""
        if old_recorder is None:
            return 0
        try:
            with self.lock:
                for player, data in dict(getattr(old_recorder, 'online_players', {})).items():
                    self.online_players[player] = dict(data)
                self.total_playtime = dict(getattr(old_recorder, 'total_playtime', {}))
                self.total_afk = dict(getattr(old_recorder, 'total_afk', {}))
                self.logged_lines = int(getattr(old_recorder, 'logged_lines', 0))
        except Exception as exc:
            self._warn('恢复旧实例状态失败: {!r}', exc)
            return 0
        return len(self.online_players)

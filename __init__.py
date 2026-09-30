"""Playtime Recorder —— 记录玩家游玩时长，并可通过 QQ（NapCat）查询与播报。

合并了原来两个独立插件的能力：

    记录侧：监听玩家进入/退出/AFK，统计游玩时长与 AFK 时长，
            写入 logs/playtime_recorder/playtime.log，累计数据存
            config/playtime_recorder/playtime_data.json
    通知侧：作为 WebSocket 客户端主动连接 NapCat 的「WebSocket 服务端」，
            QQ 群里发 #游玩历史 即可查询，玩家进出/上下线可主动播报

网络方向（关键）：
    Minecraft 侧没有公网 IP，所以由本插件主动连出去，不需要任何入站端口、
    不需要内网穿透。NapCat 侧要开的是「WebSocket 服务端」（反向 WS 服务端）。

QQ 指令：
    #游玩历史            昨天 00:00 到现在的记录（默认）
    #游玩历史 今天 / 近7天 / 2025-06-01 / 上限50
    #游玩帮助 / #游玩状态

MCDR 控制台：
    !!qqbridge / status / reload / test
    !!playtime  [status|save|top [数量|玩家名]]
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

try:  # MCDR 插件环境
    from mcdreforged.api.all import (
        CommandSource, Info, Literal, QuotableText, new_thread,
        PluginServerInterface, RText, rtr,
    )
    MCDR_AVAILABLE = True
except Exception:  # 允许在没有 MCDR 的环境里导入，便于自测
    CommandSource = object
    Info = object
    MCDR_AVAILABLE = False

    def new_thread(name=None):
        def decorator(func):
            return func
        return decorator

    class QuotableText(object):
        def __init__(self, name):
            self.name = name

        def runs(self, func):
            return self

        def then(self, node):
            return self


    class _RText(object):
        def __init__(self, text=''):
            self.text = str(text)

        def __str__(self):
            return self.text

        def to_plain_text(self):
            return self.text

    class _RTextFactory(object):
        @staticmethod
        def to_plain_text(value):
            if hasattr(value, 'to_plain_text'):
                return value.to_plain_text()
            return str(value)

    RText = _RTextFactory()

    def rtr(key, **kwargs):
        return None


# --------------------------------------------------------------------- 导入

def _find_package_roots(plugin_dir):
    """找出“包含 qqbridge 包的目录”，返回 [(父目录, 是否同层有 __init__.py)]。"""
    roots = []
    seen = set()

    def consider(parent, has_init):
        if parent in seen:
            return
        if os.path.isdir(os.path.join(parent, 'qqbridge')) and \
                os.path.isfile(os.path.join(parent, 'qqbridge', 'records.py')):
            seen.add(parent)
            roots.append((parent, has_init))

    consider(plugin_dir, True)
    try:
        for name in sorted(os.listdir(plugin_dir)):
            child = os.path.join(plugin_dir, name)
            if os.path.isdir(child) and name not in ('__pycache__', '.git', 'node_modules'):
                consider(child, None)
    except OSError:
        pass

    base_depth = plugin_dir.rstrip('\\/').count(os.sep)
    try:
        for current, dirs, files in os.walk(plugin_dir):
            depth = current.rstrip('\\/').count(os.sep) - base_depth
            if depth >= 3:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in ('__pycache__', '.git', 'node_modules')]
            if os.path.basename(current) == 'qqbridge' and 'records.py' in files:
                consider(os.path.dirname(current), '__init__.py' in files)
            elif 'qqbridge' in dirs:
                consider(current, None)
    except OSError:
        pass
    return roots


def _load_package_by_path(package_dir):
    """按文件绝对路径显式装载 qqbridge 包，不依赖 sys.path / import 查找。

    MCDR 会把插件入口当模块直接 exec，入口目录不保证在 sys.path 里，
    标准 import 可能找不到同级包。spec_from_file_location 逐个文件装载可以绕开
    import 查找；因为我们在 sys.modules 里注册了带 __path__ 的真实包对象，
    包内的相对导入（from .logging_util import ...）依然成立。
    """
    import importlib.util
    import sys
    import types

    package_dir = os.path.abspath(package_dir)
    package_name = 'qqbridge'

    package = types.ModuleType(package_name)
    package.__file__ = os.path.join(package_dir, '__init__.py')
    package.__path__ = [package_dir]
    package.__package__ = package_name
    sys.modules[package_name] = package

    def load(module_name):
        full_name = '{}.{}'.format(package_name, module_name)
        path = os.path.join(package_dir, module_name + '.py')
        if not os.path.isfile(path):
            raise ImportError('缺少文件: {}'.format(path))
        spec = importlib.util.spec_from_file_location(full_name, path)
        if spec is None or spec.loader is None:
            raise ImportError('无法为 {} 创建加载器'.format(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        module.__package__ = package_name
        spec.loader.exec_module(module)
        return module

    try:
        logging_mod = load('logging_util')
        records_mod = load('records')
        setattr(package, 'logging_util', logging_mod)
        setattr(package, 'records', records_mod)
        ws_mod = load('ws_client')
        setattr(package, 'ws_client', ws_mod)
        onebot_mod = load('onebot')
        setattr(package, 'onebot', onebot_mod)
    except Exception:
        for name in list(sys.modules):
            if name == package_name or name.startswith(package_name + '.'):
                sys.modules.pop(name, None)
        raise
    return records_mod, logging_mod.SafeLogger, onebot_mod


def _bootstrap_imports():
    """返回 (records 模块, SafeLogger, onebot 模块)。"""
    import importlib
    import sys
    import traceback

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    roots = _find_package_roots(plugin_dir)
    if not roots:
        raise ImportError(
            '在 {} 下没找到 qqbridge 包（需要存在 <该目录>/qqbridge/records.py）。\n'
            '  正常结构:\n    {}\n    {}\n    {}'.format(
                plugin_dir,
                os.path.join(plugin_dir, '__init__.py'),
                os.path.join(plugin_dir, 'qqbridge', 'records.py'),
                os.path.join(plugin_dir, 'mcdreforged.plugin.json')))

    failures = []

    for root, has_init in roots:
        try:
            return _load_package_by_path(os.path.join(root, 'qqbridge'))
        except Exception as exc:
            failures.append('  [按路径装载] {} 失败: {}: {}\n{}'.format(
                root, type(exc).__name__, exc, traceback.format_exc()))
            for name in list(sys.modules):
                if name == 'qqbridge' or name.startswith('qqbridge.'):
                    sys.modules.pop(name, None)

    for root, has_init in roots:
        added = []
        if root not in sys.path:
            sys.path.insert(0, root)
            added.append(root)
        try:
            records_mod = importlib.import_module('qqbridge.records')
            logging_mod = importlib.import_module('qqbridge.logging_util')
            onebot_mod = importlib.import_module('qqbridge.onebot')
            return records_mod, logging_mod.SafeLogger, onebot_mod
        except Exception as exc:
            failures.append('  [正常 import] {} 失败: {}: {}\n{}'.format(
                root, type(exc).__name__, exc, traceback.format_exc()))
            for name in list(sys.modules):
                if name == 'qqbridge' or name.startswith('qqbridge.'):
                    sys.modules.pop(name, None)
            for path in added:
                try:
                    sys.path.remove(path)
                except ValueError:
                    pass

    raise ImportError('找到 qqbridge 包但装载失败:\n{}'.format('\n'.join(failures)))


record_lib, SafeLogger, _onebot = _bootstrap_imports()
OneBotClient = _onebot.OneBotClient
OneBotError = _onebot.OneBotError
as_int = _onebot.as_int

from .recorder import PlaytimeRecorder  # noqa: E402

PACKAGE_SOURCE = getattr(record_lib, '__file__', '?')


# ------------------------------------------------------------------ 元数据

PLUGIN_ID = 'playtime_recorder'
PLUGIN_VERSION = '2.0.0'
PLUGIN_METADATA = {
    'id': PLUGIN_ID,
    'version': PLUGIN_VERSION,
    'name': 'Playtime Recorder',
    'description': '记录玩家游玩/AFK 时长，并通过 QQ(NapCat) 查询与播报',
    'author': 'BigOrange666',
    'link': 'https://github.com/BigOrange666',
    'dependencies': {'mcdreforged': '>=2.0.0'},
}

CONFIG_PATH = os.path.join('config', PLUGIN_ID, 'config.json')
LEGACY_CONFIG_PATHS = [os.path.join('config', 'qq_bridge', 'config.json')]

DEFAULT_CONFIG = {
    'enabled': True,
    'recorder': {
        'data_dir': os.path.join('config', 'playtime_recorder'),
        'log_dir': os.path.join('logs', 'playtime_recorder'),
        'include_afk': True,
    },
    'connection': {
        'ws_url': 'ws://997879.xyz:12538',
        'access_token': '',
        'access_token_in_query': False,
        'verify_ssl': True,
        'reconnect_min_seconds': 3,
        'reconnect_max_seconds': 60,
        'action_timeout_seconds': 15,
    },
    'access': {
        'allow_private': True,
        'allow_group': True,
        'group_whitelist': [],
        'private_whitelist': [],
        'group_require_at': True,
    },
    'commands': {
        'triggers': ['#游玩历史', '#游玩帮助', '#游玩状态', '#游玩统计'],
        'history_aliases': ['#游玩历史', '#历史', '#playtime'],
        'help_aliases': ['#游玩帮助', '#帮助'],
        'status_aliases': ['#游玩状态', '#状态'],
        'stats_aliases': ['#游玩统计', '#排行'],
    },
    'history': {
        'title': '游玩历史',
        'max_day_span': 90,
        'max_lines_per_query': 200,
        'include_absent_totals': True,
    },
    'notify': {
        'on_join': False,
        'on_leave': False,
        'on_afk': False,
        'session_min_seconds': 0,
        'targets': [],
        'join_text': '[游玩] {player} 进入了服务器 (累计 {total})',
        'leave_text': '[游玩] {player} 离开了服务器 | 本次 {session} | AFK {afk} | 活跃 {active} | 累计 {total}',
        'afk_text': '[游玩] {player} 结束 AFK，时长 {afk}',
        'send_online_notice': False,
        'online_notice_text': '[MCDR] 游玩记录插件已上线，发送 #游玩历史 可查询游玩记录',
    },
    'message': {
        'chunk_size': 1200,
        'reply_chunk_size': 1000,
    },
    'debug': False,
}

DEFAULT_HISTORY_ALIASES = ['#游玩历史', '#历史', '#playtime']
DEFAULT_HELP_ALIASES = ['#游玩帮助', '#帮助']
DEFAULT_STATUS_ALIASES = ['#游玩状态', '#状态']
DEFAULT_STATS_ALIASES = ['#游玩统计', '#排行']

CQ_AT_RE = re.compile(r'\[CQ:at,qq=(?P<qq>\d+)[^\]]*\]')
CQ_REPLY_RE = re.compile(r'\[CQ:reply,[^\]]*\]')
CQ_ANY_RE = re.compile(r'\[CQ:[^\]]*\]')

HELP_TEMPLATE = """===== 游玩记录插件帮助 =====
{history}               昨天 00:00 到现在
{history} 今天          今天 00:00 到现在
{history} 昨天          昨天一整天
{history} 近7天         最近 7 天（也支持 近30天 / 7天）
{history} 2025-06-01    指定日期
{history} 上限50        限制最多显示 50 条
{stats}                所有人累计游玩排行
{stats} Steve          某个玩家的累计时长
{status}               查看机器人连接状态"""


# --------------------------------------------------------------- 配置工具


def _copy(value):
    if isinstance(value, dict):
        return dict((k, _copy(v)) for k, v in value.items())
    if isinstance(value, list):
        return list(value)
    return value


def deep_merge(defaults, user):
    if not isinstance(user, dict):
        return _copy(defaults)
    result = {}
    for key, value in defaults.items():
        if key in user:
            if isinstance(value, dict) and isinstance(user[key], dict):
                result[key] = deep_merge(value, user[key])
            else:
                result[key] = user[key]
        else:
            result[key] = _copy(value)
    for key, value in user.items():
        if key not in result:
            result[key] = value
    return result


def _cfg(config, path, default=None):
    node = config
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', '1', 'yes', 'y', 'on'):
            return True
        if lowered in ('false', '0', 'no', 'n', 'off', ''):
            return False
    return default


def _as_int_list(value):
    """白名单/目标群配置统一成 int 列表；空列表表示“不限制/未配置”。"""
    result = []
    if value is None:
        return result
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    for item in value:
        if item in (None, '', 'all', 'ALL', '*'):
            continue
        converted = as_int(item)
        if converted is not None:
            result.append(converted)
    return result


def load_config(logger):
    directory = os.path.dirname(CONFIG_PATH)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        logger.warning('创建配置目录失败: {}', exc)

    user_config = {}
    source_path = CONFIG_PATH
    if not os.path.isfile(source_path):
        for legacy in LEGACY_CONFIG_PATHS:
            if os.path.isfile(legacy):
                source_path = legacy
                logger.info('检测到旧版配置文件 {}，本次按它加载（新配置写到 {}）',
                            legacy, CONFIG_PATH)
                break

    if os.path.isfile(source_path):
        try:
            with open(source_path, 'r', encoding='utf-8') as handle:
                user_config = json.load(handle)
            if not isinstance(user_config, dict):
                logger.warning('配置文件格式不正确，已忽略，使用默认配置')
                user_config = {}
        except (OSError, ValueError) as exc:
            logger.error('读取配置文件失败: {}，使用默认配置', exc)
            user_config = {}
    else:
        logger.info('未找到配置文件，已生成默认配置: {}', CONFIG_PATH)

    merged = deep_merge(DEFAULT_CONFIG, user_config)
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning('写入配置文件失败: {}', exc)
    return merged


# ------------------------------------------------------------- QQ 通知器


class QQNotifier(object):
    """连 NapCat、收指令、发通知。"""

    def __init__(self, mcdr_server, config, logger):
        self.mcdr = mcdr_server
        self.config = config
        self.logger = logger

        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='playtime-qq')
        self._executor_lock = threading.Lock()
        self.started_at = time.time()
        self.stats = {'replied': 0, 'denied': 0, 'errors': 0, 'notices': 0,
                      'last_error': None}

        self.client = self._build_client(config)

    def _build_client(self, config):
        return OneBotClient(
            url=str(_cfg(config, 'connection.ws_url', DEFAULT_CONFIG['connection']['ws_url'])),
            access_token=str(_cfg(config, 'connection.access_token', '') or ''),
            on_event=self._on_qq_event,
            on_state_change=self._on_state_change,
            logger=self.logger,
            reconnect_min=float(_cfg(config, 'connection.reconnect_min_seconds', 3) or 3),
            reconnect_max=float(_cfg(config, 'connection.reconnect_max_seconds', 60) or 60),
            action_timeout=float(_cfg(config, 'connection.action_timeout_seconds', 15) or 15),
            verify_ssl=_as_bool(_cfg(config, 'connection.verify_ssl', True), True),
            message_limit=int(_cfg(config, 'message.chunk_size', 1200) or 1200),
            access_token_in_query=_as_bool(
                _cfg(config, 'connection.access_token_in_query', False), False),
            name=PLUGIN_ID,
        )

    # ------------------------------------------------------------ 生命周期

    def start(self):
        if not _as_bool(_cfg(self.config, 'enabled', True), True):
            self.logger.warning('配置 enabled=false，QQ 功能不会启动（记录功能照常工作）')
            return
        url = str(_cfg(self.config, 'connection.ws_url', ''))
        if not url.startswith(('ws://', 'wss://')):
            self.logger.error('ws_url 不合法（需以 ws:// 或 wss:// 开头）: {}', url)
            return
        self.logger.info('正在连接 NapCat: {}', url)
        self.client.start()

    def stop(self):
        try:
            self.client.close()
        except Exception as exc:
            self.logger.warning('关闭 OneBot 连接出错: {}', exc)
        try:
            executor = self.executor
            if executor is not None:
                executor.shutdown(wait=False)
        except Exception:
            pass

    def reload(self):
        self.stop()
        self.config = load_config(self.logger)
        self.client = self._build_client(self.config)
        with self._executor_lock:
            self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='playtime-qq')
        self.started_at = time.time()
        self.start()

    def _submit(self, func, *args):
        with self._executor_lock:
            executor = self.executor
        try:
            executor.submit(func, *args)
        except RuntimeError as exc:
            self.logger.debug('无法提交任务: {}', exc)

    def _on_state_change(self, connected):
        if not connected:
            return
        if not _as_bool(_cfg(self.config, 'notify.send_online_notice', False), False):
            return
        targets = self._targets()
        if targets:
            self._submit(self._broadcast, str(_cfg(
                self.config, 'notify.online_notice_text', '')), targets)

    def _targets(self):
        targets = _as_int_list(_cfg(self.config, 'notify.targets', []))
        if targets:
            return targets
        return _as_int_list(_cfg(self.config, 'access.group_whitelist', []))

    # -------------------------------------------------------------- 播报

    def notify_session_end(self, player, session):
        """玩家退出时（由 recorder 回调）按配置播报。"""
        if not _as_bool(_cfg(self.config, 'notify.on_leave', False), False):
            return
        minimum = float(_cfg(self.config, 'notify.session_min_seconds', 0) or 0)
        if session.get('session_seconds', 0) < minimum:
            return
        template = str(_cfg(self.config, 'notify.leave_text', ''))
        text = template.format(
            player=player,
            session=session.get('session_text', '?'),
            afk=session.get('afk_text', '?'),
            active=session.get('active_text', '?'),
            total=session.get('total_text', '?'),
        )
        self._submit(self._broadcast, text)

    def notify_join(self, player, payload):
        if not _as_bool(_cfg(self.config, 'notify.on_join', False), False):
            return
        text = str(_cfg(self.config, 'notify.join_text', '')).format(
            player=player, total=payload.get('total_text', '?'))
        self._submit(self._broadcast, text)

    def notify_afk_change(self, player, payload):
        if not _as_bool(_cfg(self.config, 'notify.on_afk', False), False):
            return
        text = str(_cfg(self.config, 'notify.afk_text', '')).format(
            player=player, afk=payload.get('afk_text', '?'))
        self._submit(self._broadcast, text)

    def _broadcast(self, text):
        if not text:
            return
        for group_id in self._targets():
            if self.client.send_group_text(group_id, text):
                self.stats['notices'] += 1

    # ----------------------------------------------------------- 消息入口

    def _on_qq_event(self, event):
        if event.get('post_type') != 'message':
            return
        try:
            self._handle_message(event)
        except Exception as exc:
            self.stats['errors'] += 1
            self.stats['last_error'] = repr(exc)
            self.logger.error('处理 QQ 消息异常: {!r}', exc)

    def _handle_message(self, event):
        message_type = event.get('message_type')
        group_id = as_int(event.get('group_id'))
        user_id = as_int(event.get('user_id'))
        self_id = as_int(event.get('self_id'))
        if self_id is not None and user_id is not None and self_id == user_id:
            return

        raw_message = event.get('raw_message')
        if raw_message is None:
            raw_message = self._segments_to_text(event.get('message'))
        raw_message = str(raw_message or '')

        if message_type == 'group':
            if not _as_bool(_cfg(self.config, 'access.allow_group', True), True):
                self._deny('群聊已关闭')
                return
            whitelist = _as_int_list(_cfg(self.config, 'access.group_whitelist', []))
            if whitelist and group_id not in whitelist:
                self._deny('群不在白名单')
                return
            text = self._clean_text(raw_message, strip_leading_at=False)
            mentioned = self._mentioned_self(raw_message, self_id)
            if not mentioned:
                requires_at = _as_bool(_cfg(self.config, 'access.group_require_at', True), True)
                if requires_at and not self._looks_like_command(text):
                    return
            text = self._clean_text(raw_message, strip_leading_at=mentioned)
            self._dispatch(text, group_id=group_id, user_id=user_id)
            return

        if message_type == 'private':
            if not _as_bool(_cfg(self.config, 'access.allow_private', True), True):
                self._deny('私聊已关闭')
                return
            whitelist = _as_int_list(_cfg(self.config, 'access.private_whitelist', []))
            if whitelist and user_id not in whitelist:
                self._deny('QQ 不在白名单')
                return
            self._dispatch(self._clean_text(raw_message, strip_leading_at=True),
                           group_id=None, user_id=user_id)

    def _deny(self, reason):
        self.stats['denied'] += 1
        if _as_bool(_cfg(self.config, 'debug', False), False):
            self.logger.debug('拒绝消息: {}', reason)

    def _mentioned_self(self, raw_message, self_id):
        target = as_int(self_id)
        for qq in CQ_AT_RE.findall(raw_message or ''):
            mentioned = as_int(qq)
            if mentioned is not None and (target is None or mentioned == target):
                return True
        return False

    def _segments_to_text(self, segments):
        if isinstance(segments, str):
            return segments
        if not isinstance(segments, list):
            return ''
        parts = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            seg_type = segment.get('type')
            data = segment.get('data') or {}
            if seg_type == 'text':
                parts.append(str(data.get('text', '')))
            elif seg_type == 'at':
                parts.append('[CQ:at,qq={}]'.format(data.get('qq', '')))
        return ''.join(parts)

    def _clean_text(self, raw_message, strip_leading_at=False):
        text = CQ_REPLY_RE.sub('', raw_message or '')
        if strip_leading_at:
            while True:
                match = re.match(r'^\s*\[CQ:at,[^\]]*\]\s*', text)
                if not match:
                    break
                text = text[match.end():]
        text = CQ_AT_RE.sub(' ', text)
        text = CQ_ANY_RE.sub('', text)
        return text.replace('\u3000', ' ').strip()

    # ------------------------------------------------------------ 指令解析

    def _aliases(self, key, defaults):
        value = _cfg(self.config, 'commands.' + key, None)
        if isinstance(value, list) and value:
            return [str(item) for item in value if item]
        return list(defaults)

    def _triggers(self):
        triggers = _cfg(self.config, 'commands.triggers', None)
        if not isinstance(triggers, list) or not triggers:
            triggers = (self._aliases('history_aliases', DEFAULT_HISTORY_ALIASES)
                        + self._aliases('help_aliases', DEFAULT_HELP_ALIASES)
                        + self._aliases('status_aliases', DEFAULT_STATUS_ALIASES)
                        + self._aliases('stats_aliases', DEFAULT_STATS_ALIASES))
        return sorted(set(str(item) for item in triggers if item), key=len, reverse=True)

    def _looks_like_command(self, text):
        if not text:
            return False
        return any(text.startswith(trigger) for trigger in self._triggers())

    def _match_command(self, text):
        text = (text or '').strip()
        if not text:
            return None, None
        groups = (
            ('history', self._aliases('history_aliases', DEFAULT_HISTORY_ALIASES)),
            ('help', self._aliases('help_aliases', DEFAULT_HELP_ALIASES)),
            ('status', self._aliases('status_aliases', DEFAULT_STATUS_ALIASES)),
            ('stats', self._aliases('stats_aliases', DEFAULT_STATS_ALIASES)),
        )
        best = None
        for kind, aliases in groups:
            for alias in aliases:
                if text == alias:
                    candidate = (len(alias), kind, '')
                elif text.startswith(alias):
                    rest = text[len(alias):]
                    if rest and (rest[0].isspace() or rest[0] in '：:，,'):
                        candidate = (len(alias), kind, rest.lstrip('：:，, \t'))
                    else:
                        continue
                else:
                    continue
                if best is None or candidate[0] > best[0]:
                    best = candidate
        if best is None:
            return None, None
        return best[1], best[2]

    def _dispatch(self, text, group_id, user_id):
        kind, args = self._match_command(text)
        if kind is None:
            if _as_bool(_cfg(self.config, 'debug', False), False):
                self.logger.debug('未匹配到指令: {!r}', text[:80])
            return
        self._submit(self._run_command, kind, args, group_id, user_id)

    def _reply(self, group_id, user_id, text):
        chunk = int(_cfg(self.config, 'message.chunk_size', 1200) or 1200)
        if group_id is not None:
            self.client.send_group_text(group_id, text, chunk)
        elif user_id is not None:
            self.client.send_private_text(user_id, text, chunk)
        else:
            return
        self.stats['replied'] += 1

    # -------------------------------------------------------------- 各指令

    def _run_command(self, kind, args, group_id, user_id):
        handlers = {
            'history': self._cmd_history,
            'help': self._cmd_help,
            'status': self._cmd_status,
            'stats': self._cmd_stats,
        }
        handler = handlers.get(kind)
        if handler is None:
            return
        try:
            handler(args, group_id, user_id)
        except Exception as exc:
            self.stats['errors'] += 1
            self.stats['last_error'] = repr(exc)
            self.logger.error('执行指令 {} 失败: {!r}', kind, exc)
            try:
                self._reply(group_id, user_id, '查询出错了: {}'.format(exc))
            except Exception:
                pass

    def _cmd_help(self, args, group_id, user_id):
        history_alias = self._aliases('history_aliases', DEFAULT_HISTORY_ALIASES)[0]
        status_alias = self._aliases('status_aliases', DEFAULT_STATUS_ALIASES)[0]
        stats_alias = self._aliases('stats_aliases', DEFAULT_STATS_ALIASES)[0]
        text = HELP_TEMPLATE.format(history=history_alias, status=status_alias,
                                    stats=stats_alias)
        if _as_bool(_cfg(self.config, 'access.group_require_at', True), True):
            text += '\n（群内需要 @机器人 再发指令）'
        self._reply(group_id, user_id, text)

    def _cmd_status(self, args, group_id, user_id):
        stats = self.client.stats
        lines = [
            '===== 插件状态 =====',
            'QQ 连接: {}'.format('已连接' if stats.get('connected') else '未连接'),
            'NapCat: {}'.format(_cfg(self.config, 'connection.ws_url', '')),
            '机器人 QQ: {}'.format(stats.get('self_id') or '未知'),
            'QQ 收发: 事件 {} | 回复 {} | 播报 {}'.format(
                stats.get('events') or 0, self.stats['replied'], self.stats['notices']),
            '重连次数: {}'.format(stats.get('reconnects') or 0),
        ]
        recorder = self._recorder()
        if recorder is not None:
            snapshot = recorder.snapshot()
            lines.extend([
                '在线玩家: {}'.format('、'.join(snapshot['online']) or '无'),
                '记录玩家数: {}'.format(snapshot['players']),
                '累计游玩: {}'.format(record_lib.format_duration(snapshot['total_seconds'])),
                '日志文件: {}'.format(snapshot['log_file']),
            ])
        if stats.get('last_error'):
            lines.append('最近错误: {}'.format(stats['last_error']))
        self._reply(group_id, user_id, '\n'.join(lines))

    def _recorder(self):
        return globals().get('recorder')

    def _cmd_stats(self, args, group_id, user_id):
        recorder = self._recorder()
        if recorder is None:
            self._reply(group_id, user_id, '记录器尚未就绪')
            return
        snapshot = recorder.snapshot()
        query = (args or '').strip()
        width = int(_cfg(self.config, 'history.max_lines_per_query', 200) or 200)

        if query:
            matched = recorder.top_playtime(name_filter=query)
            if not matched:
                self._reply(group_id, user_id, '没有找到玩家: {}'.format(query))
                return
            lines = ['===== 玩家累计 =====']
            for name, seconds, afk in matched:
                lines.append('  {}: 累计 {} | AFK {}'.format(
                    name, record_lib.format_duration(seconds),
                    record_lib.format_duration(afk)))
            self._reply(group_id, user_id, '\n'.join(lines))
            return

        rows = recorder.top_playtime(limit=width)
        lines = ['===== 累计游玩排行 =====',
                 '记录玩家 {} 人，累计 {}'.format(
                     snapshot['players'], record_lib.format_duration(snapshot['total_seconds']))]
        if not rows:
            lines.append('  还没有任何记录')
        for index, (name, seconds, afk) in enumerate(rows, start=1):
            lines.append('  {}. {}: {}'.format(
                index, name, record_lib.format_duration(seconds)))
        if snapshot['players'] > len(rows):
            lines.append('  （只显示前 {} 名）'.format(len(rows)))
        self._reply(group_id, user_id, '\n'.join(lines))

    def _cmd_history(self, args, group_id, user_id):
        recorder = self._recorder()
        if recorder is None:
            self._reply(group_id, user_id, '记录器尚未就绪')
            return
        history_cfg = self.config.get('history') or {}
        try:
            time_range, max_lines = record_lib.parse_range_args(
                args,
                max_day_span=int(history_cfg.get('max_day_span',
                                                 record_lib.MAX_DAY_SPAN) or record_lib.MAX_DAY_SPAN))
        except ValueError as exc:
            alias = self._aliases('history_aliases', DEFAULT_HISTORY_ALIASES)[0]
            self._reply(group_id, user_id,
                        '参数错误: {}\n用法示例: {} 近7天 / {} 今天 / {} 2025-06-01'.format(
                            exc, alias, alias, alias))
            return

        if not max_lines:
            max_lines = int(history_cfg.get('max_lines_per_query', 200) or 200)

        all_records = record_lib.read_records(
            recorder.log_file, on_warning=lambda msg: self.logger.warning('{}', msg))
        if not _as_bool(_cfg(self.config, 'recorder.include_afk', True), True):
            all_records = [r for r in all_records
                           if r.event not in (record_lib.EVENT_AFK_ON, record_lib.EVENT_AFK_OFF)]
        selected = record_lib.filter_records(all_records, time_range.start, time_range.end)

        totals = {}
        if _as_bool(history_cfg.get('include_absent_totals', True), True):
            totals = record_lib.load_totals(recorder.data_file)

        title = str(history_cfg.get('title', '游玩历史') or '游玩历史')
        text = record_lib.build_history_report(
            selected, time_range, totals=totals, title=title,
            max_lines=max_lines, log_file=recorder.log_file)
        self._reply(group_id, user_id, text)
        self.logger.info('[QQ] 查询 {} | 命中 {} 条 | 群={} 用户={}',
                         time_range.describe(), len(selected), group_id, user_id)

    # --------------------------------------------------------- 控制台接口

    def status_text(self):
        stats = self.client.stats
        return ('QQ: {} | NapCat: {} | 事件 {} | 回复 {} | 重连 {}'.format(
            '已连接' if stats.get('connected') else '未连接',
            _cfg(self.config, 'connection.ws_url', ''),
            stats.get('events') or 0, self.stats['replied'], stats.get('reconnects') or 0))

    def qq_test(self, source):
        targets = self._targets()
        if not targets:
            _say(self.mcdr, source, 'msg.no_target',
                 '没有可用的群号，请先填写 notify.targets 或 access.group_whitelist')
            return False
        if not self.client.connected:
            _say(self.mcdr, source, 'msg.not_connected', '尚未连接到 NapCat，无法发送测试消息')
            return False
        text = '[游玩记录] 测试消息 {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        ok = any(self.client.send_group_text(group_id, text) for group_id in targets)
        if ok:
            _say(self.mcdr, source, 'msg.test_sent',
                 '测试消息已发送到: {targets}', targets=', '.join(str(g) for g in targets))
        else:
            _say(self.mcdr, source, 'msg.test_failed', '测试消息发送失败，请查看控制台日志')
        return ok


# ---------------------------------------------------------------- 全局状态

recorder = None
notifier = None


def _logger(server):
    return SafeLogger(getattr(server, 'logger', None))


def _say(server, source, key, fallback, **kwargs):
    """向命令来源回消息：优先 MCDR 翻译系统，拿不到就用中文兜底。"""
    full_key = '{}.{}'.format(PLUGIN_ID, key)
    if MCDR_AVAILABLE:
        try:
            text = rtr(full_key, **kwargs)
            if text is not None:
                server.tell(source, text)
                return
        except Exception:
            pass
        try:
            server.tell(source, RText(fallback.format(**kwargs) if kwargs else fallback))
            return
        except Exception:
            pass
    print(fallback.format(**kwargs) if kwargs else fallback)


def on_load(server, old_module):
    global recorder, notifier
    logger = _logger(server)

    old_notifier = getattr(old_module, 'notifier', None) if old_module is not None else None
    old_recorder = getattr(old_module, 'recorder', None) if old_module is not None else None
    if old_notifier is not None:
        try:
            old_notifier.stop()
        except Exception as exc:
            logger.warning('停止旧 QQ 连接失败: {}', exc)

    config = load_config(logger)
    recorder_cfg = config.get('recorder') or {}

    notifier = QQNotifier(server, config, logger)
    recorder = PlaytimeRecorder(
        server,
        logger=logger,
        data_dir=str(recorder_cfg.get('data_dir') or os.path.join('config', 'playtime_recorder')),
        log_dir=str(recorder_cfg.get('log_dir') or os.path.join('logs', 'playtime_recorder')),
        on_session_end=notifier.notify_session_end,
        on_afk_change=notifier.notify_afk_change,
    )

    restored = recorder.restore_from(old_recorder)
    if restored:
        logger.info('已恢复 {} 名在线玩家的记录', restored)

    notifier.start()
    _register_commands(server)
    logger.info('Playtime Recorder v{} 已加载 | qqbridge 来自: {} | 日志: {}',
                PLUGIN_VERSION, PACKAGE_SOURCE, recorder.log_file)


def on_unload(server):
    global recorder, notifier
    logger = _logger(server)
    if recorder is not None:
        try:
            recorder.save()
        except Exception as exc:
            logger.warning('保存游玩数据失败: {}', exc)
    if notifier is not None:
        try:
            notifier.stop()
        except Exception as exc:
            logger.warning('停止 QQ 连接失败: {}', exc)
    logger.info('Playtime Recorder 已卸载')


def on_server_startup(server):
    logger = _logger(server)
    if recorder is not None:
        logger.info('[游玩记录] 数据文件: {} | 日志文件: {}',
                    recorder.data_file, recorder.log_file)


def on_player_joined(server, player, info=None):
    if recorder is None:
        return
    recorder.on_player_joined(server, player, info)
    if notifier is not None:
        notifier.notify_join(player, {
            'total_text': record_lib.format_duration(recorder.total_playtime.get(player, 0)),
        })


def on_player_left(server, player):
    if recorder is not None:
        recorder.on_player_left(server, player)


def on_info(server, info):
    """解析服务端输出里的 AFK 消息（Server Utilities 风格）。"""
    if recorder is not None:
        recorder.on_info(server, info)


def _register_commands(server):
    if not MCDR_AVAILABLE:
        return False
    for attribute, value in (
            ('get_plugin_command_source', lambda *a, **k: None),
            ('register_help_message', lambda *a, **k: None),
            ('tell', lambda *a, **k: None)):
        if not hasattr(server, attribute):
            try:
                setattr(server, attribute, value)
            except Exception:
                pass

    def wrap(func):
        """把处理函数包成异步执行，避免阻塞 MCDR 主线程。"""
        def command_handler(source, context):
            return func(source, context)
        return new_thread(command_handler)

    try:
        @wrap
        def qq_status(source, context):
            if notifier is not None:
                _say(server, source, 'status', notifier.status_text())

        @wrap
        def qq_reload(source, context):
            if notifier is not None:
                notifier.reload()
                _say(server, source, 'msg.reloaded', '配置已重载')

        @wrap
        def qq_test_command(source, context):
            if notifier is not None:
                notifier.qq_test(source)

        @wrap
        def playtime_command(source, context):
            _playtime_report(server, source)

        @wrap
        def playtime_save(source, context):
            if recorder is not None:
                recorder.save()
            _say(server, source, 'msg.saved', '游玩数据已保存')

        @wrap
        def playtime_status(source, context):
            playtime_command(source, context)

        @wrap
        def playtime_top(source, context):
            _playtime_top(server, source, None)

        @wrap
        def playtime_top_args(source, context):
            _playtime_top(server, source, context.get('name'))

        qq_root = (
            Literal('!!qqbridge')
            .then(Literal('status').runs(qq_status))
            .then(Literal('reload').runs(qq_reload))
            .then(Literal('test').runs(qq_test_command))
            .runs(qq_status)
        )
        server.register_command(qq_root)

        playtime_root = (
            Literal('!!playtime')
            .then(Literal('status').runs(playtime_status))
            .then(Literal('save').runs(playtime_save))
            .then(Literal('top')
                  .then(QuotableText('name').runs(playtime_top_args))
                  .runs(playtime_top))
            .runs(playtime_command)
        )
        server.register_command(playtime_root)

        try:
            server.register_help_message(
                '!!qqbridge', rtr('{}.help.qq'.format(PLUGIN_ID)) or 'QQ 机器人桥接管理')
            server.register_help_message(
                '!!playtime', rtr('{}.help.playtime'.format(PLUGIN_ID)) or '游玩时长统计')
        except Exception:
            pass
        return True
    except Exception as exc:
        _logger(server).warning('注册 MCDR 命令失败（不影响 QQ 查询）: {!r}', exc)
        return False


def _playtime_report(server, source):
    if recorder is None:
        _say(server, source, 'msg.not_ready', '记录器尚未就绪')
        return
    snapshot = recorder.snapshot()
    lines = [
        '===== 游玩记录 =====',
        '在线玩家: {}'.format('、'.join(snapshot['online']) or '无'),
        '记录玩家数: {}'.format(snapshot['players']),
        '累计游玩: {}'.format(record_lib.format_duration(snapshot['total_seconds'])),
        '累计 AFK: {}'.format(record_lib.format_duration(snapshot['total_afk_seconds'])),
        '日志文件: {}'.format(snapshot['log_file']),
        '数据文件: {}'.format(snapshot['data_file']),
    ]
    for name, seconds in snapshot['top']:
        lines.append('  {}: {}'.format(name, record_lib.format_duration(seconds)))
    _say(server, source, 'msg.report', '\n'.join(lines))


def _playtime_top(server, source, name):
    if recorder is None:
        _say(server, source, 'msg.not_ready', '记录器尚未就绪')
        return
    rows = recorder.top_playtime(limit=20, name_filter=name)
    if name and not rows:
        _say(server, source, 'msg.no_player', '没有找到玩家: {name}', name=name)
        return
    lines = ['===== 累计游玩排行 =====']
    if not rows:
        lines.append('  还没有任何记录')
    for index, (player, seconds, afk) in enumerate(rows, start=1):
        lines.append('  {}. {}: {}'.format(
            index, player, record_lib.format_duration(seconds)))
    _say(server, source, 'msg.top', '\n'.join(lines))

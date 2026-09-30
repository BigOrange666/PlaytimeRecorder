"""游玩记录的数据层：解析日志、筛选时间范围、统计与格式化输出。

日志格式（由本插件的 recorder 模块写出，也兼容旧版 PlaytimeRecorder）：

    [2025-06-01 20:15:03] 玩家 Steve 进入服务器 (时间: 2025-06-01 20:15:03)
    [2025-06-01 22:40:11] 玩家 Steve 退出服务器 | 本次游玩: 2小时25分钟8秒 | AFK: 5分钟 | 活跃: 2小时20分钟8秒 | 累计游玩: 10小时0分钟0秒 | 累计AFK: 20分钟
    [2025-06-01 21:00:00] 玩家 Steve 开始 AFK
    [2025-06-01 21:10:00] 玩家 Steve 结束 AFK，本次 AFK: 10分钟

本模块不依赖 MCDR，也不依赖任何第三方库，可以单独做单元测试。
"""

import json
import os
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta

DEFAULT_DAY_SPAN = 7
MAX_DAY_SPAN = 90

_WS = r'[ \t]*'
LINE_RE = re.compile(
    r'^' + _WS + r'\[(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\](?P<body>.*)$'
)
JOIN_RE = re.compile(r'^' + _WS + r'玩家' + _WS + r'(?P<player>\S+?)' + _WS + r'进入服务器')
LEAVE_RE = re.compile(r'^' + _WS + r'玩家' + _WS + r'(?P<player>\S+?)' + _WS + r'退出服务器')
AFK_ON_RE = re.compile(
    r'^' + _WS + r'玩家' + _WS + r'(?P<player>\S+?)' + _WS + r'开始' + _WS + r'AFK')
AFK_OFF_RE = re.compile(
    r'^' + _WS + r'玩家' + _WS + r'(?P<player>\S+?)' + _WS + r'结束' + _WS + r'AFK')

DURATION_PART_RE = re.compile(r'(\d+)\s*(小时|时|分钟|分|秒)')
DURATION_BARE_RE = re.compile(r'(\d+)')
_FIELD_RE = re.compile(
    r'([0-9A-Za-z\u4e00-\u9fff ]{1,20}?)\s*[:：]\s*'
    r'([0-9][0-9A-Za-z\u4e00-\u9fff ]{0,20}?(?:小时|时|分钟|分|秒)[0-9A-Za-z\u4e00-\u9fff ]{0,12})'
)
_NAME_SPLIT_RE = re.compile(r'[|｜]')

EVENT_JOIN = 'join'
EVENT_LEAVE = 'leave'
EVENT_AFK_ON = 'afk_on'
EVENT_AFK_OFF = 'afk_off'

EVENT_LABEL = {
    EVENT_JOIN: '进入服务器',
    EVENT_LEAVE: '离开服务器',
    EVENT_AFK_ON: '开始 AFK',
    EVENT_AFK_OFF: '结束 AFK',
}


# --------------------------------------------------------------------- 工具


def parse_duration(text):
    """把 '2小时25分钟8秒' / '2小时0分钟0秒' / '45分钟' / '0秒' 解析成秒数。

    记录端会输出含 0 值的单位（'2小时0分钟0秒'），所以不能要求各单位同时出现。
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    total = 0
    matched = False
    for value, unit in DURATION_PART_RE.findall(text):
        matched = True
        number = int(value)
        if unit in ('小时', '时'):
            total += number * 3600
        elif unit in ('分钟', '分'):
            total += number * 60
        else:
            total += number
    if matched:
        return total
    bare = DURATION_BARE_RE.fullmatch(text)
    if bare:
        return int(bare.group(1))
    return None


def format_duration(seconds):
    """秒数 -> '2小时25分钟8秒'（和 recorder 的写法保持一致）。"""
    if seconds is None:
        return '未知'
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return '未知'
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    parts = []
    if hours:
        parts.append('{}小时'.format(hours))
    if minutes:
        parts.append('{}分钟'.format(minutes))
    if secs or not parts:
        parts.append('{}秒'.format(secs))
    return ''.join(parts)


def parse_timestamp(text):
    try:
        return datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def _strip_ws(text):
    return text.replace('\u3000', ' ').strip()


def _extract_duration(body, *names):
    """按“字段名 + 冒号 + 时长”的位置抽取秒数，失败返回 None。

    记录端两种行的字段名不同（退出行是 'AFK'，AFK 结束行是 '本次 AFK'），
    用子串定位 + 紧跟冒号校验可以同时吃下，也不怕字段名两侧的空格。

    names 按“最具体优先”顺序传，例如 ('本次 AFK', 'AFK')。
    """
    wanted = [_strip_ws(name).lower() for name in names if name]
    if not wanted:
        return None
    lowered = body.lower()

    for name in wanted:
        search_from = 0
        while True:
            index = lowered.find(name, search_from)
            if index < 0:
                break
            search_from = index + 1
            rest = body[index + len(name):]
            match = re.match(r'^\s*[:：]\s*(.+)$', rest)
            if not match:
                continue
            value = _NAME_SPLIT_RE.split(match.group(1))[0].strip()
            seconds = parse_duration(value)
            if seconds is not None:
                return seconds

    for match in _FIELD_RE.finditer(body):
        if _strip_ws(match.group(1)).lower() in wanted:
            seconds = parse_duration(match.group(2).strip())
            if seconds is not None:
                return seconds
    return None


# --------------------------------------------------------------------- 数据


class Record(object):
    """一条游玩记录。"""

    __slots__ = ('timestamp', 'player', 'event', 'session_seconds',
                 'afk_seconds', 'active_seconds', 'total_seconds',
                 'total_afk_seconds', 'raw')

    def __init__(self, timestamp, player, event, session_seconds=None,
                 afk_seconds=None, active_seconds=None, total_seconds=None,
                 total_afk_seconds=None, raw=''):
        self.timestamp = timestamp
        self.player = player
        self.event = event
        self.session_seconds = session_seconds
        self.afk_seconds = afk_seconds
        self.active_seconds = active_seconds
        self.total_seconds = total_seconds
        self.total_afk_seconds = total_afk_seconds
        self.raw = raw

    def __repr__(self):
        return '<Record {} {} {}>'.format(
            self.timestamp.strftime('%Y-%m-%d %H:%M:%S') if self.timestamp else '?',
            self.player, self.event)


def parse_line(line):
    """解析单行日志，返回 Record 或 None。"""
    if not line:
        return None
    line = line.rstrip('\n').rstrip('\r')
    if not line.strip():
        return None
    match = LINE_RE.match(line)
    if not match:
        return None
    timestamp = parse_timestamp(match.group('ts'))
    if timestamp is None:
        return None
    body = _strip_ws(match.group('body'))
    if not body:
        return None

    for pattern, event in ((JOIN_RE, EVENT_JOIN), (LEAVE_RE, EVENT_LEAVE),
                           (AFK_ON_RE, EVENT_AFK_ON), (AFK_OFF_RE, EVENT_AFK_OFF)):
        found = pattern.match(body)
        if not found:
            continue
        record = Record(timestamp, found.group('player'), event, raw=line)
        if event in (EVENT_LEAVE, EVENT_AFK_OFF):
            record.session_seconds = _extract_duration(body, '本次游玩', '游玩')
            record.afk_seconds = _extract_duration(body, '本次 AFK', 'AFK')
            record.active_seconds = _extract_duration(body, '活跃')
            record.total_seconds = _extract_duration(body, '累计游玩')
            record.total_afk_seconds = _extract_duration(body, '累计 AFK', '累计AFK')
        return record
    return None


def read_records(log_file, max_read_bytes=16 * 1024 * 1024, on_warning=None):
    """读取日志文件并解析出全部记录（按时间升序）。文件不存在返回空列表。"""
    if not log_file or not os.path.isfile(log_file):
        return []
    try:
        size = os.path.getsize(log_file)
    except OSError:
        return []

    truncated = False
    try:
        with open(log_file, 'rb') as handle:
            if size > max_read_bytes:
                truncated = True
                handle.seek(size - max_read_bytes)
            data = handle.read()
    except OSError as exc:
        if on_warning:
            on_warning('读取日志失败: {}'.format(exc))
        return []

    text = data.decode('utf-8', 'replace')
    if truncated:
        text = text.split('\n', 1)[-1]
        if on_warning:
            on_warning('日志超过 {} 字节，只读取了最后一部分'.format(max_read_bytes))

    records = []
    for line in text.split('\n'):
        record = parse_line(line)
        if record is not None:
            records.append(record)
    records.sort(key=lambda item: (item.timestamp, item.player))
    return records


def filter_records(records, start, end):
    return [r for r in records if start <= r.timestamp <= end]


def load_totals(data_file):
    """读取累计数据 {玩家: 秒}，失败返回空字典。"""
    if not data_file or not os.path.isfile(data_file):
        return {}
    try:
        with open(data_file, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    totals = {}
    raw_playtime = payload.get('total_playtime') or {}
    if isinstance(raw_playtime, dict):
        for player, value in raw_playtime.items():
            try:
                totals[str(player)] = float(value)
            except (TypeError, ValueError):
                continue
    return totals


# ----------------------------------------------------------------- 时间范围


class TimeRange(object):
    def __init__(self, start, end, explicit_date=None, day_span=None, note=None):
        self.start = start
        self.end = end
        self.explicit_date = explicit_date
        self.day_span = day_span
        self.note = note

    @property
    def is_single_day(self):
        return self.explicit_date is not None

    def describe(self):
        if self.start.date() == self.end.date():
            return '{} 00:00 至 {}'.format(
                self.start.strftime('%Y-%m-%d'), self.end.strftime('%H:%M:%S'))
        return '{} 00:00 至 {}'.format(
            self.start.strftime('%Y-%m-%d'), self.end.strftime('%m-%d %H:%M:%S'))


def yesterday_midnight(now=None):
    """“上一天 0:00”。"""
    now = now or datetime.now()
    return datetime.combine(now.date() - timedelta(days=1), datetime.min.time())


def default_range(now=None):
    """默认查询范围：昨天 00:00 -> 现在。"""
    now = now or datetime.now()
    return TimeRange(yesterday_midnight(now), now)


def today_range(now=None):
    now = now or datetime.now()
    return TimeRange(datetime.combine(now.date(), datetime.min.time()), now)


def parse_range_args(args, now=None, max_day_span=MAX_DAY_SPAN):
    """解析指令参数，返回 (TimeRange, max_lines 或 None)，失败抛 ValueError。

    支持：#游玩历史 / 今天 / 昨天 / 近7天 / 2025-06-01 / 06-01 / 上限50
    """
    now = now or datetime.now()
    args = _strip_ws(args or '')
    day_span = None
    max_lines = None
    explicit_date = None
    has_today_kw = False

    if args:
        for token in re.split(r'[\s,，、]+', args):
            token = token.strip()
            if not token:
                continue
            lowered = token.lower()

            if lowered in ('今天', 'today', '当日', '本日'):
                has_today_kw = True
                continue
            if lowered in ('昨天', 'yesterday', '昨日', '上一天'):
                explicit_date = now.date() - timedelta(days=1)
                continue
            if lowered in ('近一周', '最近一周', '一周', '本周', '7天'):
                day_span = 7
                continue
            if lowered in ('近一个月', '最近一个月', '30天'):
                day_span = 30
                continue
            if lowered in ('全部', '所有', 'all'):
                day_span = max_day_span
                continue

            matched = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', token)
            if matched:
                try:
                    explicit_date = date(int(matched.group(1)), int(matched.group(2)),
                                         int(matched.group(3)))
                except ValueError:
                    raise ValueError('日期不存在: {}'.format(token))
                continue
            matched = re.fullmatch(r'(\d{1,2})-(\d{1,2})', token)
            if matched:
                try:
                    explicit_date = date(now.year, int(matched.group(1)), int(matched.group(2)))
                except ValueError:
                    raise ValueError('日期不存在: {}'.format(token))
                continue
            matched = re.fullmatch(r'(?:近|最近|前)?\s*(\d+)\s*(?:天|日|d|D)', token)
            if matched:
                day_span = int(matched.group(1))
                continue
            matched = re.fullmatch(r'(?:上限|最多|限制|取|最后|最近)\s*(\d+)', token)
            if matched:
                max_lines = int(matched.group(1))
                continue
            matched = re.fullmatch(r'(\d+)', token)
            if matched:
                value = int(matched.group(1))
                if value >= 1000:
                    max_lines = value
                else:
                    day_span = value
                continue

            raise ValueError('无法识别的参数: {}'.format(token))

    if explicit_date is not None:
        start = datetime.combine(explicit_date, datetime.min.time())
        end = datetime.combine(explicit_date, datetime.max.time()).replace(microsecond=0)
        if explicit_date == now.date():
            end = now
        elif explicit_date > now.date():
            raise ValueError('不能查询未来的日期: {}'.format(explicit_date.isoformat()))
        return TimeRange(start, end, explicit_date=explicit_date), max_lines

    if day_span is None:
        if has_today_kw:
            return today_range(now), max_lines
        return default_range(now), max_lines

    if day_span < 1:
        raise ValueError('天数必须大于 0')
    cap_note = None
    if day_span > max_day_span:
        cap_note = '天数上限为 {} 天，已按 {} 天查询'.format(max_day_span, max_day_span)
        day_span = max_day_span
    start_date = now.date() - timedelta(days=day_span - 1)
    start = datetime.combine(start_date, datetime.min.time())
    return TimeRange(start, now, day_span=day_span, note=cap_note), max_lines


# ------------------------------------------------------------------- 统计


def summarize(records):
    """按玩家统计场次与时长，返回 [(玩家, 场次, 秒数)]，按秒数降序。"""
    stats = OrderedDict()
    for record in records:
        if record.event != EVENT_LEAVE:
            continue
        item = stats.setdefault(record.player, {'sessions': 0, 'seconds': 0.0})
        item['sessions'] += 1
        if record.session_seconds:
            item['seconds'] += float(record.session_seconds)

    def sort_key(entry):
        player, item = entry
        return (-item['seconds'], -item['sessions'], player)

    rows = []
    for player, item in sorted(stats.items(), key=sort_key):
        rows.append((player, item['sessions'], int(round(item['seconds']))))
    return rows


def players_in(records):
    seen = []
    for record in records:
        if record.player not in seen:
            seen.append(record.player)
    return seen


# ------------------------------------------------------------------- 格式化


def format_records(records, title='游玩记录'):
    """把记录格式化成 QQ 友好的纯文本。"""
    if not records:
        return ''
    lines = []
    current_player = None
    for record in records:
        if record.player != current_player:
            current_player = record.player
            lines.append('【{}】'.format(current_player))
        stamp = record.timestamp.strftime('%m-%d %H:%M')
        if record.event == EVENT_JOIN:
            lines.append('  {} 进入服务器'.format(stamp))
        elif record.event == EVENT_LEAVE:
            parts = ['  {} 离开服务器'.format(stamp)]
            if record.session_seconds is not None:
                parts.append('本次 {}'.format(format_duration(record.session_seconds)))
            if record.afk_seconds:
                parts.append('AFK {}'.format(format_duration(record.afk_seconds)))
            if record.active_seconds is not None:
                parts.append('活跃 {}'.format(format_duration(record.active_seconds)))
            lines.append(' | '.join(parts))
        elif record.event == EVENT_AFK_ON:
            lines.append('  {} 开始 AFK'.format(stamp))
        elif record.event == EVENT_AFK_OFF:
            parts = ['  {} 结束 AFK'.format(stamp)]
            if record.afk_seconds is not None:
                parts.append('时长 {}'.format(format_duration(record.afk_seconds)))
            lines.append(' | '.join(parts))
    return '\n'.join(lines)


def build_report(records, time_range, totals=None, title='游玩历史',
                 max_lines=None, note=None):
    """拼出最终的回复文本。records 必须已经筛选并排好序。"""
    header = '===== {} ====='.format(title)
    range_line = '范围: {}'.format(time_range.describe())
    notes = [item for item in (getattr(time_range, 'note', None), note) if item]

    if not records:
        body = ['没有找到该时间段内的游玩记录。']
        body.extend(notes)
        return '\n'.join([header, range_line] + body)

    selected = records
    truncated = False
    if max_lines is not None and max_lines > 0 and len(records) > max_lines:
        selected = records[-max_lines:]
        truncated = True

    text = format_records(selected)
    summary_rows = summarize(selected)
    involved = players_in(selected)
    summarized = set(row[0] for row in summary_rows)

    tail = []
    tail.append('')
    tail.append('----- 统计 -----')
    tail.append('记录 {} 条，玩家 {} 人'.format(len(selected), len(involved)))
    if summary_rows:
        for player, sessions, seconds in summary_rows:
            tail.append('  {}: 上线 {} 次，合计 {}'.format(
                player, sessions, format_duration(seconds)))
    else:
        tail.append('  范围内没有完整的退出记录，暂无时长统计')

    joined_only = [p for p in involved if p not in summarized]
    if joined_only:
        tail.append('  仅进入、未记录退出: {}'.format('、'.join(joined_only)))

    totals = totals or {}
    absent = [p for p in totals if p not in involved and totals.get(p)]
    if absent:
        tail.append('  未上线玩家的历史累计:')
        for player in sorted(absent, key=lambda name: -float(totals.get(name, 0))):
            tail.append('    {}: {}'.format(player, format_duration(totals.get(player))))

    if truncated:
        tail.append('  记录过多，只显示最后 {} 条（可用“上限N”调整）'.format(len(selected)))
    for item in notes:
        tail.append('  ' + item)

    return '\n'.join([header, range_line, text] + tail)


def build_history_report(records, time_range, totals=None, title='游玩历史',
                         max_lines=None, log_file=None):
    """带“日志缺失”提示的便捷入口。"""
    note = None
    if log_file and not os.path.isfile(log_file):
        note = '注意: 未找到日志文件 {}，可能还没产生任何记录。'.format(log_file)
    return build_report(records, time_range, totals=totals, title=title,
                        max_lines=max_lines, note=note)

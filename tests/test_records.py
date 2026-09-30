"""记录解析 / 时间范围 / 格式化 的离线测试（合并后属于本插件的一部分）。

运行： python tests/test_records.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)                 # PlaytimeRecorder/
PKG_PARENT = os.path.join(PLUGIN_DIR, 'qqbridge')  # 含 qqbridge 包的那一层
for candidate in (PLUGIN_DIR, PKG_PARENT, HERE):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from qqbridge import records as R  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=''):
    if condition:
        PASSED.append(name)
        print('[PASS] {}'.format(name))
    else:
        FAILED.append('{} {}'.format(name, detail))
        print('[FAIL] {} {}'.format(name, detail))


NOW = datetime(2025, 6, 10, 15, 30, 0)
LOG_LINES = [
    # 4 天前的旧记录：默认范围（昨天 0:00 起）不应包含
    ('2025-06-06 09:00:00', '玩家 Old 进入服务器 (时间: 2025-06-06 09:00:00)'),
    ('2025-06-06 09:30:00', '玩家 Old 退出服务器 | 本次游玩: 30分钟0秒 | AFK: 0秒 | 活跃: 30分钟0秒 | 累计游玩: 30分钟0秒 | 累计AFK: 0秒'),
    # 两天前
    ('2025-06-08 10:00:00', '玩家 Two 进入服务器 (时间: 2025-06-08 10:00:00)'),
    ('2025-06-08 11:00:00', '玩家 Two 退出服务器 | 本次游玩: 1小时0分钟0秒 | AFK: 10分钟0秒 | 活跃: 50分钟0秒 | 累计游玩: 1小时0分钟0秒 | 累计AFK: 10分钟0秒'),
    # 昨天
    ('2025-06-09 00:30:00', '玩家 Steve 进入服务器 (时间: 2025-06-09 00:30:00)'),
    ('2025-06-09 01:00:00', '玩家 Steve 开始 AFK'),
    ('2025-06-09 01:15:00', '玩家 Steve 结束 AFK，本次 AFK: 15分钟0秒'),
    ('2025-06-09 02:30:00', '玩家 Steve 退出服务器 | 本次游玩: 2小时0分钟0秒 | AFK: 15分钟0秒 | 活跃: 1小时45分钟0秒 | 累计游玩: 3小时0分钟0秒 | 累计AFK: 15分钟0秒'),
    ('2025-06-09 23:50:00', '玩家 Alex 进入服务器 (时间: 2025-06-09 23:50:00)'),
    # 今天
    ('2025-06-10 08:00:00', '玩家 Alex 退出服务器 | 本次游玩: 8小时10分钟0秒 | AFK: 1小时0分钟0秒 | 活跃: 7小时10分钟0秒 | 累计游玩: 8小时10分钟0秒 | 累计AFK: 1小时0分钟0秒'),
    ('2025-06-10 09:00:00', '玩家 Steve 进入服务器 (时间: 2025-06-10 09:00:00)'),
    # 无效行
    ('2025-06-10 09:05:00', '这是一条无关的日志'),
    ('坏掉的时间戳 09:06:00', '玩家 X 进入服务器'),
]


def build_log_file(directory):
    path = os.path.join(directory, 'playtime.log')
    with open(path, 'w', encoding='utf-8') as handle:
        for stamp, body in LOG_LINES:
            handle.write('[{}] {}\n'.format(stamp, body))
    return path


def test_duration():
    check('parse_duration 小时分钟秒', R.parse_duration('2小时25分钟8秒') == 2 * 3600 + 25 * 60 + 8)
    check('parse_duration 纯分钟', R.parse_duration('45分钟') == 45 * 60)
    check('parse_duration 0秒', R.parse_duration('0秒') == 0)
    check('parse_duration 含0单位(小时)', R.parse_duration('2小时0分钟0秒') == 7200)
    check('parse_duration 含0单位(分钟)', R.parse_duration('1小时0分钟0秒') == 3600)
    check('parse_duration 只有小时', R.parse_duration('3小时') == 3 * 3600)
    check('parse_duration 只有秒', R.parse_duration('30秒') == 30)
    check('parse_duration 纯数字', R.parse_duration('0') == 0)
    check('parse_duration 中文分', R.parse_duration('5分') == 300)
    check('parse_duration 非法', R.parse_duration('未知') is None)
    check('parse_duration 空', R.parse_duration('') is None)
    check('format_duration 回环', R.format_duration(2 * 3600 + 25 * 60 + 8) == '2小时25分钟8秒')
    check('format_duration 0', R.format_duration(0) == '0秒')
    check('format_duration None', R.format_duration(None) == '未知')


def test_extract_duration():
    """时长抽取必须同时吃下 'AFK: x' 和 '本次 AFK: x' 两种写法。"""
    leave = ('玩家 Steve 退出服务器 | 本次游玩: 2小时0分钟0秒 | AFK: 15分钟0秒 | '
             '活跃: 1小时45分钟0秒 | 累计游玩: 3小时0分钟0秒 | 累计AFK: 15分钟0秒')
    check('抽取 本次游玩', R._extract_duration(leave, '本次游玩', '游玩') == 7200)
    check('抽取 AFK（退出行）', R._extract_duration(leave, '本次 AFK', 'AFK') == 900)
    check('抽取 活跃', R._extract_duration(leave, '活跃') == 6300)
    check('抽取 累计游玩', R._extract_duration(leave, '累计游玩') == 10800)
    check('抽取 累计AFK', R._extract_duration(leave, '累计 AFK', '累计AFK') == 900)
    check('抽取 不存在的字段', R._extract_duration(leave, '不存在') is None)

    afk_off = '玩家 Steve 结束 AFK，本次 AFK: 15分钟0秒'
    check('抽取 本次 AFK（AFK结束行）', R._extract_duration(afk_off, '本次 AFK', 'AFK') == 900)
    check('抽取 全角冒号',
          R._extract_duration('玩家 Steve 结束 AFK，本次 AFK：10分钟', '本次 AFK', 'AFK') == 600)
    check('抽取 裸秒', R._extract_duration('玩家 A 结束 AFK，本次 AFK: 30秒', '本次 AFK') == 30)
    check('抽取 不误判', R._extract_duration('玩家 提到 AFK 但没时长', 'AFK') is None)
    check('抽取 全角竖线',
          R._extract_duration('本次游玩: 2小时 ｜ AFK: 10分钟', '本次游玩', '游玩') == 7200)


def test_parse_line():
    record = R.parse_line('[2025-06-09 02:30:00] 玩家 Steve 退出服务器 | 本次游玩: 2小时0分钟0秒 | '
                          'AFK: 15分钟0秒 | 活跃: 1小时45分钟0秒 | 累计游玩: 3小时0分钟0秒 | 累计AFK: 15分钟0秒')
    check('parse_line 退出记录', record is not None and record.event == R.EVENT_LEAVE)
    check('parse_line 玩家名', record is not None and record.player == 'Steve')
    check('parse_line 本次时长', record is not None and record.session_seconds == 7200)
    check('parse_line AFK 时长', record is not None and record.afk_seconds == 900)
    check('parse_line 累计时长', record is not None and record.total_seconds == 10800)

    join = R.parse_line('[2025-06-09 00:30:00] 玩家 Steve 进入服务器 (时间: 2025-06-09 00:30:00)')
    check('parse_line 进入记录', join is not None and join.event == R.EVENT_JOIN)

    afk = R.parse_line('[2025-06-09 01:00:00] 玩家 Steve 开始 AFK')
    check('parse_line AFK 开始', afk is not None and afk.event == R.EVENT_AFK_ON)
    afk_off = R.parse_line('[2025-06-09 01:15:00] 玩家 Steve 结束 AFK，本次 AFK: 15分钟0秒')
    check('parse_line AFK 结束', afk_off is not None and afk_off.event == R.EVENT_AFK_OFF
          and afk_off.afk_seconds == 900)

    check('parse_line 无关行', R.parse_line('[2025-06-10 09:05:00] 这是一条无关的日志') is None)
    check('parse_line 空行', R.parse_line('') is None)
    check('parse_line 无时间戳', R.parse_line('玩家 Steve 进入服务器') is None)
    full = R.parse_line('[2025-06-09 02:30:00] 玩家 Steve 退出服务器 | 本次游玩：1小时0分钟0秒')
    check('parse_line 全角冒号', full is not None and full.session_seconds == 3600)


def test_read_and_filter(directory):
    path = build_log_file(directory)
    all_records = R.read_records(path)
    check('read_records 条数', len(all_records) == 11, 'got {}'.format(len(all_records)))
    check('read_records 排序', all(r.timestamp <= s.timestamp
                                   for r, s in zip(all_records, all_records[1:])))

    default_range = R.default_range(NOW)
    check('默认范围起点=昨天0点', default_range.start == datetime(2025, 6, 9, 0, 0, 0))
    check('默认范围终点=现在', default_range.end == NOW)
    picked = R.filter_records(all_records, default_range.start, default_range.end)
    check('默认范围命中 7 条', len(picked) == 7, 'got {}'.format(len(picked)))
    names = sorted(set(r.player for r in picked))
    check('默认范围玩家', names == ['Alex', 'Steve'], 'got {}'.format(names))

    day_range, _ = R.parse_range_args('2025-06-08', now=NOW)
    day_picked = R.filter_records(all_records, day_range.start, day_range.end)
    check('指定日期命中 2 条', len(day_picked) == 2, 'got {}'.format(len(day_picked)))

    today_range, _ = R.parse_range_args('今天', now=NOW)
    today_picked = R.filter_records(all_records, today_range.start, today_range.end)
    check('今天命中 2 条', len(today_picked) == 2, 'got {}'.format(len(today_picked)))

    week_range, _ = R.parse_range_args('近7天', now=NOW)
    check('近7天起点', week_range.start == datetime(2025, 6, 4, 0, 0, 0))
    check('近7天命中 11 条',
          len(R.filter_records(all_records, week_range.start, week_range.end)) == 11)

    missing = R.read_records(os.path.join(directory, 'not-exist.log'))
    check('read_records 文件不存在返回空', missing == [])


def test_range_args():
    rng, lines = R.parse_range_args('', now=NOW)
    check('无参数=默认范围', rng.start == datetime(2025, 6, 9, 0, 0, 0) and lines is None)

    rng, lines = R.parse_range_args('上限50', now=NOW)
    check('上限50', lines == 50 and rng.start == datetime(2025, 6, 9, 0, 0, 0))

    rng, lines = R.parse_range_args('最近3天 上限20', now=NOW)
    check('组合参数', lines == 20 and rng.start == datetime(2025, 6, 8, 0, 0, 0))

    rng, _ = R.parse_range_args('6-01', now=NOW)
    check('当年简写日期', rng.start == datetime(2025, 6, 1, 0, 0, 0))

    rng, _ = R.parse_range_args('昨天', now=NOW)
    check('昨天=整天', rng.start == datetime(2025, 6, 9, 0, 0, 0)
          and rng.end.hour == 23 and rng.end.minute == 59)

    for bad in ('2025-13-01', '2025-02-30', '3000-01-01', '瞎写的参数'):
        try:
            R.parse_range_args(bad, now=NOW)
            check('非法参数应报错: ' + bad, False)
        except ValueError:
            check('非法参数应报错: ' + bad, True)


def test_summary_and_report(directory):
    path = build_log_file(directory)
    all_records = R.read_records(path)
    rng = R.default_range(NOW)
    picked = R.filter_records(all_records, rng.start, rng.end)

    rows = R.summarize(picked)
    check('统计行数=2', len(rows) == 2, 'got {}'.format(rows))
    check('统计排序（按时长降序）', rows[0][0] == 'Alex' and rows[0][2] == 8 * 3600 + 600,
          'got {}'.format(rows))
    check('统计 Steve', any(r[0] == 'Steve' and r[1] == 1 and r[2] == 7200 for r in rows))

    clean_totals = {'Steve': 10800.0, 'Alex': 3600.0, 'Old': 0.0, 'Two': 0.0}
    report = R.build_report(picked, rng, totals=clean_totals, title='游玩历史', max_lines=None)
    check('报告含标题', '===== 游玩历史 =====' in report)
    check('报告含范围', '2025-06-09 00:00 至 06-10 15:30:00' in report)
    check('报告含 Steve 段', '【Steve】' in report)
    check('报告含离开时长', '本次 2小时 | AFK 15分钟 | 活跃 1小时45分钟' in report,
          'got:\n{}'.format(report))
    check('报告含 Alex 时长', '本次 8小时10分钟 | AFK 1小时 | 活跃 7小时10分钟' in report)
    check('报告含统计标题', '----- 统计 -----' in report)
    check('报告含统计明细', 'Alex: 上线 1 次，合计 8小时10分钟' in report)
    check('报告不含未上线玩家累计（都在范围内）', '未上线玩家的历史累计' not in report,
          'got:\n{}'.format(report))
    check('报告不含仅进入提示（都有退出记录）', '仅进入、未记录退出' not in report,
          'got:\n{}'.format(report))

    only_absent = {'Steve': 10800.0, 'Ghost': 5400.0}
    report2 = R.build_report(picked, rng, totals=only_absent, title='游玩历史', max_lines=None)
    check('报告含未上线玩家累计', '未上线玩家的历史累计' in report2 and 'Ghost: 1小时30分钟' in report2,
          'got:\n{}'.format(report2))

    partial_range = R.TimeRange(datetime(2025, 6, 10, 9, 0, 0), NOW)
    partial = R.filter_records(all_records, partial_range.start, partial_range.end)
    report3 = R.build_report(partial, partial_range, totals=None, max_lines=None)
    check('报告含仅进入提示', '仅进入、未记录退出: Steve' in report3, 'got:\n{}'.format(report3))

    empty = R.build_report([], rng)
    check('空结果提示', '没有找到该时间段内的游玩记录' in empty)

    limited = R.build_report(picked, rng, max_lines=2)
    check('条数限制', '只显示最后 2 条' in limited)

    print('\n----- 报告样例 -----')
    print(report)
    print('---------------------')


def test_totals():
    directory = tempfile.mkdtemp(prefix='playtime-totals-')
    data_file = os.path.join(directory, 'playtime_data.json')
    with open(data_file, 'w', encoding='utf-8') as handle:
        json.dump({'total_playtime': {'Steve': 10800, 'Alex': 3600.5, '坏数据': 'x'},
                   'total_afk': {'Steve': 900}}, handle, ensure_ascii=False)
    totals = R.load_totals(data_file)
    check('load_totals 读取数字', totals.get('Steve') == 10800.0)
    check('load_totals 保留小数', totals.get('Alex') == 3600.5)
    check('load_totals 跳过非法值', '坏数据' not in totals)
    check('load_totals 文件不存在', R.load_totals(os.path.join(directory, 'nope.json')) == {})


def main():
    print('== 数据层测试 ==')
    directory = tempfile.mkdtemp(prefix='qqbridge-test-')
    test_duration()
    test_extract_duration()
    test_parse_line()
    test_read_and_filter(directory)
    test_range_args()
    test_summary_and_report(directory)
    test_totals()

    print('\n通过 {} 项，失败 {} 项'.format(len(PASSED), len(FAILED)))
    if FAILED:
        print('失败列表:')
        for item in FAILED:
            print('  - {}'.format(item))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

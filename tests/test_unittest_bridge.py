"""让三个独立测试脚本也能被 `python -m unittest discover -s tests` 发现并执行。

测试逻辑仍然写在可以单独运行的脚本里（`python tests/test_records.py` 也能跑），
这里只包一层适配 CI 的 unittest 发现机制，用子进程运行以保证彼此隔离
（假 NapCat 要占端口、端到端测试还要改工作目录）。
"""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
PY = sys.executable or 'python'


def run_script(script, extra_args=None, cwd=None):
    """在独立子进程里跑一个脚本，返回 (退出码, 合并输出)。"""
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    completed = subprocess.run(
        [PY, os.path.join(HERE, script)] + list(extra_args or []),
        cwd=cwd or HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return completed.returncode, completed.stdout.decode('utf-8', 'replace')


class OfflineTests(unittest.TestCase):
    maxDiff = None

    def _run_and_check(self, script, extra_args=None, cwd=None, marker='失败 0 项'):
        code, output = run_script(script, extra_args=extra_args, cwd=cwd)
        if code != 0:
            failures = [line for line in output.splitlines() if '[FAIL]' in line or '[问题]' in line]
            detail = '\n'.join(failures) if failures else output[-4000:]
            self.fail('{} 退出码 {}，失败项:\n{}'.format(script, code, detail))
        self.assertIn(marker, output,
                      '{} 的输出里没有 {!r}: \n{}'.format(script, marker, output[-2000:]))

    def test_00_format_strings(self):
        """静态检查：str.format 的占位符与参数是否匹配"""
        code, output = run_script(
            os.path.join('..', 'tools', 'check_format_strings.py'),
            extra_args=[PLUGIN_DIR], cwd=HERE)
        if code != 0:
            problems = [line for line in output.splitlines()
                        if '[问题]' in line or line.strip().startswith('第 ')]
            self.fail('str.format 占位符检查失败:\n{}'.format('\n'.join(problems) or output))
        self.assertIn('发现 0 处问题', output, output[-1000:])

    def test_01_records(self):
        """数据层：日志解析 / 时间范围 / 统计 / 格式化"""
        self._run_and_check('test_records.py')

    def test_02_ws_client(self):
        """WebSocket / OneBot：握手、掩码、超长帧、分段、重连"""
        self._run_and_check('test_ws_client.py')

    def test_03_plugin_e2e(self):
        """端到端：记录器 -> 日志 -> 解析 -> QQ 回复（假 NapCat + 假 MCDR）"""
        self._run_and_check('test_plugin_e2e.py')


if __name__ == '__main__':
    unittest.main(verbosity=2)

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
PY = sys.executable or 'python'


def run_script(script):
    """在独立子进程里跑一个测试脚本，返回 (退出码, 合并输出)。"""
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    completed = subprocess.run(
        [PY, os.path.join(HERE, script)],
        cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return completed.returncode, completed.stdout.decode('utf-8', 'replace')


class OfflineTests(unittest.TestCase):
    maxDiff = None

    def _run_and_check(self, script):
        code, output = run_script(script)
        if code != 0:
            failures = [line for line in output.splitlines() if '[FAIL]' in line]
            detail = '\n'.join(failures) if failures else output[-4000:]
            self.fail('{} 退出码 {}，失败项:\n{}'.format(script, code, detail))
        self.assertIn('失败 0 项', output,
                      '{} 的输出里没有“失败 0 项”: \n{}'.format(script, output[-2000:]))

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

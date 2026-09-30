"""静态检查：找出 str.format 的占位符与参数不匹配的地方。

为什么需要它：默认通知模板（notify.join_text 等）是字符串里带 {xxx} 占位符，
代码里用 .format(player=..., total=...) 去填。如果模板改了占位符、代码没跟着改，
只有真正触发播报时才会在运行时炸，测试很容易漏掉。

用法：
    python tools/check_format_strings.py            # 检查插件源码
    python tools/check_format_strings.py <目录>     # 检查指定目录

退出码 0 = 没发现问题。
"""

import ast
import os
import re
import string
import sys

FIELD_RE = re.compile(r'\{([^{}]*)\}')
SKIP_DIRS = {'__pycache__', '.git', 'dist', 'build', 'node_modules'}


def split_top_level(text, sep=','):
    """按顶层逗号切分（忽略括号/引号里的逗号）。"""
    parts = []
    depth = 0
    quote = None
    current = []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in '"\'':
            quote = char
            current.append(char)
            continue
        if char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        if char == sep and depth == 0:
            parts.append(''.join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append(''.join(current))
    return parts


def _literal_string(node):
    """取出字符串字面量（含隐式拼接与 f-string 之外的情况）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):     # f-string：占位符已被求值，跳过
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is None or right is None:
            return None
        return left + right
    return None


def parse_fields(template):
    """返回 (位置参数个数, 关键字名集合, 是否有 *args/**kwargs)。"""
    positional = 0
    keywords = set()
    star = False
    for match in FIELD_RE.finditer(template):
        field = match.group(1).strip()
        if not field:
            positional += 1
            continue
        if field.startswith('{') or field.endswith('}'):
            continue
        name = field.split(':')[0].split('!')[0].strip()
        if name == '':
            positional += 1
        elif name.isdigit():
            positional = max(positional, int(name) + 1)
        elif name == '*' or name.startswith('**'):
            star = True
        else:
            keywords.add(name)
    return positional, keywords, star


def check_source(path):
    problems = []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            source = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        return ['无法读取: {}'.format(exc)]
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return ['语法错误: {}'.format(exc)]

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'format'):
            continue
        template = _literal_string(node.func.value)
        if template is None:
            continue
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue

        positional, keywords, star = parse_fields(template)
        if star:
            continue

        given_positional = len(node.args)
        given_keywords = set(kw.arg for kw in node.keywords if kw.arg)

        if given_keywords:
            # 全部用关键字传参：模板里的 {name} 必须都有对应关键字
            missing = sorted(name for name in keywords if name not in given_keywords)
            if missing:
                problems.append('第 {} 行: 模板需要关键字 {} 但只传了 {}'.format(
                    node.lineno, missing, sorted(given_keywords)))
            continue

        # 位置传参：模板需要的个数不能超过传入的个数
        if positional > given_positional:
            problems.append('第 {} 行: 模板需要 {} 个位置参数，实际传入 {} 个'.format(
                node.lineno, positional, given_positional))
    return problems


def iter_py_files(root):
    if os.path.isfile(root):
        yield root
        return
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith('.py'):
                yield os.path.join(current, name)


def main():
    roots = sys.argv[1:] or [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    total = 0
    checked = 0
    for root in roots:
        for path in sorted(iter_py_files(root)):
            checked += 1
            problems = check_source(path)
            if problems:
                total += len(problems)
                print('[问题] {}'.format(os.path.relpath(path, os.getcwd())))
                for problem in problems:
                    print('    {}'.format(problem))
    print('检查了 {} 个文件，发现 {} 处问题'.format(checked, total))
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())

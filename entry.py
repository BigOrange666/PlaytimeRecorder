"""插件入口（mcdreforged.plugin.json 里的 entrypoint 指向本模块）。

为什么要单独一个 entry.py，而不是直接把 entrypoint 写成 __init__：

MCDR 加载插件有两种形态——
    * 解压目录：把 <插件目录> 的**父目录**加进 sys.path，再 import <目录名>
        → 插件是一个叫 <目录名> 的包（目录名可能是 PlaytimeRecorder）
    * .mcdr  ：把解压出来的目录自己加进 sys.path，再 import metadata 的 entrypoint
        → entry.py 是顶层模块，__init__.py 不会被自动执行

两种形态下包名完全不同，插件主体里的 `from qqbridge.recorder import ...` 就未必能解析。
所以这里先把插件目录注册成一个**固定名字**的包（PACKAGE_NAME），再按包的方式加载
__init__.py，让插件主体在两种形态下有完全一致、可预测的导入环境。
"""

import importlib.util
import os
import sys
import types

# 插件包的内部别名。改它不影响插件 id（id 在 mcdreforged.plugin.json 里）。
PACKAGE_NAME = 'playtime_recorder'

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bind_package():
    """保证 sys.modules[PACKAGE_NAME] 是一个指向本目录、带 __path__ 的包对象。

    总是手工构造 + 用 spec 加载 __init__.py，不用 importlib.import_module，
    这样可以完全掌控包名与 __path__，不受目录实际名字影响（目录名可能是
    PlaytimeRecorder、plugins 之类，做不了包名）。
    """
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None and getattr(existing, '__path__', None):
        return existing

    package = types.ModuleType(PACKAGE_NAME)
    package.__file__ = os.path.join(_HERE, '__init__.py')
    package.__path__ = [_HERE]
    package.__package__ = PACKAGE_NAME
    sys.modules[PACKAGE_NAME] = package
    return package


def _load_plugin():
    package = _bind_package()
    if hasattr(package, 'on_load'):        # 已经加载过（MCDR 两种形态可能都走到）
        return package, True

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, os.path.join(_HERE, '__init__.py'),
        submodule_search_locations=[_HERE])
    if spec is None or spec.loader is None:
        raise ImportError('无法加载插件入口: {}'.format(_HERE))
    module = sys.modules[PACKAGE_NAME]
    spec.loader.exec_module(module)
    return module, True


_plugin, _loaded = _load_plugin()


def entry():
    """MCDR 调用的入口方法。"""
    return _plugin


# 把包内的 MCDR 钩子提升到本模块，兼容直接在 entrypoint 模块上找钩子的版本
on_load = getattr(_plugin, 'on_load', None)
on_unload = getattr(_plugin, 'on_unload', None)
on_server_startup = getattr(_plugin, 'on_server_startup', None)
on_player_joined = getattr(_plugin, 'on_player_joined', None)
on_player_left = getattr(_plugin, 'on_player_left', None)
on_info = getattr(_plugin, 'on_info', None)

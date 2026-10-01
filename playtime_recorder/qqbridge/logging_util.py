"""日志适配：优先使用 MCDR 的 logger，独立运行时回退到标准库 logging。"""

import logging
import os
import sys

__version__ = '2.0.0'

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_PKG_DIR)
_PLUGINS_DIR = os.path.dirname(_PLUGIN_DIR)

for _candidate in (_PLUGIN_DIR, _PLUGINS_DIR):
    if _candidate and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

_std_logger = None


def _get_std_logger():
    """独立运行（没有 MCDR）时用的标准库 logger。"""
    global _std_logger
    if _std_logger is None:
        _std_logger = logging.getLogger('qqbridge')
    logger = _std_logger
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%H:%M:%S')
        )
        logger.addHandler(handler)
    if logger.level in (logging.NOTSET, logging.DEBUG):
        logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _render(msg, args):
    """把 '{}' 风格的占位符渲染成最终字符串；绝不允许抛异常。"""
    if not args:
        return str(msg)
    try:
        return str(msg).format(*args)
    except Exception:
        pass
    try:
        return str(msg) % (args if len(args) > 1 else args[0])
    except Exception:
        return '{} {}'.format(msg, ' '.join(str(a) for a in args))


class _Adapter(object):
    """包装 MCDR 的 logger：它只接受一个已经格式化好的字符串。"""

    def __init__(self, logger):
        self._logger = logger

    def _log(self, level, msg, args):
        method = getattr(self._logger, level, None)
        if method is None:
            method = getattr(self._logger, 'info', None)
        if method is None:
            return
        try:
            method(_render(msg, args))
        except Exception:
            pass

    def debug(self, msg, *args):
        self._log('debug', msg, args)

    def info(self, msg, *args):
        self._log('info', msg, args)

    def warning(self, msg, *args):
        self._log('warning', msg, args)

    def error(self, msg, *args):
        self._log('error', msg, args)

    def exception(self, msg, *args):
        if hasattr(self._logger, 'exception'):
            self._log('exception', msg, args)
        else:
            self._log('error', msg, args)


class _StdAdapter(_Adapter):
    """包装标准库 logging.Logger：先自己渲染，再交给标准库。

    若把 '{}' 模板连同参数直接传给标准库，它会按 % 格式化并抛 TypeError，
    再被 logging 内部的 handleError 打成一大段 traceback 到 stderr。
    """

    def _log(self, level, msg, args):
        method = getattr(self._logger, level, None)
        if method is None:
            return
        try:
            method(_render(msg, args))
        except Exception:
            pass


class SafeLogger(object):
    """永远可用的 logger：内部任何异常都不会影响主流程。"""

    def __init__(self, logger=None):
        if isinstance(logger, SafeLogger):
            self._inner = logger._inner
        elif logger is None:
            self._inner = _StdAdapter(_get_std_logger())
        elif isinstance(logger, logging.Logger):
            self._inner = _StdAdapter(logger)
        elif hasattr(logger, 'info') and hasattr(logger, 'warning'):
            self._inner = _Adapter(logger)
        else:
            self._inner = _StdAdapter(_get_std_logger())

    def debug(self, msg, *args):
        try:
            self._inner.debug(msg, *args)
        except Exception:
            pass

    def info(self, msg, *args):
        try:
            self._inner.info(msg, *args)
        except Exception:
            pass

    def warning(self, msg, *args):
        try:
            self._inner.warning(msg, *args)
        except Exception:
            pass

    def error(self, msg, *args):
        try:
            self._inner.error(msg, *args)
        except Exception:
            pass

    def exception(self, msg, *args):
        try:
            self._inner.exception(msg, *args)
        except Exception:
            pass

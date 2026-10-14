"""
跨线程主线程调用桥。

背景：pystray / APScheduler 工作线程里调用 QTimer.singleShot()，
定时器会挂在调用线程（无 Qt 事件循环）上，回调永远不会触发——
导致托盘菜单点击、数据刷新全部失灵。

方案：QObject + Signal(object) + QueuedConnection。
信号从任意线程 emit，Qt 自动把调用排到对象所属线程（主线程）执行。
必须在主线程先调用 init_bridge()。

异常策略：_execute 里的异常写入应用日志（monitor.log），
而非只打 stderr——窗口化 exe 中 stderr 不可见，避免静默失败。
"""
from __future__ import annotations
import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal

_bridge: "MainThreadBridge | None" = None


def _ui_log():
    try:
        from .utils.logger import setup_logger
        return setup_logger(Path.home() / "AppData" / "Roaming" / "AgentMonitor" / "logs")
    except Exception:
        return None


class MainThreadBridge(QObject):
    _invoke = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 跨线程 emit 时 Qt 自动走 queued connection
        self._invoke.connect(self._execute, Qt.QueuedConnection)

    def post(self, fn: Callable, *args, **kwargs):
        """线程安全：任意线程调用，fn 会在主线程事件循环中执行"""
        self._invoke.emit((fn, args, kwargs))

    def _execute(self, payload):
        fn, args, kwargs = payload
        try:
            fn(*args, **kwargs)
        except Exception:
            name = getattr(fn, "__name__", repr(fn))
            log = _ui_log()
            if log is not None:
                log.error("[ui] 主线程回调 %s 异常：\n%s", name, traceback.format_exc())
            traceback.print_exc()


def init_bridge() -> MainThreadBridge:
    """只能在主线程调用（QApplication 创建之后、其它线程启动之前）"""
    global _bridge
    if _bridge is None:
        _bridge = MainThreadBridge()
    return _bridge


def post(fn: Callable, *args, **kwargs):
    if _bridge is None:
        # 兜底：未显式初始化时，若当前就在 Qt 主线程，直接执行
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is not None and app.thread().isCurrentThread():
            fn(*args, **kwargs)
            return
        raise RuntimeError("qtbridge 未初始化：请先在主线程调用 init_bridge()")
    _bridge.post(fn, *args, **kwargs)


# 兼容旧调用名
def main_thread_call(fn: Callable, *args, **kwargs):
    post(fn, *args, **kwargs)

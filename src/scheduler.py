"""
调度器：定时拉取各平台用量；对外暴露 add_listener / get_snapshot。
"""
from __future__ import annotations
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from apscheduler.schedulers.background import BackgroundScheduler

from .models import AccountStatus, PlatformId, UsageSnapshot
from .platforms.base import BasePlatform
from .utils.logger import setup_logger
from .config import load_config, save_config


_LOG = None


def _logger():
    global _LOG
    if _LOG is None:
        _LOG = setup_logger(Path.home() / "AppData" / "Roaming" / "AgentMonitor" / "logs")
    return _LOG


def build_platform(platform: PlatformId, credential: str = "") -> BasePlatform | None:
    """CLI 模式下 credential 允许为空（复用各 CLI 本地登录态）"""
    from .platforms.chatgpt import ChatGPTPlatform
    from .platforms.volcengine import VolcenginePlatform
    from .platforms.qwen import QwenPlatform
    mapping = {
        PlatformId.CHATGPT: ChatGPTPlatform,
        PlatformId.VOLCENGINE: VolcenginePlatform,
        PlatformId.QWEN: QwenPlatform,
    }
    cls = mapping.get(platform)
    if cls is None:
        return None
    try:
        return cls(credential or "")
    except Exception as e:
        _logger().warning(f"构造 {platform.value} 适配器失败：{e}")
        return None


class MonitorScheduler:
    """持有最新 snapshot + 定时刷新 + 监听器回调"""

    def __init__(self):
        self.cfg = load_config()
        self._snapshots: dict[PlatformId, UsageSnapshot] = {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[PlatformId, UsageSnapshot]], None]] = []
        self._sched = BackgroundScheduler(daemon=True)
        self._running = False

    # -------- 订阅 --------
    def add_listener(self, cb: Callable[[dict[PlatformId, UsageSnapshot]], None]):
        with self._lock:
            self._listeners.append(cb)
            cb(self._snapshots)  # 立即推一帧

    def remove_listener(self, cb):
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

    def get_snapshots(self) -> dict[PlatformId, UsageSnapshot]:
        with self._lock:
            return dict(self._snapshots)

    # -------- 控制 --------
    def start(self):
        if self._running:
            return
        self._sched.start()
        # 立即拉一帧（后台线程，不阻塞 UI）
        self._sched.add_job(
            self._tick,
            "interval",
            seconds=max(15, int(self.cfg.poll_interval_sec)),
            next_run_time=datetime.now(),
            id="monitor",
            max_instances=1,
            coalesce=True,
        )
        self._running = True
        _logger().info("MonitorScheduler started, interval=%ss", self.cfg.poll_interval_sec)

    def stop(self):
        if not self._running:
            return
        try:
            self._sched.shutdown(wait=False)
        except Exception:
            pass
        self._running = False

    def reload(self, new_cfg):
        """配置更新后调用，重启定时器"""
        self.cfg = new_cfg
        try:
            self._sched.remove_job("monitor")
        except Exception:
            pass
        self._sched.add_job(
            self._tick,
            "interval",
            seconds=max(15, int(self.cfg.poll_interval_sec)),
            next_run_time=datetime.now(),
            id="monitor",
            max_instances=1,
            coalesce=True,
        )

    # -------- 内部 --------
    def _tick(self):
        # 重新读配置，避免用户中途改了 credential 后还跑老任务
        self.cfg = load_config()

        platforms: list[PlatformId] = list(PlatformId)
        for pid in platforms:
            cred = self.cfg.get_credential(pid) or ""
            # CLI 模式：credential 可为空，适配器自行检测 CLI 安装/登录态
            adapter = build_platform(pid, cred)
            if adapter is None:
                continue
            try:
                snap = adapter.fetch()
            except Exception as e:
                snap = UsageSnapshot(platform=pid, status=AccountStatus.UNKNOWN, error=f"拉取异常：{e}")
            with self._lock:
                self._snapshots[pid] = snap
            _logger().info(f"[{pid.value}] remain={snap.remaining_percent} status={snap.status}")

        self._emit()

    def _emit(self):
        with self._lock:
            snap = dict(self._snapshots)
            cbs = list(self._listeners)
        for cb in cbs:
            try:
                cb(snap)
            except Exception as e:
                _logger().warning(f"listener error: {e}")

"""
托盘控制器 —— 基于 QSystemTrayIcon（Qt 原生托盘）。

为什么弃用 pystray：
  pystray 在独立线程维护 Win32 消息泵，与 Qt 主循环并存时
  右键菜单响应不稳定（菜单不弹出 / 点击无反应）。
  QSystemTrayIcon 与 QApplication 同线程，菜单与点击 100% 可靠。

数据流：
  APScheduler 工作线程 → add_listener → qtbridge.post → 主线程刷新图标/菜单。
"""
from __future__ import annotations
import json
from typing import Callable, Optional

from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .icon_render import render_tray_icon
from .models import AccountStatus, PlatformId, UsageSnapshot
from .qtbridge import post
from .scheduler import MonitorScheduler

SHORT_LABEL = {
    PlatformId.CHATGPT: "ChatGPT",
    PlatformId.VOLCENGINE: "火山",
    PlatformId.QWEN: "千问 Token Plan",
}


def aggregate_status(snaps: dict[PlatformId, UsageSnapshot]) -> tuple[AccountStatus, Optional[float]]:
    """最差状态 + 各平台剩余百分比平均值"""
    if not snaps:
        return AccountStatus.UNKNOWN, None
    order = {AccountStatus.UNKNOWN: 0, AccountStatus.ACTIVE: 1,
             AccountStatus.WARNING: 2, AccountStatus.CRITICAL: 3,
             AccountStatus.EXPIRED: 4}
    worst = AccountStatus.UNKNOWN
    for s in (snap.status for snap in snaps.values()):
        if order[s] > order[worst]:
            worst = s
    percents = [s.remaining_percent for s in snaps.values() if s.remaining_percent is not None]
    avg = sum(percents) / len(percents) if percents else None
    return worst, avg


class TrayController:
    def __init__(self, scheduler: MonitorScheduler,
                 on_show_detail: Callable[[], None],
                 on_show_settings: Callable[[], None],
                 on_force_refresh: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_toggle_card: Callable[[bool], None]):
        self._scheduler = scheduler
        self._on_show_detail = on_show_detail
        self._on_toggle_card = on_toggle_card

        self.tray = QSystemTrayIcon()
        self.tray.setToolTip("Agent Monitor · 启动中…")

        # ---------- 菜单 ----------
        self.menu = QMenu()
        self.menu.setStyleSheet(
            "QMenu { background: #1e1e1e; color: #e8eaed; border: 1px solid #3a3a3a; "
            "padding: 6px; }"
            "QMenu::item { padding: 6px 24px; border-radius: 4px; }"
            "QMenu::item:selected { background: #3a3a3a; }"
            "QMenu::separator { height: 1px; background: #3a3a3a; margin: 4px 8px; }"
        )

        self.act_detail = self.menu.addAction("打开详情")
        self.act_detail.triggered.connect(on_show_detail)

        self.act_card = self.menu.addAction("悬浮卡片")
        self.act_card.setCheckable(True)
        self.act_card.triggered.connect(lambda checked: on_toggle_card(checked))

        self.act_refresh = self.menu.addAction("立即刷新")
        self.act_refresh.triggered.connect(on_force_refresh)

        self.act_settings = self.menu.addAction("设置")
        self.act_settings.triggered.connect(on_show_settings)

        self.menu.addSeparator()
        self._platform_actions: dict[PlatformId, QAction] = {}
        for pid in PlatformId:
            act = self.menu.addAction(f"{SHORT_LABEL[pid]}：—")
            act.setEnabled(False)
            self._platform_actions[pid] = act
        self.menu.addSeparator()

        self.act_quit = self.menu.addAction("退出")
        self.act_quit.triggered.connect(on_quit)

        self.tray.setContextMenu(self.menu)

        # 单击 / 双击托盘图标 → 详情
        self.tray.activated.connect(self._on_activated)

        # 初始图标
        self.tray.setIcon(self._render_icon(AccountStatus.UNKNOWN, None))
        self.tray.show()

        # 数据订阅（回调来自 APScheduler 线程，经 qtbridge 转主线程）
        scheduler.add_listener(self._on_snapshots)

    # ---------------- 回调 ----------------
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._on_show_detail()

    def _on_snapshots(self, snaps):
        post(self._apply, snaps)

    # ---------------- 刷新（主线程） ----------------
    def set_card_checked(self, visible: bool):
        self.act_card.setChecked(visible)

    def _apply(self, snaps: dict[PlatformId, UsageSnapshot]):
        worst, avg = aggregate_status(snaps)
        self.tray.setIcon(self._render_icon(worst, avg))

        try:
            from .qtbridge import _ui_log
            log = _ui_log()
            if log is not None:
                log.info("[ui] tray applied")
        except Exception:
            pass

        # tooltip
        parts = []
        for pid in PlatformId:
            snap = snaps.get(pid)
            if snap is None or snap.remaining_percent is None:
                parts.append(f"{SHORT_LABEL[pid]}: —")
            else:
                parts.append(f"{SHORT_LABEL[pid]}: {int(round(snap.remaining_percent))}%")
        self.tray.setToolTip("Agent Monitor  ·  " + ("  ".join(parts) if parts else "未配置"))

        # 菜单里的各平台行
        for pid in PlatformId:
            act = self._platform_actions[pid]
            snap = snaps.get(pid)
            if snap is None:
                act.setText(f"{SHORT_LABEL[pid]}：未连接")
                continue
            rem = "—" if snap.remaining_percent is None else f"{int(round(snap.remaining_percent))} %"
            err = " ⚠" if snap.error else ""
            act.setText(f"{SHORT_LABEL[pid]}：{rem}{err}")

    @staticmethod
    def _render_icon(status: AccountStatus, avg: Optional[float]) -> QIcon:
        img = render_tray_icon(status, avg, size=64)
        try:
            from PIL.ImageQt import ImageQt  # type: ignore
            pix = QPixmap.fromImage(ImageQt(img))
            return QIcon(pix)
        except Exception:
            return QIcon()

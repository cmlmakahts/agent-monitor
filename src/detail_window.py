"""
详情面板：用 PySide6 渲染，多 tab 显示三个平台的详细用量。
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QBrush, QLinearGradient
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QProgressBar, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget, QFrame, QSizePolicy,
)

from .icon_render import render_status_dot, render_tray_icon, _STATUS_COLOR
from .models import AccountStatus, LimitWindow, ModelUsage, PlatformId, UsageSnapshot
from .qtbridge import post as main_post


# -------------------- 控件 --------------------
class StatusDot(QLabel):
    def __init__(self, status: AccountStatus, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self.set_status(status)

    def set_status(self, status: AccountStatus):
        from PIL.ImageQt import ImageQt  # type: ignore
        img = render_status_dot(status, 14)
        qimg = ImageQt(img)
        self.setPixmap(QPixmap.fromImage(qimg))


class BigGauge(QFrame):
    """大半圆仪表盘：剩余 % + 重置倒计时"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self._percent: Optional[float] = None
        self._plan_name: str = "—"
        self._reset_at: Optional[datetime] = None
        self._status: AccountStatus = AccountStatus.UNKNOWN

        # 倒计时刷新
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(1000)

    def update_data(self, snap: UsageSnapshot):
        self._percent = snap.remaining_percent
        self._plan_name = snap.plan_name or "未知订阅"
        self._reset_at = snap.reset_at
        self._status = snap.status
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        # 背景
        p.fillRect(self.rect(), QColor(33, 33, 33))

        # 渐变进度环
        cx, cy = w // 2, h - 30
        radius = min(w // 3, h // 2 - 10)

        # 背景环
        pen = p.pen()
        pen.setWidth(14)
        pen.setColor(QColor(60, 60, 60))
        p.setPen(pen)
        p.drawArc(cx - radius, cy - radius, radius * 2, radius * 2, 180 * 16, -180 * 16)  # 半圆

        if self._percent is not None:
            span = int(-180 * 16 * (max(0.0, min(100.0, self._percent)) / 100.0))
            color = QColor(*_STATUS_COLOR.get(self._status, _STATUS_COLOR[AccountStatus.UNKNOWN]))
            pen.setColor(color)
            p.setPen(pen)
            p.drawArc(cx - radius, cy - radius, radius * 2, radius * 2, 180 * 16, span)

        # 中心文字
        pen.setColor(QColor(255, 255, 255))
        p.setPen(pen)
        if self._percent is None:
            p.setFont(QFont("Segoe UI", 26, QFont.Bold))
            p.drawText(self.rect().adjusted(0, 20, 0, 0), Qt.AlignHCenter | Qt.AlignTop, "—")
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect().adjusted(0, h - 70, 0, 0), Qt.AlignHCenter | Qt.AlignBottom,
                       "暂无数据")
        else:
            p.setFont(QFont("Segoe UI", 38, QFont.Bold))
            p.drawText(self.rect().adjusted(0, 18, 0, 0), Qt.AlignHCenter | Qt.AlignTop,
                       f"{int(round(self._percent))}%")
            p.setFont(QFont("Segoe UI", 10))
            sub = self._plan_name
            if self._reset_at:
                delta = self._reset_at - datetime.now()
                if delta.total_seconds() > 0:
                    sub += f"  ·  {self._format_delta(delta)}"
            p.drawText(self.rect().adjusted(0, h - 70, 0, 0), Qt.AlignHCenter | Qt.AlignBottom,
                       sub)

    @staticmethod
    def _format_delta(d: timedelta) -> str:
        secs = int(d.total_seconds())
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"重置 {h}h{m}m"


class MetricCard(QFrame):
    """小卡片：单指标"""
    def __init__(self, title: str, value: str, sub: str = "", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #1e1e1e; border-radius: 10px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        t = QLabel(title)
        t.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        v = QLabel(value)
        v.setStyleSheet("color: #fff; font-size: 22px; font-weight: 600;")
        s = QLabel(sub)
        s.setStyleSheet("color: #bdc1c6; font-size: 11px;")
        layout.addWidget(t)
        layout.addWidget(v)
        layout.addWidget(s)
        self.value_label = v

    def set_value(self, v: str):
        self.value_label.setText(v)


class ModelRow(QFrame):
    def __init__(self, mu: ModelUsage, max_tokens: int, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QFrame { background: #262626; border-radius: 6px; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 10, 6)
        name = QLabel(mu.model)
        name.setStyleSheet("color: #fff; font-weight: 500;")
        name.setMinimumWidth(130)
        bar = QProgressBar()
        bar.setRange(0, max(max_tokens, 1))
        bar.setValue(mu.tokens)
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar.setStyleSheet(
            "QProgressBar { background: #3a3a3a; border: none; border-radius: 4px; }"
            "QProgressBar::chunk { background: #4d8cff; border-radius: 4px; }"
        )
        val = QLabel(f"{mu.calls} 次 · {self._fmt_tokens(mu.tokens)}")
        val.setStyleSheet("color: #bdc1c6; font-size: 11px;")
        val.setMinimumWidth(120)
        h.addWidget(name)
        h.addWidget(bar, 1)
        h.addWidget(val)

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        if n >= 10000:
            return f"{n/1000:.1f}K tokens"
        return f"{n} tokens"


class HistorySparkline(QFrame):
    """30天用量小柱图"""
    def __init__(self, history: dict[str, int], parent=None):
        super().__init__(parent)
        self._data = history
        self.setMinimumHeight(60)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if not self._data:
            return
        items = sorted(self._data.items())[-30:]
        if not items:
            return
        values = [v for _, v in items]
        maxv = max(values) or 1
        bar_w = max(2, self.width() // max(len(items), 1) - 1)
        for i, (_, v) in enumerate(items):
            h = int(self.height() * (v / maxv) * 0.85)
            x = i * (bar_w + 1)
            color = QColor(77, 140, 255, 220)
            p.fillRect(x, self.height() - h, bar_w, h, color)


# -------------------- 平台详情 Tab --------------------
_PLATFORM_NAME = {
    PlatformId.CHATGPT: "ChatGPT",
    PlatformId.VOLCENGINE: "火山方舟",
    PlatformId.QWEN: "千问 Token Plan",
}

class PlatformTab(QWidget):
    def __init__(self, platform: PlatformId, parent=None):
        super().__init__(parent)
        self.platform = platform
        self.setStyleSheet("background: #121212; color: #fff;")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        # 顶部：状态 + 标题
        head = QHBoxLayout()
        self.dot = StatusDot(AccountStatus.UNKNOWN)
        title = QLabel(_PLATFORM_NAME[platform])
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.email_label = QLabel("未连接")
        self.email_label.setStyleSheet("color: #9aa0a6;")
        head.addWidget(self.dot)
        head.addSpacing(6)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.email_label)
        root.addLayout(head)

        # 半圆仪表
        self.gauge = BigGauge()
        root.addWidget(self.gauge)

        # 多窗口余量（如 ChatGPT 的 5h 窗口 / 周额度）
        self.windows_container = QWidget()
        self.windows_layout = QVBoxLayout(self.windows_container)
        self.windows_layout.setContentsMargins(0, 0, 0, 0)
        self.windows_layout.setSpacing(6)
        self.windows_container.setVisible(False)
        root.addWidget(self.windows_container)

        # 今日指标卡
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_calls = MetricCard("今日调用", "0", "次")
        self.card_tokens = MetricCard("今日 Tokens", "0", "tokens")
        self.card_cost = MetricCard("今日费用", "—", "USD")
        cards.addWidget(self.card_calls)
        cards.addWidget(self.card_tokens)
        cards.addWidget(self.card_cost)
        root.addLayout(cards)

        # 按模型分布
        models_label = QLabel("模型用量分布")
        models_label.setStyleSheet("color: #9aa0a6; font-size: 12px; margin-top: 6px;")
        root.addWidget(models_label)
        self.models_container = QWidget()
        self.models_layout = QVBoxLayout(self.models_container)
        self.models_layout.setContentsMargins(0, 0, 0, 0)
        self.models_layout.setSpacing(4)
        sc_models = QScrollArea()
        sc_models.setWidget(self.models_container)
        sc_models.setWidgetResizable(True)
        sc_models.setStyleSheet("QScrollArea { border: none; }")
        sc_models.setMinimumHeight(120)
        root.addWidget(sc_models, 1)

        # 月度趋势
        trend_label = QLabel("近期用量趋势（最近 30 天）")
        trend_label.setStyleSheet("color: #9aa0a6; font-size: 12px;")
        root.addWidget(trend_label)
        self.sparkline = HistorySparkline({})
        root.addWidget(self.sparkline)

        self.error_bar = QLabel("")
        self.error_bar.setStyleSheet("color: #f4a261; font-size: 11px;")
        self.error_bar.setVisible(False)
        root.addWidget(self.error_bar)

    def update_snapshot(self, snap: UsageSnapshot):
        # 头部
        self.dot.set_status(snap.status)
        if snap.account_email:
            self.email_label.setText(f"{snap.account_email}  ·  {snap.plan_name or ''}")
        else:
            self.email_label.setText(snap.plan_name or "未连接")

        # 仪表
        self.gauge.update_data(snap)

        # 多窗口余量
        self._refresh_windows(snap.windows or [])

        # 卡片
        self.card_calls.set_value(f"{snap.calls_today:,}")
        self.card_tokens.set_value(self._fmt(snap.tokens_today))
        self.card_cost.set_value(f"${snap.cost_today_usd:.2f}" if snap.cost_today_usd else "—")

        # 模型分布
        self._refresh_models(snap.by_model or [])

        # 趋势
        self.sparkline._data = snap.history or {}
        self.sparkline.update()

        # 错误条
        if snap.error:
            self.error_bar.setText(snap.error)
            self.error_bar.setVisible(True)
        else:
            self.error_bar.setVisible(False)

    def _refresh_windows(self, windows: list[LimitWindow]):
        # 清空旧控件
        while self.windows_layout.count():
            it = self.windows_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        if not windows:
            self.windows_container.setVisible(False)
            return
        self.windows_container.setVisible(True)
        for win in windows:
            self.windows_layout.addWidget(self._make_window_row(win))

    def _make_window_row(self, win: LimitWindow) -> QWidget:
        row = QFrame()
        row.setStyleSheet("QFrame { background: #1c1c1c; border-radius: 8px; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 8, 12, 8)
        label = QLabel(win.label)
        label.setStyleSheet("color: #bdc1c6; font-size: 12px;")
        label.setMinimumWidth(70)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(round(win.remaining_percent or 0)))
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        # 颜色随剩余量：充足绿 / 偏低黄 / 紧张红
        rem = win.remaining_percent
        if rem is None:
            color = "#5f6368"
        elif rem < 5:
            color = "#ea4335"
        elif rem < 20:
            color = "#fbbc04"
        else:
            color = "#34a853"
        bar.setStyleSheet(
            "QProgressBar { background: #3a3a3a; border: none; border-radius: 4px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}"
        )
        sub = "—" if rem is None else f"剩 {rem:.0f}%"
        if win.resets_at:
            delta = win.resets_at - datetime.now()
            secs = int(delta.total_seconds())
            if secs > 0:
                sub += f" · {secs // 3600}h{(secs % 3600) // 60}m 后重置"
        val = QLabel(sub)
        val.setStyleSheet("color: #bdc1c6; font-size: 11px;")
        val.setMinimumWidth(150)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(label)
        h.addWidget(bar, 1)
        h.addWidget(val)
        return row

    def _refresh_models(self, rows: list[ModelUsage]):
        # 清空旧控件
        while self.models_layout.count():
            it = self.models_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        if not rows:
            ph = QLabel("尚无模型级数据")
            ph.setStyleSheet("color: #5f6368; font-size: 11px;")
            self.models_layout.addWidget(ph)
            return
        max_tokens = max((r.tokens for r in rows), default=1)
        for r in rows:
            self.models_layout.addWidget(ModelRow(r, max_tokens))
        self.models_layout.addStretch()

    @staticmethod
    def _fmt(n: int) -> str:
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M"
        if n >= 1000:
            return f"{n/1000:.1f}K"
        return str(n)


# -------------------- 主窗口 --------------------
class DetailWindow(QMainWindow):
    def __init__(self, on_open_settings, on_refresh, scheduler):
        super().__init__()
        self.setWindowTitle("Agent 订阅监控")
        self.resize(560, 720)
        self.setStyleSheet("QMainWindow { background: #121212; }")
        self._on_open_settings = on_open_settings
        self._on_refresh = on_refresh
        self._scheduler = scheduler

        central = QWidget()
        central.setStyleSheet("background: #121212;")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶栏
        bar = QHBoxLayout()
        bar.setContentsMargins(14, 10, 14, 10)
        ico = QLabel("·")
        ico.setStyleSheet("color: #4d8cff; font-size: 20px; font-weight: 800;")
        title = QLabel("Agent Monitor")
        title.setStyleSheet("color: #fff; font-size: 14px; font-weight: 600;")
        self.last_update = QLabel("尚未拉取")
        self.last_update.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        btn_refresh = QPushButton("刷新")
        btn_refresh.setFixedHeight(28)
        btn_refresh.setStyleSheet(
            "QPushButton { background: #2d2d2d; color: #fff; border: none; "
            "border-radius: 6px; padding: 0 12px; }"
            "QPushButton:hover { background: #3a3a3a; }"
        )
        btn_refresh.clicked.connect(self._on_refresh)
        btn_settings = QPushButton("设置")
        btn_settings.setFixedHeight(28)
        btn_settings.setStyleSheet(btn_refresh.styleSheet())
        btn_settings.clicked.connect(self._on_open_settings)
        bar.addWidget(ico)
        bar.addSpacing(6)
        bar.addWidget(title)
        bar.addStretch()
        bar.addWidget(self.last_update)
        bar.addSpacing(8)
        bar.addWidget(btn_refresh)
        bar.addSpacing(4)
        bar.addWidget(btn_settings)
        layout.addLayout(bar)

        # tab
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border: none; background: #121212; }"
            "QTabBar::tab { background: #1c1c1c; color: #bdc1c6; padding: 8px 14px; "
            "border-top-left-radius: 6px; border-top-right-radius: 6px; }"
            "QTabBar::tab:selected { background: #2a2a2a; color: #fff; }"
        )
        self.tab_map: dict[PlatformId, PlatformTab] = {}
        for pid in PlatformId:
            tab = PlatformTab(pid)
            self.tab_map[pid] = tab
            self.tabs.addTab(tab, _PLATFORM_NAME[pid])
        layout.addWidget(self.tabs, 1)

        # 监听
        self._scheduler.add_listener(self._on_snapshots)

    def _on_snapshots(self, snaps):
        # 任意线程调用 → 桥接到 Qt 主线程执行
        # （旧实现用 QTimer.singleShot，从 APScheduler 工作线程调用时定时器
        #   挂在无事件循环的线程上，回调永不触发——已废弃）
        main_post(self._apply_snapshots, snaps)

    def _apply_snapshots(self, snaps: dict):
        latest = None
        for pid, tab in self.tab_map.items():
            snap = snaps.get(pid)
            if snap is None:
                continue
            tab.update_snapshot(snap)
            latest = snap.fetched_at
        if latest:
            self.last_update.setText(f"更新于 {latest.strftime('%H:%M:%S')}")

    def closeEvent(self, ev):
        ev.ignore()
        self.hide()

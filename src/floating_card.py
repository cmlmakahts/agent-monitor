"""
悬浮卡片：贴在屏幕右下角（可拖动）的常驻额度卡片。

内容：三个厂商的限额百分比（ChatGPT 5h+周、火山 5h+周、千问仅额度）。
配色：正常绿色，剩余 < 5% 红色。
交互：鼠标按住标题栏拖动；✕ 隐藏（托盘菜单可重新唤出）。
"""
from __future__ import annotations
from typing import Callable, Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .models import AccountStatus, LimitWindow, PlatformId, UsageSnapshot


_PLATFORMS = [
    (PlatformId.CHATGPT, "ChatGPT"),
    (PlatformId.VOLCENGINE, "火山"),
    (PlatformId.QWEN, "千问"),
]

# 窗口 kind → 卡片短标签
_KIND_LABEL = {"5h": "5h", "weekly": "周", "monthly": "月", "quota": "额度", "daily": "日"}

_GREEN = QColor(52, 168, 83)     # 正常
_RED = QColor(234, 67, 53)      # < 5%
_GREY = QColor(95, 99, 104)     # 无数据


class Chip(QLabel):
    """单个限额 chip：『5h 91%』"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("")
        self.setStyleSheet(
            "QLabel { border-radius: 7px; padding: 1px 7px; font-size: 11px; font-weight: 600; }"
        )
        self._set_color(_GREY, "—")

    def _set_color(self, color: QColor, text: str):
        self.setText(text)
        c = color.name()
        self.setStyleSheet(
            f"QLabel {{ color: {c}; border: 1px solid {c}; border-radius: 7px; "
            f"padding: 1px 7px; font-size: 11px; font-weight: 600; background: transparent; }}"
        )

    def set_limit(self, label: str, percent: Optional[float]):
        if percent is None:
            self._set_color(_GREY, f"{label} —")
            return
        color = _RED if percent < 5.0 else _GREEN
        self._set_color(color, f"{label} {int(round(percent))}%")


class FloatingCard(QWidget):
    """
    参数:
        on_hide: 卡片被用户隐藏时回调（用于持久化 show_card=False）
        on_moved: 卡片被拖动后回调 (x, y)（用于持久化位置）
    """

    def __init__(self, on_hide: Callable[[], None], on_moved: Callable[[int, int], None],
                 parent=None):
        super().__init__(parent)
        self._on_hide = on_hide
        self._on_moved = on_moved
        self._drag_pos: Optional[QPoint] = None

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool           # 不在任务栏显示
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # ---------- 内容 ----------
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 10, 12)
        root.setSpacing(6)

        # 标题栏（也是拖动区）
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("Agent 额度")
        title.setStyleSheet("color: #e8eaed; font-size: 12px; font-weight: 700; background: transparent;")
        self.last_update = QLabel("")
        self.last_update.setStyleSheet("color: #5f6368; font-size: 10px; background: transparent;")
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(18, 18)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet(
            "QPushButton { color: #9aa0a6; border: none; border-radius: 4px;"
            "font-size: 12px; background: transparent; }"
            "QPushButton:hover { color: #fff; background: #3a3a3a; }"
        )
        btn_close.clicked.connect(self._hide_requested)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.last_update)
        head.addWidget(btn_close)
        root.addLayout(head)

        # 三厂商行
        self._rows: dict[PlatformId, tuple[QLabel, list[Chip]]] = {}
        for pid, name in _PLATFORMS:
            row = QHBoxLayout()
            row.setSpacing(6)
            label = QLabel(name)
            label.setFixedWidth(52)
            label.setStyleSheet(
                "color: #bdc1c6; font-size: 12px; font-weight: 600; background: transparent;"
            )
            chips = [Chip() for _ in range(2)]  # 最多两列：5h + 周（千问只有额度）
            row.addWidget(label)
            for c in chips:
                row.addWidget(c)
            row.addStretch()
            root.addLayout(row)
            self._rows[pid] = (label, chips)

        self.adjustSize()

    # ---------- 数据更新 ----------
    def update_snapshots(self, snaps: dict[PlatformId, UsageSnapshot]):
        for pid, (label, chips) in self._rows.items():
            snap = snaps.get(pid)
            if snap is None or (not snap.windows and snap.remaining_percent is None):
                for c in chips:
                    c.set_limit("", None)
                continue
            windows = list(snap.windows)
            if not windows and snap.remaining_percent is not None:
                # 无 windows 时退回主余量
                windows = [LimitWindow(label="额度", kind="quota",
                                       remaining_percent=snap.remaining_percent,
                                       resets_at=snap.reset_at)]
            for i, chip in enumerate(chips):
                if i < len(windows):
                    w = windows[i]
                    chip.set_limit(_KIND_LABEL.get(getattr(w, "kind", ""), getattr(w, "label", "?")),
                                   getattr(w, "remaining_percent", None))
                else:
                    chip.setVisible(False)
        latest = max((s.fetched_at for s in snaps.values()), default=None)
        if latest:
            # 精确到秒：让用户能直观确认卡片在自动刷新
            self.last_update.setText(latest.strftime("%H:%M:%S"))
        self.adjustSize()

    # ---------- 拖动 ----------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag_pos is not None and ev.buttons() & Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._drag_pos is not None:
            self._drag_pos = None
            try:
                self._on_moved(self.x(), self.y())
            except Exception:
                pass
            ev.accept()

    # ---------- 隐藏 ----------
    def _hide_requested(self):
        self.hide()
        try:
            self._on_hide()
        except Exception:
            pass

    def show_at_bottom_right(self):
        """默认贴屏幕右下角（留出任务栏与边距）"""
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.adjustSize()
        x = geo.right() - self.width() - 16
        y = geo.bottom() - self.height() - 16
        self.move(x, y)
        self.show()

    def show_at(self, x: int, y: int):
        """在持久化位置显示（越界则退回右下角）"""
        from PySide6.QtWidgets import QApplication
        screen_geo = QApplication.primaryScreen().availableGeometry()
        if not (screen_geo.left() - 200 <= x <= screen_geo.right() and
                screen_geo.top() - 200 <= y <= screen_geo.bottom()):
            self.show_at_bottom_right()
            return
        self.move(x, y)
        self.show()

    # ---------- 外观 ----------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 12, 12)
        p.fillPath(path, QColor(28, 28, 30, 235))
        p.setPen(QPen(QColor(60, 60, 64), 1))
        p.drawPath(path)

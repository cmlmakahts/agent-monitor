"""
GUI 渲染测试：依次刷新每个 tab，截图保存，确保所有数据 → UI 的链路跑通。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QEventLoop

app = QApplication.instance() or QApplication(sys.argv)
from src.qtbridge import init_bridge
init_bridge()

# 字体兜底：保证中文字体在沙箱 / offscreen 平台可用
from PySide6.QtGui import QFont, QFontDatabase
for fp in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"]:
    if Path(fp).exists():
        QFontDatabase.addApplicationFont(fp)
app.setFont(QFont("Microsoft YaHei", 10))


from src.detail_window import DetailWindow, PlatformTab
from src.settings_dialog import SettingsDialog
from src.models import (
    AccountStatus, ModelUsage, PlatformId, UsageSnapshot,
)
from datetime import datetime, timedelta, timezone


class MockSched:
    def __init__(self):
        self._listeners = []
    def add_listener(self, cb):
        self._listeners.append(cb)
        cb({})  # 推一帧空
    def get_snapshots(self):
        return {}


def build_snapshot():
    return {
        PlatformId.CHATGPT: UsageSnapshot(
            platform=PlatformId.CHATGPT, account_email="alex@openai.com",
            plan_name="Plus", remaining_percent=72.5,
            tokens_today=12345, calls_today=87, cost_today_usd=0.45,
            by_model=[
                ModelUsage("gpt-4o", tokens=8200, calls=42, cost_usd=0.30),
                ModelUsage("gpt-4o-mini", tokens=4000, calls=44, cost_usd=0.14),
                ModelUsage("o1-preview", tokens=145, calls=1, cost_usd=0.01),
            ],
            history={f"2026-10-{i:02d}": 3000 + i * 800 for i in range(1, 14)},
            status=AccountStatus.ACTIVE,
            reset_at=datetime.now() + timedelta(hours=8, minutes=42),
        ),
        PlatformId.VOLCENGINE: UsageSnapshot(
            platform=PlatformId.VOLCENGINE, account_email="alex@bytedance.com",
            plan_name="方舟订阅", remaining_percent=42.0,
            tokens_today=3200, calls_today=18, cost_today_usd=0.20,
            by_model=[
                ModelUsage("doubao-pro", tokens=2200, calls=12, cost_usd=0.15),
                ModelUsage("doubao-lite", tokens=1000, calls=6, cost_usd=0.05),
            ],
            history={f"2026-10-{i:02d}": 2000 + i * 200 for i in range(1, 14)},
            status=AccountStatus.WARNING,
            reset_at=datetime.now() + timedelta(days=12),
        ),
        PlatformId.QWEN: UsageSnapshot(
            platform=PlatformId.QWEN, account_email="alex@aliyun.com",
            plan_name="百炼订阅", remaining_percent=8.3,
            tokens_today=56000, calls_today=210, cost_today_usd=1.85,
            by_model=[
                ModelUsage("qwen-max", tokens=42000, calls=120, cost_usd=1.40),
                ModelUsage("qwen-plus", tokens=12000, calls=75, cost_usd=0.40),
                ModelUsage("qwen-turbo", tokens=2000, calls=15, cost_usd=0.05),
            ],
            history={f"2026-10-{i:02d}": 30000 + i * 1500 for i in range(1, 14)},
            status=AccountStatus.CRITICAL,
            error="",
        ),
    }


print("=" * 60)
print("GUI RENDER TEST")
print("=" * 60)

sched = MockSched()
detail = DetailWindow(
    on_open_settings=lambda: None,
    on_refresh=lambda: None,
    scheduler=sched,
)
detail.resize(560, 720)
detail.show()

# 推一帧真实数据
snaps = build_snapshot()
for cb in sched._listeners:
    cb(snaps)

# 让 Qt 至少跑一帧
loop = QEventLoop()
QTimer.singleShot(150, loop.quit)
loop.exec()

# 逐个 tab 截图
print("\n[1] 逐 tab 截图...")
tabs = ["ChatGPT", "volcengine", "qwen"]
for i, label in enumerate(tabs):
    detail.tabs.setCurrentIndex(i)
    # 让 Qt 重绘
    loop2 = QEventLoop()
    QTimer.singleShot(150, loop2.quit)
    loop2.exec()

    w = detail.tabs.currentWidget()
    pix = w.grab()
    out = ROOT / "assets" / f"ui_tab_{label}.png"
    pix.save(str(out))
    print(f"  ✅ {out.name}: {out.stat().st_size} bytes")

# 整体窗口截图
print("\n[2] 主窗口整体截图...")
pix = detail.grab()
out = ROOT / "assets" / "ui_window.png"
pix.save(str(out))
print(f"  ✅ {out.name}: {out.stat().st_size} bytes")

# 设置窗截图
print("\n[3] 设置窗截图...")
settings = SettingsDialog(None)
settings.resize(560, 540)
settings.show()
loop3 = QEventLoop()
QTimer.singleShot(150, loop3.quit)
loop3.exec()
pix = settings.grab()
out = ROOT / "assets" / "ui_settings.png"
pix.save(str(out))
print(f"  ✅ {out.name}: {out.stat().st_size} bytes")

print("\n" + "=" * 60)
print("🎉 渲染测试通过")
print("=" * 60)

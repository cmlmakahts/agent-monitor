"""
GUI 构造测试：用 offscreen 平台，确认 DetailWindow / SettingsDialog 能构造并绘制。
不启动 pystray / scheduler，不发任何网络请求。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

print("=" * 60)
print("GUI CONSTRUCTION TEST")
print("=" * 60)

app = QApplication.instance() or QApplication(sys.argv)

from src.detail_window import DetailWindow, PlatformTab
from src.settings_dialog import SettingsDialog
from src.icon_render import render_tray_icon
from src.models import (
    AccountStatus, ModelUsage, PlatformId, UsageSnapshot,
)
from datetime import datetime, timedelta

# mock scheduler（仅暴露 add_listener / get_snapshots）
class MockSched:
    def __init__(self):
        self._listeners = []
    def add_listener(self, cb):
        self._listeners.append(cb)
    def get_snapshots(self):
        return {}

print("\n[1] 构造 DetailWindow...")
sched = MockSched()
detail = DetailWindow(
    on_open_settings=lambda: print("  settings opened"),
    on_refresh=lambda: print("  refreshed"),
    scheduler=sched,
)
print(f"  ✅ Window size={detail.width()}x{detail.height()}")
print(f"  ✅ Tabs: {detail.tabs.count()} -> {[detail.tabs.tabText(i) for i in range(detail.tabs.count())]}")

# 塞入 mock 数据触发渲染
print("\n[2] 注入 mock 数据 → 触发 update_data...")
def feed(i):
    if i >= 3:
        app.quit()
        return
    snaps = {
        PlatformId.CHATGPT: UsageSnapshot(
            platform=PlatformId.CHATGPT, account_email="alex@openai.com",
            plan_name="Plus", remaining_percent=[72.5, 35.2, 12.8][i],
            tokens_today=8200, calls_today=42, cost_today_usd=0.32,
            by_model=[
                ModelUsage("gpt-4o", tokens=4200, calls=18, cost_usd=0.18),
                ModelUsage("gpt-4o-mini", tokens=4000, calls=24, cost_usd=0.14),
            ],
            history={"2026-10-08": 5000, "2026-10-09": 7200, "2026-10-10": 6100,
                     "2026-10-11": 8400, "2026-10-12": 9100, "2026-10-13": 8000},
            status=[AccountStatus.ACTIVE, AccountStatus.WARNING, AccountStatus.CRITICAL][i],
            reset_at=datetime.now() + timedelta(hours=8, minutes=42),
        ),
        PlatformId.VOLCENGINE: UsageSnapshot(
            platform=PlatformId.VOLCENGINE, account_email="alex@bytedance.com",
            plan_name="方舟订阅",
            remaining_percent=[80.0, 60.0, 40.0][i],
            status=AccountStatus.ACTIVE,
            error="用量接口暂不可达" if i == 0 else None,
        ),
        PlatformId.QWEN: UsageSnapshot(
            platform=PlatformId.QWEN, plan_name="DashScope 余额",
            remaining_percent=[50.0, 30.0, 5.0][i],
            tokens_today=12000, calls_today=66, cost_today_usd=0.50,
            status=[AccountStatus.WARNING, AccountStatus.WARNING, AccountStatus.CRITICAL][i],
        ),
    }
    # 走真实 listener 链路
    for cb in sched._listeners:
        cb(snaps)
    # 切到第一个 tab 渲染
    detail.tabs.setCurrentIndex(0)

count = [0]
def tick():
    feed(count[0])
    count[0] += 1
timer = QTimer()
timer.timeout.connect(tick)
timer.start(60)

# 截图（用 QPixmap.grabWidget 不存在，QWidget.grab）
print("\n[3] 渲染截图（offscreen）...")
# 给最后一帧留时间渲染
def do_shot():
    try:
        w = detail.tabs.currentWidget()
        pix = w.grab()
        out = ROOT / "assets" / f"ui_tab_{count[0]-1}.png"
        pix.save(str(out))
        print(f"  ✅ 截图 {out} ({out.stat().st_size} bytes)")
    except Exception as e:
        print(f"  ⚠️ 截图失败：{e}")
    app.quit()

QTimer.singleShot(400, do_shot)
app.exec()

# 设置弹窗也测一下
print("\n[4] 构造 SettingsDialog...")
settings = SettingsDialog(None)
settings.resize(560, 540)
print(f"  ✅ Settings size={settings.width()}x{settings.height()}")

print("\n" + "=" * 60)
print("🎉 GUI 构造测试通过")
print("=" * 60)

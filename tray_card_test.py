"""
托盘 + 悬浮卡片专项测试（offscreen，不弹真窗）

覆盖：
  1. TrayController 构造 + 快照刷新（icon/tooltip/菜单文本）
  2. FloatingCard 数据渲染：三厂商 chips 文本与配色规则（<5% 红，否则绿）
  3. 卡片隐藏回调 / 拖动位置回调
  4. 火山 plan → windows 解析（5h + weekly + monthly）
  5. 千问 → 单额度窗口
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

print("=" * 60)
print("TRAY + FLOATING CARD TEST")
print("=" * 60)

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from src.qtbridge import init_bridge
init_bridge()

from src.models import AccountStatus, LimitWindow, PlatformId, UsageSnapshot

# ---------- 假 scheduler ----------
class FakeScheduler:
    def __init__(self):
        self.listeners = []
        self._snapshots = {}
    def add_listener(self, cb):
        self.listeners.append(cb)
    def get_snapshots(self):
        return dict(self._snapshots)

fake = FakeScheduler()

# ---------- 快照数据 ----------
now = datetime.now()
snaps = {
    PlatformId.CHATGPT: UsageSnapshot(
        platform=PlatformId.CHATGPT, plan_name="ChatGPT Plus", fetched_at=now,
        remaining_percent=99.0, status=AccountStatus.ACTIVE,
        windows=[
            LimitWindow(label="5h 窗口", kind="5h", used_percent=9.0,
                        remaining_percent=91.0, resets_at=now + timedelta(hours=3)),
            LimitWindow(label="周额度", kind="weekly", used_percent=1.0,
                        remaining_percent=99.0, resets_at=now + timedelta(days=6)),
        ]),
    PlatformId.VOLCENGINE: UsageSnapshot(
        platform=PlatformId.VOLCENGINE, plan_name="方舟订阅", fetched_at=now,
        remaining_percent=97.0, status=AccountStatus.ACTIVE,
        windows=[
            LimitWindow(label="5h 窗口", kind="5h",
                        remaining_percent=97.0, resets_at=now + timedelta(hours=2)),
            LimitWindow(label="周额度", kind="weekly",
                        remaining_percent=98.0, resets_at=now + timedelta(days=5)),
        ]),
    PlatformId.QWEN: UsageSnapshot(
        platform=PlatformId.QWEN, plan_name="Qwen Token Plan", fetched_at=now,
        remaining_percent=3.0, status=AccountStatus.CRITICAL,   # <5% → 应红
        windows=[LimitWindow(label="额度", kind="quota", remaining_percent=3.0,
                             resets_at=now + timedelta(days=10))]),
}

print("\n[1] TrayController 构造与刷新...")
from src.tray import TrayController

calls = {"detail": 0, "card": []}
tray = TrayController(
    scheduler=fake,
    on_show_detail=lambda: calls.__setitem__("detail", calls["detail"] + 1),
    on_show_settings=lambda: None,
    on_force_refresh=lambda: None,
    on_quit=lambda: None,
    on_toggle_card=lambda v: calls["card"].append(v),
)
fake._snapshots = snaps
for cb in fake.listeners:
    cb(snaps)  # 线程模拟：直接调用，应经 qtbridge 排队

# 处理事件循环让 queued 调用落地
from PySide6.QtCore import QTimer, QEventLoop
loop = QEventLoop(); QTimer.singleShot(100, loop.quit); loop.exec()

tip = tray.tray.toolTip()
print(f"  tooltip: {tip}")
assert "ChatGPT: 99%" in tip and "火山: 97%" in tip and "千问: 3%" in tip
assert tray._platform_actions[PlatformId.CHATGPT].text() == "ChatGPT：99 %"
assert "⚠" not in tray._platform_actions[PlatformId.QWEN].text()
tray.set_card_checked(True)
assert tray.act_card.isChecked()
print("  ✅ 托盘刷新 OK（tooltip / 菜单文本 / 卡片勾选）")

print("\n[2] FloatingCard 渲染...")
from src.floating_card import FloatingCard

events = {"hide": 0, "moved": []}
card = FloatingCard(on_hide=lambda: events.__setitem__("hide", events["hide"] + 1),
                    on_moved=lambda x, y: events["moved"].append((x, y)))
card.update_snapshots(snaps)

# 检查 chips 文本
from src.floating_card import Chip
row_gpt = card._rows[PlatformId.CHATGPT][1]
row_volc = card._rows[PlatformId.VOLCENGINE][1]
row_qwen = card._rows[PlatformId.QWEN][1]
assert row_gpt[0].text() == "5h 91%", row_gpt[0].text()
assert row_gpt[1].text() == "周 99%", row_gpt[1].text()
assert row_volc[0].text() == "5h 97%"
assert row_qwen[0].text() == "额度 3%"
assert row_qwen[1].isVisible() is False, "千问只有 1 个窗口，第二个 chip 应隐藏"
# 配色：<5% 红
assert "#ea4335" in row_qwen[0].styleSheet(), "千问 3% 应为红色"
assert "#34a853" in row_gpt[0].styleSheet(), "ChatGPT 91% 应为绿色"
print(f"  chips: GPT[{row_gpt[0].text()} | {row_gpt[1].text()}]  "
      f"火山[{row_volc[0].text()} | {row_volc[1].text()}]  千问[{row_qwen[0].text()}]")
print("  ✅ 卡片渲染 OK（绿/红规则、千问单窗口隐藏第二 chip）")

# 隐藏回调
card._hide_requested()
assert events["hide"] == 1
assert card.isVisible() is False
print("  ✅ 卡片隐藏回调 OK")

# 截图
card.show()
card.update_snapshots(snaps)
card.grab().save(str(ROOT / "assets" / "ui_card.png"))
print("  ✅ 截图 assets/ui_card.png")

print("\n[3] 火山 plan → windows 解析...")
from src.platforms.volcengine import VolcenginePlatform
volc = VolcenginePlatform.__new__(VolcenginePlatform)
volc.credential = ""
plan_a = {
    "plans": [
        {"plan": "agent-plan", "period": "5h", "usedPercent": 30.0,
         "resetAt": (datetime.now() + timedelta(hours=2)).isoformat()},
        {"plan": "agent-plan", "period": "weekly", "usedPercent": 47.5,
         "resetAt": (datetime.now() + timedelta(days=3)).isoformat()},
        {"plan": "agent-plan", "period": "monthly", "used": 38200, "total": 50000,
         "resetAt": (datetime.now() + timedelta(days=17)).isoformat()},
    ]
}
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc._apply_plan(snap, plan_a)
kinds = {w.kind: w for w in snap.windows}
print(f"  windows: {[(w.label, w.kind, round(w.remaining_percent,1)) for w in snap.windows]}")
assert set(kinds) == {"5h", "weekly", "monthly"}, kinds
assert kinds["5h"].remaining_percent == 70.0
assert kinds["weekly"].remaining_percent == 52.5
assert abs(kinds["monthly"].remaining_percent - 23.6) < 0.2
assert abs(snap.remaining_percent - 23.6) < 0.2, "主口径仍为 monthly"
print("  ✅ 火山三窗口解析 OK")

print("\n[4] 千问 → 单额度窗口...")
from src.platforms.qwen import QwenPlatform
qwen = QwenPlatform.__new__(QwenPlatform)
qwen.credential = ""
summary = {"usage": {"token_plan": {"totalCredits": 50000, "remainingCredits": 41700,
                                    "resetDate": "2026-11-02"}}}
tp = qwen._find_token_plan(summary)
snap = UsageSnapshot(platform=PlatformId.QWEN, fetched_at=datetime.now())
qwen._apply(snap, tp, summary)
assert len(snap.windows) == 1 and snap.windows[0].kind == "quota"
assert abs(snap.windows[0].remaining_percent - 83.4) < 0.2
print("  ✅ 千问单窗口 OK（额度 83.4%）")

print("\n" + "=" * 60)
print("🎉 托盘 + 卡片测试全部通过")
print("=" * 60)

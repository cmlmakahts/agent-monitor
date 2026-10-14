"""
活链路复现测试：真实 MonitorScheduler → qtbridge → 托盘/悬浮卡片。
复刻 main.py 接线，跑 75 秒，记录每次 UI 刷新是否到达。
同时把桥 _execute 的异常打印出来（原来 print_exc 只进 stderr，窗口化 exe 不可见）。
"""
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)
from src.qtbridge import init_bridge
init_bridge()

# ---- 桥异常可视化 ----
import src.qtbridge as qb


def _logged_execute(self, payload):
    fn, args, kwargs = payload
    try:
        fn(*args, **kwargs)
        print(f"  [bridge] OK  {getattr(fn, '__name__', fn)}", flush=True)
    except Exception:
        print(f"  [bridge] EXCEPTION in {getattr(fn, '__name__', fn)}:", flush=True)
        traceback.print_exc()


qb.MainThreadBridge._execute = _logged_execute

from src.floating_card import FloatingCard
from src.scheduler import MonitorScheduler
from src.tray import TrayController

scheduler = MonitorScheduler()
scheduler.cfg.poll_interval_sec = 15  # 加快：15s 一拍

events: list = []

# ---- 复刻 main.py 的卡片接线 ----
card = FloatingCard(on_hide=lambda: None, on_moved=lambda x, y: None)


def _on_snapshots_for_card(snaps):
    from src.qtbridge import post
    post(_card_apply, snaps)


def _card_apply(snaps):
    if card is not None and card.isVisible():
        before = card.last_update.text()
        card.update_snapshots(snaps)
        events.append(("card", before, card.last_update.text()))
        print(f"  [card] {before} -> {card.last_update.text()}", flush=True)


scheduler.add_listener(_on_snapshots_for_card)

# ---- 托盘 ----
tray = TrayController(
    scheduler=scheduler,
    on_show_detail=lambda: None,
    on_show_settings=lambda: None,
    on_force_refresh=lambda: None,
    on_quit=lambda: None,
    on_toggle_card=lambda v: None,
)
scheduler.add_listener(lambda snaps: events.append(("sched_emit", len(snaps))))

scheduler.start()
card.show()
print(f"card.isVisible() = {card.isVisible()}", flush=True)
print("waiting 75s for live ticks (interval=15s)...", flush=True)

from PySide6.QtCore import QEventLoop, QTimer

loop = QEventLoop()
QTimer.singleShot(75_000, loop.quit)
loop.exec()

scheduler.stop()
card_ticks = [e for e in events if e[0] == "card"]
sched_ticks = [e for e in events if e[0] == "sched_emit"]
print(f"\nscheduler emitted: {len(sched_ticks)} times")
print(f"card updated:      {len(card_ticks)} times")
tip = tray.tray.toolTip()
print(f"final tooltip:     {tip}")
print(f"final chatgpt chips: {[c.text() for c in card._rows[0][1]]}")
assert len(sched_ticks) >= 3, "调度器至少应 emit 3 次"
assert len(card_ticks) >= 2, "卡片应至少自动刷新 2 次"
print("✅ 活链路自动刷新正常")

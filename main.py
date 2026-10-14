"""
主入口：PySide6 单一 Qt 主循环。

架构（全部 UI 在 Qt 主线程，无第三方托盘线程）：
  main thread          QApplication、托盘(QSystemTrayIcon)、详情窗口、悬浮卡片
  scheduler            APScheduler BackgroundScheduler 线程 → qtbridge 回主线程

单实例：QLocalServer/QLocalSocket，重复启动唤醒已有实例。
"""
from __future__ import annotations
import sys
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from src.config import load_config, save_config
from src.detail_window import DetailWindow
from src.floating_card import FloatingCard
from src.models import PlatformId
from src.qtbridge import init_bridge, post as main_thread_call
from src.scheduler import MonitorScheduler
from src.settings_dialog import SettingsDialog
from src.tray import TrayController
from src.utils.logger import setup_logger


def logger():
    return setup_logger(Path.home() / "AppData" / "Roaming" / "AgentMonitor" / "logs")


# ---------- 退出/崩溃验尸日志 ----------
EXIT_LOG = Path.home() / "AppData" / "Roaming" / "AgentMonitor" / "logs" / "exit.log"


def _init_exit_diagnostics():
    """faulthandler 落盘原生崩溃栈；atexit 记录退出原因——
    窗口化 exe 静默退出时（无 stderr），这是唯一的死亡证据。"""
    try:
        EXIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = open(EXIT_LOG, "a", encoding="utf-8", buffering=1)
        fh.write(f"\n===== session start {__import__('datetime').datetime.now()} pid={os.getpid()} =====\n")
        import faulthandler
        faulthandler.enable(file=fh, all_threads=True)
        import atexit

        def _on_exit():
            import traceback
            tb = "".join(traceback.format_stack())
            fh.write(f"[exit] atexit at {__import__('datetime').datetime.now()}\n{tb}\n")
            fh.flush()

        atexit.register(_on_exit)
        sys.excepthook = lambda t, v, tb_: (
            fh.write(f"[exit] unhandled exception: {t.__name__}: {v}\n"),
            traceback.print_exc(file=fh), fh.flush())
    except Exception:
        pass


# ---------- 进程单例 ----------
INSTANCE_KEY = "AgentMonitor-SingleInstance-8f2a71c4"


def _activate_existing_instance() -> bool:
    """已有实例在运行则通知它弹出窗口，返回 True"""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if not sock.waitForConnected(500):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _start_instance_server(on_show) -> QLocalServer:
    """创建本地服务端，后续实例连上发 show 时回调 on_show"""
    QLocalServer.removeServer(INSTANCE_KEY)  # 清理上次崩溃的残留
    server = QLocalServer()
    if not server.listen(INSTANCE_KEY):
        logger().warning(f"单例服务创建失败：{server.errorString()}")
    return server


def _serve_new_connection(server: QLocalServer, on_show):
    """处理新连接（在主线程调用）"""
    try:
        conn = server.nextPendingConnection()
        if conn is None:
            return
        if conn.waitForReadyRead(500):
            data = bytes(conn.readAll())
            if b"show" in data:
                on_show()
        conn.disconnectFromServer()
    except Exception:
        pass


# ---------- 主入口 ----------
def main():
    app_ref: dict = {}
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    # 字体兜底：在精简/沙箱 Windows 上也保证中文可读
    from PySide6.QtGui import QFont, QFontDatabase
    for fp in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"]:
        if Path(fp).exists():
            QFontDatabase.addApplicationFont(fp)
    app.setFont(QFont("Microsoft YaHei", 10))

    # 跨线程桥：必须在任何工作线程启动前、主线程中初始化
    init_bridge()
    _init_exit_diagnostics()

    # ---- 进程单例：已有实例则通知它并退出 ----
    if _activate_existing_instance():
        logger().info("检测到已有实例，已通知其显示窗口，本次启动退出")
        sys.exit(0)
    server: Optional[QLocalServer] = None

    scheduler = MonitorScheduler()

    detail_window: Optional[DetailWindow] = None
    settings_dialog: Optional[SettingsDialog] = None
    card: Optional[FloatingCard] = None
    tray: Optional[TrayController] = None

    # ---------- 悬浮卡片 ----------
    def _card_hide_persist():
        cfg = load_config()
        cfg.show_card = False
        cfg.card_pos = [card.x(), card.y()]
        save_config(cfg)

    def _card_move_persist(x: int, y: int):
        cfg = load_config()
        cfg.card_pos = [x, y]
        save_config(cfg)

    def show_card():
        nonlocal card
        if card is None:
            card = FloatingCard(on_hide=_card_hide_persist, on_moved=_card_move_persist)
        snaps = scheduler.get_snapshots()
        if snaps:
            card.update_snapshots(snaps)
        cfg = load_config()
        pos = cfg.card_pos
        if pos:
            card.show_at(int(pos[0]), int(pos[1]))
        else:
            card.show_at_bottom_right()
        card.raise_()
        if tray is not None:
            tray.set_card_checked(True)

    def hide_card():
        nonlocal card
        if card is not None:
            card.hide()
        if tray is not None:
            tray.set_card_checked(False)

    def toggle_card(visible: bool):
        if visible:
            show_card()
        else:
            hide_card()

    # ---------- 详情 / 设置 ----------
    def show_detail():
        nonlocal detail_window
        if detail_window is None:
            detail_window = DetailWindow(
                on_open_settings=show_settings,
                on_refresh=force_refresh,
                scheduler=scheduler,
            )
        # 从最小化恢复 / 从托盘隐藏唤起，三种状态都覆盖
        if detail_window.isMinimized():
            detail_window.showNormal()
        else:
            detail_window.show()
        detail_window.setWindowState(detail_window.windowState() & ~Qt.WindowMinimized)
        detail_window.activateWindow()
        detail_window.raise_()

    def show_settings():
        nonlocal settings_dialog
        dialog = SettingsDialog(detail_window)
        if dialog.exec() == dialog.Accepted:
            scheduler.reload(load_config())
            logger().info("config reloaded")

    def force_refresh():
        # _tick 会跑 CLI 子进程（最长数十秒），放后台线程避免冻结 UI；
        # 完成后 listener 经 qtbridge 自动回主线程刷新
        import threading
        threading.Thread(target=scheduler._tick, daemon=True).start()

    def quit_app():
        scheduler.stop()
        try:
            if server is not None:
                server.close()
        except Exception:
            pass
        try:
            app.quit()
        except Exception:
            os._exit(0)

    # ---------- 托盘（Qt 原生，主线程） ----------
    tray = TrayController(
        scheduler=scheduler,
        on_show_detail=show_detail,
        on_show_settings=show_settings,
        on_force_refresh=force_refresh,
        on_quit=quit_app,
        on_toggle_card=toggle_card,
    )

    # 卡片数据订阅：scheduler → bridge → 主线程刷新
    def _on_snapshots_for_card(snaps):
        main_thread_call(_card_apply, snaps)

    def _card_apply(snaps):
        if card is not None and card.isVisible():
            card.update_snapshots(snaps)
            logger().info("[ui] card applied, platforms=%d", len(snaps))
        else:
            logger().info("[ui] card skipped (not visible), platforms=%d", len(snaps))

    scheduler.add_listener(_on_snapshots_for_card)

    # ---------- 单例服务：后续实例发 show → 弹出本实例窗口 ----------
    server = _start_instance_server(show_detail)
    server.newConnection.connect(lambda: _serve_new_connection(server, show_detail))

    scheduler.start()

    # 启动：按配置决定卡片是否显示；详情窗口延迟弹出
    cfg = load_config()
    if cfg.show_card:
        QTimer.singleShot(100, show_card)
    QTimer.singleShot(400, show_detail)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

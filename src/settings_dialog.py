"""
设置窗口：添加/更新各平台凭证，调整轮询间隔
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget, QMessageBox,
)

from .config import load_config, save_config
from .models import PlatformId, AppConfig


_PLATFORM_NAME = {
    PlatformId.CHATGPT: "ChatGPT",
    PlatformId.VOLCENGINE: "火山方舟",
    PlatformId.QWEN: "千问 Token Plan",
}


_PLATFORM_HINT = {
    PlatformId.CHATGPT:
        "无需填写凭证。只要本机 Codex CLI 登录过并至少跑过一次任务，\n"
        "程序会自动解析 ~/.codex/sessions 里的用量与限额数据。\n"
        "安装：npm install -g @openai/codex",
    PlatformId.VOLCENGINE:
        "推荐先在终端完成 SSO 登录（一次性）：\n"
        "  arkcli auth login volc-sso\n"
        "  无浏览器环境：arkcli auth login volc-sso --no-browser 拿授权码后\n"
        "  arkcli auth login --no-browser --code <授权码>\n"
        "此处可留空。可选填 AK/SK（空格/逗号分隔），程序会写入 profile（config init）。",
    PlatformId.QWEN:
        "无需 CLI。程序用内置浏览器（Qt 自带 Chromium）定期打开控制台页面\n"
        "  platform.qianwenai.com → 工作台 → Token Plan → 我的订阅\n"
        "读取额度。首次使用请点下方「打开控制台登录」登录一次，\n"
        "登录态会持久保存，之后自动带 Cookie 抓取。\n"
        "凭证框可留空；如需换地址（团队版/企业专属域名）填完整 URL。",
}


_PLATFORM_PLACEHOLDER = {
    PlatformId.CHATGPT: "凭证值（留空即可）",
    PlatformId.VOLCENGINE: "凭证值（可选填 AK/SK，空格/逗号分隔）",
    PlatformId.QWEN: "控制台地址（留空用默认 platform.qianwenai.com）",
}


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(560, 520)
        self.setStyleSheet("QDialog { background: #121212; color: #fff; }")
        self.cfg = load_config()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        title = QLabel("凭证管理")
        title.setFont(QFont("Segoe UI", 14, QFont.DemiBold))
        root.addWidget(title)
        tip = QLabel("各凭证用 Windows 凭据管理器加密存储（DPAPI）。")
        tip.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        root.addWidget(tip)

        # 平台 tab 用 combo + 单面板：简洁
        platform_row = QHBoxLayout()
        platform_row.addWidget(QLabel("平台："))
        self.platform_combo = QComboBox()
        self.platform_combo.setStyleSheet(
            "QComboBox { background: #1e1e1e; color: #fff; padding: 4px 8px; "
            "border: 1px solid #3a3a3a; border-radius: 4px; }"
        )
        for pid in PlatformId:
            self.platform_combo.addItem(_PLATFORM_NAME[pid], pid.value)
        platform_row.addWidget(self.platform_combo)
        platform_row.addStretch()
        root.addLayout(platform_row)

        # 提示
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet(
            "color: #bdc1c6; background: #1c1c1c; border-radius: 6px; "
            "padding: 8px 10px; font-size: 11px;"
        )
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)
        self.platform_combo.currentIndexChanged.connect(self._reload_platform)

        # 输入
        self.cred_edit = QPlainTextEdit()
        self.cred_edit.setStyleSheet(
            "QPlainTextEdit { background: #1e1e1e; color: #fff; border: 1px solid #2a2a2a; "
            "border-radius: 6px; padding: 8px; }"
        )
        self.cred_edit.setPlaceholderText("凭证值（仅火山可选填 AK/SK，其余留空）")
        self.cred_edit.setFixedHeight(80)
        root.addWidget(self.cred_edit)

        # cred_edit 已就绪 → 触发首次填充
        self._reload_platform()

        cred_row = QHBoxLayout()
        self.validate_btn = QPushButton("测试连通")
        self.validate_btn.clicked.connect(self._on_validate)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._on_clear)
        self.login_btn = QPushButton("打开控制台登录")
        self.login_btn.clicked.connect(self._on_open_login)
        for b in (self.validate_btn, clear_btn, self.login_btn):
            b.setStyleSheet(
                "QPushButton { background: #2d2d2d; color: #fff; border: none; "
                "border-radius: 6px; padding: 6px 12px; }"
                "QPushButton:hover { background: #3a3a3a; }"
            )
            cred_row.addWidget(b)
        cred_row.addStretch()
        root.addLayout(cred_row)

        self.validate_result = QLabel("")
        self.validate_result.setStyleSheet("font-size: 11px;")
        self.validate_result.setWordWrap(True)
        root.addWidget(self.validate_result)

        # 轮询间隔
        sep = QLabel("轮询间隔（秒）")
        sep.setStyleSheet("color: #9aa0a6; font-size: 12px; margin-top: 12px;")
        root.addWidget(sep)
        interval_row = QHBoxLayout()
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(15, 3600)
        self.interval_spin.setValue(self.cfg.poll_interval_sec)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setStyleSheet(
            "QSpinBox { background: #1e1e1e; color: #fff; padding: 4px; "
            "border: 1px solid #3a3a3a; border-radius: 4px; }"
        )
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch()
        root.addLayout(interval_row)

        root.addStretch()

        # 底部按钮
        bottom = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        for b in (cancel, save):
            b.setStyleSheet(
                "QPushButton { background: #4d8cff; color: #fff; border: none; "
                "border-radius: 6px; padding: 6px 16px; }"
                "QPushButton:hover { background: #5b95ff; }"
            )
            b.setFixedHeight(30)
        bottom.addStretch()
        bottom.addWidget(cancel)
        bottom.addSpacing(8)
        bottom.addWidget(save)
        root.addLayout(bottom)

    def _current_platform(self) -> PlatformId:
        idx = self.platform_combo.currentIndex()
        return PlatformId(self.platform_combo.itemData(idx))

    def _reload_platform(self):
        pid = self._current_platform()
        if hasattr(self, "hint_label"):
            self.hint_label.setText(_PLATFORM_HINT.get(pid, ""))
        if hasattr(self, "cred_edit"):
            self.cred_edit.setPlainText(self.cfg.get_credential(pid) or "")
            self.cred_edit.setPlaceholderText(_PLATFORM_PLACEHOLDER.get(
                pid, "凭证值（仅火山可选填 AK/SK，其余留空）"))
        if hasattr(self, "validate_result"):
            self.validate_result.setText("")
        # 「打开控制台登录」只对走浏览器抓取的平台有意义
        if hasattr(self, "login_btn"):
            self.login_btn.setVisible(pid == PlatformId.QWEN)

    def _on_clear(self):
        self.cred_edit.setPlainText("")
        self.validate_result.setText("")

    def _on_open_login(self):
        """弹出内置浏览器让用户登录一次，Cookie 落盘后抓取即带登录态。"""
        from .platforms.qwen import LOGIN_URL
        from .webengine_fetch import open_login_window
        ok, err = open_login_window(LOGIN_URL, "登录千问控制台")
        if not ok:
            self.validate_result.setStyleSheet("color: #f44336;")
            self.validate_result.setText("⚠️ " + err)
            return
        self.validate_result.setStyleSheet("color: #4CAF50;")
        self.validate_result.setText(
            "✅ 已打开登录窗口。登录完成后关掉它，再点「测试连通」。")

    def _on_validate(self):
        from .scheduler import build_platform
        pid = self._current_platform()
        cred = self.cred_edit.toPlainText().strip()
        self.validate_result.setStyleSheet("color: #bdc1c6;")
        self.validate_result.setText("检测中…")
        adapter = build_platform(pid, cred)
        if adapter is None:
            self.validate_result.setStyleSheet("color: #f44336;")
            self.validate_result.setText("适配器未找到")
            return
        try:
            ok, msg = adapter.validate_credential()
        except Exception as e:
            ok, msg = False, f"异常：{e}"
        self.validate_result.setStyleSheet("color: %s" % ("#4CAF50" if ok else "#f4a261"))
        self.validate_result.setText(("✅ " if ok else "⚠️ ") + msg)

    def _on_save(self):
        self.cfg.set_credential(self._current_platform(), self.cred_edit.toPlainText().strip())
        self.cfg.poll_interval_sec = self.interval_spin.value()
        try:
            save_config(self.cfg)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self.accept()

"""
配置管理：用 Fernet+keyring 加密落盘到 %APPDATA%/AgentMonitor/config.bin
"""
from __future__ import annotations
import json
from pathlib import Path

from .models import AppConfig, PlatformId
from .utils.crypto import save_encrypted_blob, load_encrypted_blob


def config_dir() -> Path:
    base = Path.home() / "AppData" / "Roaming" / "AgentMonitor"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return config_dir() / "config.bin"


_SENTINEL_TEXT = b"AGENTMONITOR-CFG-V1\n"


def load_config() -> AppConfig:
    cfg = AppConfig()
    try:
        raw = load_encrypted_blob(config_path())
        if raw and raw.startswith(_SENTINEL_TEXT):
            obj = json.loads(raw[len(_SENTINEL_TEXT):].decode("utf-8"))
            cfg.poll_interval_sec = int(obj.get("poll_interval_sec", 60))
            cfg.dark_mode = bool(obj.get("dark_mode", True))
            cfg.enable_notify = bool(obj.get("enable_notify", True))
            cfg.notify_warning_threshold = float(obj.get("notify_warning_threshold", 20.0))
            cfg.notify_critical_threshold = float(obj.get("notify_critical_threshold", 5.0))
            cfg.credentials = dict(obj.get("credentials", {}))
            cfg.show_card = bool(obj.get("show_card", True))
            pos = obj.get("card_pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                cfg.card_pos = [int(pos[0]), int(pos[1])]
    except Exception:
        # 文件损坏 / 密钥不可用 → 用默认配置
        pass
    return cfg


def save_config(cfg: AppConfig):
    data = json.dumps({
        "poll_interval_sec": cfg.poll_interval_sec,
        "dark_mode": cfg.dark_mode,
        "enable_notify": cfg.enable_notify,
        "notify_warning_threshold": cfg.notify_warning_threshold,
        "notify_critical_threshold": cfg.notify_critical_threshold,
        "credentials": cfg.credentials,
        "show_card": cfg.show_card,
        "card_pos": cfg.card_pos,
    }, ensure_ascii=False).encode("utf-8")
    save_encrypted_blob(config_path(), _SENTINEL_TEXT + data)

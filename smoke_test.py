"""
冒烟测试：验证模块导入、模型序列化、加密落盘、图标渲染等不需要 GUI/网络的部分。
"""
import os
import sys
import json
import time
from pathlib import Path

# 切到项目根
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

print("=" * 60)
print("SMOKE TEST")
print("=" * 60)

# 1. 模块导入
print("\n[1] 模块导入...")
from src.models import (
    PlatformId, AccountStatus, ModelUsage, UsageSnapshot, AppConfig,
)
from src.utils.crypto import encrypt, decrypt, save_encrypted_blob, load_encrypted_blob
from src.utils.logger import setup_logger
from src.platforms.base import BasePlatform
from src.platforms.chatgpt import ChatGPTPlatform
from src.platforms.volcengine import VolcenginePlatform
from src.platforms.qwen import QwenPlatform
from src.icon_render import render_tray_icon, render_status_dot
from src.config import load_config, save_config, config_path
from src.scheduler import MonitorScheduler, build_platform

print("  ✅ 所有模块导入成功")

# 2. 数据模型
print("\n[2] 数据模型序列化...")
now = time.time()
snap = UsageSnapshot(
    platform=PlatformId.CHATGPT,
    account_email="user@example.com",
    plan_name="Plus",
    remaining_percent=72.3,
    tokens_today=12345,
    calls_today=87,
    cost_today_usd=0.45,
    by_model=[ModelUsage("gpt-4o", tokens=9000, calls=60, cost_usd=0.30),
              ModelUsage("gpt-4o-mini", tokens=3000, calls=27, cost_usd=0.15)],
    history={"2026-10-12": 100, "2026-10-13": 200},
    status=AccountStatus.ACTIVE,
)
d = snap.to_dict()
assert d["platform"] == "chatgpt"
assert d["by_model"][0]["model"] == "gpt-4o"
assert d["reset_at"] is None
print(f"  ✅ UsageSnapshot 序列化 OK: {len(json.dumps(d))} chars")

# 3. 加密往返
print("\n[3] 加密 / 解密往返...")
plaintext = b"hello-secret-token-123"
cipher = encrypt(plaintext)
decoded = decrypt(cipher)
assert decoded == plaintext, "decrypt 不一致"
print(f"  ✅ Fernet 加密 OK, ciphertext {len(cipher)} bytes")

# 4. 配置读写
print("\n[4] 配置读写...")
cfg = AppConfig()
cfg.set_credential(PlatformId.CHATGPT, "demo_session_token")
cfg.set_credential(PlatformId.VOLCENGINE, "demo_cookie=value")
cfg.poll_interval_sec = 30
save_config(cfg)
print(f"  落盘: {config_path()}")

loaded = load_config()
assert loaded.get_credential(PlatformId.CHATGPT) == "demo_session_token"
assert loaded.poll_interval_sec == 30
print(f"  ✅ 配置读写 OK")

# 5. 图标生成（落盘 PNG）
print("\n[5] 托盘图标生成...")
from src.icon_render import save_default_icon
out = ROOT / "assets" / "tray.png"
save_default_icon(out)
print(f"  ✅ 已生成: {out} ({out.stat().st_size} bytes)")

# 6. 各状态图标渲染
print("\n[6] 各状态图标...")
for s in (AccountStatus.ACTIVE, AccountStatus.WARNING, AccountStatus.CRITICAL,
         AccountStatus.EXPIRED, AccountStatus.UNKNOWN):
    img = render_tray_icon(s, 50.0, 64)
    img.save(ROOT / "assets" / f"icon_{s.value}.png")
    print(f"  - {s.value}: ok")

# 7. build_platform（不实际联网，只检查构造）
print("\n[7] 平台适配器构造（不联网）...")
for pid in (PlatformId.CHATGPT, PlatformId.VOLCENGINE, PlatformId.QWEN):
    p = build_platform(pid, "fake_cred_for_construction_test")
    assert p is not None, f"build_platform {pid} 返回 None"
    print(f"  - {pid.value}: ok")

# 8. 验证凭证逻辑（不联网，超时短的请求会失败，但返回 (False, msg) 就好）
# 这里跳过真实网络调用，只确认方法存在
print("\n[8] 适配器方法签名检查...")
for cls in (ChatGPTPlatform, VolcenginePlatform, QwenPlatform):
    assert hasattr(cls, "validate_credential")
    assert hasattr(cls, "fetch")
    print(f"  - {cls.__name__}: ok")

print("\n" + "=" * 60)
print("🎉 全部 smoke 测试通过")
print("=" * 60)

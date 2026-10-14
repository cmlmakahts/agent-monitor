"""
CLI 模式适配器单测：用 fixture 数据验证解析逻辑，不依赖真实 CLI / 网络。

覆盖：
  1. ChatGPT — 构造假 ~/.codex/sessions rollout jsonl，验证 token_count 事件解析
              （monkeypatch sessions_dir 到临时目录）
  2. Volcengine — mock arkcli JSON（多种字段形态）→ _apply_plan 解析
  3. Qwen — mock qianwen usage summary JSON → _find_token_plan / _apply 解析
  4. 降级 — CLI 未安装时 fetch() 返回 EXPIRED + 安装指引
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

print("=" * 60)
print("CLI MODE ADAPTER TEST")
print("=" * 60)

from src.models import AccountStatus, PlatformId, UsageSnapshot
from src.platforms.chatgpt import ChatGPTPlatform
from src.platforms.volcengine import VolcenginePlatform
from src.platforms.qwen import QwenPlatform

# ---------- 1. ChatGPT ----------
print("\n[1] ChatGPT — 本地 rollout jsonl 解析（真实 Codex schema）...")
tmpdir = Path(tempfile.mkdtemp())
(today := tmpdir / datetime.now().strftime("%Y/%m/%d")).mkdir(parents=True, exist_ok=True)
rollout = today / f"rollout-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}-test.jsonl"

reset_5h = (datetime.now() + timedelta(hours=3, minutes=12)).isoformat()
reset_wk = (datetime.now() + timedelta(days=4)).isoformat()

# 形态 1：新版 schema（rate_limits 在 payload 下，primary/secondary + window_minutes）
new_schema_ev = {
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {
            "total_token_usage": {"total_tokens": 1182169, "input_tokens": 1178400},
            "last_token_usage": {"total_tokens": 107484},
        },
        "rate_limits": {
            "limit_id": "codex",
            "primary": {"used_percent": 38.0, "window_minutes": 300, "resets_at": reset_5h},
            "secondary": {"used_percent": 22.5, "window_minutes": 10080, "resets_at": reset_wk},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "individual_limit": None,
            "plan_type": "plus",
        },
    },
}
lines = [
    json.dumps({"type": "session_meta", "payload": {"cwd": "C:/tmp"}}),
    json.dumps({"type": "response", "payload": {"response": {"model": "gpt-5.2"}}}),
    json.dumps(new_schema_ev),
]
rollout.write_text("\n".join(lines), encoding="utf-8")

ChatGPTPlatform.sessions_dir = staticmethod(lambda: tmpdir)  # monkeypatch
# 本测试只验证 rollout 降级通道：屏蔽 app-server 实时通道与真实 state_5.sqlite，
# 否则装了 Codex 的机器上 fetch() 会优先返回真实账号数据，fixture 就白写了。
ChatGPTPlatform.codex_candidates = staticmethod(lambda: [])
ChatGPTPlatform.state_db = staticmethod(lambda: tmpdir / "no-such-state.sqlite")
gpt = ChatGPTPlatform()
snap = gpt.fetch()
print(f"  plan={snap.plan_name}  remain={snap.remaining_percent:.1f}%  "
      f"status={snap.status.value}  reset={snap.reset_at}")
print(f"  windows={[(w.label, w.remaining_percent) for w in snap.windows]}")
# 主口径 = 最受约束窗口：5h 剩 62% < 周额度剩 77.5%
assert snap.plan_name == "ChatGPT Plus (Codex)", f"plan_type 解析失败: {snap.plan_name}"
assert snap.remaining_percent == 62.0, f"期望 62.0%（5h），实际 {snap.remaining_percent}"
assert snap.status == AccountStatus.ACTIVE
assert snap.reset_at is not None
# 两个窗口都应入列：5h 剩 62%，周额度剩 77.5%
kinds = {w.kind: w for w in snap.windows}
assert "5h" in kinds and "weekly" in kinds, f"窗口缺失: {list(kinds)}"
assert kinds["weekly"].remaining_percent == 77.5
# credits 等非窗口节点不应混入
assert len(snap.windows) == 2, f"credits 混入窗口列表: {len(snap.windows)}"
# 今日 tokens 来自 total_token_usage.total_tokens
assert snap.tokens_today == 1182169, f"今日 tokens 解析失败: {snap.tokens_today}"
ok, msg = gpt.validate_credential()
assert ok, msg
print("  ✅ ChatGPT 新版 schema 解析 OK（主口径=5h 62%，周额度=77.5%，credits 未混入）")
print(f"  ✅ validate: {msg}")

# 形态 2：旧版 schema（info.rate_limits 为 list）——兼容保留
rollout2 = today / "rollout-weekly-only.jsonl"
rollout2.write_text("\n".join([
    json.dumps({"type": "token_count", "payload": {"info": {
        "rate_limits": [{"window": "weekly", "used_ratio": 0.93}],
    }}}),
]), encoding="utf-8")
rollout2.touch()
# 让它更新（mtime 晚于第一个）
import os, time
os.utime(rollout2, (time.time() + 1, time.time() + 1))
snap2 = gpt.fetch()
assert snap2.remaining_percent == 7.0, f"weekly-only 期望 7%，实际 {snap2.remaining_percent}"
assert snap2.status == AccountStatus.WARNING, f"7% 应为 WARNING，实际 {snap2.status}"
print(f"  ✅ 旧版 list 形态 OK（剩 7% → WARNING）")

# 形态 3：主口径取更紧张的 5h（weekly 缺失时兜底 windows[0]）
rollout3 = today / "rollout-5h-only.jsonl"
rollout3.write_text("\n".join([
    json.dumps({"type": "token_count", "payload": {
        "rate_limits": {"primary": {"used_percent": 96.0, "window_minutes": 300,
                                    "resets_at": reset_5h}},
    }}),
]), encoding="utf-8")
os.utime(rollout3, (time.time() + 2, time.time() + 2))
snap3 = gpt.fetch()
assert snap3.remaining_percent == 4.0, f"5h-only 期望 4%，实际 {snap3.remaining_percent}"
assert snap3.status == AccountStatus.CRITICAL
print(f"  ✅ 无周额度兜底 OK（5h 剩 4% → CRITICAL）")

# 空目录降级
empty = Path(tempfile.mkdtemp()) / "sessions"
ChatGPTPlatform.sessions_dir = staticmethod(lambda: empty)
snap3 = gpt.fetch()
assert snap3.status == AccountStatus.EXPIRED and "sessions" in snap3.error
print(f"  ✅ 无数据降级 OK：{snap3.error[:40]}...")

# ---------- 2. Volcengine ----------
print("\n[2] Volcengine — arkcli JSON 解析（多种形态）...")
volc = VolcenginePlatform.__new__(VolcenginePlatform)  # 跳过 __init__（不跑 ensure_login）
volc.credential = ""

# 形态 REAL：v1.0.38+ 真实返回（items[].periods[].label/percent/reset_at）
plan_real = {
    "viewer": {"auth_method": "sso", "user_id": "2120360004"},
    "items": [{
        "product": "coding-plan", "edition": "personal", "subscribed": True,
        "periods": [
            {"label": "session", "percent": 0.0016275,
             "reset_at": "2026-10-13T19:54:58+08:00"},
            {"label": "weekly", "percent": 16.9232096,
             "reset_at": "2026-10-19T00:00:00+08:00"},
            {"label": "monthly", "percent": 8.4616048,
             "reset_at": "2026-11-12T23:59:59+08:00"},
        ],
        "updated_at": 1791874833,
    }],
}
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc._apply_plan(snap, plan_real)
print(f"  REAL: plan={snap.plan_name}  remain={snap.remaining_percent:.2f}%  "
      f"windows={[(w.label, f'{w.remaining_percent:.1f}%') for w in snap.windows]}")
assert snap.plan_name == "coding-plan personal", f"plan_name 解析失败: {snap.plan_name}"
# 主口径 = 最受约束窗口（剩余最小）：weekly 100 - 16.92 ≈ 83.08
assert abs(snap.remaining_percent - 83.0768) < 0.01, f"期望 83.08%，实际 {snap.remaining_percent}"
kinds = {w.kind: w for w in snap.windows}
assert set(kinds) == {"5h", "weekly", "monthly"}, f"窗口缺失: {list(kinds)}"
assert abs(kinds["5h"].remaining_percent - 99.998) < 0.01
assert abs(kinds["weekly"].remaining_percent - 83.077) < 0.01
assert abs(kinds["monthly"].remaining_percent - 91.538) < 0.01
assert kinds["weekly"].resets_at is not None
print("  ✅ 形态 REAL（items[].periods[] 真实 schema）OK")

# 形态 A：官方 plan 结构（SKUs 列表）
plan_a = {
    "plans": [
        {"plan": "agent-plan-personal-medium", "period": "5h",
         "usedPercent": 30.0, "resetAt": (datetime.now() + timedelta(hours=2)).isoformat()},
        {"plan": "agent-plan-personal-medium", "period": "weekly",
         "usedPercent": 47.5, "resetAt": (datetime.now() + timedelta(days=3)).isoformat()},
        {"plan": "agent-plan-personal-medium", "period": "monthly",
         "used": 38200, "total": 50000,
         "resetAt": (datetime.now() + timedelta(days=17)).isoformat()},
    ]
}
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc._apply_plan(snap, plan_a)
print(f"  A: remain={snap.remaining_percent}  plan={snap.plan_name}  reset={snap.reset_at}")
# monthly 优先（used/total 推导 = 23.6%）
assert abs(snap.remaining_percent - 23.6) < 0.2, f"期望 23.6%，实际 {snap.remaining_percent}"
assert snap.reset_at is not None
print("  ✅ 形态 A（monthly used/total 推导）OK")

# 形态 B：直接 remaining ratio
plan_b = {"data": {"plan": {"planName": "Coding Plan", "monthly": {
    "remainingRatio": 0.81, "resetTime": datetime.now().isoformat()}}}}
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc._apply_plan(snap, plan_b)
assert snap.remaining_percent == 81.0, f"期望 81%，实际 {snap.remaining_percent}"
print(f"  B: remain={snap.remaining_percent}%  plan={snap.plan_name}")
print("  ✅ 形态 B（remainingRatio 0-1）OK")

# stats 解析：真实列式 schema（fields + records 数组）
today_s = datetime.now().strftime("%Y-%m-%d")
yest_s = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
stats = {
    "fields": [{"name": "Day", "type": "DATE"}, {"name": "ModelName", "type": "STRING"},
               {"name": "TotalTokens", "type": "BIGINT"}, {"name": "ReqCnt", "type": "BIGINT"}],
    "records": [
        [today_s, "doubao-seed-1.6", 90000, 60],
        [today_s, "doubao-seed-1.6", 10000, 10],
        [today_s, "doubao-pro-32k", 25000, 13],
        [yest_s, "doubao-seed-1.6", 7000, 5],
    ],
    "data_count": 4,
}
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc._apply_stats(snap, stats)
assert snap.tokens_today == 125000, f"今日 token 应为 125000，实际 {snap.tokens_today}"
assert snap.calls_today == 83, f"今日请求数应为 83，实际 {snap.calls_today}"
assert len(snap.by_model) == 2, "同名模型应聚合"
assert snap.by_model[0].model == "doubao-seed-1.6" and snap.by_model[0].tokens == 100000
assert snap.history == {yest_s: 7000, today_s: 125000}, f"趋势解析错误: {snap.history}"
print(f"  ✅ stats 解析 OK：{snap.tokens_today} tokens / {snap.calls_today} calls / "
      f"{[f'{m.model}:{m.tokens}' for m in snap.by_model]} / hist {len(snap.history)}d")

# 错误响应（缺 --start / 区间超 31 天）不得污染快照
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc._apply_stats(snap, {"ok": False, "error": {
    "type": "validation", "message": "time range must be less than 31 days"}})
assert snap.tokens_today == 0 and not snap.by_model and not snap.history
print("  ✅ stats 错误响应降级 OK")

# records 为空（coding-plan 账号无 ARK 按量推理数据）也不应报错
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc._apply_stats(snap, {"fields": [{"name": "Day", "type": "DATE"}],
                         "records": [], "data_count": 0})
assert snap.tokens_today == 0 and not snap.history
print("  ✅ stats 空结果降级 OK")

# CLI 未安装降级
VolcenginePlatform.cli_command = classmethod(lambda cls: None)
snap = UsageSnapshot(platform=PlatformId.VOLCENGINE, fetched_at=datetime.now())
volc2 = VolcenginePlatform.__new__(VolcenginePlatform)
volc2.credential = ""
snap = volc2.fetch()
assert snap.status == AccountStatus.EXPIRED and "ArkCLI" in snap.error
print(f"  ✅ 未安装降级 OK：{snap.error[:50]}...")

# ---------- 3. Qwen ----------
print("\n[3] Qwen — qianwen usage summary JSON 解析...")
qwen = QwenPlatform.__new__(QwenPlatform)
qwen.credential = ""

summary = {
    "usage": {
        "token_plan": {
            "totalCredits": 50000,
            "remainingCredits": 41700,
            "usedRatio": 0.166,
            "resetDate": (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d"),
        },
        "daily": [{"date": datetime.now().strftime("%Y-%m-%d"), "tokens": 86000}],
    }
}
tp = qwen._find_token_plan(summary)
assert tp is not None and tp["totalCredits"] == 50000
snap = UsageSnapshot(platform=PlatformId.QWEN, fetched_at=datetime.now())
qwen._apply(snap, tp, summary)
qwen._finalize_status(snap)
print(f"  remain={snap.remaining_percent:.1f}%  today_tokens={snap.tokens_today}  "
      f"status={snap.status.value}  reset={snap.reset_at}")
assert abs(snap.remaining_percent - 83.4) < 0.2, f"期望 83.4%，实际 {snap.remaining_percent}"
assert snap.status == AccountStatus.ACTIVE
assert snap.tokens_today == 86000
assert snap.reset_at is not None
print("  ✅ Qwen 解析 OK（remaining/total 优先，今日 tokens 命中）")

# 嵌套无 token_plan 键的兜底
summary2 = {"data": {"credits": {"total": 1000, "remaining": 40}}}
tp2 = qwen._find_token_plan(summary2)
assert tp2 is not None, "兜底查找应命中 credits 节点"
snap2 = UsageSnapshot(platform=PlatformId.QWEN, fetched_at=datetime.now())
qwen._apply(snap2, tp2, summary2)
assert snap2.remaining_percent == 4.0
qwen._finalize_status(snap2)
assert snap2.status == AccountStatus.CRITICAL
print(f"  ✅ 兜底 credits 形态 OK（剩 4% → CRITICAL）")

# CLI 未安装降级
QwenPlatform.cli_path = staticmethod(lambda: None)
snap3 = qwen.fetch()
assert snap3.status == AccountStatus.EXPIRED and "qianwen" in snap3.error
print(f"  ✅ 未安装降级 OK：{snap3.error[:50]}...")

# 清理
shutil.rmtree(tmpdir, ignore_errors=True)

print("\n" + "=" * 60)
print("🎉 CLI 模式适配器测试全部通过")
print("=" * 60)

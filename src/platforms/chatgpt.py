"""
ChatGPT（Codex）Plan 适配器

主通道（实时）—— Codex 官方 app-server JSON-RPC over stdio：
  codex app-server
    → initialize
    → account/rateLimits/read   实时限额（primary=5h、secondary=周额度）
    → account/usage/read        每日 token 桶 + 累计统计
  这是 Codex Desktop / IDE 插件自己用的官方协议，返回的是服务端当前值，
  因此余量能随每次轮询真实变化。

降级通道（本地日志）—— app-server 不可用时读
  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl 的 token_count 事件：
  {"type": "event_msg", "payload": {
     "type": "token_count",
     "info": {"total_token_usage": {"total_tokens": N, ...},
              "last_token_usage": {...}, "model_context_window": 475000},
     "rate_limits": {
        "limit_id": "codex",
        "primary":   {"used_percent": 62.0, "window_minutes": 300,   "resets_at": 1791868720},
        "secondary": {"used_percent": 29.0, "window_minutes": 10080, "resets_at": 1792368378},
        "plan_type": "plus", ...}}}
  注意：rollout 只在 Codex 跑回合时才落盘，数据可能陈旧数小时——
  因此对 resets_at 已过期的窗口按周期长度推算下一次重置并归零已用。

app-server 字段是 camelCase（usedPercent / windowDurationMins / resetsAt），
rollout 是 snake_case，两套 key 均已兼容。

展示口径：remaining_percent 取「最受约束」的窗口（剩余最少者），
全部窗口写入 snap.windows 供详情面板展示。
"""
from __future__ import annotations
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..models import AccountStatus, LimitWindow, ModelUsage, PlatformId, UsageSnapshot
from .base import BasePlatform

# Windows: 不弹出子进程控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 窗口类型 → 周期分钟数（用于推算过期窗口的下一次重置）
_KIND_MINUTES = {"5h": 300, "daily": 1440, "weekly": 10080, "monthly": 43200}


class ChatGPTPlatform(BasePlatform):
    platform = PlatformId.CHATGPT

    APP_SERVER_TIMEOUT = 25.0

    def __init__(self, credential: str = ""):
        # credential 不再使用（保留参数兼容 build_platform）
        super().__init__(credential or "")

    @staticmethod
    def sessions_dir() -> Path:
        return Path.home() / ".codex" / "sessions"

    @staticmethod
    def state_db() -> Path:
        """新版 Codex (Electron) 的实时状态库，threads.tokens_used 随对话实时更新"""
        return Path.home() / ".codex" / "state_5.sqlite"

    # ---------------- Codex 可执行文件定位 ----------------
    @staticmethod
    def codex_candidates() -> list[str]:
        """按优先级返回可用的 codex 可执行文件路径"""
        home = Path.home()
        raw: list[str] = []
        p = shutil.which("codex")
        if p:
            raw.append(p)
        for rel in (
            Path(".codex") / "plugins" / ".plugin-appserver" / "codex.exe",
            Path(".codex") / ".sandbox-bin" / "codex.exe",
            Path(".codex") / "bin" / "codex.exe",
            Path("AppData") / "Local" / "Programs" / "codex" / "codex.exe",
            Path(".codex") / "plugins" / ".plugin-appserver" / "codex",
            Path(".codex") / ".sandbox-bin" / "codex",
        ):
            raw.append(str(home / rel))
        raw += ["/usr/local/bin/codex", "/opt/homebrew/bin/codex"]

        out: list[str] = []
        for c in raw:
            if c in out:
                continue
            try:
                if Path(c).exists():
                    out.append(c)
            except Exception:
                continue
        return out

    def is_available(self) -> bool:
        return bool(self.codex_candidates()) or self.sessions_dir().exists()

    def validate_credential(self) -> tuple[bool, str]:
        if self.codex_candidates():
            live = self._query_app_server()
            if self._apply_live(UsageSnapshot(platform=self.platform), live):
                return True, "Codex app-server 实时额度可读"
        d = self.sessions_dir()
        if not d.exists():
            return False, ("未检测到 Codex CLI（codex 可执行文件与 ~/.codex/sessions 均缺失）。\n"
                           "请安装并登录 Codex CLI 后至少运行一次会话。")
        if self._latest_token_event() is None:
            return False, ("codex app-server 实时通道不可用，且 sessions 目录没有 "
                           "token_count 事件（请先用 Codex 跑一次任务）")
        return True, "Codex 本地用量数据可读（实时通道不可用，已降级为会话日志）"

    # ---------------- app-server（实时通道） ----------------
    def _query_app_server(self) -> Optional[dict]:
        """逐个尝试候选 exe，返回 {"rateLimits": ..., "usage": ...}；全失败返回 None"""
        for exe in self.codex_candidates():
            data = self._run_app_server(exe)
            if data:
                return data
        return None

    def _run_app_server(self, exe: str) -> Optional[dict]:
        """启动 `codex app-server`，完成 initialize → rateLimits/read → usage/read 后退出"""
        env = dict(os.environ)
        # app-server 的 stderr 是 Rust tracing 刷屏日志，直接丢弃
        env["RUST_LOG"] = "off"
        try:
            proc = subprocess.Popen(
                [exe, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env, cwd=str(Path.home()),
            )
        except Exception:
            return None

        q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _pump():
            try:
                for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
                    line = line.strip()
                    if line:
                        q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)

        threading.Thread(target=_pump, daemon=True).start()

        def send(payload: dict) -> bool:
            try:
                proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
                return True
            except Exception:
                return False

        def wait_for(rid: int, deadline: float) -> Optional[dict]:
            """阻塞等待指定 id 的响应；通知/其它响应直接跳过"""
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    line = q.get(timeout=min(left, 0.5))
                except queue.Empty:
                    if proc.poll() is not None:
                        return None
                    continue
                if line is None:      # stdout EOF
                    return None
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("id") == rid:
                    res = msg.get("result")
                    return res if isinstance(res, dict) else None

        out: dict = {}
        try:
            deadline = time.monotonic() + self.APP_SERVER_TIMEOUT
            if not send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"clientInfo": {"name": "agent-monitor", "version": "1.0"},
                                    "capabilities": {"experimentalApi": True}}}):
                return None
            if wait_for(1, deadline) is None:
                # initialize 无响应（可能该 exe 不支持 app-server 子命令）
                return None
            send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read",
                  "params": {"excludeResetCreditDetails": True}})
            rl = wait_for(2, deadline)
            if rl:
                out["rateLimits"] = rl
            send({"jsonrpc": "2.0", "id": 3, "method": "account/usage/read",
                  "params": None})
            us = wait_for(3, deadline)
            if us:
                out["usage"] = us
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return out or None

    def _apply_live(self, snap: UsageSnapshot, live: Optional[dict]) -> bool:
        """把 app-server 结果写进快照；不可用返回 False"""
        if not isinstance(live, dict):
            return False
        rl = live.get("rateLimits")
        if not isinstance(rl, dict):
            return False

        rate = rl.get("rateLimits")
        if not isinstance(rate, dict):
            by_id = rl.get("rateLimitsByLimitId")
            if isinstance(by_id, dict):
                rate = next((v for v in by_id.values() if isinstance(v, dict)), None)
        if not isinstance(rate, dict):
            return False

        windows = self._extract_windows({"rate_limits": rate})
        if not windows:
            return False
        snap.windows = windows

        plan_type = rate.get("planType") or rate.get("plan_type")
        snap.plan_name = (f"ChatGPT {str(plan_type).capitalize()} (Codex)"
                          if plan_type else "ChatGPT Plan (Codex)")

        primary = self._pick_primary(windows)
        if primary is None:
            return False
        snap.remaining_percent = primary.remaining_percent
        snap.reset_at = primary.resets_at

        credits = rl.get("rateLimitResetCredits")
        if isinstance(credits, dict):
            n = self._to_f(credits.get("availableCount"))
            if n:
                snap.plan_name += f" · 加量包 {int(n)}"

        usage = live.get("usage")
        if isinstance(usage, dict):
            self._apply_usage(snap, usage)
        return True

    def _apply_usage(self, snap: UsageSnapshot, usage: dict):
        """account/usage/read → snap.history（每日 token 桶）"""
        buckets = usage.get("dailyUsageBuckets") or usage.get("daily_usage_buckets")
        if isinstance(buckets, list):
            hist: dict[str, int] = {}
            for b in buckets:
                if not isinstance(b, dict):
                    continue
                day = b.get("startDate") or b.get("start_date")
                tk = self._to_f(b.get("tokens"))
                if day and tk is not None:
                    hist[str(day)[:10]] = int(tk)
            if hist:
                snap.history = hist

    # ---------------- 数据拉取 ----------------
    def fetch(self) -> UsageSnapshot:
        snap = UsageSnapshot(platform=self.platform, fetched_at=datetime.now())

        if not self._apply_live(snap, self._query_app_server()):
            reason = self._apply_rollout(snap)
            if reason:
                snap.status = AccountStatus.EXPIRED
                snap.error = reason
                return snap

        self._apply_today_usage(snap)
        self._finalize_status(snap)
        return snap

    # ---------------- 解析（降级通道：rollout 日志） ----------------
    def _apply_rollout(self, snap: UsageSnapshot) -> Optional[str]:
        """成功返回 None，失败返回错误文案"""
        d = self.sessions_dir()
        if not d.exists():
            return ("未检测到 Codex 实时通道，且 ~/.codex/sessions 不存在"
                    "（请安装 Codex CLI 并登录使用一次）")
        found = self._latest_token_event()
        if found is None:
            return "没有可用的 token_count 事件，请先用 Codex 跑一次任务"
        ev, _ts = found
        if not self._apply_event(snap, ev):
            return "token_count 事件中没有可解析的 rate_limits 数据"
        return None

    def _latest_token_event(self) -> Optional[tuple[dict, float]]:
        """跨文件找**事件时间戳最新**的 token_count 事件。
        返回 (payload, epoch_seconds)。

        旧实现按文件 mtime 取「第一个有事件的文件」，会在新会话尚未写出
        token_count 时静默回退到几小时前的旧数据。"""
        d = self.sessions_dir()
        try:
            files = sorted(d.rglob("rollout-*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            return None

        best: Optional[tuple[dict, float]] = None
        for f in files[:30]:
            got = self._read_last_token_event(f)
            if got is None:
                continue
            if best is None or got[1] > best[1]:
                best = got
        return best

    @staticmethod
    def _event_ts(obj: dict, path: Path) -> float:
        """取事件时间戳（epoch 秒）；缺失时退回文件 mtime"""
        for k in ("timestamp", "ts", "time", "created_at"):
            v = obj.get(k)
            if v is None:
                continue
            if isinstance(v, (int, float)):
                ts = float(v)
                return ts / 1000 if ts > 1e12 else ts
            s = str(v).strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return dt.astimezone().timestamp() if dt.tzinfo else dt.timestamp()
            except Exception:
                continue
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0

    @staticmethod
    def _read_last_token_event(path: Path) -> Optional[tuple[dict, float]]:
        """读单个 jsonl，返回最后一条 token_count 事件 (payload, epoch_seconds)"""
        try:
            size = path.stat().st_size
            if size > 20 * 1024 * 1024:
                # 大文件只读尾部 2MB
                with open(path, "rb") as fp:
                    fp.seek(max(0, size - 2 * 1024 * 1024))
                    lines = fp.read().decode("utf-8", errors="replace").splitlines()
                    lines = lines[1:]  # 尾部首行可能截断半行
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as fp:
                    lines = fp.read().splitlines()
        except Exception:
            return None

        for line in reversed(lines):
            line = line.strip()
            if not line or '"token_count"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload") or obj
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    continue
            if not isinstance(payload, dict):
                continue
            # 事件命中：type==token_count，或 payload（含嵌套）有 rate_limits
            hit = payload.get("type") == "token_count"
            if not hit and '"rate_limits"' not in json.dumps(payload, ensure_ascii=False):
                continue
            return payload, ChatGPTPlatform._event_ts(obj, path)
        return None

    # ---------------- rate_limits 解析 ----------------
    @staticmethod
    def _to_f(v) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(str(v).replace("%", "").strip())
        except Exception:
            return None

    def _extract_windows(self, ev: dict) -> list[LimitWindow]:
        """
        从 rate_limits 结构中提取全部限额窗口。兼容三种形态：
          app-server camelCase：{"primary": {"usedPercent": 18,
                                  "windowDurationMins": 300, "resetsAt": 1791973792}}
          rollout 新版 dict：   rate_limits.primary / .secondary（含 window_minutes）
          rollout 旧版 list：   info.rate_limits = [{"window": "5h", ...}, ...]
        """
        info = ev.get("info") if isinstance(ev.get("info"), dict) else {}
        rl = ev.get("rate_limits")
        if not isinstance(rl, (dict, list)):
            rl = info.get("rate_limits")

        windows: list[LimitWindow] = []

        def make(raw: dict, name_hint: str = "") -> Optional[LimitWindow]:
            wm = self._to_f(raw.get("window_minutes") or raw.get("windowDurationMins"))
            text = json.dumps(raw, ensure_ascii=False).lower() + f" {name_hint.lower()}"
            label, kind = self._classify(wm, name_hint, text)

            used = None
            rem = None
            for k in ("used_percent", "usedPercent", "percent_used", "usage_percent"):
                used = self._to_f(raw.get(k))
                if used is not None:
                    break
            if used is None:
                for k in ("used_ratio", "usage_ratio"):
                    used = self._to_f(raw.get(k))
                    if used is not None:
                        used *= 100
                        break
            if used is None:
                for k in ("remaining_percent", "remainingPercent", "percent_remaining"):
                    rem = self._to_f(raw.get(k))
                    if rem is not None:
                        break
            if rem is None:
                for k in ("remaining_ratio",):
                    r = self._to_f(raw.get(k))
                    if r is not None:
                        rem = r * 100
                        break
            if used is not None and rem is None:
                rem = 100.0 - used
            if rem is None:
                # 兜底：used/total token 数推导
                u = self._to_f(raw.get("used_tokens") or raw.get("used"))
                t = self._to_f(raw.get("total_tokens") or raw.get("limit") or raw.get("total"))
                if u is not None and t:
                    rem = (1 - u / t) * 100
            if rem is None:
                return None
            rem = max(0.0, min(100.0, rem))
            if used is not None:
                used = max(0.0, min(100.0, used))
            return LimitWindow(label=label, kind=kind,
                               used_percent=used, remaining_percent=rem,
                               resets_at=self._window_reset(raw))

        if isinstance(rl, dict):
            # primary / secondary（也兼容任意子 dict 带 used_percent/window_minutes）
            for k, v in rl.items():
                if not isinstance(v, dict):
                    continue
                w = make(v, name_hint=str(k))
                if w is not None:
                    windows.append(w)
        elif isinstance(rl, list):
            for v in rl:
                if isinstance(v, dict):
                    w = make(v, name_hint=str(v.get("window") or v.get("type") or ""))
                    if w is not None:
                        windows.append(w)

        # 去重（保持 5h 在前）
        seen: set[str] = set()
        out: list[LimitWindow] = []
        for w in windows:
            if w.kind in seen:
                continue
            seen.add(w.kind)
            out.append(w)
        return self._refresh_stale(out)

    @staticmethod
    def _classify(mins: Optional[float], name_hint: str, text: str = "") -> tuple[str, str]:
        """窗口分钟数 / 名称提示 → (显示名, kind)"""
        hint = str(name_hint or "").lower()
        blob = f"{hint} {text}".lower()
        if mins == 300 or "5h" in blob or "five_hour" in blob or hint == "primary":
            return "5h 窗口", "5h"
        if mins == 10080 or "week" in blob or hint == "secondary":
            return "周额度", "weekly"
        if mins == 1440 or "daily" in blob or "day" in blob:
            return "日额度", "daily"
        if mins == 43200 or "month" in blob:
            return "月额度", "monthly"
        if mins:
            return f"{int(mins) // 60}h 窗口", "other"
        return (hint or "限额窗口"), "other"

    @staticmethod
    def _refresh_stale(windows: list[LimitWindow]) -> list[LimitWindow]:
        """rollout 是被动产物，resets_at 可能早已过期。
        对已过期窗口按周期长度推算下一次重置时间，并把已用归零——
        否则界面会长期显示一个早已重置的旧百分比。"""
        now = datetime.now()
        out: list[LimitWindow] = []
        for w in windows:
            mins = _KIND_MINUTES.get(w.kind)
            if mins and w.resets_at is not None and w.resets_at < now:
                span = timedelta(minutes=mins)
                nxt = w.resets_at
                for _ in range(4096):
                    nxt += span
                    if nxt > now:
                        break
                w = LimitWindow(label=w.label, kind=w.kind,
                                used_percent=0.0, remaining_percent=100.0,
                                resets_at=nxt)
            out.append(w)
        return out

    def _apply_event(self, snap: UsageSnapshot, ev: dict) -> bool:
        """rollout token_count 事件 → 快照。成功返回 True。"""
        snap.plan_name = "ChatGPT Plan (Codex)"

        info = ev.get("info") if isinstance(ev.get("info"), dict) else {}
        rl = ev.get("rate_limits") if isinstance(ev.get("rate_limits"), dict) else None

        # plan_type → 订阅名
        plan_type = (rl or {}).get("plan_type") or (rl or {}).get("planType") \
            or info.get("plan_type")
        if plan_type:
            snap.plan_name = f"ChatGPT {str(plan_type).capitalize()} (Codex)"

        windows = self._extract_windows(ev)
        if not windows:
            return False
        snap.windows = windows

        primary = self._pick_primary(windows)
        if primary is None:
            return False
        snap.remaining_percent = primary.remaining_percent
        snap.reset_at = primary.resets_at

        # 今日 token（该 session 的累计值，_apply_today_usage 会用全天扫描覆盖）
        ttu = info.get("total_token_usage")
        if isinstance(ttu, dict):
            snap.tokens_today = int(self._to_f(ttu.get("total_tokens")) or 0)
        return True

    @staticmethod
    def _window_reset(w: dict) -> Optional[datetime]:
        for k in ("resets_at", "reset_at", "resetsAt", "reset_time", "reset", "resetTime"):
            v = w.get(k)
            if v is None:
                continue
            if isinstance(v, dict):
                v = v.get("time") or v.get("at") or v.get("iso")
            if v is None:
                continue
            if isinstance(v, (int, float)):
                ts = float(v)
                if ts > 1e12:
                    ts /= 1000
                try:
                    return datetime.fromtimestamp(ts)
                except Exception:
                    return None
            s = str(v).strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except Exception:
                for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        return datetime.strptime(s.replace("Z", "+0000"), fmt)
                    except Exception:
                        continue
        return None

    def _apply_today_usage(self, snap: UsageSnapshot):
        """扫描今天的 rollout 文件，累计 token 与模型分布；
        新版 Codex (Electron) 还会把 tokens_used 实时写进 state_5.sqlite，
        rollout jsonl 仅会话结束落盘——两者取新。"""
        d = self.sessions_dir()
        today = datetime.now().date()
        try:
            files = sorted(d.rglob("rollout-*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            files = []

        tokens = 0
        calls = 0
        scanned = 0
        by_model: dict[str, ModelUsage] = {}
        for f in files:
            try:
                if datetime.fromtimestamp(f.stat().st_mtime).date() != today:
                    continue
            except Exception:
                continue
            scanned += 1
            if scanned > 40:
                break
            t, c, models = self._scan_file_today(f)
            tokens += t
            calls += c
            for name, mu in models.items():
                if name in by_model:
                    by_model[name].tokens += mu.tokens
                    by_model[name].calls += mu.calls
                else:
                    by_model[name] = mu

        if scanned:
            snap.tokens_today = max(snap.tokens_today, tokens)
            snap.calls_today = max(snap.calls_today, calls)
            if by_model:
                snap.by_model = sorted(by_model.values(), key=lambda m: -m.calls)

        # 新版 Codex 实时通道：state_5.sqlite.threads.tokens_used
        # rollout 已停写时，这里仍随每次对话刷新
        live_tokens, live_calls = self._read_state_db_today()
        if live_tokens > snap.tokens_today:
            snap.tokens_today = live_tokens
        if live_calls > snap.calls_today:
            snap.calls_today = live_calls

    def _read_state_db_today(self) -> tuple[int, int]:
        """从 state_5.sqlite 读今日活跃 thread 的 tokens_used 总和（实时）。
        返回 (tokens, calls)；DB 不可读时返回 (0, 0)。"""
        db = self.state_db()
        if not db.exists():
            return 0, 0
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                cur = con.cursor()
                # updated_at 是 ms 时间戳；今日 0 点起
                today_start_ms = int(datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
                cur.execute(
                    "SELECT COALESCE(SUM(tokens_used),0), COUNT(*) FROM threads "
                    "WHERE updated_at >= ?", (today_start_ms,))
                row = cur.fetchone()
                if row:
                    return int(row[0] or 0), int(row[1] or 0)
            finally:
                con.close()
        except Exception:
            pass
        return 0, 0

    @staticmethod
    def _scan_file_today(path: Path):
        """
        扫一个文件（一个 session）：
        - tokens：取该文件最后一条 token_count 的 total_token_usage.total_tokens（session 内累计）
        - calls：token_count 事件数（近似为回合数）
        - 模型分布：统计 response 事件的 model 字段
        """
        last_total = 0
        calls = 0
        by_model: dict[str, ModelUsage] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    if '"token_count"' in line:
                        try:
                            obj = json.loads(line)
                            payload = obj.get("payload") or obj
                            if isinstance(payload, dict):
                                calls += 1
                                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                                ttu = info.get("total_token_usage")
                                if isinstance(ttu, dict):
                                    t = ChatGPTPlatform._to_f(ttu.get("total_tokens"))
                                    if t:
                                        last_total = int(t)
                                # 旧版形态
                                total = payload.get("total")
                                if not last_total and isinstance(total, dict):
                                    t = ChatGPTPlatform._to_f(total.get("subtotal"))
                                    if t:
                                        last_total = int(t)
                        except Exception:
                            pass
                    elif '"model"' in line:
                        try:
                            obj = json.loads(line)
                            p = obj.get("payload") or obj
                            if not isinstance(p, dict):
                                continue
                            model = p.get("model")
                            if model is None and isinstance(p.get("response"), dict):
                                model = p["response"].get("model")
                            if model:
                                name = str(model)
                                m = by_model.setdefault(name, ModelUsage(model=name))
                                m.calls += 1
                        except Exception:
                            pass
        except Exception:
            pass
        return last_total, calls, by_model

    # ---------------- 兼容基类 ----------------
    def _build_session(self):
        # CLI 模式无 HTTP session
        return None

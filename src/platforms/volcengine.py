"""
火山方舟（豆包）Plan 适配器 —— 基于 ArkCLI（官方推荐方式）

前置（arkcli v1.0.38+ 实测语法）：
  npm install -g @volcengine/ark-cli@latest
  arkcli auth login volc-sso        # 浏览器 SSO 登录
  # 无浏览器/远程环境：
  #   arkcli auth login volc-sso --no-browser   # 打印 URL，任意设备授权拿码
  #   arkcli auth login --no-browser --code <授权码>

数据源：
  arkcli usage plan [--format json]   套餐额度快照（session/5h、weekly、monthly）
  arkcli usage stats                  推理用量（token / 请求数，按日聚合）
                                      必填 --start YYYY-MM-DD；--interval Day|Hour；
                                      区间 < 31 天；--by model 出按模型明细
  注：usage plan-details 仅支持 AgentPlan，coding-plan 调用必报错，
      且按模型明细已由 usage stats --by model 覆盖，故不再调用。

实现说明：
  Windows 上 arkcli 是 npm 的 .CMD 垫片，从 Python subprocess 调用
  存在编码（GBK）与命令行解析问题——优先解析出
  node_modules/@volcengine/ark-cli 的 JS 入口，用 node.exe 直跑，
  输出为纯 UTF-8 JSON；解析失败时退回 .CMD 直调。
"""
from __future__ import annotations
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..models import AccountStatus, LimitWindow, ModelUsage, PlatformId, UsageSnapshot
from .base import BasePlatform


class VolcenginePlatform(BasePlatform):
    platform = PlatformId.VOLCENGINE

    TIMEOUT = 30
    _PKG = "@volcengine/ark-cli"

    # ---------------- CLI 解析 ----------------
    @staticmethod
    def _find_shim() -> Optional[Path]:
        """定位 arkcli 垫片（.CMD / 无后缀脚本）"""
        p = shutil.which("arkcli")
        if p:
            return Path(p)
        for cand in (Path.home() / "AppData" / "Roaming" / "npm",
                     Path.home() / ".workbuddy" / "binaries" / "node" / "workspace"):
            for name in ("arkcli.cmd", "arkcli.CMD", "arkcli"):
                f = cand / name
                if f.exists():
                    return f
        return None

    @classmethod
    def _find_node(cls) -> Optional[Path]:
        p = shutil.which("node")
        if p:
            return Path(p)
        for cand in (Path("C:/Program Files/nodejs/node.exe"),
                     Path("C:/Program Files (x86)/nodejs/node.exe")):
            if cand.exists():
                return cand
        # 托管 node 目录：垫片所在目录往往就是 node 安装目录
        shim = cls._find_shim()
        if shim is not None:
            for parent in shim.parents:
                n = parent / "node.exe"
                if n.exists():
                    return n
        return None

    @classmethod
    def cli_command(cls) -> Optional[list[str]]:
        """
        返回可直接执行的 argv 前缀。
        优先 [node.exe, <ark-cli>/scripts/run.js]（干净 UTF-8）；
        退化 [arkcli 垫片]。
        """
        shim = cls._find_shim()
        if shim is None:
            return None

        # 垫片同目录（或其上级）下的 node_modules 里找 ark-cli JS 入口
        for base in shim.parents:
            pkg_dir = base / "node_modules" / cls._PKG
            pkg_json = pkg_dir / "package.json"
            if pkg_json.exists():
                try:
                    pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                    bin_ = pkg.get("bin") or {}
                    rel = bin_.get("arkcli") if isinstance(bin_, dict) else bin_
                    if rel:
                        js = (pkg_dir / rel).resolve()
                        node = cls._find_node()
                        if js.exists() and node is not None:
                            return [str(node), str(js)]
                except Exception:
                    pass
                break
        return [str(shim)]

    @classmethod
    def is_installed(cls) -> bool:
        return cls.cli_command() is not None

    def _run(self, *args, timeout: Optional[int] = None) -> tuple[int, str]:
        """运行 arkcli，返回 (returncode, combined_output)"""
        cmd = self.cli_command()
        if not cmd:
            return 127, "arkcli 未安装（npm install -g @volcengine/ark-cli@latest）"
        # 窗口化 exe 拉起子进程时避免闪黑框（CREATE_NO_WINDOW）
        creation = 0x08000000 if sys.platform == "win32" else 0
        try:
            r = subprocess.run(
                [*cmd, *args],
                capture_output=True,
                timeout=timeout or self.TIMEOUT,
                creationflags=creation,
                stdin=subprocess.DEVNULL,
            )
            out = (r.stdout or b"").decode("utf-8", errors="replace")
            err = (r.stderr or b"").decode("utf-8", errors="replace")
            return r.returncode, (out + "\n" + err).strip()
        except subprocess.TimeoutExpired:
            return 124, "CLI 执行超时"
        except Exception as e:
            return 1, f"CLI 执行异常：{e}"

    def _run_json(self, *args, timeout: Optional[int] = None) -> tuple[Optional[dict], str]:
        """
        运行并解析 JSON。
        v1.0.38 实测：错误响应也是 JSON（"ok": false）且退出码非 0——
        所以先固定 --format json 跑一次，无论 rc 都尝试解析输出；
        上层用 data.get("ok") is False 识别业务错误。
        解析不出 JSON 才退回旧 flag 兼容。
        """
        out = ""
        for flag in (("--format", "json"), ("--json",), ("--output", "json")):
            code, out = self._run(*args, *flag, timeout=timeout)
            data = self._try_json(out)
            if data is not None:
                return data, out
        return None, out

    @staticmethod
    def _try_json(text: str) -> Optional[dict]:
        text = (text or "").strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except Exception:
            # JSON 可能混在日志里，抓第一个平衡的 {...}
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
        return None

    @staticmethod
    def _err_message(data: Optional[dict], raw: str) -> str:
        """从 ok:false 响应或原始输出中提取人类可读错误"""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)[:200]
            if data.get("message"):
                return str(data.get("message"))[:200]
        return (raw or "").strip()[:200]

    # ---------------- 兼容基类 ----------------
    def _build_session(self):
        # CLI 模式无 HTTP session
        return None

    # ---------------- 登录态 ----------------
    def ensure_login(self) -> bool:
        """设置里填了 AK/SK 就用 config init 写入 profile；留空则依赖 CLI 已有登录态"""
        cred = (self.credential or "").strip()
        if not cred:
            return True
        parts = [p.strip() for p in re.split(r"[,\s;]+", cred) if p.strip()]
        if len(parts) < 2:
            return True  # 不像 AK/SK 对，不强行配置
        ak, sk = parts[0], parts[1]
        # v1.0.38：AK/SK 走 config init（profile），不是旧的 login --ak/--sk
        code, _ = self._run("config", "init",
                            "--access-key", ak, "--secret-key", sk,
                            "--set-default", timeout=20)
        return code == 0

    def _login_state(self) -> tuple[str, str]:
        """返回 (auth_method, hint)。auth_method: none/apikey/sso/…"""
        data, raw = self._run_json("auth", "status", timeout=15)
        if isinstance(data, dict):
            return str(data.get("auth_method") or ""), str(data.get("hint") or "")
        low = raw.lower()
        if "logged_in" in low:
            return ("none", "") if "false" in low else ("sso", "")
        return "", raw[:120]

    def validate_credential(self) -> tuple[bool, str]:
        if not self.is_installed():
            return False, ("未安装 ArkCLI。安装：\n"
                           "  npm install -g @volcengine/ark-cli@latest")
        self.ensure_login()
        data, raw = self._run_json("usage", "plan", timeout=30)
        if data is not None and data.get("ok") is not False and "error" not in (data or {}):
            return True, "ArkCLI 已登录，套餐数据可读"
        msg = self._err_message(data, raw)
        low = msg.lower() + " " + raw.lower()
        if "sso" in low or "auth login" in low or "refresh_token" in low:
            return False, ("ArkCLI 未登录（套餐查询需要 SSO 登录态）。\n"
                           "请运行：arkcli auth login volc-sso\n"
                           "（远程/无浏览器环境可用 --no-browser 模式）")
        if "not configured" in low:
            return False, "ArkCLI 未配置 profile，请先 arkcli auth login volc-sso"
        if msg:
            return False, f"ArkCLI 返回异常：{msg[:120]}"
        return False, "ArkCLI 无输出（请升级到最新版）"

    # ---------------- 数据拉取 ----------------
    def fetch(self) -> UsageSnapshot:
        snap = UsageSnapshot(platform=self.platform, fetched_at=datetime.now())

        if not self.is_installed():
            snap.status = AccountStatus.EXPIRED
            snap.error = "未安装 ArkCLI（npm install -g @volcengine/ark-cli@latest）"
            return snap

        self.ensure_login()

        # 1) 套餐额度快照（错误响应也是 JSON：ok:false）
        plan, raw = self._run_json("usage", "plan", timeout=30)
        ok = plan is not None and plan.get("ok") is not False and "error" not in (plan or {})
        if not ok:
            msg = self._err_message(plan, raw)
            low = msg.lower() + " " + raw.lower()
            if ("sso" in low or "auth login" in low or "refresh_token" in low
                    or "not configured" in low or "logged" in low):
                snap.status = AccountStatus.EXPIRED
                snap.error = ("ArkCLI 未登录/未配置，请运行：arkcli auth login volc-sso"
                              "（无浏览器环境可用 --no-browser）")
            else:
                snap.status = AccountStatus.UNKNOWN
                snap.error = f"arkcli usage plan 失败：{msg or '无输出'}"
            return snap

        self._apply_plan(snap, plan)

        # 2) 推理用量：一次拉「近 30 天 × 按模型」，同时得到趋势、今日量与模型分布。
        #    usage stats 的硬性约束（v1.0.38 实测）：--start 必填且为 YYYY-MM-DD；
        #    --interval 取值是 Day|Hour（首字母大写）；单次区间必须 < 31 天，
        #    否则返回 {"ok":false,"error":…}。失败不致命。
        now = datetime.now()
        stats, _ = self._run_json(
            "usage", "stats",
            "--start", (now - timedelta(days=29)).strftime("%Y-%m-%d"),
            "--end", now.strftime("%Y-%m-%d"),
            "--interval", "Day", "--by", "model",
            timeout=20)
        self._apply_stats(snap, stats)

        self._finalize_status(snap)
        return snap

    # ---------------- 解析 ----------------
    def _apply_plan(self, snap: UsageSnapshot, plan: dict):
        """
        真实 arkcli usage plan schema（v1.0.38+ 实测）：
          {"items": [{"product": "coding-plan", "edition": "personal",
                      "subscribed": true,
                      "periods": [{"label": "session", "percent": 0.0016,
                                   "reset_at": "2026-10-13T19:54:58+08:00"},
                                  {"label": "weekly",  "percent": 16.92, ...},
                                  {"label": "monthly", "percent": 8.46,  ...}]}]}
        percent 是 **已用百分比**（0-100）；label "session" 即 5h 窗口。
        优先走这条精确路径，匹配不到再退回旧版宽松探测。
        """
        if self._apply_plan_items(snap, plan):
            return
        self._apply_plan_legacy(snap, plan)

    # 真实 schema: period.label → (kind, 显示名)
    _PERIOD_KIND = {
        "session": ("5h", "5h 窗口"),
        "5h": ("5h", "5h 窗口"),
        "five_hour": ("5h", "5h 窗口"),
        "daily": ("daily", "日额度"),
        "weekly": ("weekly", "周额度"),
        "monthly": ("monthly", "月额度"),
    }

    def _apply_plan_items(self, snap: UsageSnapshot, plan: dict) -> bool:
        """解析 items[].periods[] 真实 schema，命中返回 True"""
        items = plan.get("items") if isinstance(plan, dict) else None
        if not isinstance(items, list) or not items:
            return False

        # 取第一个 subscribed 的 item（或第一个 item）
        chosen = next((it for it in items
                       if isinstance(it, dict) and it.get("subscribed")), None)
        if chosen is None:
            chosen = items[0] if isinstance(items[0], dict) else None
        if chosen is None:
            return False

        periods = chosen.get("periods")
        if not isinstance(periods, list) or not periods:
            return False

        # 订阅名：product + edition
        product = chosen.get("product") or ""
        edition = chosen.get("edition") or ""
        plan_name = " ".join(p for p in (str(product), str(edition)) if p).strip()
        snap.plan_name = plan_name or "方舟订阅"

        # 逐 period 解析窗口
        windows: list[LimitWindow] = []
        for p in periods:
            if not isinstance(p, dict):
                continue
            label = str(p.get("label") or "").lower().strip()
            kind_info = self._PERIOD_KIND.get(label)
            if kind_info is None:
                # 未识别 label 也保留（按 other 处理）
                kind, disp = "other", label or "限额窗口"
            else:
                kind, disp = kind_info
            used_pct = self._to_float(p.get("percent"))
            if used_pct is None:
                # 兜底：可能直接给 remaining
                rem = self._to_float(p.get("remaining_percent") or p.get("remainingPercent"))
            else:
                rem = max(0.0, 100.0 - used_pct)
            if rem is None:
                continue
            reset = self._parse_dt(p.get("reset_at") or p.get("resetAt"))
            windows.append(LimitWindow(label=disp, kind=kind,
                                       used_percent=used_pct,
                                       remaining_percent=rem,
                                       resets_at=reset))
        if not windows:
            return False

        # 去重：同 kind 保留第一个
        seen: set[str] = set()
        uniq: list[LimitWindow] = []
        for w in windows:
            if w.kind in seen:
                continue
            seen.add(w.kind)
            uniq.append(w)
        snap.windows = uniq

        # 主口径：取「最受约束」的窗口（剩余百分比最小者）。
        # 不能固定取 monthly —— 实测 monthly 21 小时只变化 0.0003，
        # 头部数字看起来像卡死；weekly / 5h 才是真正随使用波动的那个。
        primary = self._pick_primary(uniq)
        if primary is not None:
            snap.remaining_percent = primary.remaining_percent
            snap.reset_at = primary.resets_at
        return True

    def _apply_plan_legacy(self, snap: UsageSnapshot, plan: dict):
        """旧版宽松探测（保留作为兜底）"""
        snap.plan_name = "方舟订阅"

        nodes = self._flatten(plan)

        # 叶子候选：所有值均为标量的 dict（具体数据节点，而非聚合容器）
        def is_leaf(n) -> bool:
            if not isinstance(n, dict) or not n:
                return False
            return all(isinstance(v, (str, int, float, bool)) or v is None
                       for v in n.values())

        leaves = [n for n in nodes if is_leaf(n)]
        # 只在含周期关键词的叶子中挑
        cyc_leaves = []
        for n in leaves:
            text = json.dumps(n, ensure_ascii=False).lower()
            if any(t in text for t in ("5h", "weekly", "monthly", "agent-plan", "coding-plan")):
                cyc_leaves.append(n)

        best: Optional[dict] = None
        pool = cyc_leaves or leaves
        if pool:
            for n in pool:
                if best is None or self._plan_score(n) > self._plan_score(best):
                    best = n

        if best is None:
            dicts = [n for n in nodes if isinstance(n, dict)]
            best = dicts[0] if dicts else None
        if best is None:
            return

        keys = {k.lower(): k for k in best.keys()}

        def get(*names, default=None):
            for nm in names:
                if nm in keys:
                    v = best[keys[nm]]
                    if v is not None:
                        return v
            return default

        # SKU / 周期名
        plan_name = get("plan", "planname", "sku", "product", default=None)
        if isinstance(plan_name, dict):
            plan_name = plan_name.get("name") or plan_name.get("id")
        if plan_name:
            snap.plan_name = str(plan_name)

        remaining, reset = self._leaf_metrics(best)
        snap.remaining_percent = remaining
        snap.reset_at = reset

        # 多窗口：把 5h / weekly / monthly 各周期叶子都收集进 snap.windows
        windows: list[LimitWindow] = []
        seen: set[str] = set()
        for n in cyc_leaves or []:
            text = json.dumps(n, ensure_ascii=False).lower()
            if "monthly" in text:
                kind, label = "monthly", "月额度"
            elif "weekly" in text:
                kind, label = "weekly", "周额度"
            elif "5h" in text or "five" in text:
                kind, label = "5h", "5h 窗口"
            else:
                continue
            if kind in seen:
                continue
            rem, rst = self._leaf_metrics(n)
            if rem is None:
                continue
            seen.add(kind)
            windows.append(LimitWindow(label=label, kind=kind,
                                       remaining_percent=rem, resets_at=rst))
        if windows:
            snap.windows = windows

    def _leaf_metrics(self, leaf: dict) -> tuple[Optional[float], Optional[datetime]]:
        """从单个周期叶子节点提取 (剩余百分比, 重置时间)"""
        keys = {k.lower(): k for k in leaf.keys()}

        def get(*names, default=None):
            for nm in names:
                if nm in keys:
                    v = leaf[keys[nm]]
                    if v is not None:
                        return v
            return default

        raw_json = json.dumps(leaf, ensure_ascii=False).lower()
        remaining = self._to_float(
            get("remainingpercent", "remaining_percent", "remainingratio", "remaining_ratio",
                "percentremaining", "leftpercent", "remain", default=None))
        if remaining is not None and remaining <= 1.0 and "percent" not in raw_json:
            remaining *= 100
        if remaining is None:
            used_pct = self._to_float(
                get("usedpercent", "used_percent", "usedratio", "used_ratio", default=None))
            if used_pct is not None:
                used_pct = used_pct * 100 if used_pct <= 1.0 else used_pct
                remaining = max(0.0, 100.0 - used_pct)
        if remaining is None:
            used = self._to_float(get("used", "usedquota", "used_quota", default=None))
            total = self._to_float(get("total", "quota", "totalquota", default=None))
            if used is not None and total:
                remaining = max(0.0, (1 - used / total) * 100)

        reset = self._parse_dt(get("resetat", "reset_at", "resetime", "reset_time",
                                   "refreshat", "refresh_at", "nextreset", default=None))
        if reset is None:
            secs = self._to_float(get("resetinseconds", "reset_seconds", "ttl", default=None))
            if secs and secs > 0:
                reset = datetime.now() + timedelta(seconds=secs)
        return remaining, reset

    @staticmethod
    def _plan_score(n: dict) -> float:
        """展示优先级：monthly > weekly > 5h"""
        text = json.dumps(n, ensure_ascii=False).lower()
        score = 0.0
        if "monthly" in text:
            score += 3
        if "weekly" in text:
            score += 2
        if "5h" in text or "five" in text:
            score += 1
        return score

    @staticmethod
    def _stats_records(stats: Optional[dict]) -> list[dict]:
        """把 usage stats 的列式响应归一成 dict 列表。

        真实 schema（v1.0.38 实测）：
          {"fields": [{"name": "Day", "type": "DATE"},
                      {"name": "TotalTokens", "type": "BIGINT"},
                      {"name": "ReqCnt", "type": "BIGINT"}, …],
           "records": […], "data_count": N}
        records 可能是与 fields 对齐的数组，也可能已是 dict；两种都兼容。
        所有键统一转小写，便于按名取字段。
        """
        if not isinstance(stats, dict) or stats.get("ok") is False:
            return []
        records = stats.get("records")
        if not isinstance(records, list) or not records:
            return []
        fields = stats.get("fields")
        names = [str(f.get("name")) for f in fields if isinstance(f, dict)] \
            if isinstance(fields, list) else []
        out: list[dict] = []
        for r in records:
            if isinstance(r, dict):
                out.append({str(k).lower(): v for k, v in r.items()})
            elif isinstance(r, (list, tuple)) and len(names) == len(r):
                out.append({n.lower(): v for n, v in zip(names, r)})
        return out

    def _apply_stats(self, snap: UsageSnapshot, stats: Optional[dict]):
        """从一份「近 30 天 × 按模型」的 usage stats 得出：
        近 30 天趋势、今日 token/请求数、今日按模型分布。"""
        today = datetime.now().strftime("%Y-%m-%d")
        hist: dict[str, int] = {}
        merged: dict[str, ModelUsage] = {}
        for r in self._stats_records(stats):
            day = str(r.get("day") or "")[:10]
            tokens = int(self._to_float(r.get("totaltokens")) or 0)
            if len(day) == 10:
                hist[day] = hist.get(day, 0) + tokens
            if day != today:
                continue
            calls = int(self._to_float(r.get("reqcnt")) or 0)
            snap.tokens_today += tokens
            snap.calls_today += calls
            model = r.get("modelname") or r.get("model")
            if not model:
                continue
            m = merged.get(str(model))
            if m is None:
                merged[str(model)] = ModelUsage(model=str(model),
                                                tokens=tokens, calls=calls)
            else:
                m.tokens += tokens
                m.calls += calls
        if hist:
            snap.history = dict(sorted(hist.items()))
        if merged:
            snap.by_model = sorted(merged.values(), key=lambda m: -m.tokens)

    # ---------------- 小工具 ----------------
    @staticmethod
    def _flatten(obj) -> list:
        """把嵌套 dict/list 拉平成所有 dict 节点列表"""
        out: list = []

        def walk(o):
            if isinstance(o, dict):
                out.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(obj)
        return out

    @staticmethod
    def _to_float(v) -> Optional[float]:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace(",", "").replace("%", "").strip())
        except Exception:
            return None

    @staticmethod
    def _parse_dt(v) -> Optional[datetime]:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            ts = float(v)
            if ts > 1e12:  # ms
                ts /= 1000
            try:
                return datetime.fromtimestamp(ts)
            except Exception:
                return None
        s = str(v).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s.replace("Z", "+0000"), fmt)
                if dt.tzinfo:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except Exception:
                continue
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:
            return None

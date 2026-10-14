"""
千问（Qwen Token Plan）适配器 —— 浏览器自动化抓取控制台页面

为什么不走 CLI / OpenAPI
------------------------
- 本机没有 qianwen / qwen CLI，也没有可安装的官方包名；
- Token Plan 的用量数据没有对外开放的 OpenAPI（阿里云 OpenAPI + AK/SK
  那套属于「百炼」门户，和 platform.qianwenai.com 是两套入口）；
- 唯一稳定、且官方就是给人看的数据入口，是控制台页面本身：
      https://platform.qianwenai.com/home/analytics/token-plan/individual
  （工作台 > Token Plan > 我的订阅）

所以这里复用项目已有的 PySide6，用 QtWebEngine（Qt 自带 Chromium，
无需 Playwright / Selenium，也不用额外下载内核）带登录态打开该页面，
录下页面自己发出的接口响应，再从里面解析额度。详见 src/webengine_fetch.py。

登录：设置窗口 → 千问 → 「打开控制台登录」，登录一次即可，
Cookie 持久化在 %APPDATA%/AgentMonitor/web-profile。

凭证字段：留空即用默认控制台地址；如需自定义（例如企业专属域名或
个人版/团队版另一个路径），把完整 URL 填进凭证框即可。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from ..models import AccountStatus, LimitWindow, PlatformId, UsageSnapshot
from .base import BasePlatform

# 控制台页面（工作台 > Token Plan > 我的订阅）
DEFAULT_CONSOLE_URL = "https://platform.qianwenai.com/home/analytics/token-plan/individual"
# 登录入口：直接开首页，未登录时它自己会引导到登录/SSO
LOGIN_URL = "https://platform.qianwenai.com/"


class QwenPlatform(BasePlatform):
    platform = PlatformId.QWEN

    # 页面加载完后再给 SPA 的时间；控制台接口不多，8s 足够
    SETTLE_MS = 8000
    # 整体墙钟上限（含 DNS / 重定向 / 渲染）
    TIMEOUT = 45.0

    def _build_session(self):
        return None

    # ---------------- 配置 ----------------
    def console_url(self) -> str:
        """凭证框复用为「自定义控制台地址」，留空则用默认。"""
        c = (self.credential or "").strip()
        if c.startswith("http://") or c.startswith("https://"):
            return c
        return DEFAULT_CONSOLE_URL

    @staticmethod
    def engine_available() -> tuple[bool, str]:
        from .. import webengine_fetch
        return webengine_fetch.is_available()

    def is_installed(self) -> bool:
        return self.engine_available()[0]

    def validate_credential(self) -> tuple[bool, str]:
        ok, err = self.engine_available()
        if not ok:
            return False, err
        snap = self.fetch()
        if snap.remaining_percent is not None:
            extra = f"（{snap.plan_name}）" if snap.plan_name else ""
            return True, f"控制台可读，剩余 {snap.remaining_percent:.1f}%{extra}"
        if snap.status == AccountStatus.EXPIRED:
            return False, snap.error or "未登录，请在设置里点「打开控制台登录」"
        return False, snap.error or "未能从控制台页面解析出额度"

    # ---------------- 数据拉取 ----------------
    def fetch(self) -> UsageSnapshot:
        snap = UsageSnapshot(platform=self.platform, fetched_at=datetime.now())

        ok, err = self.engine_available()
        if not ok:
            snap.status = AccountStatus.UNKNOWN
            snap.error = err
            return snap

        from .. import webengine_fetch
        url = self.console_url()
        res = webengine_fetch.fetch_page(url, settle_ms=self.SETTLE_MS,
                                        timeout=self.TIMEOUT)

        # 被踢到登录页 → 明确的 EXPIRED，提示用户去登录窗口
        if webengine_fetch.looks_logged_out(res.url, res.title, res.text):
            snap.status = AccountStatus.EXPIRED
            snap.error = "千问控制台未登录，请在 设置 → 千问 → 打开控制台登录"
            return snap

        if not res.ok:
            snap.status = AccountStatus.UNKNOWN
            snap.error = f"控制台页面抓取失败：{res.error or '未知原因'}"
            return snap

        # 1) 首选：页面自己发出的接口 JSON
        best, best_score = None, -1
        for api_url, data in res.json_captures():
            tp = self._find_token_plan(data)
            if tp is None:
                continue
            score = self._score(tp)
            if score > best_score:
                best, best_score, best_full = tp, score, data

        if best is not None:
            self._apply(snap, best, best_full)
            self._finalize_status(snap)
            return snap

        # 2) 兜底：直接读页面上渲染出来的文字
        if self._apply_from_text(snap, res.text):
            self._finalize_status(snap)
            return snap

        snap.status = AccountStatus.UNKNOWN
        snap.error = ("控制台页面已打开，但没找到额度数据"
                      "（页面可能改版，或当前账号未订阅 Token Plan）")
        return snap

    # ---------------- 解析 ----------------
    @staticmethod
    def _score(tp: dict) -> int:
        """给候选额度节点打分：同时有总量和剩余量的最可信。"""
        keys = {k.lower() for k in tp.keys()}
        has_total = any("total" in k for k in keys)
        has_remain = any(("remain" in k or "left" in k) for k in keys)
        has_ratio = any(("ratio" in k or "percent" in k) for k in keys)
        if has_total and has_remain:
            return 3
        if has_ratio:
            return 2
        if has_total or has_remain:
            return 1
        return 0

    @staticmethod
    def _find_token_plan(data) -> Optional[dict]:
        """在嵌套结构中找 token_plan 节点；找不到时找含 credits 字段的节点"""
        result: Optional[dict] = None

        def walk(o):
            nonlocal result
            if result is not None:
                return
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("token_plan", "tokenPlan", "plan") and isinstance(v, dict):
                        result = v
                        return
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(data)
        if result is not None:
            return result

        # 兜底：键名含 credit 的节点（自身带 remain/total，或值就是额度 dict）
        def walk2(o):
            nonlocal result
            if result is not None:
                return
            if isinstance(o, dict):
                keys = {k.lower(): k for k in o.keys()}
                # 情况 1：{"credits": {...}} —— 键含 credit，值是 dict
                for kl, k in keys.items():
                    if "credit" in kl and isinstance(o[k], dict):
                        sub = o[k]
                        subkeys = {x.lower() for x in sub.keys()} if isinstance(sub, dict) else set()
                        if subkeys and any(("remain" in x or "total" in x) for x in subkeys):
                            result = sub
                            return
                # 情况 2：{"totalCredits": ..., "remainingCredits": ...} —— 自身即额度节点
                if any("credit" in k for k in keys) and \
                        any(("remain" in k or "total" in k) for k in keys):
                    result = o
                    return
                for v in o.values():
                    walk2(v)
            elif isinstance(o, list):
                for v in o:
                    walk2(v)

        walk2(data)
        return result

    def _apply(self, snap: UsageSnapshot, tp: dict, full: dict):
        snap.plan_name = self._plan_name(tp) or "千问 Token Plan"

        keys = {k.lower(): k for k in tp.keys()}

        def get(*names, default=None):
            for nm in names:
                if nm in keys:
                    v = tp[keys[nm]]
                    if v is not None:
                        return v
            return default

        total_credits = self._to_f(get("totalcredits", "total_credits", "credits",
                                       "total", default=None))
        remain_credits = self._to_f(get("remainingcredits", "remaining_credits", "remain",
                                        "left", "remaining", "available", default=None))
        used_credits = self._to_f(get("usedcredits", "used_credits", "used",
                                      "consumed", default=None))
        used_ratio = self._to_f(get("usedratio", "used_ratio", "usedpercent", "used_percent",
                                    "usage_ratio", default=None))

        # 三个数只要有任意两个就能凑齐
        if total_credits and remain_credits is None and used_credits is not None:
            remain_credits = max(0.0, total_credits - used_credits)
        if remain_credits is not None and total_credits is None and used_credits is not None:
            total_credits = remain_credits + used_credits

        if total_credits:
            if remain_credits is not None:
                snap.remaining_percent = max(0.0, min(100.0, remain_credits / total_credits * 100))
            elif used_ratio is not None:
                snap.remaining_percent = self._remain_from_ratio(used_ratio)
        elif used_ratio is not None:
            snap.remaining_percent = self._remain_from_ratio(used_ratio)

        if total_credits and remain_credits is not None:
            snap.plan_name += f" · 剩余 {remain_credits:,.0f}/{total_credits:,.0f} Credits"

        # resetDate（订阅实例 EndTime）
        reset = get("resetdate", "reset_date", "resetat", "reset_at", "enddate",
                    "end_date", "endtime", "end_time", "expiretime", default=None)
        if reset:
            snap.reset_at = self._parse_dt(reset)

        # 今日 tokens（如果同一份响应里带了）
        daily = self._find_today_tokens(full)
        if daily:
            snap.tokens_today = daily

        # 千问只有订阅额度这一个口径 → 单窗口
        if snap.remaining_percent is not None:
            snap.windows = [LimitWindow(
                label="额度", kind="quota",
                remaining_percent=snap.remaining_percent,
                resets_at=snap.reset_at,
            )]

    @staticmethod
    def _remain_from_ratio(used_ratio: float) -> float:
        r = used_ratio * 100 if used_ratio <= 1.0 else used_ratio
        return max(0.0, min(100.0, 100.0 - r))

    @staticmethod
    def _plan_name(tp: dict) -> str:
        keys = {k.lower(): k for k in tp.keys()}
        for nm in ("planname", "specname", "packagename", "subscriptionname",
                   "edition", "name", "type"):
            if nm in keys:
                v = tp[keys[nm]]
                if isinstance(v, str) and v.strip() and len(v.strip()) <= 40:
                    return v.strip()
        return ""

    @staticmethod
    def _to_f(v) -> Optional[float]:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace(",", "").replace("%", "").strip())
        except Exception:
            return None

    # ---- DOM 文字兜底 ----
    _NUM = r"(\d[\d,]*(?:\.\d+)?)"
    _TEXT_PATTERNS = (
        # "剩余 1,234 / 5,000 Credits"
        re.compile(rf"剩余[^0-9\-]{{0,12}}{_NUM}\s*/\s*{_NUM}", re.I),
        # "Remaining 1234 of 5000 Credits"
        re.compile(rf"remain\w*[^0-9]{{0,12}}{_NUM}\s*(?:/|of)\s*{_NUM}", re.I),
        # "Credits 1234/5000"
        re.compile(rf"credits?[^0-9]{{0,12}}{_NUM}\s*/\s*{_NUM}", re.I),
    )
    _PERCENT_PATTERN = re.compile(
        r"(?:剩余|remaining|remain)[^0-9]{0,12}(\d{1,3}(?:\.\d+)?)\s*%", re.I)

    def _apply_from_text(self, snap: UsageSnapshot, text: str) -> bool:
        """接口没抓到时，退而求其次读页面上渲染出来的数字。

        只在能明确匹配到「剩余/总量」或「剩余百分比」时才认，
        宁可不返回也不要给个错数字。
        """
        if not text:
            return False
        for pat in self._TEXT_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            remain = self._to_f(m.group(1))
            total = self._to_f(m.group(2))
            if not total or remain is None or remain > total:
                continue
            snap.remaining_percent = max(0.0, remain / total * 100)
            snap.plan_name = f"千问 Token Plan · 剩余 {remain:,.0f}/{total:,.0f} Credits"
            snap.windows = [LimitWindow(label="额度", kind="quota",
                                        remaining_percent=snap.remaining_percent)]
            return True

        m = self._PERCENT_PATTERN.search(text)
        if m:
            pct = self._to_f(m.group(1))
            if pct is not None and 0 <= pct <= 100:
                snap.remaining_percent = pct
                snap.plan_name = "千问 Token Plan"
                snap.windows = [LimitWindow(label="额度", kind="quota",
                                            remaining_percent=pct)]
                return True
        return False

    @staticmethod
    def _find_today_tokens(full) -> int:
        """在响应里找今日 token 统计"""
        today = datetime.now().strftime("%Y-%m-%d")
        found = 0

        def walk(o):
            nonlocal found
            if found:
                return
            if isinstance(o, dict):
                keys = {k.lower(): k for k in o.keys()}
                text = json.dumps(o, ensure_ascii=False)
                if today in text:
                    for kl in ("todaytokens", "today_tokens", "tokens", "totaltokens"):
                        if kl in keys:
                            try:
                                found = int(float(o[keys[kl]]))
                                return
                            except Exception:
                                pass
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(full)
        return found

    @staticmethod
    def _parse_dt(v) -> Optional[datetime]:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            ts = float(v)
            if ts > 1e12:
                ts /= 1000
            try:
                return datetime.fromtimestamp(ts)
            except Exception:
                return None
        s = str(v).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.replace("Z", "+0000"), fmt)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

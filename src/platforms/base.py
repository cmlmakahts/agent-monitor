"""
平台适配器抽象基类。所有平台实现以下接口：
  - fetch() -> UsageSnapshot：拉取当前用量快照
  - validate_credential(s) -> (ok, msg)：启动前检查 token/cookie 是否能用

实现策略：
  Plan 余量各家均无公开 API，因此统一用「cookie/token 注入 + 内部接口抓取」。
  接口路径变动时返回 EXPIRED 状态，提示用户更新凭证。
"""
from __future__ import annotations
import abc
from datetime import datetime
from typing import Optional

import requests

from ..models import AccountStatus, LimitWindow, PlatformId, UsageSnapshot


class BasePlatform(abc.ABC):
    platform: PlatformId

    # CLI 模式下 credential 大多可为空（复用 CLI 本地登录态）
    def __init__(self, credential: str = ""):
        self.credential = (credential or "").strip()
        self.session = self._build_session()

    @abc.abstractmethod
    def _build_session(self) -> requests.Session:
        ...

    @abc.abstractmethod
    def validate_credential(self) -> tuple[bool, str]:
        """验证凭证是否可用，返回 (ok, message)"""
        ...

    @abc.abstractmethod
    def fetch(self) -> UsageSnapshot:
        """获取最新用量快照。任何异常 / 401 / 解析失败都要自处理并返回合理状态。"""
        ...

    # ---------------- 工具 ----------------
    @staticmethod
    def _pick_primary(windows: list[LimitWindow]) -> Optional[LimitWindow]:
        """头部指标口径：取「最受约束」的窗口——剩余百分比最小者，
        并列时取重置时间最近者。这样余量数字会随实际使用明显变动，
        而不是长期停在变化极慢的月额度上。"""
        if not windows:
            return None

        def key(w: LimitWindow):
            rem = w.remaining_percent if w.remaining_percent is not None else 100.0
            rst = w.resets_at.timestamp() if w.resets_at else float("inf")
            return (rem, rst)

        return min(windows, key=key)

    @staticmethod
    def _finalize_status(snap: UsageSnapshot):
        r = snap.remaining_percent
        if r is None:
            return
        if r < 5:
            snap.status = AccountStatus.CRITICAL
        elif r < 20:
            snap.status = AccountStatus.WARNING
        else:
            snap.status = AccountStatus.ACTIVE

    def _empty_snapshot(self, *, error: Optional[str] = None,
                        status: AccountStatus = AccountStatus.UNKNOWN) -> UsageSnapshot:
        return UsageSnapshot(
            platform=self.platform,
            status=status,
            error=error,
            fetched_at=datetime.now(),
        )

    def _session_expired_snapshot(self, reason: str) -> UsageSnapshot:
        return self._empty_snapshot(
            error=f"凭证已失效：{reason}",
            status=AccountStatus.EXPIRED,
        )

"""
Agent订阅监控器 - 统一数据模型
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class PlatformId(str, Enum):
    CHATGPT = "chatgpt"
    VOLCENGINE = "volcengine"   # 火山方舟
    QWEN = "qwen"               # 千问 Token Plan（浏览器抓取控制台）


class AccountStatus(str, Enum):
    ACTIVE = "active"           # 正常
    WARNING = "warning"         # 余量 < 20%
    CRITICAL = "critical"       # 余量 < 5%
    EXPIRED = "expired"         # 凭证失效 / 需重新登录
    UNKNOWN = "unknown"         # 拉取失败


@dataclass
class ModelUsage:
    """单模型用量"""
    model: str
    tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0


@dataclass
class LimitWindow:
    """单个限额窗口（如 ChatGPT 的 5h 窗口 / 周额度）"""
    label: str                                   # "5h 窗口" / "周额度"
    kind: str = "other"                          # "5h" / "weekly" / "monthly" / "other"
    used_percent: Optional[float] = None
    remaining_percent: Optional[float] = None
    resets_at: Optional[datetime] = None


@dataclass
class UsageSnapshot:
    """平台用量快照"""
    platform: PlatformId
    account_email: Optional[str] = None
    plan_name: Optional[str] = None       # "Plus", "Pro", "体验套餐" 等

    # 核心余量
    remaining_percent: Optional[float] = None   # 0-100, None=未知
    reset_at: Optional[datetime] = None         # 下次重置时间

    # 多窗口余量（ChatGPT: 5h + weekly；其它平台可为空）
    windows: list[LimitWindow] = field(default_factory=list)

    # 今日用量
    tokens_today: int = 0
    calls_today: int = 0
    cost_today_usd: float = 0.0

    # 模型分布
    by_model: list[ModelUsage] = field(default_factory=list)

    # 月度趋势 (key=YYYY-MM-DD, value=tokens)
    history: dict[str, int] = field(default_factory=dict)

    status: AccountStatus = AccountStatus.UNKNOWN
    error: Optional[str] = None
    fetched_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["platform"] = self.platform.value
        d["status"] = self.status.value
        d["reset_at"] = self.reset_at.isoformat() if self.reset_at else None
        d["fetched_at"] = self.fetched_at.isoformat()
        return d


@dataclass
class AppConfig:
    """应用配置（加密落盘）"""
    # 各平台 token / cookie，key 为 PlatformId.value
    credentials: dict[str, str] = field(default_factory=dict)

    # 轮询间隔（秒）
    poll_interval_sec: int = 60

    # 托盘主题
    dark_mode: bool = True

    # 通知阈值（剩余百分比）
    notify_warning_threshold: float = 20.0
    notify_critical_threshold: float = 5.0

    # 是否启用托盘气泡通知
    enable_notify: bool = True

    # 悬浮卡片：是否显示、上次位置 (x, y)
    show_card: bool = True
    card_pos: Optional[list] = None     # [x, y]

    def set_credential(self, platform: PlatformId, value: str):
        if value:
            self.credentials[platform.value] = value
        else:
            self.credentials.pop(platform.value, None)

    def get_credential(self, platform: PlatformId) -> Optional[str]:
        return self.credentials.get(platform.value)

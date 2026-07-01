from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class ShareLink:
    url: str
    share_code: str
    receive_code: str = ""

    @property
    def key(self) -> str:
        return f"{self.share_code}:{self.receive_code}"


@dataclass(slots=True)
class TelegramResource:
    channel: str
    message_id: int
    title: str
    text: str
    keyword: str = ""
    published_at: str = ""
    message_url: str = ""
    links: list[ShareLink] = field(default_factory=list)
    content_hash: str = ""


@dataclass(slots=True)
class SubscriptionInfo:
    sid: int
    name: str
    year: str = ""
    media_type: str = ""
    season: Optional[int] = None
    keyword: str = ""
    lack_episode: Any = None
    state: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchResult:
    subscription: Optional[SubscriptionInfo]
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchResult:
    subscription: SubscriptionInfo
    keyword: str
    channel: str
    resources: list[TelegramResource] = field(default_factory=list)
    error: str = ""


@dataclass(slots=True)
class TransferResult:
    success: bool
    message: str
    share: ShareLink
    target_cid: str = ""
    transferred_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TransferLimits:
    max_transfers_per_run: int = 0
    max_transfers_per_subscription: int = 0
    max_quality_probes_per_subscription: int = 0


@dataclass(slots=True)
class SearchRunContext:
    source: str
    store: Any
    controller: Any
    seen_message_keys: set[tuple[str, int]] = field(default_factory=set)
    seen_share_keys: set[str] = field(default_factory=set)
    stop_requested: bool = False
    stop_reason: str = ""
    bridge_required: bool = False
    subscriptions_total: int = 0
    subscriptions_processed: int = 0
    subscriptions_remaining: int = 0
    total_result: dict[str, int | str | bool] = field(default_factory=dict)

    def request_stop(self, reason: str) -> None:
        self.stop_requested = True
        self.stop_reason = reason or self.stop_reason


@dataclass(slots=True)
class ProcessOutcome:
    result: dict[str, int]
    stop_subscription: bool = False
    stop_run: bool = False
    stop_reason: str = ""
    bridge_required: bool = False

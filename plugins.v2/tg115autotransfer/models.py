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

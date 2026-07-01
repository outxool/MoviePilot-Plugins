from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, List

from .models import MatchResult, SubscriptionInfo, TelegramResource
from .text import normalize_text, parse_episodes, parse_season


class SubscriptionMatcher:
    def __init__(self, minimum_score: int = 80) -> None:
        self.minimum_score = max(0, min(100, int(minimum_score or 80)))

    @staticmethod
    def from_moviepilot(rows: Iterable[Any]) -> list[SubscriptionInfo]:
        result: list[SubscriptionInfo] = []
        for row in rows:
            sid = int(getattr(row, "id", None) or getattr(row, "sid", None) or 0)
            if sid <= 0:
                continue
            name = str(getattr(row, "name", None) or getattr(row, "title", None) or "").strip()
            if not name:
                continue
            year = str(getattr(row, "year", None) or "").strip()
            media_type = str(getattr(row, "type", None) or getattr(row, "mtype", None) or getattr(row, "media_type", None) or "").strip()
            season = getattr(row, "season", None)
            try:
                season = int(season) if season not in (None, "") else None
            except Exception:
                season = None
            keyword = str(getattr(row, "keyword", None) or getattr(row, "keywords", None) or "").strip()
            lack_episode = getattr(row, "lack_episode", None)
            state = str(getattr(row, "state", "") or "")
            aliases = SubscriptionMatcher._extract_aliases(row)
            result.append(SubscriptionInfo(sid=sid, name=name, year=year, media_type=media_type, season=season, keyword=keyword, lack_episode=lack_episode, state=state, aliases=aliases))
        return result

    @staticmethod
    def _extract_aliases(row: Any) -> list[str]:
        values: list[str] = []
        for attr in ("alias", "aliases", "keyword", "keywords"):
            value = getattr(row, attr, None)
            if not value:
                continue
            if isinstance(value, (list, tuple, set)):
                values.extend(str(item).strip() for item in value if str(item).strip())
            else:
                values.extend(item.strip() for item in re.split(r"[,，/|\n]", str(value)) if item.strip())
        unique: list[str] = []
        for item in values:
            if item and item not in unique:
                unique.append(item)
        return unique

    @staticmethod
    def build_keywords(subscription: SubscriptionInfo, max_keywords: int = 3) -> list[str]:
        candidates = [subscription.name]
        candidates.extend(subscription.aliases or [])
        if subscription.keyword:
            candidates.extend(item.strip() for item in re.split(r"[,，/|\n]", subscription.keyword) if item.strip())
        if subscription.year:
            candidates.append(f"{subscription.name} {subscription.year}")
        result: list[str] = []
        for item in candidates:
            value = str(item or "").strip()
            if value and value not in result:
                result.append(value)
            if len(result) >= max(1, int(max_keywords or 3)):
                break
        return result

    def match(self, resource: TelegramResource, subscription: SubscriptionInfo) -> MatchResult:
        resource_text = f"{resource.title}\n{resource.text}"
        resource_norm = normalize_text(resource_text)
        names = [subscription.name] + list(subscription.aliases or [])
        if subscription.keyword:
            names.extend(item.strip() for item in re.split(r"[,，/|\n]", subscription.keyword) if item.strip())
        best = 0
        reasons: list[str] = []
        for name in names:
            name_norm = normalize_text(name)
            if not name_norm:
                continue
            if name_norm in resource_norm:
                score = 100
                reason = f"包含关键词:{name}"
            else:
                score = int(SequenceMatcher(None, name_norm, resource_norm[: max(len(name_norm) + 20, 30)]).ratio() * 100)
                reason = f"相似度:{name}={score}"
            if score > best:
                best = score
                reasons = [reason]
        if subscription.year and subscription.year in resource_text:
            best = min(100, best + 5)
            reasons.append(f"年份:{subscription.year}")
        sub_season = int(subscription.season or 0)
        res_season = parse_season(resource_text)
        if sub_season and res_season and sub_season != res_season:
            best = max(0, best - 20)
            reasons.append(f"季不一致:订阅S{sub_season},资源S{res_season}")
        return MatchResult(subscription=subscription if best >= self.minimum_score else None, score=best, reasons=reasons)

    @staticmethod
    def is_tv(subscription: SubscriptionInfo) -> bool:
        return "电视" in subscription.media_type or "剧" in subscription.media_type or subscription.season is not None

    @staticmethod
    def normalize_lack_episodes(value: Any) -> set[int]:
        if value is None:
            return set()
        if isinstance(value, (list, set, tuple)):
            result: set[int] = set()
            for item in value:
                try:
                    result.add(int(item))
                except Exception:
                    result.update(parse_episodes(str(item)))
            return result
        if isinstance(value, dict):
            result: set[int] = set()
            for item in value.values():
                result.update(SubscriptionMatcher.normalize_lack_episodes(item))
            return result
        return parse_episodes(str(value))

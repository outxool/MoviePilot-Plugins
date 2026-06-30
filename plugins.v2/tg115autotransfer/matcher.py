from __future__ import annotations

import re
from typing import Iterable

from .models import MatchResult, SubscriptionInfo, TelegramResource
from .text import normalize_text, parse_episodes, parse_season, parse_years


class SubscriptionMatcher:
    def __init__(self, minimum_score: int = 80) -> None:
        self.minimum_score = int(minimum_score)

    @staticmethod
    def from_moviepilot(rows: Iterable[object]) -> list[SubscriptionInfo]:
        result: list[SubscriptionInfo] = []
        for row in rows:
            name = str(getattr(row, "name", "") or "").strip()
            if not name:
                continue
            aliases: list[str] = []
            keyword = str(getattr(row, "keyword", "") or "").strip()
            if keyword:
                aliases.extend(part.strip() for part in re.split(r"[|,，/\n]", keyword) if part.strip())
            result.append(
                SubscriptionInfo(
                    sid=int(getattr(row, "id", 0) or 0),
                    name=name,
                    year=str(getattr(row, "year", "") or "").strip(),
                    media_type=str(getattr(row, "type", "") or "").strip(),
                    season=getattr(row, "season", None),
                    keyword=keyword,
                    lack_episode=getattr(row, "lack_episode", None),
                    state=str(getattr(row, "state", "") or "").strip(),
                    aliases=aliases,
                )
            )
        return result

    def match(self, resource: TelegramResource, subscriptions: list[SubscriptionInfo]) -> MatchResult:
        haystack_raw = f"{resource.title}\n{resource.text}"
        haystack = normalize_text(haystack_raw)
        resource_years = parse_years(haystack_raw)
        resource_season = parse_season(haystack_raw)
        resource_episodes = parse_episodes(haystack_raw)

        best = MatchResult(subscription=None, score=0, reasons=[])
        for sub in subscriptions:
            score = 0
            reasons: list[str] = []
            names = [sub.name] + list(sub.aliases)
            normalized_names = [normalize_text(name) for name in names if normalize_text(name)]
            exact_name = next((name for name in normalized_names if name and name in haystack), "")
            if not exact_name:
                continue
            if normalize_text(sub.name) in haystack:
                score += 70
                reasons.append("主标题命中")
            else:
                score += 45
                reasons.append("别名命中")

            if sub.year:
                if sub.year in resource_years:
                    score += 15
                    reasons.append("年份一致")
                elif resource_years:
                    score -= 25
                    reasons.append("年份冲突")

            is_tv = "电视" in sub.media_type or "剧" in sub.media_type or sub.season is not None
            if is_tv and sub.season:
                if resource_season == int(sub.season):
                    score += 25
                    reasons.append("季度一致")
                elif resource_season is not None:
                    score -= 50
                    reasons.append("季度冲突")
                elif int(sub.season) == 1:
                    score += 5
                    reasons.append("未标季度，按第一季弱匹配")

            if is_tv and resource_episodes:
                score += 10
                reasons.append("包含集数标记")
            if resource.links or "115.com/s/" in haystack_raw.lower() or "115cdn.com/s/" in haystack_raw.lower():
                score += 10
                reasons.append("包含115链接")

            if score > best.score:
                best = MatchResult(subscription=sub, score=score, reasons=reasons)
        return best

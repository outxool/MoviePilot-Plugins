from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


WEEKDAY_ALIASES = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}


@dataclass
class FollowScheduleParseResult:
    matched: bool
    raw_text: str
    parsed_days: str
    parsed_time: str
    episode_count: int
    source: str
    confidence: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched": self.matched,
            "raw_text": self.raw_text,
            "parsed_days": self.parsed_days,
            "parsed_time": self.parsed_time,
            "episode_count": self.episode_count,
            "source": self.source,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _parse_episode_count(text: str) -> int:
    if re.search(r"两集连播|2集连播|连更两集|更新两集|更两集", text):
        return 2
    match = re.search(r"(?:每次|一次|连播|更新|连更)\s*([一二三四五六七八九十两俩\d]+)\s*集", text)
    if not match:
        return 0
    value = match.group(1)
    mapping = {"一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if value.isdigit():
        return int(value)
    return mapping.get(value, 0)


def _expand_weekdays(text: str) -> str:
    if re.search(r"每日|每天|每晚|每夜|日更", text):
        return "daily"
    range_match = re.search(r"(?:每周|周)([一二三四五六日天1-7])\s*(?:到|至|-)\s*(?:周)?([一二三四五六日天1-7])", text)
    if range_match:
        start = WEEKDAY_ALIASES.get(range_match.group(1))
        end = WEEKDAY_ALIASES.get(range_match.group(2))
        if start is not None and end is not None:
            if start <= end:
                return ",".join(str(i) for i in range(start, end + 1))
            return ",".join(str(i) for i in list(range(start, 7)) + list(range(0, end + 1)))
    tokens = re.findall(r"(?:每周|周)([一二三四五六日天1-7])", text)
    more = re.findall(r"周([一二三四五六日天1-7])", text)
    tokens.extend(more)
    days: List[int] = []
    for token in tokens:
        day = WEEKDAY_ALIASES.get(token)
        if day is not None and day not in days:
            days.append(day)
    if days:
        return ",".join(str(i) for i in sorted(days))
    return ""


def parse_follow_schedule_text(text: str, source: str = "手动填写") -> FollowScheduleParseResult:
    raw = (text or "").strip()
    normalized = normalize_text(raw)
    time_match = re.search(r"([01]?\d|2[0-3])[:：点时]([0-5]\d)?", normalized)
    if not time_match:
        return FollowScheduleParseResult(False, raw, "", "", 0, source, "低", "没有识别到更新时间")
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    parsed_time = f"{hour:02d}:{minute:02d}"
    days = _expand_weekdays(normalized) or "daily"
    episode_count = _parse_episode_count(normalized)
    confidence = "高" if re.search(r"更新|连播|会员|VIP|CCTV|卫视|腾讯|爱奇艺|优酷", normalized, flags=re.I) else "中"
    return FollowScheduleParseResult(True, raw, days, parsed_time, episode_count, source, confidence, "识别到更新时间")


def weekday_text(parsed_days: str) -> str:
    if parsed_days == "daily":
        return "每日"
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    result = []
    for item in (parsed_days or "").split(","):
        try:
            idx = int(item)
        except Exception:
            continue
        if 0 <= idx <= 6:
            result.append(names[idx])
    return "、".join(result) if result else "未设置"


def calculate_next_run(parsed_days: str, parsed_time: str, delay_minutes: int, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    match = re.match(r"^(\d{1,2}):(\d{2})$", parsed_time or "")
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2))
    delay = timedelta(minutes=max(0, int(delay_minutes or 0)))
    allowed_days = set(range(7)) if parsed_days == "daily" else set()
    if parsed_days != "daily":
        for item in (parsed_days or "").split(","):
            try:
                day = int(item)
            except Exception:
                continue
            if 0 <= day <= 6:
                allowed_days.add(day)
    if not allowed_days:
        allowed_days = set(range(7))
    for offset in range(0, 15):
        candidate_day = now.date() + timedelta(days=offset)
        candidate = datetime.combine(candidate_day, datetime.min.time()).replace(hour=hour, minute=minute, second=0, microsecond=0) + delay
        if candidate.weekday() in allowed_days and candidate > now:
            return candidate.strftime("%Y-%m-%d %H:%M:%S")
    return ""


def search_web_for_follow_schedule(title: str, timeout: int = 12, proxy: str = "") -> FollowScheduleParseResult:
    clean_title = (title or "").strip()
    if not clean_title:
        return FollowScheduleParseResult(False, "", "", "", 0, "全网查询", "低", "标题为空")
    queries = [
        f"{clean_title} 更新时间",
        f"{clean_title} 每天几点更新",
        f"{clean_title} 追剧日历",
        f"{clean_title} 腾讯视频 爱奇艺 更新时间",
        f"{clean_title} CCTV8 更新时间",
    ]
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    snippets: List[str] = []
    for query in queries:
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MoviePilot TG115AutoTransfer/0.4.2"})
        try:
            with opener.open(req, timeout=max(5, int(timeout or 12))) as resp:
                body = resp.read(200000).decode("utf-8", errors="ignore")
        except Exception as err:
            snippets.append(f"{query} 查询失败：{err}")
            continue
        text = normalize_text(body)
        idx = text.find(clean_title)
        if idx >= 0:
            snippets.append(text[max(0, idx - 200): idx + 800])
        else:
            snippets.append(text[:1000])
        joined = "；".join(snippets)
        parsed = parse_follow_schedule_text(joined, source="全网查询")
        if parsed.matched:
            parsed.raw_text = joined[:1500]
            parsed.reason = f"从搜索词“{query}”附近文本识别到更新时间"
            return parsed
    joined = "；".join(snippets)
    parsed = parse_follow_schedule_text(joined, source="全网查询")
    if parsed.matched:
        parsed.raw_text = joined[:1500]
        return parsed
    return FollowScheduleParseResult(False, joined[:1500], "", "", 0, "全网查询", "低", "没有从公开搜索结果识别到更新时间")

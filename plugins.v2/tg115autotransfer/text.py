from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

from .models import ShareLink

_SHARE_URL_RE = re.compile(
    r"https?://(?:115\.com|115cdn\.com)/s/(?P<code>[A-Za-z0-9]+)(?:\?[^\s<>'\"]*)?",
    re.IGNORECASE,
)
_RECEIVE_CODE_RE = re.compile(
    r"(?:提取码|访问码|密码|pwd|password)\s*[:：=]?\s*([A-Za-z0-9]{4,8})",
    re.IGNORECASE,
)
_SEASON_RE = re.compile(r"(?:\bS(?P<s1>\d{1,2})\b|第\s*(?P<s2>\d{1,2})\s*季)", re.IGNORECASE)
_EPISODE_RE = re.compile(r"\bE(?P<e>\d{1,4})\b", re.IGNORECASE)
_SEASON_EPISODE_RE = re.compile(r"\bS(?P<s>\d{1,2})\s*E(?P<e>\d{1,4})(?:\s*[-~至]\s*E?(?P<e2>\d{1,4}))?\b", re.IGNORECASE)
_CN_EPISODE_RE = re.compile(r"第\s*(?P<e>\d{1,4})\s*[集话話](?:\s*[-~至]\s*第?\s*(?P<e2>\d{1,4})\s*[集话話]?)?")
_UPDATE_EPISODE_RE = re.compile(r"(?:更新至|更至|更新到|全)\s*(?P<e>\d{1,4})\s*[集话話]?")
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_JUNK_TITLE_RE = re.compile(r"^[\W_\s📺🎬🎞️🔥⭐️✨💥✅🆕【】\[\]（）()]+$")


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[\s\-_—–·•:：,，。.!！?？'\"“”‘’()（）\[\]【】{}]+", "", text)
    return text


def parse_season(text: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", text or "")
    match = _SEASON_RE.search(normalized)
    if not match:
        return 1 if parse_episodes(normalized) else None
    value = match.group("s1") or match.group("s2")
    return int(value) if value else None


def parse_episodes(text: str) -> set[int]:
    normalized = unicodedata.normalize("NFKC", text or "")
    episodes: set[int] = set()
    for match in _SEASON_EPISODE_RE.finditer(normalized):
        start = int(match.group("e"))
        end = int(match.group("e2") or start)
        episodes.update(range(min(start, end), max(start, end) + 1))
    for match in _EPISODE_RE.finditer(normalized):
        episodes.add(int(match.group("e")))
    for match in _CN_EPISODE_RE.finditer(normalized):
        start = int(match.group("e"))
        end = int(match.group("e2") or start)
        episodes.update(range(min(start, end), max(start, end) + 1))
    for match in _UPDATE_EPISODE_RE.finditer(normalized):
        episodes.add(int(match.group("e")))
    return episodes


def parse_episode_range(text: str) -> tuple[int, int] | None:
    episodes = sorted(parse_episodes(text))
    if not episodes:
        return None
    return episodes[0], episodes[-1]


def parse_years(text: str) -> set[str]:
    return set(_YEAR_RE.findall(text or ""))


def normalize_posix_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return "/"
    normalized = "/" + str(PurePosixPath("/" + raw.lstrip("/"))).lstrip("/")
    return normalized.rstrip("/") or "/"


def cloud_path_to_pan_path(cloud_path: str, cloud_prefix: str) -> str:
    full = normalize_posix_path(cloud_path)
    prefix = normalize_posix_path(cloud_prefix)
    if prefix == "/":
        return full
    if full == prefix:
        return "/"
    expected = prefix + "/"
    if not full.startswith(expected):
        raise ValueError(f"转存目录 {full} 不在 CloudDrive2 前缀 {prefix} 下")
    return normalize_posix_path(full[len(prefix):])


def _receive_code_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("password", "pwd", "receive_code", "code"):
        values = query.get(key)
        if values and values[0]:
            return values[0].strip()
    if parsed.fragment:
        match = _RECEIVE_CODE_RE.search(parsed.fragment)
        if match:
            return match.group(1)
    return ""


def extract_share_links(text: str, hrefs: list[str] | None = None) -> list[ShareLink]:
    sources = [text or ""] + list(hrefs or [])
    surrounding_code = ""
    code_match = _RECEIVE_CODE_RE.search(text or "")
    if code_match:
        surrounding_code = code_match.group(1)

    result: list[ShareLink] = []
    seen: set[str] = set()
    for source in sources:
        for match in _SHARE_URL_RE.finditer(source or ""):
            url = match.group(0).rstrip(".,，。;；)]）}")
            share_code = match.group("code")
            receive_code = _receive_code_from_url(url) or surrounding_code
            item = ShareLink(url=url, share_code=share_code, receive_code=receive_code)
            if item.key in seen:
                continue
            seen.add(item.key)
            result.append(item)
    return result


def is_junk_title_line(line: str) -> bool:
    value = unicodedata.normalize("NFKC", str(line or "")).strip()
    if not value:
        return True
    if _JUNK_TITLE_RE.match(value):
        return True
    lowered = value.lower()
    if "115.com/s/" in lowered or "115cdn.com/s/" in lowered:
        return True
    if _RECEIVE_CODE_RE.search(value):
        return True
    if len(value) <= 2 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", value):
        return True
    return False


def extract_display_title(text: str, fallback: str = "") -> str:
    lines = [line.strip() for line in unicodedata.normalize("NFKC", text or "").splitlines()]
    for line in lines:
        if is_junk_title_line(line):
            continue
        cleaned = re.sub(r"^[📺🎬🎞️🔥⭐️✨💥✅🆕\s]+", "", line).strip()
        cleaned = re.sub(r"^(电影|电视剧|剧集|资源|片名)\s*[:：]\s*", "", cleaned).strip()
        if cleaned and not is_junk_title_line(cleaned):
            return cleaned[:240]
    return (fallback or "").strip()[:240]


def extract_quality(text: str) -> str:
    match = re.search(r"\b(2160p|1080p|720p|4k|8k|uhd|bluray|web-?dl|hdtv)\b", text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def looks_like_low_quality(text: str) -> bool:
    return bool(re.search(r"\b(CAM|TC|TS|枪版|抢先|录屏)\b", text or "", re.IGNORECASE))

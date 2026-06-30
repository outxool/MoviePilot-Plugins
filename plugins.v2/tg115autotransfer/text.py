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
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[\s\-_—–·•:：,，。.!！?？'\"“”‘’()（）\[\]【】{}]+", "", text)
    return text


def parse_season(text: str) -> int | None:
    match = _SEASON_RE.search(unicodedata.normalize("NFKC", text or ""))
    if not match:
        return None
    value = match.group("s1") or match.group("s2")
    return int(value) if value else None


def parse_episodes(text: str) -> set[int]:
    return {int(match.group("e")) for match in _EPISODE_RE.finditer(text or "")}


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

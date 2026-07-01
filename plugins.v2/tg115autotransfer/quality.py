from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(slots=True)
class QualityDecision:
    allowed: bool
    score: int
    resolution: str = ""
    source: str = ""
    codec: str = ""
    hdr: str = ""
    audio: str = ""
    release_group: str = ""
    reason: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ShareStructureDecision:
    allowed: bool
    reason: str
    top_level_count: int = 0
    video_file_count: int = 0
    directory_count: int = 0
    selected_ids: list[str] = field(default_factory=list)
    selected_names: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


_RESOLUTION_ORDER = {
    "": 0,
    "480p": 480,
    "576p": 576,
    "720p": 720,
    "1080p": 1080,
    "2160p": 2160,
    "4k": 2160,
    "uhd": 2160,
    "8k": 4320,
}

_VIDEO_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".ts",
    ".m2ts",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".rmvb",
)

_LOW_QUALITY_RE = re.compile(r"\b(cam|tc|ts|hdcam|hd-?ts|枪版|抢先|录屏|偷拍|尝鲜|试看)\b", re.IGNORECASE)
_RESOLUTION_RE = re.compile(r"(?<!\d)(8k|4k|2160p|1080p|720p|576p|480p|uhd)(?!\d)", re.IGNORECASE)
_SOURCE_RE = re.compile(r"\b(web[- .]?dl|webrip|bluray|blu[- .]?ray|remux|hdtv|hdrip|dvdrip)\b", re.IGNORECASE)
_CODEC_RE = re.compile(r"\b(hevc|h[ .-]?265|x265|av1|h[ .-]?264|x264)\b", re.IGNORECASE)
_HDR_RE = re.compile(r"\b(dolby[ .-]?vision|dovi|dv|hdrvivid|hdr10\+|hdr10|hdr)\b", re.IGNORECASE)
_AUDIO_RE = re.compile(r"\b(atmos|truehd|dts[- .]?hd|ddp?5\.1|aac|flac)\b", re.IGNORECASE)
_RELEASE_GROUP_RE = re.compile(r"-([A-Za-z0-9]{2,20})\b")


def _normalize(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def normalize_custom_skip_keywords(value: object) -> list[str]:
    text = _normalize(value).lower()
    if not text:
        return []
    parts = re.split(r"[\n,，;；]+", text)
    return [part.strip() for part in parts if part.strip()]


def _canonical_resolution(raw: str) -> str:
    value = str(raw or "").lower().replace(" ", "")
    if value in {"4k", "uhd"}:
        return "2160p"
    return value


def _resolution_value(resolution: str) -> int:
    return _RESOLUTION_ORDER.get(str(resolution or "").lower(), 0)


def _score_resolution(resolution: str) -> int:
    value = _resolution_value(resolution)
    if value >= 4320:
        return 120
    if value >= 2160:
        return 100
    if value >= 1080:
        return 60
    if value >= 720:
        return 20
    if value > 0:
        return -30
    return 0


def _score_source(source: str) -> int:
    value = str(source or "").lower().replace(" ", "").replace(".", "").replace("-", "")
    if value == "remux":
        return 45
    if value in {"bluray", "bluray"}:
        return 35
    if value == "webdl":
        return 30
    if value == "webrip":
        return 20
    if value == "hdtv":
        return 5
    if value in {"hdrip", "dvdrip"}:
        return 0
    return 0


def _score_codec(codec: str) -> int:
    value = str(codec or "").lower().replace(" ", "").replace(".", "").replace("-", "")
    if value in {"hevc", "h265", "x265"}:
        return 15
    if value == "av1":
        return 10
    if value in {"h264", "x264"}:
        return 5
    return 0


def _score_hdr(hdr: str) -> int:
    value = str(hdr or "").lower().replace(" ", "").replace(".", "").replace("-", "")
    if value in {"dolbyvision", "dovi", "dv"}:
        return 20
    if value == "hdrvivid":
        return 18
    if value == "hdr10+":
        return 18
    if value in {"hdr10", "hdr"}:
        return 12
    return 0


def evaluate_text_quality(
    text: str,
    *,
    min_resolution: str = "1080p",
    allow_unknown_quality: bool = False,
    prefer_4k: bool = True,
    score_threshold: int = 40,
) -> QualityDecision:
    normalized = _normalize(text)
    lowered = normalized.lower()
    flags: list[str] = []

    low_quality = _LOW_QUALITY_RE.search(normalized)
    resolution_match = _RESOLUTION_RE.search(normalized)
    source_match = _SOURCE_RE.search(normalized)
    codec_match = _CODEC_RE.search(normalized)
    hdr_match = _HDR_RE.search(normalized)
    audio_match = _AUDIO_RE.search(normalized)
    release_group_match = _RELEASE_GROUP_RE.search(normalized)

    resolution = _canonical_resolution(resolution_match.group(1)) if resolution_match else ""
    source = source_match.group(1).replace(" ", "-") if source_match else ""
    codec = codec_match.group(1) if codec_match else ""
    hdr = hdr_match.group(1) if hdr_match else ""
    audio = audio_match.group(1) if audio_match else ""
    release_group = release_group_match.group(1) if release_group_match else ""

    score = _score_resolution(resolution) + _score_source(source) + _score_codec(codec) + _score_hdr(hdr)
    if audio:
        score += 5
    if release_group:
        score += 3

    if prefer_4k and _resolution_value(resolution) >= 2160:
        flags.append("4k优先")
        score += 10

    if re.search(r"\b(sample|trailer|making[- .]?of|花絮|预告)\b", lowered, re.IGNORECASE):
        flags.append("样片/花絮关键词")
        score -= 60

    if re.search(r"\b(rar|zip|7z|压缩包)\b", lowered, re.IGNORECASE):
        flags.append("压缩包关键词")
        score -= 50

    if low_quality:
        flags.append("低质量关键词")
        return QualityDecision(False, score - 100, resolution, source, codec, hdr, audio, release_group, "低质量/枪版关键词，跳过", flags)

    if not resolution and not allow_unknown_quality:
        return QualityDecision(False, score, resolution, source, codec, hdr, audio, release_group, "未知质量，配置不允许自动转存", flags)

    if resolution and _resolution_value(resolution) < _resolution_value(min_resolution):
        return QualityDecision(False, score, resolution, source, codec, hdr, audio, release_group, f"低于最低分辨率 {min_resolution}，跳过", flags)

    if score < int(score_threshold or 0) and not (allow_unknown_quality and not resolution):
        return QualityDecision(False, score, resolution, source, codec, hdr, audio, release_group, f"质量分 {score} 低于阈值 {score_threshold}，跳过", flags)

    reason = f"质量检查通过：{resolution or '未知质量'} score={score}"
    return QualityDecision(True, score, resolution, source, codec, hdr, audio, release_group, reason, flags)


def item_name(item: dict) -> str:
    return str(item.get("n") or item.get("file_name") or item.get("name") or item.get("fn") or "")


def item_id(item: dict) -> str:
    return str(item.get("cid") or item.get("fid") or item.get("file_id") or item.get("id") or "")


def is_directory(item: dict) -> bool:
    if item.get("cid") not in (None, "", 0, "0"):
        return True
    value = item.get("is_dir")
    if value is not None:
        return bool(int(value)) if str(value).isdigit() else bool(value)
    return str(item.get("file_category") or item.get("fc") or "") == "0"


def is_video_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return lowered.endswith(_VIDEO_EXTENSIONS)


def evaluate_share_structure(
    items: Iterable[dict],
    *,
    title: str = "",
    custom_skip_keywords: object = "",
    skip_bdmv_structure: bool = True,
    child_items_by_parent: dict[str, list[dict]] | None = None,
) -> ShareStructureDecision:
    top_items = list(items or [])
    names = [item_name(item) for item in top_items]
    lowered_names = [name.lower() for name in names]
    flags: list[str] = []
    selected_ids = [item_id(item) for item in top_items if item_id(item)]
    directory_count = sum(1 for item in top_items if is_directory(item))
    video_file_count = sum(1 for name in names if is_video_name(name))

    bdmv_found = any(name == "bdmv" or "bdmv" in name for name in lowered_names)
    if not bdmv_found and child_items_by_parent:
        for children in child_items_by_parent.values():
            child_names = [item_name(child).lower() for child in children]
            if any(name == "bdmv" or "bdmv" in name for name in child_names):
                bdmv_found = True
                break
    title_lower = _normalize(title).lower()
    if "bdmv" in title_lower:
        bdmv_found = True

    if skip_bdmv_structure and bdmv_found:
        flags.append("BDMV")
        return ShareStructureDecision(False, "检测到 BDMV 蓝光目录结构，默认跳过", len(top_items), video_file_count, directory_count, [], [], flags)

    keywords = normalize_custom_skip_keywords(custom_skip_keywords)
    if keywords:
        haystacks = [title_lower] + lowered_names
        if child_items_by_parent:
            for children in child_items_by_parent.values():
                haystacks.extend(item_name(child).lower() for child in children)
        for keyword in keywords:
            if any(keyword in hay for hay in haystacks):
                flags.append(f"自定义跳过:{keyword}")
                return ShareStructureDecision(False, f"命中自定义跳过结构/关键词：{keyword}", len(top_items), video_file_count, directory_count, [], [], flags)

    return ShareStructureDecision(True, "分享结构检查通过", len(top_items), video_file_count, directory_count, selected_ids, names, flags)

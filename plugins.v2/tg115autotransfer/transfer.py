from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.log import logger

from .matcher import SubscriptionMatcher
from .models import ProcessOutcome, SearchRunContext, ShareLink, SubscriptionInfo, TelegramResource, TransferLimits
from .p115 import P115TransferClient
from .quality import evaluate_share_structure, evaluate_text_quality, item_id, is_directory
from .records import (
    REASON_BDMV_STRUCTURE,
    REASON_COOLDOWN,
    REASON_CUSTOM_STRUCTURE,
    REASON_DUPLICATE,
    REASON_LOW_QUALITY,
    REASON_MEDIA_EXISTING,
    REASON_NEED_CONFIRM,
    REASON_NETWORK_ERROR,
    REASON_PREVIEW,
    REASON_PROBE_LIMIT,
    REASON_QUALITY_THRESHOLD,
    REASON_RATE_LIMIT,
    REASON_RUN_LIMIT,
    REASON_SEASON_MISMATCH,
    REASON_SUBSCRIPTION_LIMIT,
    STATUS_DEFERRED,
    STATUS_EXISTING,
    STATUS_FAILED_FINAL,
    STATUS_FAILED_RETRYABLE,
    STATUS_NEED_CONFIRM,
    STATUS_PREVIEWED,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_SKIPPED_PERMANENT,
    STATUS_TRANSFERRED,
    Tg115StateStore,
)
from .text import extract_quality, looks_like_low_quality, parse_episodes, parse_season


class RateLimitReached(RuntimeError):
    pass


class TransferController:
    def __init__(self, plugin: Any, store: Tg115StateStore, limits: Optional[TransferLimits] = None) -> None:
        self.plugin = plugin
        self.store = store
        self.limits = limits or TransferLimits(
            max_transfers_per_run=int(getattr(plugin, "_max_transfers_per_run", 0) or 0),
            max_transfers_per_subscription=int(getattr(plugin, "_max_transfers_per_subscription", 0) or 0),
            max_quality_probes_per_subscription=int(getattr(plugin, "_max_quality_probe_per_subscription", 0) or 0),
        )
        self.transfer_client: Optional[P115TransferClient] = None
        self.target_cid: Optional[str] = None
        self.last_transfer_at = 0.0
        self.transfers_in_run = 0
        self.transfers_by_subscription: Dict[int, int] = {}
        self.probe_count_by_subscription: Dict[int, int] = {}

    def close(self) -> None:
        self.transfer_client = None

    def can_probe(self, subscription_id: int) -> bool:
        limit = int(self.limits.max_quality_probes_per_subscription or 0)
        if limit <= 0:
            return True
        return int(self.probe_count_by_subscription.get(int(subscription_id), 0)) < limit

    def mark_probe(self, subscription_id: int) -> None:
        sid = int(subscription_id)
        self.probe_count_by_subscription[sid] = int(self.probe_count_by_subscription.get(sid, 0)) + 1

    def media_decision(self, resource: TelegramResource, subscription: SubscriptionInfo) -> tuple[bool, str, str, str, set[int], int, str]:
        text = f"{resource.title}\n{resource.text}"
        episodes = parse_episodes(text)
        resource_season = parse_season(text)
        subscription_season = int(subscription.season or 1) if SubscriptionMatcher.is_tv(subscription) else 0
        if SubscriptionMatcher.is_tv(subscription) and resource_season is not None and subscription_season and resource_season != subscription_season:
            return False, STATUS_SKIPPED_PERMANENT, "资源季与订阅季不一致", REASON_SEASON_MISMATCH, episodes, resource_season, extract_quality(text)
        season = resource_season or subscription_season
        quality = extract_quality(text)
        if self.plugin._skip_low_quality and looks_like_low_quality(text):
            return False, STATUS_SKIPPED_PERMANENT, "低质量/枪版关键词，跳过", REASON_LOW_QUALITY, episodes, int(season or 0), quality
        if SubscriptionMatcher.is_tv(subscription) and self.plugin._tv_only_missing_episodes:
            if not episodes:
                if self.plugin._auto_transfer_unknown_episode:
                    return True, STATUS_NEED_CONFIRM, "无法识别季集，但配置允许未知集数转存", REASON_NEED_CONFIRM, episodes, int(season or 0), quality
                return False, STATUS_NEED_CONFIRM, "无法识别季集，默认不自动转存", REASON_NEED_CONFIRM, episodes, int(season or 0), quality
            missing = SubscriptionMatcher.normalize_lack_episodes(subscription.lack_episode)
            if missing and not (episodes & missing):
                return False, STATUS_EXISTING, f"订阅不缺这些集数：E{','.join(str(e) for e in sorted(episodes))}", REASON_MEDIA_EXISTING, episodes, int(season or 0), quality
            return True, STATUS_PREVIEWED, f"命中缺失集：E{','.join(str(e) for e in sorted(episodes))}", REASON_PREVIEW, episodes, int(season or 0), quality
        if not SubscriptionMatcher.is_tv(subscription) and self.plugin._skip_existing_movie:
            state = str(subscription.state or "").upper()
            if state in {"Y", "DONE"}:
                return False, STATUS_EXISTING, "电影订阅已完成，跳过", REASON_MEDIA_EXISTING, episodes, int(season or 0), quality
        return True, STATUS_PREVIEWED, "媒体检查通过", REASON_PREVIEW, episodes, int(season or 0), quality

    @staticmethod
    def is_rate_limited(err: Exception | str) -> bool:
        return P115TransferClient.is_rate_limited_error(err)

    def cooldown_until(self) -> Optional[datetime]:
        value = str(self.plugin._load_state().get("p115_cooldown_until") or "")
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def in_cooldown(self) -> bool:
        until = self.cooldown_until()
        return bool(until and until > datetime.now())

    def set_cooldown(self, err: Exception | str) -> None:
        state = self.plugin._load_state()
        until = datetime.now() + timedelta(minutes=self.plugin._p115_cooldown_minutes)
        state["p115_cooldown_until"] = until.strftime("%Y-%m-%d %H:%M:%S")
        state["p115_last_rate_limit_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["p115_last_rate_limit_error"] = str(err)[:500]
        self.plugin._save_state(state)
        logger.warning("〖TG115自动转存〗检测到115限流，进入冷却 %s 分钟：%s", self.plugin._p115_cooldown_minutes, err)

    def _empty_part(self) -> dict[str, int]:
        return {"matched": 0, "previewed": 0, "transferred": 0, "skipped": 0, "skipped_permanent": 0, "deferred": 0, "existing": 0, "need_confirm": 0, "failed": 0, "duplicates": 0}

    def _ensure_client(self) -> P115TransferClient:
        if self.transfer_client is None:
            self.transfer_client = P115TransferClient(self.plugin._cookies, auto_create=self.plugin._auto_create_dir)
        return self.transfer_client

    def _record_duplicate(self, base_record: dict, reason: str = "单轮重复分享") -> None:
        # link_key 是订阅维度唯一键；不要用 duplicate 记录覆盖已转存/已存在等真实处理结果。
        # 只有数据库中尚无该 link_key 时才写入 skipped_duplicate。
        if self.store.get_record_by_key(str(base_record.get("link_key") or "")):
            return
        self.store.upsert_record(**base_record, status=STATUS_SKIPPED_DUPLICATE, reason=reason, reason_code=REASON_DUPLICATE)

    def process_resource(self, resource: TelegramResource, subscription: SubscriptionInfo, matched_score: int, context: Optional[SearchRunContext] = None) -> ProcessOutcome:
        result = self._empty_part()
        for share in resource.links:
            if context and context.stop_requested:
                return ProcessOutcome(result, stop_run=True, stop_reason=context.stop_reason)
            run_share_key = f"{int(subscription.sid)}:{share.share_code}:{share.receive_code or ''}"
            link_key = self.store.build_link_key(subscription.sid, share.share_code, share.receive_code)
            base_record = self._base_record(resource, subscription, share, link_key, matched_score)
            if context:
                if run_share_key in context.seen_share_keys:
                    result["duplicates"] += 1
                    result["skipped"] += 1
                    base_record.update({"reason": "本轮同一订阅内重复115分享", "reason_code": REASON_DUPLICATE})
                    self._record_duplicate(base_record, reason="本轮同一订阅内重复115分享")
                    continue
                context.seen_share_keys.add(run_share_key)
            skip, reason, record = self.store.should_skip_record(link_key, self.plugin._retry_limit, dry_run=self.plugin._dry_run)
            if skip:
                result["duplicates"] += 1
                result["skipped"] += 1
                logger.info("〖TG115自动转存〗跳过已处理链接：订阅=%s，链接=%s，原因=%s", subscription.name, share.url, reason)
                continue

            allowed, status, decision_reason, reason_code, episodes, season, quality = self.media_decision(resource, subscription)
            episodes_text = ",".join(str(i) for i in sorted(episodes))
            text_for_quality = f"{resource.title}\n{resource.text}"
            quality_decision = evaluate_text_quality(
                text_for_quality,
                min_resolution=self.plugin._min_resolution,
                allow_unknown_quality=self.plugin._allow_unknown_quality,
                prefer_4k=self.plugin._prefer_4k,
                score_threshold=self.plugin._quality_score_threshold,
            ) if self.plugin._quality_filter_enabled else None
            base_record.update({
                "season": season,
                "episodes": episodes_text,
                "quality": quality_decision.resolution if quality_decision else quality,
                "quality_score": quality_decision.score if quality_decision else 0,
                "resolution": quality_decision.resolution if quality_decision else "",
                "quality_flags": ",".join(quality_decision.flags) if quality_decision else "",
                "reason": decision_reason,
                "reason_code": reason_code,
            })
            if not allowed:
                self.store.upsert_record(**base_record, status=status)
                if status == STATUS_EXISTING:
                    result["existing"] += 1
                elif status == STATUS_NEED_CONFIRM:
                    result["need_confirm"] += 1
                else:
                    result["skipped"] += 1
                    result["skipped_permanent"] += 1
                continue
            if quality_decision and not quality_decision.allowed:
                q_reason_code = REASON_LOW_QUALITY if "低质量" in quality_decision.reason else REASON_QUALITY_THRESHOLD
                self.store.upsert_record(**base_record, status=STATUS_SKIPPED_PERMANENT, reason=quality_decision.reason, reason_code=q_reason_code)
                result["skipped"] += 1
                result["skipped_permanent"] += 1
                logger.info("〖TG115自动转存〗质量检测跳过：订阅=%s，标题=%s，原因=%s", subscription.name, resource.title, quality_decision.reason)
                continue
            if quality_decision:
                base_record["reason"] = f"{decision_reason}；{quality_decision.reason}"
            result["matched"] += 1
            if self.plugin._dry_run:
                self.store.upsert_record(**base_record, status=STATUS_PREVIEWED, reason_code=REASON_PREVIEW)
                result["previewed"] += 1
                continue
            if self.in_cooldown():
                retry_after = self.cooldown_until().strftime("%Y-%m-%d %H:%M:%S") if self.cooldown_until() else ""
                self.store.upsert_record(**base_record, status=STATUS_DEFERRED, reason="115冷却中", reason_code=REASON_COOLDOWN, retryable=1, retry_after=retry_after)
                result["deferred"] += 1
                result["skipped"] += 1
                if self.plugin._stop_on_rate_limit:
                    return ProcessOutcome(result, stop_run=True, stop_reason="115冷却中")
                continue
            if self.limits.max_transfers_per_run and self.transfers_in_run >= self.limits.max_transfers_per_run:
                return ProcessOutcome(result, stop_run=True, stop_reason="达到单轮最多转存数量")
            sub_count = self.transfers_by_subscription.get(subscription.sid, 0)
            if self.limits.max_transfers_per_subscription and sub_count >= self.limits.max_transfers_per_subscription:
                return ProcessOutcome(result, stop_subscription=True, stop_reason="达到单订阅最多转存数量")
            if self.plugin._quality_filter_enabled:
                if not self.can_probe(subscription.sid):
                    self.store.upsert_record(**base_record, status=STATUS_DEFERRED, reason="达到每订阅质量预检上限，下轮重试", reason_code=REASON_PROBE_LIMIT, retryable=1)
                    result["deferred"] += 1
                    result["skipped"] += 1
                    return ProcessOutcome(result, stop_subscription=True, stop_reason="达到每订阅质量预检上限")
            if self.plugin._transfer_delay_seconds > 0 and self.last_transfer_at > 0:
                wait = self.plugin._transfer_delay_seconds - (time.time() - self.last_transfer_at)
                if wait > 0:
                    time.sleep(wait)
            try:
                client = self._ensure_client()
                selected_ids = None
                if self.plugin._quality_filter_enabled:
                    self.mark_probe(subscription.sid)
                    root_items = client.list_share_root(share)
                    child_items_by_parent = {}
                    if len(root_items) == 1 and is_directory(root_items[0]):
                        parent_id = item_id(root_items[0])
                        if parent_id:
                            try:
                                child_items_by_parent[parent_id] = client.list_directory(parent_id)
                            except Exception as child_err:
                                logger.warning("〖TG115自动转存〗分享单层结构预检失败，继续按根目录判断：%s", child_err)
                    structure_decision = evaluate_share_structure(
                        root_items,
                        title=text_for_quality,
                        custom_skip_keywords=self.plugin._custom_skip_structure_keywords,
                        skip_bdmv_structure=self.plugin._skip_bdmv_structure,
                        child_items_by_parent=child_items_by_parent,
                    )
                    base_record["structure_flags"] = ",".join(structure_decision.flags)
                    base_record["selected_file_count"] = len(structure_decision.selected_ids)
                    base_record["selected_names"] = "\n".join(structure_decision.selected_names)[:1000]
                    if not structure_decision.allowed:
                        struct_code = REASON_BDMV_STRUCTURE if "BDMV" in structure_decision.reason.upper() else REASON_CUSTOM_STRUCTURE
                        self.store.upsert_record(**base_record, status=STATUS_SKIPPED_PERMANENT, reason=structure_decision.reason, reason_code=struct_code)
                        result["skipped"] += 1
                        result["skipped_permanent"] += 1
                        logger.info("〖TG115自动转存〗分享结构检测跳过：订阅=%s，链接=%s，原因=%s", subscription.name, share.url, structure_decision.reason)
                        continue
                    selected_ids = structure_decision.selected_ids
                if self.target_cid is None:
                    self.target_cid = client.resolve_path(self.plugin._target_pan_path())
                transfer = client.receive(share, self.target_cid, selected_ids=selected_ids)
                if not transfer.success:
                    raise RuntimeError(transfer.message)
                self.store.upsert_record(**base_record, status=STATUS_TRANSFERRED, target_cid=self.target_cid, transferred_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason=transfer.message, reason_code="")
                self.transfers_in_run += 1
                self.transfers_by_subscription[subscription.sid] = sub_count + 1
                self.last_transfer_at = time.time()
                result["transferred"] += 1
                logger.info("〖TG115自动转存〗转存成功：%s -> %s，结果=%s", share.url, self.plugin._target_pan_path(), transfer.message)
                if context:
                    context.bridge_required = True
            except Exception as err:
                result["failed"] += 1
                retry_count = int((record or {}).get("retry_count") or 0) + 1
                rate_limited = self.is_rate_limited(err)
                if rate_limited:
                    self.set_cooldown(err)
                final = retry_count >= int(self.plugin._retry_limit or 3) and not rate_limited
                retry_after = ""
                if rate_limited and self.cooldown_until():
                    retry_after = self.cooldown_until().strftime("%Y-%m-%d %H:%M:%S")
                self.store.upsert_record(
                    **base_record,
                    status=STATUS_DEFERRED if rate_limited else (STATUS_FAILED_FINAL if final else STATUS_FAILED_RETRYABLE),
                    reason=str(err),
                    reason_code=REASON_RATE_LIMIT if rate_limited else REASON_NETWORK_ERROR,
                    retryable=0 if final else 1,
                    retry_after=retry_after,
                    retry_count=retry_count,
                )
                logger.error("〖TG115自动转存〗转存失败：%s，错误=%s", share.url, err, exc_info=True)
                if rate_limited and self.plugin._stop_on_rate_limit:
                    return ProcessOutcome(result, stop_run=True, stop_reason="115限流")
        return ProcessOutcome(result)

    @staticmethod
    def _base_record(resource: TelegramResource, subscription: SubscriptionInfo, share: ShareLink, link_key: str, matched_score: int) -> dict:
        return {
            "subscription_id": subscription.sid,
            "subscription_name": subscription.name,
            "channel": resource.channel,
            "message_id": resource.message_id,
            "message_url": resource.message_url,
            "resource_title": resource.title,
            "share_url": share.url,
            "share_code": share.share_code,
            "receive_code": share.receive_code,
            "link_key": link_key,
            "season": 0,
            "episodes": "",
            "quality": "",
            "quality_score": 0,
            "resolution": "",
            "quality_flags": "",
            "structure_flags": "",
            "selected_file_count": 0,
            "selected_names": "",
            "matched_score": matched_score,
            "reason": "",
            "reason_code": "",
        }

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.log import logger

from .matcher import SubscriptionMatcher
from .models import ShareLink, SubscriptionInfo, TelegramResource
from .p115 import P115TransferClient
from .quality import evaluate_share_structure, evaluate_text_quality, item_id, is_directory
from .records import (
    STATUS_EXISTING,
    STATUS_FAILED,
    STATUS_NEED_CONFIRM,
    STATUS_PREVIEWED,
    STATUS_SKIPPED,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_TRANSFERRED,
    Tg115StateStore,
)
from .text import extract_quality, looks_like_low_quality, parse_episodes, parse_season


class TransferController:
    def __init__(self, plugin: Any, store: Tg115StateStore) -> None:
        self.plugin = plugin
        self.store = store
        self.transfer_client: Optional[P115TransferClient] = None
        self.target_cid: Optional[str] = None
        self.last_transfer_at = 0.0
        self.transfers_in_run = 0
        self.transfers_by_subscription: Dict[int, int] = {}

    def close(self) -> None:
        self.transfer_client = None

    def media_decision(self, resource: TelegramResource, subscription: SubscriptionInfo) -> tuple[bool, str, str, set[int], int, str]:
        text = f"{resource.title}\n{resource.text}"
        episodes = parse_episodes(text)
        season = parse_season(text) or int(subscription.season or 1) if SubscriptionMatcher.is_tv(subscription) else 0
        quality = extract_quality(text)
        if self.plugin._skip_low_quality and looks_like_low_quality(text):
            return False, STATUS_SKIPPED, "低质量/枪版关键词，跳过", episodes, int(season or 0), quality
        if SubscriptionMatcher.is_tv(subscription) and self.plugin._tv_only_missing_episodes:
            if not episodes:
                if self.plugin._auto_transfer_unknown_episode:
                    return True, STATUS_NEED_CONFIRM, "无法识别季集，但配置允许未知集数转存", episodes, int(season or 0), quality
                return False, STATUS_NEED_CONFIRM, "无法识别季集，默认不自动转存", episodes, int(season or 0), quality
            missing = SubscriptionMatcher.normalize_lack_episodes(subscription.lack_episode)
            if missing and not (episodes & missing):
                return False, STATUS_EXISTING, f"订阅不缺这些集数：E{','.join(str(e) for e in sorted(episodes))}", episodes, int(season or 0), quality
            return True, STATUS_PREVIEWED, f"命中缺失集：E{','.join(str(e) for e in sorted(episodes))}", episodes, int(season or 0), quality
        if not SubscriptionMatcher.is_tv(subscription) and self.plugin._skip_existing_movie:
            state = str(subscription.state or "").upper()
            if state in {"Y", "DONE"}:
                return False, STATUS_EXISTING, "电影订阅已完成，跳过", episodes, int(season or 0), quality
        return True, STATUS_PREVIEWED, "媒体检查通过", episodes, int(season or 0), quality

    @staticmethod
    def is_rate_limited(err: Exception | str) -> bool:
        text = str(err)
        return any(key in text for key in ("770004", "已达到当前访问上限", "稍后再试", "访问频繁", "rate limit", "too many", "Too Many"))

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

    def process_resource(self, resource: TelegramResource, subscription: SubscriptionInfo, matched_score: int) -> Dict[str, int]:
        result = {"matched": 0, "previewed": 0, "transferred": 0, "skipped": 0, "existing": 0, "need_confirm": 0, "failed": 0, "duplicate": 0}
        for share in resource.links:
            link_key = self.store.build_link_key(subscription.sid, share.share_code, share.receive_code)
            skip, reason, record = self.store.should_skip_record(link_key, self.plugin._retry_limit)
            if skip:
                result["duplicate"] += 1
                result["skipped"] += 1
                logger.info("〖TG115自动转存〗跳过已处理链接：订阅=%s，链接=%s，原因=%s", subscription.name, share.url, reason)
                continue
            allowed, status, decision_reason, episodes, season, quality = self.media_decision(resource, subscription)
            episodes_text = ",".join(str(i) for i in sorted(episodes))
            text_for_quality = f"{resource.title}\n{resource.text}"
            quality_decision = evaluate_text_quality(
                text_for_quality,
                min_resolution=self.plugin._min_resolution,
                allow_unknown_quality=self.plugin._allow_unknown_quality,
                prefer_4k=self.plugin._prefer_4k,
                score_threshold=self.plugin._quality_score_threshold,
            ) if self.plugin._quality_filter_enabled else None
            base_record = {
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
                "season": season,
                "episodes": episodes_text,
                "quality": quality_decision.resolution if quality_decision else quality,
                "quality_score": quality_decision.score if quality_decision else 0,
                "resolution": quality_decision.resolution if quality_decision else "",
                "quality_flags": ",".join(quality_decision.flags) if quality_decision else "",
                "matched_score": matched_score,
                "reason": decision_reason,
            }
            if not allowed:
                self.store.upsert_record(**base_record, status=status)
                if status == STATUS_EXISTING:
                    result["existing"] += 1
                elif status == STATUS_NEED_CONFIRM:
                    result["need_confirm"] += 1
                else:
                    result["skipped"] += 1
                continue
            if quality_decision and not quality_decision.allowed:
                self.store.upsert_record(**base_record, status=STATUS_SKIPPED, reason=quality_decision.reason)
                result["skipped"] += 1
                logger.info("〖TG115自动转存〗质量检测跳过：订阅=%s，标题=%s，原因=%s", subscription.name, resource.title, quality_decision.reason)
                continue
            if quality_decision:
                base_record["reason"] = f"{decision_reason}；{quality_decision.reason}"
            result["matched"] += 1
            if self.plugin._dry_run:
                self.store.upsert_record(**base_record, status=STATUS_PREVIEWED)
                result["previewed"] += 1
                continue
            if self.in_cooldown():
                logger.warning("〖TG115自动转存〗115冷却中，跳过真实转存")
                self.store.upsert_record(**base_record, status=STATUS_SKIPPED, reason="115冷却中")
                result["skipped"] += 1
                continue
            if self.transfers_in_run >= self.plugin._max_transfers_per_run:
                self.store.upsert_record(**base_record, status=STATUS_SKIPPED, reason="达到单轮最多转存数量")
                result["skipped"] += 1
                continue
            sub_count = self.transfers_by_subscription.get(subscription.sid, 0)
            if self.plugin._max_transfers_per_subscription and sub_count >= self.plugin._max_transfers_per_subscription:
                self.store.upsert_record(**base_record, status=STATUS_SKIPPED, reason="达到单订阅最多转存数量")
                result["skipped"] += 1
                continue
            if self.plugin._transfer_delay_seconds > 0 and self.last_transfer_at > 0:
                wait = self.plugin._transfer_delay_seconds - (time.time() - self.last_transfer_at)
                if wait > 0:
                    time.sleep(wait)
            try:
                if self.transfer_client is None:
                    self.transfer_client = P115TransferClient(self.plugin._cookies, auto_create=self.plugin._auto_create_dir)
                selected_ids = None
                if self.plugin._quality_filter_enabled:
                    root_items = self.transfer_client.list_share_root(share)
                    child_items_by_parent = {}
                    if len(root_items) == 1 and is_directory(root_items[0]):
                        parent_id = item_id(root_items[0])
                        if parent_id:
                            try:
                                child_items_by_parent[parent_id] = self.transfer_client.list_directory(parent_id)
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
                        self.store.upsert_record(**base_record, status=STATUS_SKIPPED, reason=structure_decision.reason)
                        result["skipped"] += 1
                        logger.info("〖TG115自动转存〗分享结构检测跳过：订阅=%s，链接=%s，原因=%s", subscription.name, share.url, structure_decision.reason)
                        continue
                    selected_ids = structure_decision.selected_ids
                if self.target_cid is None:
                    self.target_cid = self.transfer_client.resolve_path(self.plugin._target_pan_path())
                transfer = self.transfer_client.receive(share, self.target_cid, selected_ids=selected_ids)
                if not transfer.success:
                    raise RuntimeError(transfer.message)
                self.store.upsert_record(**base_record, status=STATUS_TRANSFERRED, target_cid=self.target_cid, transferred_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason=transfer.message)
                self.transfers_in_run += 1
                self.transfers_by_subscription[subscription.sid] = sub_count + 1
                self.last_transfer_at = time.time()
                result["transferred"] += 1
                logger.info("〖TG115自动转存〗转存成功：%s -> %s，结果=%s", share.url, self.plugin._target_pan_path(), transfer.message)
            except Exception as err:
                result["failed"] += 1
                self.store.upsert_record(**base_record, status=STATUS_FAILED, reason=str(err), retry_count=int((record or {}).get("retry_count") or 0) + 1)
                logger.error("〖TG115自动转存〗转存失败：%s，错误=%s", share.url, err, exc_info=True)
                if self.is_rate_limited(err):
                    self.set_cooldown(err)
                    if self.plugin._stop_on_rate_limit:
                        break
        return result

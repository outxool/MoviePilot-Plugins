from __future__ import annotations

import random
import time
from typing import Dict

import httpx
from app.log import logger

from .matcher import SubscriptionMatcher
from .models import ShareLink, SubscriptionInfo, TelegramResource
from .telegram import TelegramPublicClient


class TgDirectSearcher:
    def __init__(self, channels: list[str], timeout: int = 20, proxy: str = "", request_interval_seconds: int = 2, max_keywords_per_subscription: int = 3, retry_limit: int = 1) -> None:
        self.channels = channels
        self.timeout = max(5, int(timeout or 20))
        self.proxy = proxy or ""
        self.request_interval_seconds = max(0, int(request_interval_seconds or 0))
        self.max_keywords_per_subscription = max(1, int(max_keywords_per_subscription or 3))
        self.retry_limit = max(1, int(retry_limit or 1))

    @staticmethod
    def _should_retry(err: Exception) -> bool:
        if isinstance(err, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            status = err.response.status_code
            return status in {429, 500, 502, 503, 504}
        return False

    @staticmethod
    def _merge_resource_links(target: TelegramResource, incoming: TelegramResource) -> None:
        seen = {item.key for item in target.links}
        for link in incoming.links:
            if link.key not in seen:
                target.links.append(ShareLink(url=link.url, share_code=link.share_code, receive_code=link.receive_code))
                seen.add(link.key)
        if incoming.keyword and incoming.keyword not in target.keyword.split("|"):
            target.keyword = f"{target.keyword}|{incoming.keyword}" if target.keyword else incoming.keyword

    def search_subscription(self, subscription: SubscriptionInfo) -> tuple[list[TelegramResource], Dict[str, int]]:
        stats = {
            "search_operations": 0,
            "requests": 0,
            "raw_messages_found": 0,
            "raw_links_found": 0,
            "messages_found": 0,
            "links_found": 0,
            "unique_messages_found": 0,
            "unique_links_found": 0,
            "errors": 0,
            "retries": 0,
        }
        resources: list[TelegramResource] = []
        keywords = SubscriptionMatcher.build_keywords(subscription, self.max_keywords_per_subscription)
        if not keywords:
            logger.warning("〖TG115自动转存〗订阅 %s 没有可用搜索关键词", subscription.name)
            return resources, stats
        if not self.channels:
            logger.warning("〖TG115自动转存〗未配置TG频道，无法搜索订阅 %s", subscription.name)
            return resources, stats
        client = TelegramPublicClient(timeout=self.timeout, proxy=self.proxy)
        try:
            for channel in self.channels:
                for keyword in keywords:
                    if stats["requests"] > 0 and self.request_interval_seconds > 0:
                        time.sleep(self.request_interval_seconds)
                    stats["search_operations"] += 1
                    found: list[TelegramResource] = []
                    operation_failed = False
                    for attempt in range(self.retry_limit):
                        stats["requests"] += 1
                        try:
                            found = client.search(channel, keyword)
                            operation_failed = False
                            break
                        except Exception as err:
                            operation_failed = True
                            if attempt + 1 >= self.retry_limit or not self._should_retry(err):
                                stats["errors"] += 1
                                logger.error("〖TG115自动转存〗TG直搜失败：频道=%s，关键词=%s，错误=%s", channel, keyword, err, exc_info=True)
                                break
                            stats["retries"] += 1
                            delay = min(8, 2 ** attempt) + random.uniform(0, 0.3)
                            logger.warning("〖TG115自动转存〗TG直搜临时失败，%.1f秒后重试：频道=%s，关键词=%s，错误=%s", delay, channel, keyword, err)
                            time.sleep(delay)
                    if operation_failed and not found:
                        continue
                    stats["raw_messages_found"] += len(found)
                    stats["raw_links_found"] += sum(len(item.links) for item in found)
                    resources.extend(found)
                    logger.info("〖TG115自动转存〗TG直搜：订阅=%s，频道=%s，关键词=%s，含115消息=%s", subscription.name, channel, keyword, len(found))
        finally:
            client.close()
        unique: dict[tuple[str, int], TelegramResource] = {}
        for item in resources:
            key = (item.channel, int(item.message_id))
            if key not in unique:
                unique[key] = item
            else:
                self._merge_resource_links(unique[key], item)
        final = list(unique.values())
        stats["messages_found"] = len(final)
        stats["links_found"] = sum(len(item.links) for item in final)
        stats["unique_messages_found"] = stats["messages_found"]
        stats["unique_links_found"] = stats["links_found"]
        return final, stats

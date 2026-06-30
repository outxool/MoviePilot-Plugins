from __future__ import annotations

import time
from typing import Dict, List

from app.log import logger

from .matcher import SubscriptionMatcher
from .models import SearchResult, SubscriptionInfo, TelegramResource
from .telegram import TelegramPublicClient


class TgDirectSearcher:
    def __init__(self, channels: list[str], timeout: int = 20, proxy: str = "", request_interval_seconds: int = 2, max_keywords_per_subscription: int = 3) -> None:
        self.channels = channels
        self.timeout = max(5, int(timeout or 20))
        self.proxy = proxy or ""
        self.request_interval_seconds = max(0, int(request_interval_seconds or 0))
        self.max_keywords_per_subscription = max(1, int(max_keywords_per_subscription or 3))

    def search_subscription(self, subscription: SubscriptionInfo) -> tuple[list[TelegramResource], Dict[str, int]]:
        stats = {"requests": 0, "messages_found": 0, "links_found": 0, "errors": 0}
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
                    stats["requests"] += 1
                    try:
                        found = client.search(channel, keyword)
                    except Exception as err:
                        stats["errors"] += 1
                        logger.error("〖TG115自动转存〗TG直搜失败：频道=%s，关键词=%s，错误=%s", channel, keyword, err, exc_info=True)
                        continue
                    stats["messages_found"] += len(found)
                    stats["links_found"] += sum(len(item.links) for item in found)
                    resources.extend(found)
                    logger.info("〖TG115自动转存〗TG直搜：订阅=%s，频道=%s，关键词=%s，含115消息=%s", subscription.name, channel, keyword, len(found))
        finally:
            client.close()
        unique: dict[tuple[str, int, str], TelegramResource] = {}
        for item in resources:
            unique[(item.channel, item.message_id, item.content_hash)] = item
        return list(unique.values()), stats

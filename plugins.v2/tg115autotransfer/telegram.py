from __future__ import annotations

import hashlib
import re
from html import unescape
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .models import TelegramResource
from .text import extract_display_title, extract_share_links


class TelegramPublicClient:
    BASE_URL = "https://t.me/s/"

    def __init__(self, timeout: int = 20, proxy: str = "", user_agent: str = "") -> None:
        headers = {
            "User-Agent": user_agent
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        kwargs: dict = {"timeout": timeout, "headers": headers, "follow_redirects": True}
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.Client(**kwargs)

    @staticmethod
    def normalize_channel(value: str) -> str:
        channel = str(value or "").strip()
        channel = re.sub(r"^https?://t\.me/(?:s/)?", "", channel, flags=re.IGNORECASE)
        channel = channel.lstrip("@").strip("/")
        return channel.split("?")[0]

    def fetch_latest(self, channel: str) -> list[TelegramResource]:
        channel = self.normalize_channel(channel)
        if not channel:
            return []
        response = self._client.get(urljoin(self.BASE_URL, channel))
        response.raise_for_status()
        return self.parse_channel_html(channel, response.text)

    def fetch_before(self, channel: str, before_id: int) -> list[TelegramResource]:
        channel = self.normalize_channel(channel)
        if not channel:
            return []
        params = {"before": int(before_id)} if int(before_id or 0) > 0 else None
        response = self._client.get(urljoin(self.BASE_URL, channel), params=params)
        response.raise_for_status()
        return self.parse_channel_html(channel, response.text)

    def search(self, channel: str, keyword: str) -> list[TelegramResource]:
        channel = self.normalize_channel(channel)
        response = self._client.get(urljoin(self.BASE_URL, channel), params={"q": keyword})
        response.raise_for_status()
        return self.parse_channel_html(channel, response.text)

    def fetch_telegraph_links(self, url: str) -> list:
        response = self._client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        hrefs = [a.get("href") or "" for a in soup.select("a[href]")]
        text = soup.get_text("\n", strip=True)
        return extract_share_links(text, hrefs)

    def parse_channel_html(self, channel: str, html: str) -> list[TelegramResource]:
        soup = BeautifulSoup(html, "html.parser")
        resources: list[TelegramResource] = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            message = wrap.select_one(".tgme_widget_message")
            if not message:
                continue
            data_post = message.get("data-post") or ""
            try:
                message_id = int(data_post.rsplit("/", 1)[-1])
            except (TypeError, ValueError):
                continue

            text_node = message.select_one(".js-message_text")
            text = text_node.get_text("\n", strip=True) if text_node else ""
            text = unescape(text)
            raw_first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            title = extract_display_title(text, fallback=raw_first_line or f"{channel}/{message_id}") or f"{channel}/{message_id}"
            hrefs = [a.get("href") or "" for a in message.select("a[href]")]
            links = extract_share_links(text, hrefs)

            if not links:
                for button in message.select("a.tgme_widget_message_inline_button.url_button[href]"):
                    href = button.get("href") or ""
                    if "telegra.ph/" not in href:
                        continue
                    try:
                        links.extend(self.fetch_telegraph_links(href))
                    except Exception:
                        continue

            if not links:
                continue

            time_node = message.select_one("time")
            published_at = (time_node.get("datetime") if time_node else "") or ""
            message_url = f"https://t.me/{channel}/{message_id}"
            digest_source = f"{channel}|{message_id}|{text}|{'|'.join(item.key for item in links)}"
            content_hash = hashlib.sha256(digest_source.encode("utf-8", errors="ignore")).hexdigest()
            resources.append(
                TelegramResource(
                    channel=channel,
                    message_id=message_id,
                    title=title,
                    text=text,
                    published_at=published_at,
                    message_url=message_url,
                    links=links,
                    content_hash=content_hash,
                )
            )
        return resources

    def close(self) -> None:
        self._client.close()

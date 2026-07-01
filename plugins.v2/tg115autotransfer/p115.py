from __future__ import annotations

import time
from typing import Any

from .models import ShareLink, TransferResult
from .text import normalize_posix_path


class P115TransferClient:
    """使用 p115client 的基础 request 封装，避免依赖 P115StrmHelper。"""

    FILES_API = "https://webapi.115.com/files"
    MKDIR_API = "https://webapi.115.com/files/add"
    SHARE_SNAP_API = "https://webapi.115.com/share/snap"
    SHARE_RECEIVE_API = "https://webapi.115.com/share/receive"

    def __init__(self, cookies: str, auto_create: bool = True) -> None:
        if not str(cookies or "").strip():
            raise ValueError("115 Cookie 不能为空")
        try:
            from p115client import P115Client, check_response
        except ImportError as err:
            raise RuntimeError("缺少 p115client 依赖，请完整安装插件依赖后重启 MoviePilot；演练模式不需要该依赖，真实转存需要。") from err

        self._check_response = check_response
        self._client = P115Client(str(cookies).strip())
        self._auto_create = bool(auto_create)

    @staticmethod
    def is_rate_limited_error(err: Exception | str) -> bool:
        text = str(err)
        return any(key in text for key in ("770004", "已达到当前访问上限", "稍后再试", "访问频繁", "rate limit", "too many", "Too Many"))

    def _request(self, url: str, *, method: str = "GET", params: dict | None = None, data: dict | None = None, retry_limit: int = 1) -> dict:
        attempts = max(1, int(retry_limit or 1)) if method.upper() == "GET" else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                result = self._client.request(url=url, method=method, params=params, data=data)
                return self._check_response(result)
            except Exception as err:
                if self.is_rate_limited_error(err):
                    raise
                last_error = err
                if attempt + 1 >= attempts:
                    break
                time.sleep(min(4, 2 ** attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _data_list(response: dict) -> list[dict]:
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, dict):
            for key in ("list", "files", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        if isinstance(data, list):
            return data
        for key in ("list", "files", "items"):
            value = response.get(key) if isinstance(response, dict) else None
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _total_count(response: dict) -> int:
        if not isinstance(response, dict):
            return 0
        data = response.get("data")
        candidates: list[Any] = []
        if isinstance(data, dict):
            candidates.extend([data.get("count"), data.get("total"), data.get("file_count")])
        candidates.extend([response.get("count"), response.get("total")])
        for value in candidates:
            try:
                count = int(value)
                if count > 0:
                    return count
            except Exception:
                continue
        return 0

    def _paged_list(self, url: str, params: dict, page_size: int = 1000, max_pages: int = 20, retry_limit: int = 2) -> list[dict]:
        result: list[dict] = []
        offset = int(params.get("offset") or 0)
        for _page in range(max(1, int(max_pages or 20))):
            page_params = dict(params)
            page_params["limit"] = int(page_size or 1000)
            page_params["offset"] = offset
            response = self._request(url, params=page_params, retry_limit=retry_limit)
            items = self._data_list(response)
            result.extend(items)
            total = self._total_count(response)
            if len(items) < int(page_size or 1000):
                break
            offset += int(page_size or 1000)
            if total and offset >= total:
                break
        return result

    @staticmethod
    def _item_name(item: dict) -> str:
        return str(item.get("n") or item.get("file_name") or item.get("name") or item.get("fn") or "")

    @staticmethod
    def _item_id(item: dict) -> str:
        return str(item.get("cid") or item.get("fid") or item.get("file_id") or item.get("id") or "")

    @staticmethod
    def _is_directory(item: dict) -> bool:
        if item.get("cid") not in (None, "", 0, "0"):
            return True
        value = item.get("is_dir")
        if value is not None:
            return bool(int(value)) if str(value).isdigit() else bool(value)
        return str(item.get("file_category") or item.get("fc") or "") == "0"

    def list_directory(self, cid: str | int) -> list[dict]:
        return self._paged_list(
            self.FILES_API,
            params={"cid": cid, "show_dir": 1, "limit": 1000, "offset": 0, "format": "json"},
            retry_limit=2,
        )

    def mkdir(self, parent_cid: str | int, name: str) -> str:
        # POST 不盲目重试。若接口成功但返回不完整，则重新列目录确认。
        response = self._request(self.MKDIR_API, method="POST", data={"pid": parent_cid, "cname": name})
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, dict):
            cid = data.get("cid") or data.get("file_id") or data.get("id")
            if cid:
                return str(cid)
        cid = response.get("cid") if isinstance(response, dict) else None
        if cid:
            return str(cid)
        for item in self.list_directory(parent_cid):
            if self._is_directory(item) and self._item_name(item) == name:
                return self._item_id(item)
        raise RuntimeError(f"创建115目录失败：{name}")

    def resolve_path(self, path: str) -> str:
        normalized = normalize_posix_path(path)
        if normalized == "/":
            return "0"
        current = "0"
        for part in [segment for segment in normalized.split("/") if segment]:
            found = ""
            for item in self.list_directory(current):
                if self._is_directory(item) and self._item_name(item) == part:
                    found = self._item_id(item)
                    break
            if not found:
                if not self._auto_create:
                    raise FileNotFoundError(f"115目录不存在：{normalized}")
                found = self.mkdir(current, part)
            current = found
        return current

    def list_share_root(self, share: ShareLink) -> list[dict]:
        return self._paged_list(
            self.SHARE_SNAP_API,
            params={"share_code": share.share_code, "receive_code": share.receive_code, "cid": 0, "limit": 1000, "offset": 0},
            retry_limit=2,
        )

    def receive(self, share: ShareLink, target_cid: str, selected_ids: list[str] | None = None) -> TransferResult:
        ids = [str(item_id).strip() for item_id in list(selected_ids or []) if str(item_id).strip()]
        if not ids:
            items = self.list_share_root(share)
            ids = [self._item_id(item) for item in items if self._item_id(item)]
        if not ids:
            return TransferResult(False, "分享根目录没有可接收项目", share, target_cid=target_cid)
        payload = {"share_code": share.share_code, "receive_code": share.receive_code, "file_id": ",".join(ids), "cid": target_cid}
        self._request(self.SHARE_RECEIVE_API, method="POST", data=payload)
        message = f"已提交 {len(ids)} 个筛选项目" if selected_ids else f"已提交 {len(ids)} 个顶层项目"
        return TransferResult(True, message, share, target_cid=target_cid, transferred_ids=ids)

    def transfer(self, share: ShareLink, target_path: str) -> TransferResult:
        target_cid = self.resolve_path(target_path)
        return self.receive(share, target_cid)

from __future__ import annotations

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
        from p115client import P115Client, check_response

        self._check_response = check_response
        self._client = P115Client(str(cookies).strip())
        self._auto_create = bool(auto_create)

    def _request(self, url: str, *, method: str = "GET", params: dict | None = None, data: dict | None = None) -> dict:
        result = self._client.request(url=url, method=method, params=params, data=data)
        return self._check_response(result)

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
        response = self._request(
            self.FILES_API,
            params={"cid": cid, "show_dir": 1, "limit": 1000, "offset": 0, "format": "json"},
        )
        return self._data_list(response)

    def mkdir(self, parent_cid: str | int, name: str) -> str:
        response = self._request(self.MKDIR_API, method="POST", data={"pid": parent_cid, "cname": name})
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, dict):
            cid = data.get("cid") or data.get("file_id") or data.get("id")
            if cid:
                return str(cid)
        cid = response.get("cid") if isinstance(response, dict) else None
        if cid:
            return str(cid)
        # 某些接口成功后不返回 cid，重新列目录确认。
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
        response = self._request(
            self.SHARE_SNAP_API,
            params={
                "share_code": share.share_code,
                "receive_code": share.receive_code,
                "cid": 0,
                "limit": 1000,
                "offset": 0,
            },
        )
        return self._data_list(response)

    def receive(self, share: ShareLink, target_cid: str) -> TransferResult:
        items = self.list_share_root(share)
        ids = [self._item_id(item) for item in items if self._item_id(item)]
        if not ids:
            return TransferResult(False, "分享根目录没有可接收项目", share, target_cid=target_cid)
        payload = {
            "share_code": share.share_code,
            "receive_code": share.receive_code,
            "file_id": ",".join(ids),
            "cid": target_cid,
        }
        self._request(self.SHARE_RECEIVE_API, method="POST", data=payload)
        return TransferResult(True, f"已提交 {len(ids)} 个顶层项目", share, target_cid=target_cid, transferred_ids=ids)

    def transfer(self, share: ShareLink, target_path: str) -> TransferResult:
        target_cid = self.resolve_path(target_path)
        return self.receive(share, target_cid)

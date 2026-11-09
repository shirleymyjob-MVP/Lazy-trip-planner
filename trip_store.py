"""行程儲存：Supabase PostgreSQL `public.trips` 加私人 bucket `trip-images`。

授權只信伺服器上 `auth_gate.get_member_id()` 讀到的 Google OIDC `sub`。
呼叫方傳入的 owner_sub、trip_id、storage path 都唔可以當權限。
RLS 已開啟，但呢度用 service_role（繞過 RLS），所以每次讀寫都要再核對擁有者。

密鑰只可以喺執行時從 Streamlit secrets 或環境變數讀取，唔好寫入程式、日誌或回傳文字。
設定缺失、未登入、id 無效、或者連線失敗，都要拒絕，唔好退回本機 `data/trips` 或 `?share=`。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import auth_gate

logger = logging.getLogger("lazy_trip_planner.store")

# 避免 httpx 喺 DEBUG 時把 Authorization header 打進日誌。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TRIPS_TABLE = "trips"
IMAGES_BUCKET = "trip-images"
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_TRIPS = 3
_DEFAULT_MAX_IMAGES = 50

MSG_LOGIN = "請先用 Google 登入。"
MSG_CONFIG = "行程儲存未設定好，暫時未能讀取或儲存行程。"
MSG_UNAVAILABLE = "暫時連不到行程儲存，請稍後再試。今次沒有改動已儲存的行程。"
MSG_DENIED = "找不到這個行程，或者你沒有權限。"
MSG_IMAGE_DENIED = "找不到這張圖片，或者你沒有權限。"
MSG_NOT_IMAGE = "只可以儲存圖片。"
MSG_TOO_BIG = "圖片太大，單張上限是 10MB。"
MSG_CONTENT = "行程內容未能儲存，請稍後再試。"

_SUB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
_ITEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ITEM_FIELDS = (
    "id", "label", "remark", "status", "analysis", "places", "city", "regions",
    "region_confidence", "warning", "added_by", "facts", "batch", "added_at",
)


class TripStoreError(Exception):
    """可以顯示俾用家嘅繁體中文訊息。唔包含密鑰、路徑或對方帳戶資料。"""


def max_trips() -> int:
    return _limit(
        ("limits", "max_trips_per_user"),
        "MAX_TRIPS_PER_USER",
        _DEFAULT_MAX_TRIPS,
    )


def max_images() -> int:
    return _limit(
        ("limits", "max_images_per_user"),
        "MAX_IMAGES_PER_USER",
        _DEFAULT_MAX_IMAGES,
    )


def trips_cap_message() -> str:
    return f"每位會員最多可儲存 {max_trips()} 個行程。"


def images_cap_message() -> str:
    return f"每位會員最多可儲存 {max_images()} 張圖片。"


def list_trips() -> list[dict]:
    """回傳目前會員自己的行程摘要（不含圖片）。"""
    owner = _require_owner()
    rows = _select_owned(owner)
    summaries = []
    for index, row in enumerate(rows, start=1):
        payload = _payload_dict(row)
        title = str(payload.get("title") or "").strip() or f"行程 {index}"
        summaries.append({
            "id": str(row.get("id")),
            "title": title,
            "created_at": _stamp(row.get("created_at")),
            "updated_at": _stamp(row.get("updated_at")),
        })
    return summaries


def create_trip() -> dict:
    owner = _require_owner()
    rows = _select_owned(owner)
    cap = max_trips()
    if len(rows) >= cap:
        raise TripStoreError(trips_cap_message())
    trip_id = str(uuid.uuid4())
    title = f"行程 {len(rows) + 1}"
    now = _now()
    record = {
        "id": trip_id,
        "owner_sub": owner,
        "payload": _empty_payload(title),
        "created_at": now,
        "updated_at": now,
    }
    inserted = _insert_owned(owner, record)
    payload = _payload_dict(inserted)
    return {
        "id": str(inserted.get("id") or trip_id),
        "title": str(payload.get("title") or title),
        "created_at": _stamp(inserted.get("created_at")),
        "updated_at": _stamp(inserted.get("updated_at")),
    }


def load_trip(trip_id: str) -> dict:
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _owned_row(owner, trip_id)
    payload = _payload_dict(row)
    inbox = []
    for item in payload.get("inbox") or []:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        item_id = str(copied.get("id") or "")
        if copied.get("has_image"):
            path = _object_path(owner, trip_id, item_id)
            copied["image"] = _download_owned(owner, path)
        else:
            copied["image"] = b""
        inbox.append(copied)
    loaded = dict(payload)
    loaded["inbox"] = inbox
    loaded["trip_id"] = trip_id
    loaded["title"] = str(payload.get("title") or "").strip() or "行程"
    loaded["updated_at"] = _stamp(row.get("updated_at"))
    loaded.pop("owner_sub", None)
    return loaded


def save_trip(snapshot: dict) -> str:
    """寫入目前會員自己的行程。回傳資料庫的 updated_at。"""
    owner = _require_owner()
    if not isinstance(snapshot, dict):
        raise TripStoreError(MSG_DENIED)
    trip_id = _validate_trip_id(str(snapshot.get("trip_id") or ""))
    row = _owned_row(owner, trip_id)
    previous = _payload_dict(row)
    inbox_out = []
    new_images: list[tuple[str, bytes]] = []
    seen_ids: set[str] = set()
    for item in snapshot.get("inbox") or []:
        if not isinstance(item, dict):
            raise TripStoreError(MSG_CONTENT)
        item_id = _validate_item_id(str(item.get("id") or ""))
        if item_id in seen_ids:
            raise TripStoreError(MSG_CONTENT)
        seen_ids.add(item_id)
        image = item.get("image") or b""
        if image:
            if not isinstance(image, (bytes, bytearray)):
                raise TripStoreError(MSG_NOT_IMAGE)
            blob = bytes(image)
            _validate_image(blob)
            new_images.append((item_id, blob))
        slim = _slim_item(item)
        slim["has_image"] = bool(image)
        inbox_out.append(slim)
    old_ids = _image_ids(previous)
    new_ids = {item_id for item_id, _blob in new_images}
    others = count_images(excluding_trip_id=trip_id)
    if others + len(new_ids) > max_images():
        raise TripStoreError(images_cap_message())
    payload = _snapshot_payload(snapshot, inbox_out)
    for item_id, blob in new_images:
        path = _object_path(owner, trip_id, item_id)
        _upload_owned(owner, path, blob)
    updated = _update_owned(owner, trip_id, payload)
    removed = old_ids - new_ids
    for item_id in removed:
        path = _object_path(owner, trip_id, item_id)
        _remove_owned(owner, [path])
    return _stamp(updated.get("updated_at"))


def delete_trip(trip_id: str) -> None:
    """刪除目前會員自己的行程同圖片。介面未提供刪除，但接口都要核對擁有者。"""
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _owned_row(owner, trip_id)
    paths = [
        _object_path(owner, trip_id, item_id)
        for item_id in _image_ids(_payload_dict(row))
    ]
    if paths:
        _remove_owned(owner, paths)
    _delete_owned(owner, trip_id)


def delete_image(trip_id: str, item_id: str) -> None:
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    item_id = _validate_item_id(item_id)
    row = _owned_row(owner, trip_id)
    path = _object_path(owner, trip_id, item_id)
    payload = _payload_dict(row)
    inbox = []
    for item in payload.get("inbox") or []:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if str(copied.get("id") or "") == item_id:
            copied["has_image"] = False
        inbox.append(copied)
    payload = dict(payload)
    payload["inbox"] = inbox
    _update_owned(owner, trip_id, payload)
    _remove_owned(owner, [path])


def delete_image_path(path: str) -> None:
    """只接受屬於目前會員的 `{sub}/{trip_id}/{item_id}.jpg`。其他路徑直接拒絕。"""
    owner = _require_owner()
    trip_id, item_id = _parse_owned_path(owner, path)
    delete_image(trip_id, item_id)


def count_images(*, excluding_trip_id: str | None = None) -> int:
    owner = _require_owner()
    excluded = ""
    if excluding_trip_id:
        try:
            excluded = _validate_trip_id(excluding_trip_id)
        except TripStoreError:
            excluded = ""
    total = 0
    for row in _select_owned(owner):
        if excluded and str(row.get("id")) == excluded:
            continue
        total += len(_image_ids(_payload_dict(row)))
    return total


def digest(snapshot: dict) -> str:
    payload = {
        "inbox": [_slim_item(item, include_image_hash=True) for item in snapshot.get("inbox") or []],
        "itinerary": snapshot.get("itinerary"),
        "itinerary_places": snapshot.get("itinerary_places") or [],
        "next_image_num": snapshot.get("next_image_num") or 1,
        "picks": snapshot.get("picks") or {},
        "members": snapshot.get("members") or [],
        "destination_choice": snapshot.get("destination_choice"),
        "days_choice": snapshot.get("days_choice"),
        "pref_pills": snapshot.get("pref_pills"),
        "party_pills": snapshot.get("party_pills"),
        "destination_other": snapshot.get("destination_other") or "",
        "pref_other": snapshot.get("pref_other") or "",
        "party_other": snapshot.get("party_other") or "",
        "favourites": snapshot.get("favourites") or [],
        "removed_fav_ids": sorted(snapshot.get("removed_fav_ids") or []),
        "trip_style": snapshot.get("trip_style") or "",
        "allow_ai": bool(snapshot.get("allow_ai")),
        "day_count": snapshot.get("day_count") or 3,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _snapshot_payload(snapshot: dict, inbox_out: list[dict]) -> dict:
    title = str(snapshot.get("title") or "").strip() or "行程"
    removed = snapshot.get("removed_fav_ids") or []
    if isinstance(removed, set):
        removed = sorted(removed)
    payload = {
        "title": title,
        "inbox": inbox_out,
        "itinerary": snapshot.get("itinerary"),
        "itinerary_places": snapshot.get("itinerary_places") or [],
        "next_image_num": snapshot.get("next_image_num") or 1,
        "picks": snapshot.get("picks") or {},
        "members": snapshot.get("members") or [],
        "destination_choice": snapshot.get("destination_choice"),
        "days_choice": snapshot.get("days_choice"),
        "pref_pills": snapshot.get("pref_pills"),
        "party_pills": snapshot.get("party_pills"),
        "destination_other": snapshot.get("destination_other") or "",
        "pref_other": snapshot.get("pref_other") or "",
        "party_other": snapshot.get("party_other") or "",
        "favourites": snapshot.get("favourites") or [],
        "removed_fav_ids": list(removed),
        "trip_style": snapshot.get("trip_style") or "",
        "allow_ai": bool(snapshot.get("allow_ai")),
        "day_count": snapshot.get("day_count") or 3,
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
        cleaned = json.loads(encoded)
    except (TypeError, ValueError):
        raise TripStoreError(MSG_CONTENT) from None
    if not isinstance(cleaned, dict):
        raise TripStoreError(MSG_CONTENT)
    return cleaned


def _empty_payload(title: str) -> dict:
    return {
        "title": title,
        "inbox": [],
        "itinerary": None,
        "itinerary_places": [],
        "next_image_num": 1,
        "picks": {},
        "members": [],
        "destination_choice": None,
        "days_choice": None,
        "pref_pills": None,
        "party_pills": None,
        "destination_other": "",
        "pref_other": "",
        "party_other": "",
        "favourites": [],
        "removed_fav_ids": [],
        "trip_style": "",
        "allow_ai": True,
        "day_count": 3,
    }


def _slim_item(item: dict, *, include_image_hash: bool = False) -> dict:
    slim = {key: item.get(key) for key in _ITEM_FIELDS}
    if include_image_hash:
        image = item.get("image") or b""
        slim["image_sha"] = hashlib.sha256(image).hexdigest() if image else ""
    return slim


def _image_ids(payload: dict) -> set[str]:
    found: set[str] = set()
    for item in payload.get("inbox") or []:
        if not isinstance(item, dict) or not item.get("has_image"):
            continue
        item_id = str(item.get("id") or "")
        if _ITEM_RE.fullmatch(item_id):
            found.add(item_id)
    return found


def _require_owner() -> str:
    sub = auth_gate.get_member_id()
    if not isinstance(sub, str) or not sub.strip():
        raise TripStoreError(MSG_LOGIN)
    try:
        return _validate_sub(sub.strip())
    except TripStoreError:
        raise TripStoreError("未能確認帳戶身份，行程未有讀取或儲存。") from None


def _validate_sub(sub: str) -> str:
    if not isinstance(sub, str) or not _SUB_RE.fullmatch(sub):
        raise TripStoreError(MSG_DENIED)
    if ".." in sub or "/" in sub or "\\" in sub:
        raise TripStoreError(MSG_DENIED)
    return sub


def _validate_trip_id(trip_id: str) -> str:
    if not isinstance(trip_id, str):
        raise TripStoreError(MSG_DENIED)
    try:
        parsed = uuid.UUID(trip_id)
    except (ValueError, AttributeError, TypeError):
        raise TripStoreError(MSG_DENIED) from None
    return str(parsed)


def _validate_item_id(item_id: str) -> str:
    if not isinstance(item_id, str) or not _ITEM_RE.fullmatch(item_id):
        raise TripStoreError(MSG_DENIED)
    if item_id in {".", ".."} or ".." in item_id:
        raise TripStoreError(MSG_DENIED)
    return item_id


def _validate_image(blob: bytes) -> str:
    if len(blob) > _MAX_IMAGE_BYTES:
        raise TripStoreError(MSG_TOO_BIG)
    return _image_mime(blob)


def _image_mime(blob: bytes) -> str:
    if blob.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(blob) >= 12 and blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "image/gif"
    raise TripStoreError(MSG_NOT_IMAGE)


def _object_path(owner: str, trip_id: str, item_id: str) -> str:
    owner = _validate_sub(owner)
    trip_id = _validate_trip_id(trip_id)
    item_id = _validate_item_id(item_id)
    path = f"{owner}/{trip_id}/{item_id}.jpg"
    parsed_owner_trip, parsed_item = _parse_owned_path(owner, path)
    if parsed_owner_trip != trip_id or parsed_item != item_id:
        raise TripStoreError(MSG_IMAGE_DENIED)
    return path


def _parse_owned_path(owner: str, path: str) -> tuple[str, str]:
    owner = _validate_sub(owner)
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        raise TripStoreError(MSG_IMAGE_DENIED)
    if "\\" in path or "\x00" in path or path.startswith(owner + "/") is False:
        raise TripStoreError(MSG_IMAGE_DENIED)
    parts = path.split("/")
    if len(parts) != 3 or any(part in {"", ".", ".."} for part in parts):
        raise TripStoreError(MSG_IMAGE_DENIED)
    sub, trip_id, filename = parts
    if sub != owner or ".." in filename or not filename.endswith(".jpg"):
        raise TripStoreError(MSG_IMAGE_DENIED)
    item_id = filename[: -len(".jpg")]
    try:
        trip_id = _validate_trip_id(trip_id)
        item_id = _validate_item_id(item_id)
    except TripStoreError:
        raise TripStoreError(MSG_IMAGE_DENIED) from None
    if path != f"{owner}/{trip_id}/{item_id}.jpg":
        raise TripStoreError(MSG_IMAGE_DENIED)
    return trip_id, item_id


def _select_owned(owner: str) -> list[dict]:
    def run(client: Any) -> list[dict]:
        response = (
            client.table(TRIPS_TABLE)
            .select("*")
            .eq("owner_sub", owner)
            .execute()
        )
        return _owned_only(owner, _rows(response))

    return _call("select", run)


def _owned_row(owner: str, trip_id: str) -> dict:
    def run(client: Any) -> dict:
        response = (
            client.table(TRIPS_TABLE)
            .select("*")
            .eq("id", trip_id)
            .eq("owner_sub", owner)
            .limit(1)
            .execute()
        )
        matched = [
            row for row in _owned_only(owner, _rows(response))
            if str(row.get("id")) == trip_id
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_DENIED)
        return matched[0]

    return _call("select", run)


def _insert_owned(owner: str, record: dict) -> dict:
    if record.get("owner_sub") != owner:
        raise TripStoreError(MSG_DENIED)

    def run(client: Any) -> dict:
        response = client.table(TRIPS_TABLE).insert(record).execute()
        rows = _owned_only(owner, _rows(response))
        matched = [row for row in rows if str(row.get("id")) == str(record.get("id"))]
        if len(matched) != 1:
            raise TripStoreError(MSG_UNAVAILABLE)
        return matched[0]

    return _call("insert", run)


def _update_owned(owner: str, trip_id: str, payload: dict) -> dict:
    patch = {"payload": payload, "updated_at": _now()}

    def run(client: Any) -> dict:
        response = (
            client.table(TRIPS_TABLE)
            .update(patch)
            .eq("id", trip_id)
            .eq("owner_sub", owner)
            .execute()
        )
        matched = [
            row for row in _owned_only(owner, _rows(response))
            if str(row.get("id")) == trip_id
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_DENIED)
        return matched[0]

    return _call("update", run)


def _delete_owned(owner: str, trip_id: str) -> None:
    def run(client: Any) -> None:
        response = (
            client.table(TRIPS_TABLE)
            .delete()
            .eq("id", trip_id)
            .eq("owner_sub", owner)
            .execute()
        )
        matched = [
            row for row in _owned_only(owner, _rows(response))
            if str(row.get("id")) == trip_id
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_DENIED)

    _call("delete", run)


def _upload_owned(owner: str, path: str, blob: bytes) -> None:
    _parse_owned_path(owner, path)
    mime = _validate_image(blob)

    def run(client: Any) -> None:
        bucket = client.storage.from_(IMAGES_BUCKET)
        bucket.upload(
            path,
            blob,
            file_options={"content-type": mime, "upsert": "true"},
        )

    _call("upload", run)


def _download_owned(owner: str, path: str) -> bytes:
    _parse_owned_path(owner, path)

    def run(client: Any) -> bytes:
        bucket = client.storage.from_(IMAGES_BUCKET)
        data = bucket.download(path)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise TripStoreError(MSG_UNAVAILABLE)
        return bytes(data)

    return _call("download", run)


def _remove_owned(owner: str, paths: list[str]) -> None:
    safe: list[str] = []
    for path in paths:
        _parse_owned_path(owner, path)
        if not path.startswith(owner + "/"):
            raise TripStoreError(MSG_IMAGE_DENIED)
        safe.append(path)

    def run(client: Any) -> None:
        bucket = client.storage.from_(IMAGES_BUCKET)
        bucket.remove(safe)

    _call("remove", run)


def _owned_only(owner: str, rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("owner_sub") == owner]


def _rows(response: Any) -> list[dict]:
    data = getattr(response, "data", None)
    if data is None and isinstance(response, Mapping):
        data = response.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _payload_dict(row: Mapping[str, Any]) -> dict:
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _call(action: str, fn: Callable[[Any], Any]) -> Any:
    client = _client()
    try:
        return fn(client)
    except TripStoreError:
        raise
    except Exception as exc:
        logger.warning("Supabase %s failed (%s)", action, type(exc).__name__)
        raise TripStoreError(MSG_UNAVAILABLE) from None


def _client() -> Any:
    _require_settings()
    try:
        return _connect()
    except TripStoreError:
        raise
    except Exception as exc:
        logger.warning("Supabase connect failed (%s)", type(exc).__name__)
        raise TripStoreError(MSG_UNAVAILABLE) from None


def _connect() -> Any:
    """建立官方客戶端。測試會替換呢個函數，避免連到真專案。"""
    url, key = _require_settings()
    try:
        from supabase import create_client
    except Exception as exc:
        logger.warning("Supabase library import failed (%s)", type(exc).__name__)
        raise TripStoreError(MSG_CONFIG) from None
    return create_client(url, key)


def _require_settings() -> tuple[str, str]:
    url = _first_text(
        _lookup_secret(("supabase", "url")),
        _lookup_secret(("SUPABASE_URL",)),
        os.environ.get("SUPABASE_URL"),
    )
    key = _first_text(
        _lookup_secret(("supabase", "service_role_key")),
        _lookup_secret(("SUPABASE_SERVICE_ROLE_KEY",)),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
    )
    if not url or not key or not _https_origin(url):
        raise TripStoreError(MSG_CONFIG)
    return url, key


def _https_origin(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    if any(char.isspace() for char in url):
        return False
    return True


def _limit(secret_path: tuple[str, ...], env_name: str, default: int) -> int:
    for raw in (
        _lookup_secret(secret_path),
        _lookup_secret((env_name,)),
        os.environ.get(env_name),
    ):
        parsed = _positive_int(raw)
        if parsed is not None:
            return parsed
    return default


def _lookup_secret(path: tuple[str, ...]) -> object:
    try:
        import streamlit as st

        node: object = st.secrets
        for key in path:
            if isinstance(node, Mapping):
                if key not in node:
                    return None
                node = node[key]
                continue
            getter = getattr(node, "get", None)
            if not callable(getter):
                return None
            try:
                if key not in node:
                    return None
            except Exception:
                node = getter(key)
                if node is None:
                    return None
                continue
            node = getter(key)
        return node
    except Exception:
        return None


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _positive_int(raw: object) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 1 else None
    if isinstance(raw, str) and raw.strip():
        try:
            value = int(raw.strip())
        except ValueError:
            return None
        return value if value >= 1 else None
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(value: object) -> str:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()  # type: ignore[no-any-return]
        except Exception:
            return ""
    return str(value or "")

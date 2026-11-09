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
import threading
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
MSG_INBOX_KEEP = "收藏未能完成整理，原有收藏仍然保留。請再試一次。"

# 收藏匣用現有 trips 列。沒有 kind、或者 kind 不是 inbox，都當普通行程。
KIND_INBOX = "inbox"

_owner_locks_guard = threading.Lock()
_owner_locks: dict[str, threading.Lock] = {}

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
    """回傳目前會員自己的行程摘要（不含圖片，亦不含收藏匣）。"""
    owner = _require_owner()
    rows = _trip_rows(_select_owned(owner))
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
    rows = _trip_rows(_select_owned(owner))
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


def load_collection_inbox() -> dict | None:
    """讀取收藏匣。沒有就回傳 None，唔會新增列。"""
    owner = _require_owner()
    row = _collection_inbox_row(owner)
    if row is None:
        return None
    return _load_owned_row(owner, row)


def collection_inbox_id() -> str:
    """目前會員的收藏匣 id。沒有就係空字串，唔會新增。"""
    owner = _require_owner()
    row = _collection_inbox_row(owner)
    if row is None:
        return ""
    return str(row.get("id") or "")


def save_collection_inbox(snapshot: dict) -> str:
    """寫入收藏匣。未有列先至建立，而且同一會員最多一列。"""
    owner = _require_owner()
    if not isinstance(snapshot, dict):
        raise TripStoreError(MSG_CONTENT)
    with _owner_lock(owner):
        inbox_id = _ensure_collection_inbox_locked(owner)
        copied = dict(snapshot)
        copied["trip_id"] = inbox_id
        return _write_snapshot(owner, inbox_id, copied, kind=KIND_INBOX)


def adopt_collection_inbox(snapshot: dict) -> dict:
    """開一個普通行程，複製收藏匣的內容同圖片，核對成功先清收藏匣。

    任何複製步驟失敗都會刪走新行程，以及只屬於新行程路徑的圖片。
    收藏匣同佢的圖片保持原狀。清理失敗亦唔會刪走仍被收藏匣引用的圖片。
    """
    owner = _require_owner()
    if not isinstance(snapshot, dict):
        raise TripStoreError(MSG_CONTENT)
    with _owner_lock(owner):
        return _adopt_collection_inbox_locked(owner, snapshot)


def release_adopted_collection_inbox() -> None:
    """收藏匣已經成功複製到行程時，再試一次清理。未複製過就什麼都不做。"""
    owner = _require_owner()
    with _owner_lock(owner):
        inbox = _collection_inbox_row(owner)
        if inbox is None:
            return
        promoted = str(_payload_dict(inbox).get("promoted_trip_id") or "")
        if not promoted:
            return
        try:
            promoted = _validate_trip_id(promoted)
            row = _owned_row(owner, promoted)
        except TripStoreError:
            return
        if _is_collection_inbox(row):
            return
        _delete_collection_inbox(owner, str(inbox.get("id") or ""))


def load_trip(trip_id: str) -> dict:
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _owned_row(owner, trip_id)
    if _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    return _load_owned_row(owner, row)


def save_trip(snapshot: dict) -> str:
    """寫入目前會員自己的行程。回傳資料庫的 updated_at。"""
    owner = _require_owner()
    if not isinstance(snapshot, dict):
        raise TripStoreError(MSG_DENIED)
    trip_id = _validate_trip_id(str(snapshot.get("trip_id") or ""))
    return _write_snapshot(owner, trip_id, snapshot, kind=None)


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
    excluded: set[str] = set()
    if excluding_trip_id:
        try:
            excluded.add(_validate_trip_id(excluding_trip_id))
        except TripStoreError:
            pass
    return _count_images(owner, excluded)


def _owner_lock(owner: str) -> threading.Lock:
    with _owner_locks_guard:
        lock = _owner_locks.get(owner)
        if lock is None:
            lock = threading.Lock()
            _owner_locks[owner] = lock
        return lock


def _is_collection_inbox(row: Mapping[str, Any]) -> bool:
    return _payload_dict(row).get("kind") == KIND_INBOX


def _trip_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not _is_collection_inbox(row)]


def _collection_inbox_row(owner: str) -> dict | None:
    found = [row for row in _select_owned(owner) if _is_collection_inbox(row)]
    if not found:
        return None
    found.sort(key=lambda row: _stamp(row.get("created_at")))
    return found[0]


def _ensure_collection_inbox_locked(owner: str) -> str:
    existing = _collection_inbox_row(owner)
    if existing is not None:
        return str(existing.get("id") or "")
    inbox_id = str(uuid.uuid4())
    now = _now()
    payload = _empty_payload("收藏")
    payload["kind"] = KIND_INBOX
    record = {
        "id": inbox_id,
        "owner_sub": owner,
        "payload": payload,
        "created_at": now,
        "updated_at": now,
    }
    inserted = _insert_owned(owner, record)
    return str(inserted.get("id") or inbox_id)


def _count_images(owner: str, excluded: set[str]) -> int:
    total = 0
    for row in _select_owned(owner):
        if str(row.get("id") or "") in excluded:
            continue
        total += len(_image_ids(_payload_dict(row)))
    return total


def _stage_items(snapshot: dict) -> tuple[list[dict], list[tuple[str, bytes]]]:
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
    return inbox_out, new_images


def _write_snapshot(
    owner: str,
    trip_id: str,
    snapshot: dict,
    *,
    kind: str | None,
    exclude_ids: set[str] | None = None,
) -> str:
    row = _owned_row(owner, trip_id)
    if kind == KIND_INBOX and not _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    if kind != KIND_INBOX and _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    previous = _payload_dict(row)
    inbox_out, new_images = _stage_items(snapshot)
    old_ids = _image_ids(previous)
    new_ids = {item_id for item_id, _blob in new_images}
    excluded = {trip_id}
    if exclude_ids:
        excluded.update(exclude_ids)
    others = _count_images(owner, excluded)
    if others + len(new_ids) > max_images():
        raise TripStoreError(images_cap_message())
    payload = _snapshot_payload(snapshot, inbox_out)
    if kind == KIND_INBOX:
        payload["kind"] = KIND_INBOX
        promoted = str(previous.get("promoted_trip_id") or "")
        if promoted:
            payload["promoted_trip_id"] = promoted
    uploaded: list[str] = []
    try:
        for item_id, blob in new_images:
            path = _object_path(owner, trip_id, item_id)
            _upload_owned(owner, path, blob)
            uploaded.append(path)
        updated = _update_owned(owner, trip_id, payload)
    except TripStoreError:
        if kind == KIND_INBOX and not old_ids and uploaded:
            _rollback_paths(owner, trip_id, uploaded)
        raise
    removed = old_ids - new_ids
    for item_id in removed:
        path = _object_path(owner, trip_id, item_id)
        _remove_owned(owner, [path])
    return _stamp(updated.get("updated_at"))


def _load_owned_row(owner: str, row: dict) -> dict:
    trip_id = _validate_trip_id(str(row.get("id") or ""))
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


def _verify_copied_trip(owner: str, trip_id: str, images: list[tuple[str, bytes]], payload: dict) -> None:
    row = _owned_row(owner, trip_id)
    if _is_collection_inbox(row):
        raise TripStoreError(MSG_CONTENT)
    saved = _payload_dict(row)
    if saved.get("kind") == KIND_INBOX:
        raise TripStoreError(MSG_CONTENT)
    if [item.get("id") for item in saved.get("inbox") or []] != [item.get("id") for item in payload.get("inbox") or []]:
        raise TripStoreError(MSG_CONTENT)
    if _image_ids(saved) != {item_id for item_id, _blob in images}:
        raise TripStoreError(MSG_CONTENT)
    if saved.get("favourites") != payload.get("favourites"):
        raise TripStoreError(MSG_CONTENT)
    for item_id, blob in images:
        path = _object_path(owner, trip_id, item_id)
        if _download_owned(owner, path) != blob:
            raise TripStoreError(MSG_CONTENT)


def _rollback_paths(owner: str, trip_id: str, uploaded: list[str]) -> None:
    prefix = f"{owner}/{trip_id}/"
    safe = [
        path for path in uploaded
        if isinstance(path, str) and path.startswith(prefix) and path.count("/") == 2
    ]
    if not safe:
        return
    try:
        _remove_owned(owner, safe)
    except TripStoreError:
        logger.warning("Rollback of new trip images failed")


def _rollback_created_trip(owner: str, trip_id: str, uploaded: list[str]) -> None:
    _rollback_paths(owner, trip_id, uploaded)
    try:
        _delete_owned(owner, trip_id)
    except TripStoreError:
        logger.warning("Rollback of new trip row failed")


def _stamp_promoted(owner: str, inbox_id: str, trip_id: str) -> None:
    row = _owned_row(owner, inbox_id)
    if not _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    payload = dict(_payload_dict(row))
    payload["kind"] = KIND_INBOX
    payload["promoted_trip_id"] = trip_id
    _update_owned(owner, inbox_id, payload)


def _delete_collection_inbox(owner: str, inbox_id: str) -> None:
    """清收藏匣。失敗時原列同圖片仍然可以讀到。"""
    row = _owned_row(owner, inbox_id)
    if not _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    inbox_id = _validate_trip_id(str(row.get("id") or inbox_id))
    payload = _payload_dict(row)
    stored: list[tuple[str, bytes]] = []
    for item_id in _image_ids(payload):
        path = _object_path(owner, inbox_id, item_id)
        stored.append((path, _download_owned(owner, path)))
    paths = [path for path, _blob in stored]
    if paths:
        _remove_owned(owner, paths)
    try:
        _delete_owned(owner, inbox_id)
    except TripStoreError as err:
        for path, blob in stored:
            try:
                _upload_owned(owner, path, blob)
            except TripStoreError:
                logger.warning("Restoring collection inbox image failed")
        raise TripStoreError(MSG_INBOX_KEEP) from err


def _trip_summary(owner: str, trip_id: str) -> dict:
    row = _owned_row(owner, trip_id)
    payload = _payload_dict(row)
    return {
        "id": str(row.get("id") or trip_id),
        "title": str(payload.get("title") or "").strip() or "行程",
        "created_at": _stamp(row.get("created_at")),
        "updated_at": _stamp(row.get("updated_at")),
    }


def _resume_promoted_inbox(owner: str, inbox_id: str, promoted: str, snapshot: dict) -> dict:
    """上次複製已核對，但收藏匣未清走。唔好再開第二個行程。"""
    inbox_out, images = _stage_items(snapshot)
    payload = _snapshot_payload(snapshot, inbox_out)
    payload.pop("kind", None)
    payload.pop("promoted_trip_id", None)
    try:
        _verify_copied_trip(owner, promoted, images, payload)
    except TripStoreError:
        copied = dict(snapshot)
        copied["trip_id"] = promoted
        copied["title"] = _trip_summary(owner, promoted)["title"]
        _write_snapshot(owner, promoted, copied, kind=None, exclude_ids={inbox_id})
        inbox_out, images = _stage_items(copied)
        payload = _snapshot_payload(copied, inbox_out)
        _verify_copied_trip(owner, promoted, images, payload)
    _delete_collection_inbox(owner, inbox_id)
    return _trip_summary(owner, promoted)


def _adopt_collection_inbox_locked(owner: str, snapshot: dict) -> dict:
    inbox = _collection_inbox_row(owner)
    inbox_id = str(inbox.get("id") or "") if inbox else ""
    if inbox is not None:
        promoted = str(_payload_dict(inbox).get("promoted_trip_id") or "")
        if promoted:
            try:
                promoted = _validate_trip_id(promoted)
                row = _owned_row(owner, promoted)
            except TripStoreError:
                promoted = ""
            else:
                if not _is_collection_inbox(row):
                    return _resume_promoted_inbox(owner, inbox_id, promoted, snapshot)
    created = _insert_trip_for_adopt(owner)
    new_id = str(created["id"])
    copied = dict(snapshot)
    copied["trip_id"] = new_id
    copied["title"] = created["title"]
    inbox_out, images = _stage_items(copied)
    others = _count_images(owner, {new_id, inbox_id} - {""})
    if others + len(images) > max_images():
        _rollback_created_trip(owner, new_id, [])
        raise TripStoreError(images_cap_message())
    payload = _snapshot_payload(copied, inbox_out)
    payload.pop("kind", None)
    payload.pop("promoted_trip_id", None)
    uploaded: list[str] = []
    stamped = False
    try:
        for item_id, blob in images:
            path = _object_path(owner, new_id, item_id)
            _upload_owned(owner, path, blob)
            uploaded.append(path)
        updated = _update_owned(owner, new_id, payload)
        _verify_copied_trip(owner, new_id, images, payload)
        if inbox_id:
            _stamp_promoted(owner, inbox_id, new_id)
            stamped = True
            _delete_collection_inbox(owner, inbox_id)
    except TripStoreError as err:
        if not stamped:
            _rollback_created_trip(owner, new_id, uploaded)
            raise
        raise TripStoreError(MSG_INBOX_KEEP) from err
    return {
        "id": new_id,
        "title": str(payload.get("title") or created["title"]),
        "created_at": created.get("created_at") or "",
        "updated_at": _stamp(updated.get("updated_at")),
    }


def _insert_trip_for_adopt(owner: str) -> dict:
    rows = _trip_rows(_select_owned(owner))
    if len(rows) >= max_trips():
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

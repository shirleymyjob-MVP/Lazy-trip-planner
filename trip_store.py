"""行程儲存：Supabase PostgreSQL `public.trips` 加私人 bucket `trip-images`。

授權只信伺服器上 `auth_gate.get_member_id()` 讀到的 Google OIDC `sub`。
呼叫方傳入的 owner_sub、trip_id、成員名單、storage path 都唔可以當權限。
RLS 已開啟，但呢度用 service_role（繞過 RLS），所以每次讀寫都要再核對
擁有者或者共同行程的現有成員。私人行程即使留有成員列，都唔可以進入。

密鑰只可以喺執行時從 Streamlit secrets 或環境變數讀取，唔好寫入程式、日誌或回傳文字。
設定缺失、未登入、id 無效、或者連線失敗，都要拒絕，唔好退回本機 `data/trips` 或 `?share=`。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import auth_gate

logger = logging.getLogger("lazy_trip_planner.store")

# 避免 httpx 喺 DEBUG 時把 Authorization header 打進日誌。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TRIPS_TABLE = "trips"
MEMBERS_TABLE = "trip_members"
INVITES_TABLE = "trip_invites"
IMAGES_BUCKET = "trip-images"
# 邀請連結用正式 Streamlit 網址。唔好用假域名，亦唔好加 Streamlit 伺服唔到的 /join/。
PUBLIC_APP_URL = "https://lazy-trip-planner.streamlit.app"
INVITE_DAYS = 7
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_TRIPS = 3
_DEFAULT_MAX_IMAGES = 50
_DEFAULT_MAX_JOINED = 3
_DEFAULT_MAX_MEMBERS = 5

MSG_LOGIN = "請先用 Google 登入。"
MSG_CONFIG = "行程儲存未設定好，暫時未能讀取或儲存行程。"
MSG_UNAVAILABLE = "暫時連不到行程儲存，請稍後再試。今次沒有改動已儲存的行程。"
MSG_DENIED = "找不到這個行程，或者你沒有權限。"
MSG_IMAGE_DENIED = "找不到這張圖片，或者你沒有權限。"
MSG_NOT_IMAGE = "只可以儲存圖片。"
MSG_TOO_BIG = "圖片太大，單張上限是 10MB。"
MSG_CONTENT = "行程內容未能儲存，請稍後再試。"
MSG_INBOX_KEEP = "收藏未能完成整理，原有收藏仍然保留。請再試一次。"
MSG_CONFLICT = "其他裝置或成員已修改呢份行程，你嘅更改未儲存。"
MSG_INVITE = "邀請連結無效、已過期或已撤銷。"

# 收藏匣用現有 trips 列。沒有 kind、或者 kind 不是 inbox，都當普通行程。
KIND_INBOX = "inbox"

_owner_locks_guard = threading.Lock()
_owner_locks: dict[str, threading.Lock] = {}

_SUB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
_ITEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,200}\Z")
_ITEM_FIELDS = (
    "id", "label", "remark", "status", "analysis", "places", "city", "regions",
    "region_confidence", "warning", "added_by", "facts", "batch", "added_at",
)


class TripStoreError(Exception):
    """可以顯示俾用家嘅繁體中文訊息。唔包含密鑰、路徑或對方帳戶資料。

    ``reason`` 只係診斷代碼用，唔改變幾時拋錯、訊息文字，或者會唔會寫入。
    """

    def __init__(
        self,
        message: str = "",
        *args,
        reason: str | None = None,
        expected_version: object = None,
        remote_version: object = None,
        payload_bytes: object = None,
    ):
        if args:
            super().__init__(message, *args)
        else:
            super().__init__(message)
        self.reason = reason
        self.expected_version = expected_version
        self.remote_version = remote_version
        self.payload_bytes = payload_bytes


class TripConflictError(TripStoreError):
    """讀取之後行程版本已經變咗。拒絕覆蓋，唔好靜默寫入。"""

    def __init__(self, message: str = "", *args, **kwargs):
        kwargs.setdefault("reason", "conflict")
        super().__init__(message, *args, **kwargs)


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


def max_joined_trips() -> int:
    """可以加入別人幾多個共同行程。同自己擁有的行程上限分開計。"""
    return _limit(
        ("limits", "max_joined_trips_per_user"),
        "MAX_JOINED_TRIPS_PER_USER",
        _DEFAULT_MAX_JOINED,
    )


def max_members_per_trip() -> int:
    """一個共同行程除擁有者之外最多幾多位成員。"""
    return _limit(
        ("limits", "max_members_per_trip"),
        "MAX_MEMBERS_PER_TRIP",
        _DEFAULT_MAX_MEMBERS,
    )


def trips_cap_message() -> str:
    return f"每位會員最多可儲存 {max_trips()} 個行程。"


def images_cap_message() -> str:
    return f"每位會員最多可儲存 {max_images()} 張圖片。"


def joined_cap_message() -> str:
    return f"你最多可以加入 {max_joined_trips()} 個別人的共同行程。"


def members_cap_message() -> str:
    return f"每個共同行程最多 {max_members_per_trip()} 位成員（唔計擁有者）。"


def list_trips() -> list[dict]:
    """回傳目前會員自己擁有的行程摘要（不含圖片，亦不含收藏匣）。"""
    owner = _require_owner()
    rows = _trip_rows(_select_owned(owner))
    return [
        _summary_from_row(row, index=index, role="owner")
        for index, row in enumerate(rows, start=1)
    ]


def list_joined_trips() -> list[dict]:
    """其他人分享出嚟、而且 is_shared 仍係 true 的行程。私人行程上的舊成員列唔計。"""
    actor = _require_owner()
    summaries = []
    for membership in _memberships_of(actor):
        trip_id = str(membership.get("trip_id") or "")
        try:
            trip_id = _validate_trip_id(trip_id)
            row = _trip_row_by_id(trip_id)
        except TripStoreError:
            continue
        if _is_collection_inbox(row) or not _is_shared(row):
            continue
        if row.get("owner_sub") == actor:
            continue
        summaries.append(_summary_from_row(row, index=len(summaries) + 1, role="member"))
    return summaries


def list_visible_trips() -> list[dict]:
    """自己擁有的行程在前，跟住係已加入的共同行程。收藏匣唔喺呢度。"""
    owned = list_trips()
    seen = {row["id"] for row in owned}
    return owned + [row for row in list_joined_trips() if row["id"] not in seen]


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
        "version": 1,
        "is_shared": False,
    }
    inserted = _insert_owned(owner, record)
    payload = _payload_dict(inserted)
    return {
        "id": str(inserted.get("id") or trip_id),
        "title": str(payload.get("title") or title),
        "created_at": _stamp(inserted.get("created_at")),
        "updated_at": _stamp(inserted.get("updated_at")),
        "version": _require_version(inserted),
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
        existing = _collection_inbox_row(owner)
        if existing is None:
            inbox_id = _ensure_collection_inbox_locked(owner)
            raw_version: object = 1
        else:
            inbox_id = str(existing.get("id") or "")
            raw_version = snapshot.get("version") if "version" in snapshot else None
        copied = dict(snapshot)
        copied["trip_id"] = inbox_id
        stamp, version = _write_snapshot(
            owner,
            inbox_id,
            copied,
            kind=KIND_INBOX,
            expected_version=raw_version,
        )
        snapshot["version"] = version
        return stamp


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


def has_pending_collection_inbox() -> bool:
    """收藏匣已蓋章指向一個行程，但尚未清理。不會新增列。"""
    owner = _require_owner()
    inbox = _collection_inbox_row(owner)
    if inbox is None:
        return False
    return bool(str(_payload_dict(inbox).get("promoted_trip_id") or ""))


def release_adopted_collection_inbox() -> None:
    """收藏匣已經成功複製到行程時，再試一次清理。未複製過就什麼都不做。

    清理失敗只記日誌，不向呼叫方拋出，也不覆蓋已複製的行程。
    """
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
        try:
            _delete_collection_inbox(owner, str(inbox.get("id") or ""))
        except TripStoreError:
            return


def load_trip(trip_id: str) -> dict:
    access = _access_trip(trip_id)
    if access["inbox"]:
        raise TripStoreError(MSG_DENIED)
    loaded = _load_owned_row(access["owner"], access["row"])
    loaded["role"] = access["role"]
    loaded["is_shared"] = bool(access["shared"])
    loaded.pop("owner_sub", None)
    return loaded


def save_trip(snapshot: dict) -> str:
    """寫入目前會員有權編輯的行程。回傳資料庫的 updated_at。

    擁有者同共同行程的現有成員都可以改內容。圖片仍然放喺擁有者的路徑。
    ``snapshot["version"]`` 必須係上次成功讀到嘅版本。寫入用
    ``id + owner_sub + version`` 比較交換，對唔上就拋 ``TripConflictError``，
    唔會覆蓋。成功之後會把新版本寫回 ``snapshot["version"]``。
    成員唔可以改擁有者顯示名，亦唔可以靠 payload 關掉分享。
    """
    if not isinstance(snapshot, dict):
        raise TripStoreError(MSG_DENIED)
    trip_id = _validate_trip_id(str(snapshot.get("trip_id") or ""))
    access = _access_trip(trip_id)
    if access["inbox"]:
        raise TripStoreError(MSG_DENIED)
    raw_version = snapshot.get("version") if "version" in snapshot else None
    stamp, version = _write_snapshot(
        access["owner"],
        trip_id,
        snapshot,
        kind=None,
        expected_version=raw_version,
        preserve_owner_name=access["role"] != "owner",
    )
    snapshot["version"] = version
    return stamp


def delete_trip(trip_id: str) -> None:
    """刪除目前會員自己擁有的行程同圖片。成員唔可以刪。"""
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _owned_row(owner, trip_id)
    paths = [
        _object_path(owner, trip_id, item_id)
        for item_id in _image_ids(_payload_dict(row))
    ]
    if paths:
        _remove_owned(owner, paths)
    _delete_members_of(trip_id)
    _delete_invites_of(trip_id)
    _delete_owned(owner, trip_id)


def delete_image(trip_id: str, item_id: str) -> None:
    trip_id = _validate_trip_id(trip_id)
    item_id = _validate_item_id(item_id)
    access = _access_trip(trip_id)
    if access["inbox"]:
        raise TripStoreError(MSG_DENIED)
    owner = access["owner"]
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
    version = _require_version(row)
    _update_owned(owner, trip_id, payload, version)
    try:
        _delete_unreferenced(owner, [path])
    except TripStoreError:
        logger.warning("Removing replaced trip images failed")


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


def count_images_for_trip(trip_id: str) -> int:
    """呢個行程以外、計入擁有者圖片上限的張數。成員上載都計落擁有者。"""
    access = _access_trip(trip_id)
    if access["inbox"]:
        raise TripStoreError(MSG_DENIED)
    return _count_images(access["owner"], {access["trip_id"]})


def collection_inbox_status() -> dict | None:
    """收藏匣有內容先回傳摘要。唔會新增列，亦唔會當成普通行程。"""
    owner = _require_owner()
    row = _collection_inbox_row(owner)
    if row is None:
        return None
    payload = _payload_dict(row)
    items = [item for item in (payload.get("inbox") or []) if isinstance(item, dict)]
    favourites = payload.get("favourites") or []
    if not items and not favourites and not payload.get("itinerary"):
        return None
    return {
        "id": str(row.get("id") or ""),
        "title": "私人收藏匣",
        "items": len(items),
    }


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
        "version": 1,
        "is_shared": False,
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
        slim["image_sha"] = hashlib.sha256(blob).hexdigest() if image else ""
        inbox_out.append(slim)
    return inbox_out, new_images


def _write_snapshot(
    owner: str,
    trip_id: str,
    snapshot: dict,
    *,
    kind: str | None,
    expected_version: object,
    exclude_ids: set[str] | None = None,
    preserve_owner_name: bool = False,
) -> tuple[str, int]:
    row = _owned_row(owner, trip_id)
    if kind == KIND_INBOX and not _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    if kind != KIND_INBOX and _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    parsed_version = _coerce_version(expected_version)
    if parsed_version is None:
        raise TripStoreError(MSG_CONTENT)
    current_version = _require_version(row)
    if current_version != parsed_version:
        raise TripConflictError(
            MSG_CONFLICT,
            expected_version=parsed_version,
            remote_version=current_version,
        )
    previous = copy.deepcopy(_payload_dict(row))
    inbox_out, new_images = _stage_items(snapshot)
    stored_ids = _image_ids(previous)
    stored_sha = _stored_shas(previous)
    explicit_clear = _explicit_clears(snapshot)
    images_by_id = {item_id: blob for item_id, blob in new_images}
    upload_new: list[tuple[str, bytes]] = []
    upload_replace: list[tuple[str, bytes]] = []
    for slim in inbox_out:
        item_id = str(slim.get("id") or "")
        blob = images_by_id.get(item_id, b"")
        if blob:
            sha = hashlib.sha256(blob).hexdigest()
            slim["has_image"] = True
            slim["image_sha"] = sha
            if item_id not in stored_ids:
                upload_new.append((item_id, blob))
            elif stored_sha.get(item_id) == sha:
                pass
            elif stored_sha.get(item_id):
                upload_replace.append((item_id, blob))
            elif not _stored_image_matches(owner, trip_id, item_id, blob):
                upload_replace.append((item_id, blob))
        elif item_id in stored_ids and item_id not in explicit_clear:
            # 舊快照冇帶圖片位元組，唔當作用家刪圖。
            slim["has_image"] = True
            slim["image_sha"] = stored_sha.get(item_id, "")
        else:
            slim["has_image"] = False
            slim["image_sha"] = ""
    final_ids = {
        str(item.get("id") or "")
        for item in inbox_out
        if item.get("has_image")
    }
    removed = stored_ids - final_ids
    excluded = {trip_id}
    if exclude_ids:
        excluded.update(exclude_ids)
    others = _count_images(owner, excluded)
    if others + len(final_ids) > max_images():
        raise TripStoreError(images_cap_message())
    payload = _snapshot_payload(snapshot, inbox_out)
    if preserve_owner_name:
        payload["owner_display_name"] = str(previous.get("owner_display_name") or "")
    if kind == KIND_INBOX:
        payload["kind"] = KIND_INBOX
        promoted = str(previous.get("promoted_trip_id") or "")
        if promoted:
            payload["promoted_trip_id"] = promoted
    uploaded_new: list[str] = []
    backups: list[tuple[str, bytes]] = []
    updated_row: dict | None = None
    try:
        for item_id, blob in upload_new:
            path = _object_path(owner, trip_id, item_id)
            uploaded_new.append(path)
            _upload_owned(owner, path, blob)
        updated_row = _update_owned(owner, trip_id, payload, parsed_version)
        for item_id, blob in upload_replace:
            path = _object_path(owner, trip_id, item_id)
            backups.append((path, _download_owned(owner, path)))
            _upload_owned(owner, path, blob)
    except TripStoreError:
        if updated_row is None:
            _rollback_unreferenced(owner, uploaded_new)
        else:
            for path, blob in backups:
                try:
                    _upload_owned(owner, path, blob)
                except TripStoreError:
                    logger.warning("Restoring previous trip image failed")
            try:
                _update_owned(owner, trip_id, previous, parsed_version + 1)
            except TripStoreError:
                logger.warning("Revert of trip row after image failure failed")
            else:
                _rollback_unreferenced(owner, uploaded_new)
        raise
    removed_paths = [_object_path(owner, trip_id, item_id) for item_id in removed]
    try:
        _delete_unreferenced(owner, removed_paths)
    except TripStoreError:
        logger.warning("Removing replaced trip images failed")
    return _stamp(updated_row.get("updated_at")), _require_version(updated_row)


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
    loaded["version"] = _require_version(row)
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
    _update_owned(owner, inbox_id, payload, _require_version(row))


def _only_inbox_paths(owner: str, inbox_id: str, paths: list[str]) -> list[str]:
    """只留 `{owner}/{inbox_id}/{item}.jpg`。新行程路徑唔可以清。"""
    prefix = f"{owner}/{inbox_id}/"
    safe: list[str] = []
    for path in paths:
        if (
            isinstance(path, str)
            and path.startswith(prefix)
            and path.count("/") == 2
            and path.endswith(".jpg")
        ):
            safe.append(path)
    return safe


def _log_inbox_cleanup_pending(inbox_id: str, trip_id: str, exc: BaseException) -> None:
    logger.warning(
        "Collection inbox cleanup pending inbox=%s trip=%s (%s)",
        inbox_id,
        trip_id,
        type(exc).__name__,
    )


def _delete_collection_inbox(owner: str, inbox_id: str) -> None:
    """清收藏匣。失敗時原列同圖片仍然可以讀到。"""
    row = _owned_row(owner, inbox_id)
    if not _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    inbox_id = _validate_trip_id(str(row.get("id") or inbox_id))
    payload = _payload_dict(row)
    promoted = str(payload.get("promoted_trip_id") or "")
    stored: list[tuple[str, bytes]] = []
    for item_id in _image_ids(payload):
        path = _object_path(owner, inbox_id, item_id)
        stored.append((path, _download_owned(owner, path)))
    stored = [
        (path, blob)
        for path, blob in stored
        if path in _only_inbox_paths(owner, inbox_id, [path])
    ]
    paths = [path for path, _blob in stored]
    removed = False
    try:
        if paths:
            _remove_owned(owner, paths)
            removed = True
        _delete_owned(owner, inbox_id)
    except TripStoreError as err:
        if removed:
            for path, blob in stored:
                try:
                    _upload_owned(owner, path, blob)
                except TripStoreError:
                    logger.warning("Restoring collection inbox image failed")
        _log_inbox_cleanup_pending(inbox_id, promoted, err)
        raise TripStoreError(MSG_INBOX_KEEP) from err


def _trip_summary(owner: str, trip_id: str) -> dict:
    row = _owned_row(owner, trip_id)
    payload = _payload_dict(row)
    return {
        "id": str(row.get("id") or trip_id),
        "title": str(payload.get("title") or "").strip() or "行程",
        "created_at": _stamp(row.get("created_at")),
        "updated_at": _stamp(row.get("updated_at")),
        "version": _require_version(row),
    }


def _resume_promoted_inbox(owner: str, inbox_id: str, promoted: str, _snapshot: dict) -> dict:
    """已蓋章的收藏匣只再試清理，唔好覆寫已經複製好的行程。"""
    summary = _trip_summary(owner, promoted)
    try:
        _delete_collection_inbox(owner, inbox_id)
    except TripStoreError:
        summary["inbox_cleanup_pending"] = True
        return summary
    summary["inbox_cleanup_pending"] = False
    return summary


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
        updated = _update_owned(owner, new_id, payload, 1)
        _verify_copied_trip(owner, new_id, images, payload)
        if inbox_id:
            _stamp_promoted(owner, inbox_id, new_id)
            stamped = True
            try:
                _delete_collection_inbox(owner, inbox_id)
            except TripStoreError:
                return _adopted_summary(created, payload, updated, pending=True)
    except TripStoreError:
        if not stamped:
            _rollback_created_trip(owner, new_id, uploaded)
            raise
        raise
    return _adopted_summary(created, payload, updated, pending=False)


def _adopted_summary(created: dict, payload: dict, updated: dict, *, pending: bool) -> dict:
    return {
        "id": str(created["id"]),
        "title": str(payload.get("title") or created["title"]),
        "created_at": created.get("created_at") or "",
        "updated_at": _stamp(updated.get("updated_at")),
        "version": _require_version(updated),
        "inbox_cleanup_pending": pending,
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
        "version": 1,
        "is_shared": False,
    }
    inserted = _insert_owned(owner, record)
    payload = _payload_dict(inserted)
    return {
        "id": str(inserted.get("id") or trip_id),
        "title": str(payload.get("title") or title),
        "created_at": _stamp(inserted.get("created_at")),
        "updated_at": _stamp(inserted.get("updated_at")),
        "version": _require_version(inserted),
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
        "travel_start": _date_field(snapshot.get("travel_start")),
        "travel_end": _date_field(snapshot.get("travel_end")),
        "owner_display_name": _clean_display_name(snapshot.get("owner_display_name"), "") if snapshot.get("owner_display_name") else "",
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
        "travel_start": _date_field(snapshot.get("travel_start")),
        "travel_end": _date_field(snapshot.get("travel_end")),
        "owner_display_name": _clean_display_name(snapshot.get("owner_display_name"), "") if snapshot.get("owner_display_name") else "",
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
        "travel_start": "",
        "travel_end": "",
        "owner_display_name": "",
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


def _stored_shas(payload: dict) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in payload.get("inbox") or []:
        if not isinstance(item, dict) or not item.get("has_image"):
            continue
        item_id = str(item.get("id") or "")
        sha = item.get("image_sha")
        if _ITEM_RE.fullmatch(item_id) and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
            found[item_id] = sha
    return found


def _explicit_clears(snapshot: dict) -> set[str]:
    """有項目但仍標明冇圖，先算明確刪圖。淨係冇帶位元組唔算。"""
    found: set[str] = set()
    for item in snapshot.get("inbox") or []:
        if not isinstance(item, dict):
            continue
        if item.get("image"):
            continue
        if item.get("has_image") is False:
            item_id = str(item.get("id") or "")
            if _ITEM_RE.fullmatch(item_id):
                found.add(item_id)
    return found


def _coerce_version(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _require_version(row: Mapping[str, Any]) -> int:
    parsed = _coerce_version(row.get("version"))
    if parsed is None:
        raise TripStoreError(MSG_UNAVAILABLE)
    return parsed


def _stored_image_matches(owner: str, trip_id: str, item_id: str, blob: bytes) -> bool:
    return _download_owned(owner, _object_path(owner, trip_id, item_id)) == blob


def _referenced_image_paths(owner: str) -> set[str]:
    found: set[str] = set()
    for row in _select_owned(owner):
        try:
            row_id = _validate_trip_id(str(row.get("id") or ""))
        except TripStoreError:
            continue
        for item_id in _image_ids(_payload_dict(row)):
            try:
                found.add(_object_path(owner, row_id, item_id))
            except TripStoreError:
                continue
    return found


def _paths_not_referenced(owner: str, paths: list[str]) -> list[str] | None:
    """``None`` 代表未能確認引用，呼叫方唔可以刪。"""
    if not paths:
        return []
    try:
        referenced = _referenced_image_paths(owner)
    except TripStoreError:
        return None
    prefix = owner + "/"
    doomed: list[str] = []
    for path in paths:
        if not isinstance(path, str) or not path.startswith(prefix) or path in referenced:
            continue
        doomed.append(path)
    return doomed


def _rollback_unreferenced(owner: str, paths: list[str]) -> None:
    doomed = _paths_not_referenced(owner, paths)
    if not doomed:
        if doomed is None:
            logger.warning("Rollback of new trip images failed")
        return
    try:
        _remove_owned(owner, doomed)
    except TripStoreError:
        logger.warning("Rollback of new trip images failed")


def _delete_unreferenced(owner: str, paths: list[str]) -> None:
    doomed = _paths_not_referenced(owner, paths)
    if doomed is None:
        raise TripStoreError(MSG_UNAVAILABLE)
    if not doomed:
        return
    _remove_owned(owner, doomed)


def _summary_from_row(row: Mapping[str, Any], *, index: int, role: str) -> dict:
    payload = _payload_dict(row)
    title = str(payload.get("title") or "").strip() or f"行程 {index}"
    trip_id = str(row.get("id") or "")
    shared = _is_shared(row) and not _is_collection_inbox(row)
    return {
        "id": trip_id,
        "title": title,
        "created_at": _stamp(row.get("created_at")),
        "updated_at": _stamp(row.get("updated_at")),
        "version": _require_version(row),
        "is_shared": shared,
        "role": role,
        "member_count": _member_total(trip_id) if shared else 1,
    }


def _is_shared(row: Mapping[str, Any]) -> bool:
    """只有布林 true 先算共同行程。缺欄、null、字串都不當已分享。"""
    return row.get("is_shared") is True


def invite_link(token: str) -> str:
    token = _validate_token(token)
    return f"{PUBLIC_APP_URL}/?invite={token}"


def create_invite(trip_id: str) -> dict:
    """擁有者產生邀請。私人行程會就地設 is_shared，唔搬圖片。

    重新產生會撤銷舊連結。原文 token 只喺呢次回傳，資料庫只存 hash。
    """
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _owned_row(owner, trip_id)
    if _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    _revoke_active_invites(owner, trip_id)
    if not _is_shared(row):
        _mark_shared(owner, trip_id)
    token = secrets.token_urlsafe(32)
    _validate_token(token)
    digest_value = _token_hash(token)
    if _invite_by_hash(digest_value) is not None:
        raise TripStoreError(MSG_UNAVAILABLE)
    now = _utcnow()
    expires = now + timedelta(days=INVITE_DAYS)
    record = {
        "token_hash": digest_value,
        "trip_id": trip_id,
        "created_by_sub": owner,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "revoked_at": None,
    }
    _insert_invite(record)
    return {
        "trip_id": trip_id,
        "token": token,
        "expires_at": record["expires_at"],
        "url": invite_link(token),
    }


def revoke_invites(trip_id: str) -> None:
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _owned_row(owner, trip_id)
    if _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    _revoke_active_invites(owner, trip_id)


def invite_status(trip_id: str) -> dict:
    """擁有者先可以睇。唔會回傳 token 或 hash。"""
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _owned_row(owner, trip_id)
    if _is_collection_inbox(row):
        raise TripStoreError(MSG_DENIED)
    active = []
    for invite in _invites_for_trip(trip_id):
        if invite.get("created_by_sub") != owner or _invite_blocked(invite):
            continue
        active.append(invite)
    if not active:
        return {"active": False, "expires_at": ""}
    active.sort(key=lambda item: str(item.get("expires_at") or ""))
    return {"active": True, "expires_at": str(active[-1].get("expires_at") or "")}


def accept_invite(token: str, display_name: str) -> dict:
    """未登入、過期、已撤銷、滿人、或私人行程都拒絕。成功就進入該行程。"""
    actor = _require_owner()
    try:
        token = _validate_token(token)
    except TripStoreError:
        raise TripStoreError(MSG_INVITE) from None
    invite = _invite_by_hash(_token_hash(token))
    if invite is None or _invite_blocked(invite):
        raise TripStoreError(MSG_INVITE)
    try:
        trip_id = _validate_trip_id(str(invite.get("trip_id") or ""))
        row = _trip_row_by_id(trip_id)
    except TripStoreError:
        raise TripStoreError(MSG_INVITE) from None
    owner = row.get("owner_sub")
    if (
        _is_collection_inbox(row)
        or not _is_shared(row)
        or not isinstance(owner, str)
        or invite.get("created_by_sub") != owner
    ):
        raise TripStoreError(MSG_INVITE)
    try:
        owner = _validate_sub(owner)
    except TripStoreError:
        raise TripStoreError(MSG_INVITE) from None
    if owner == actor:
        return {"id": trip_id, "role": "owner", "already": True}
    name = _clean_display_name(display_name, actor)
    if _member_row(trip_id, actor) is not None:
        current = _member_row(trip_id, actor) or {}
        if str(current.get("display_name") or "") != name:
            _update_member_name(trip_id, actor, name)
        return {"id": trip_id, "role": "member", "already": True}
    if _joined_count(actor) >= max_joined_trips():
        raise TripStoreError(joined_cap_message())
    if len(_member_rows(trip_id)) >= max_members_per_trip():
        raise TripStoreError(members_cap_message())
    _insert_member({
        "trip_id": trip_id,
        "member_sub": actor,
        "display_name": name,
        "joined_at": _utcnow().isoformat(),
    })
    over_members = len(_member_rows(trip_id)) > max_members_per_trip()
    over_joined = _joined_count(actor) > max_joined_trips()
    if over_members or over_joined:
        _delete_member(trip_id, actor)
        if over_joined and not over_members:
            raise TripStoreError(joined_cap_message())
        raise TripStoreError(members_cap_message())
    return {"id": trip_id, "role": "member", "already": False}


def remove_member(trip_id: str, member_sub: str) -> None:
    owner = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    try:
        member_sub = _validate_sub(member_sub)
    except TripStoreError:
        raise TripStoreError(MSG_DENIED) from None
    if member_sub == owner:
        raise TripStoreError(MSG_DENIED)
    row = _owned_row(owner, trip_id)
    if _is_collection_inbox(row) or not _is_shared(row):
        raise TripStoreError(MSG_DENIED)
    if _member_row(trip_id, member_sub) is None:
        raise TripStoreError(MSG_DENIED)
    _delete_member(trip_id, member_sub)


def leave_trip(trip_id: str) -> None:
    actor = _require_owner()
    access = _access_trip(trip_id)
    if access["role"] != "member" or access["inbox"] or not access["shared"]:
        raise TripStoreError(MSG_DENIED)
    _delete_member(access["trip_id"], actor)


def list_collaborators(trip_id: str) -> list[dict]:
    """顯示名。member_sub 只俾伺服器端移除成員用，畫面唔好渲染。"""
    access = _access_trip(trip_id)
    if access["inbox"]:
        raise TripStoreError(MSG_DENIED)
    owner_name = _clean_display_name(
        _payload_dict(access["row"]).get("owner_display_name"),
        access["owner"],
    )
    people = [{
        "role": "owner",
        "display_name": owner_name,
        "is_self": access["role"] == "owner",
        "member_sub": "",
    }]
    for member in _member_rows(access["trip_id"]):
        sub = member.get("member_sub")
        if not isinstance(sub, str):
            continue
        try:
            sub = _validate_sub(sub)
        except TripStoreError:
            continue
        people.append({
            "role": "member",
            "display_name": _clean_display_name(member.get("display_name"), sub),
            "is_self": sub == access["actor"],
            "member_sub": sub,
        })
    return people


def refresh_actor_name(trip_id: str, display_name: str) -> dict | None:
    """成員改 trip_members，唔郁行程 version。擁有者先至寫入 payload。

    空白或者同 sub 一樣的名字唔寫，避免打開頁面就改到舊行程。
    """
    access = _access_trip(trip_id)
    if access["inbox"]:
        return None
    supplied = display_name if isinstance(display_name, str) else ""
    if not supplied.strip() or supplied.strip() == access["actor"]:
        return None
    name = _clean_display_name(supplied, access["actor"])
    if access["role"] == "member":
        current = str((access.get("member") or {}).get("display_name") or "")
        if current != name:
            _update_member_name(access["trip_id"], access["actor"], name)
        return None
    payload = dict(_payload_dict(access["row"]))
    if str(payload.get("owner_display_name") or "") == name:
        return None
    payload["owner_display_name"] = name
    updated = _update_owned(
        access["owner"],
        access["trip_id"],
        payload,
        _require_version(access["row"]),
    )
    return {"version": _require_version(updated), "owner_display_name": name}


def _access_trip(trip_id: str) -> dict:
    actor = _require_owner()
    trip_id = _validate_trip_id(trip_id)
    row = _trip_row_by_id(trip_id)
    owner = row.get("owner_sub")
    if not isinstance(owner, str):
        raise TripStoreError(MSG_DENIED)
    try:
        owner = _validate_sub(owner)
    except TripStoreError:
        raise TripStoreError(MSG_DENIED) from None
    inbox = _is_collection_inbox(row)
    if owner == actor:
        return {
            "actor": actor,
            "owner": owner,
            "row": row,
            "trip_id": trip_id,
            "role": "owner",
            "inbox": inbox,
            "shared": False if inbox else _is_shared(row),
            "member": None,
        }
    if inbox or not _is_shared(row):
        raise TripStoreError(MSG_DENIED)
    member = _member_row(trip_id, actor)
    if member is None:
        raise TripStoreError(MSG_DENIED)
    return {
        "actor": actor,
        "owner": owner,
        "row": row,
        "trip_id": trip_id,
        "role": "member",
        "inbox": False,
        "shared": True,
        "member": member,
    }


def _trip_row_by_id(trip_id: str) -> dict:
    def run(client: Any) -> dict:
        response = (
            client.table(TRIPS_TABLE)
            .select("*")
            .eq("id", trip_id)
            .limit(1)
            .execute()
        )
        matched = [row for row in _rows(response) if str(row.get("id")) == trip_id]
        if len(matched) != 1:
            raise TripStoreError(MSG_DENIED)
        return matched[0]

    return _call("select", run)


def _mark_shared(owner: str, trip_id: str) -> None:
    """只改 is_shared 欄，唔改 payload、唔搬圖片、唔推進 version。"""
    current = _owned_row(owner, trip_id)
    if _is_shared(current):
        return

    def run(client: Any) -> None:
        response = (
            client.table(TRIPS_TABLE)
            .update({"is_shared": True})
            .eq("id", trip_id)
            .eq("owner_sub", owner)
            .execute()
        )
        matched = [
            row for row in _rows(response)
            if str(row.get("id")) == trip_id
            and row.get("owner_sub") == owner
            and row.get("is_shared") is True
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_UNAVAILABLE)

    _call("update", run)


def _member_total(trip_id: str) -> int:
    return 1 + len(_member_rows(trip_id))


def _joined_count(actor: str) -> int:
    total = 0
    for membership in _memberships_of(actor):
        raw_id = str(membership.get("trip_id") or "")
        try:
            trip_id = _validate_trip_id(raw_id)
            row = _trip_row_by_id(trip_id)
        except TripStoreError:
            continue
        if _is_collection_inbox(row) or not _is_shared(row):
            continue
        if row.get("owner_sub") == actor:
            continue
        total += 1
    return total


def _member_rows(trip_id: str) -> list[dict]:
    def run(client: Any) -> list[dict]:
        response = (
            client.table(MEMBERS_TABLE)
            .select("*")
            .eq("trip_id", trip_id)
            .execute()
        )
        return [
            row for row in _rows(response)
            if str(row.get("trip_id")) == trip_id and isinstance(row.get("member_sub"), str)
        ]

    return _call("select", run)


def _member_row(trip_id: str, member_sub: str) -> dict | None:
    def run(client: Any) -> dict | None:
        response = (
            client.table(MEMBERS_TABLE)
            .select("*")
            .eq("trip_id", trip_id)
            .eq("member_sub", member_sub)
            .limit(1)
            .execute()
        )
        matched = [
            row for row in _rows(response)
            if str(row.get("trip_id")) == trip_id and row.get("member_sub") == member_sub
        ]
        return matched[0] if matched else None

    return _call("select", run)


def _memberships_of(member_sub: str) -> list[dict]:
    def run(client: Any) -> list[dict]:
        response = (
            client.table(MEMBERS_TABLE)
            .select("*")
            .eq("member_sub", member_sub)
            .execute()
        )
        return [row for row in _rows(response) if row.get("member_sub") == member_sub]

    return _call("select", run)


def _insert_member(record: dict) -> None:
    def run(client: Any) -> None:
        response = client.table(MEMBERS_TABLE).insert(record).execute()
        matched = [
            row for row in _rows(response)
            if str(row.get("trip_id")) == str(record.get("trip_id"))
            and row.get("member_sub") == record.get("member_sub")
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_UNAVAILABLE)

    _call("insert", run)


def _update_member_name(trip_id: str, member_sub: str, name: str) -> None:
    def run(client: Any) -> None:
        response = (
            client.table(MEMBERS_TABLE)
            .update({"display_name": name})
            .eq("trip_id", trip_id)
            .eq("member_sub", member_sub)
            .execute()
        )
        matched = [
            row for row in _rows(response)
            if str(row.get("trip_id")) == trip_id and row.get("member_sub") == member_sub
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_UNAVAILABLE)

    _call("update", run)


def _delete_member(trip_id: str, member_sub: str) -> None:
    def run(client: Any) -> None:
        response = (
            client.table(MEMBERS_TABLE)
            .delete()
            .eq("trip_id", trip_id)
            .eq("member_sub", member_sub)
            .execute()
        )
        matched = [
            row for row in _rows(response)
            if str(row.get("trip_id")) == trip_id and row.get("member_sub") == member_sub
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_DENIED)

    _call("delete", run)


def _delete_members_of(trip_id: str) -> None:
    def run(client: Any) -> None:
        client.table(MEMBERS_TABLE).delete().eq("trip_id", trip_id).execute()

    _call("delete", run)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_token(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise TripStoreError(MSG_INVITE)
    return token


def _invite_blocked(invite: Mapping[str, Any]) -> bool:
    if invite.get("revoked_at"):
        return True
    expires = _parse_time(invite.get("expires_at"))
    if expires is None:
        return True
    return expires <= _utcnow()


def _invite_by_hash(digest_value: str) -> dict | None:
    def run(client: Any) -> dict | None:
        response = (
            client.table(INVITES_TABLE)
            .select("*")
            .eq("token_hash", digest_value)
            .limit(1)
            .execute()
        )
        matched = [row for row in _rows(response) if row.get("token_hash") == digest_value]
        if any("token" in row for row in matched):
            raise TripStoreError(MSG_UNAVAILABLE)
        return matched[0] if matched else None

    return _call("select", run)


def _invites_for_trip(trip_id: str) -> list[dict]:
    def run(client: Any) -> list[dict]:
        response = (
            client.table(INVITES_TABLE)
            .select("*")
            .eq("trip_id", trip_id)
            .execute()
        )
        rows = [row for row in _rows(response) if str(row.get("trip_id")) == trip_id]
        if any("token" in row for row in rows):
            raise TripStoreError(MSG_UNAVAILABLE)
        return rows

    return _call("select", run)


def _insert_invite(record: dict) -> None:
    if "token" in record:
        raise TripStoreError(MSG_UNAVAILABLE)

    def run(client: Any) -> None:
        response = client.table(INVITES_TABLE).insert(record).execute()
        matched = [
            row for row in _rows(response)
            if row.get("token_hash") == record.get("token_hash")
        ]
        if len(matched) != 1 or "token" in matched[0]:
            raise TripStoreError(MSG_UNAVAILABLE)

    _call("insert", run)


def _revoke_active_invites(owner: str, trip_id: str) -> None:
    for invite in _invites_for_trip(trip_id):
        if invite.get("created_by_sub") != owner or invite.get("revoked_at"):
            continue
        digest_value = str(invite.get("token_hash") or "")
        if not digest_value:
            continue
        _revoke_invite_hash(owner, trip_id, digest_value)


def _revoke_invite_hash(owner: str, trip_id: str, digest_value: str) -> None:
    stamp = _utcnow().isoformat()

    def run(client: Any) -> None:
        response = (
            client.table(INVITES_TABLE)
            .update({"revoked_at": stamp})
            .eq("token_hash", digest_value)
            .eq("trip_id", trip_id)
            .eq("created_by_sub", owner)
            .execute()
        )
        matched = [
            row for row in _rows(response)
            if row.get("token_hash") == digest_value and row.get("revoked_at")
        ]
        if len(matched) != 1:
            raise TripStoreError(MSG_UNAVAILABLE)

    _call("update", run)


def _delete_invites_of(trip_id: str) -> None:
    def run(client: Any) -> None:
        client.table(INVITES_TABLE).delete().eq("trip_id", trip_id).execute()

    _call("delete", run)


def _clean_display_name(name: object, sub: str) -> str:
    if not isinstance(name, str):
        return "旅人"
    cleaned = " ".join(name.split())[:80]
    if not cleaned or cleaned == sub:
        return "旅人"
    if "/" in cleaned or "\\" in cleaned or cleaned.startswith("."):
        return "旅人"
    return cleaned


def _date_field(value: object) -> str:
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return str(value.isoformat())[:32]
        except Exception:
            return ""
    if isinstance(value, str):
        return value.strip()[:32]
    return ""


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _update_owned(owner: str, trip_id: str, payload: dict, expected_version: int) -> dict:
    """比較交換：只喺 ``version`` 仍然係讀到嗰個值時先更新，並設做該值 + 1。

    0 行代表衝突或者已經唔屬於呢個會員。唔好唔帶 version 條件再寫一次。
    """
    parsed = _coerce_version(expected_version)
    if parsed is None:
        raise TripStoreError(MSG_CONTENT)
    patch = {
        "payload": payload,
        "updated_at": _now(),
        "version": parsed + 1,
    }

    def run(client: Any) -> dict:
        response = (
            client.table(TRIPS_TABLE)
            .update(patch)
            .eq("id", trip_id)
            .eq("owner_sub", owner)
            .eq("version", parsed)
            .execute()
        )
        matched = [
            row for row in _owned_only(owner, _rows(response))
            if str(row.get("id")) == trip_id
        ]
        if len(matched) == 1:
            return matched[0]
        probe = (
            client.table(TRIPS_TABLE)
            .select("id,owner_sub,version")
            .eq("id", trip_id)
            .eq("owner_sub", owner)
            .limit(1)
            .execute()
        )
        owned = [
            row for row in _owned_only(owner, _rows(probe))
            if str(row.get("id")) == trip_id
        ]
        if len(owned) != 1:
            raise TripStoreError(MSG_DENIED)
        current = _coerce_version(owned[0].get("version"))
        if current is None:
            raise TripStoreError(MSG_UNAVAILABLE, reason="unavailable")
        if current != parsed:
            raise TripConflictError(
                MSG_CONFLICT,
                expected_version=parsed,
                remote_version=current,
            )
        raise TripStoreError(
            MSG_UNAVAILABLE,
            reason="update0",
            expected_version=parsed,
            remote_version=current,
        )

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
        raise TripStoreError(MSG_UNAVAILABLE, reason="unavailable") from None


def _client() -> Any:
    _require_settings()
    try:
        return _connect()
    except TripStoreError:
        raise
    except Exception as exc:
        logger.warning("Supabase connect failed (%s)", type(exc).__name__)
        raise TripStoreError(MSG_UNAVAILABLE, reason="unavailable") from None


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

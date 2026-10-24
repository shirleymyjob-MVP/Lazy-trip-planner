"""分享行程匣：一個 token 一個資料夾。最後寫入贏，夠 demo 兩個人一齊改。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

_TOKEN = re.compile(r"[0-9a-f]{8,32}")
_ITEM = re.compile(r"[0-9a-f]{8,64}")
_ITEM_FIELDS = (
    "id", "label", "remark", "status", "analysis", "places", "city", "regions",
    "region_confidence", "warning", "added_by",
)


def store_dir() -> Path:
    path = Path(os.environ.get("TRIP_STORE_DIR", Path(__file__).resolve().parent / "data" / "trips"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_token() -> str:
    return uuid.uuid4().hex[:12]


def _folder(token: str) -> Path:
    if not _TOKEN.fullmatch(token or ""):
        raise ValueError("bad share token")
    return store_dir() / token


def trip_path(token: str) -> Path:
    return _folder(token) / "trip.json"


def image_path(token: str, item_id: str) -> Path:
    if not _ITEM.fullmatch(item_id or ""):
        raise ValueError("bad item id")
    return _folder(token) / f"{item_id}.jpg"


def mtime(token: str) -> float:
    path = trip_path(token)
    if not path.exists():
        return 0.0
    return path.stat().st_mtime


def _slim_item(item: dict, *, include_image_hash: bool) -> dict:
    slim = {key: item.get(key) for key in _ITEM_FIELDS}
    if include_image_hash:
        image = item.get("image") or b""
        slim["image_sha"] = hashlib.sha256(image).hexdigest() if image else ""
    return slim


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
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def load_trip(token: str) -> dict | None:
    path = trip_path(token)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    inbox = []
    for item in data.get("inbox") or []:
        copied = dict(item)
        try:
            img = image_path(token, copied.get("id") or "")
            copied["image"] = img.read_bytes() if img.exists() else b""
        except ValueError:
            copied["image"] = b""
        inbox.append(copied)
    data["inbox"] = inbox
    data["token"] = token
    return data


def save_trip(snapshot: dict) -> float:
    token = snapshot["token"]
    folder = _folder(token)
    folder.mkdir(parents=True, exist_ok=True)
    inbox_out = []
    for item in snapshot.get("inbox") or []:
        image = item.get("image") or b""
        if image:
            image_path(token, item["id"]).write_bytes(image)
        inbox_out.append(_slim_item(item, include_image_hash=False))
    payload = {
        "token": token,
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
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=1)
    target = folder / "trip.json"
    tmp = folder / "trip.json.tmp"
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(target)
    return target.stat().st_mtime

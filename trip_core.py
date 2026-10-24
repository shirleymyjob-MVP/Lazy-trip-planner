"""純邏輯工具：圖片處理、JSON 解析、去重、Google Maps 連結、行程格式化。

呢個 module 唔 import streamlit 或 Gemini，方便單元測試。
"""
from __future__ import annotations

import io
import json
import re
import unicodedata
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from PIL import Image, ImageOps

MAX_IMAGE_SIDE = 1600
JPEG_QUALITY = 85

CATEGORIES = ("restaurant", "cafe", "dessert", "attraction", "shop", "other")
CATEGORY_LABELS = {
    "restaurant": "🍜 餐廳",
    "cafe": "☕ Cafe",
    "dessert": "🍰 甜品",
    "attraction": "📍 景點",
    "shop": "🛍️ 購物",
    "other": "✨ 其他",
}
CONFIDENCE_LEVELS = ("高", "中", "低")
_CONFIDENCE_ALIASES = {
    "high": "高", "medium": "中", "mid": "中", "low": "低",
    "高": "高", "中": "中", "低": "低",
}


# ---------------------------------------------------------------- images
def prepare_image(data: bytes, max_side: int = MAX_IMAGE_SIDE) -> bytes:
    """縮細圖片（最長邊 max_side）並轉成 RGB JPEG，慳 token 同加快回應。

    處理 RGBA / palette(P) / LA 等格式：透明位用白底填充。
    """
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # 跟手機相片方向
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- JSON parsing
def extract_json(text: str | None) -> Any:
    """盡量由模型回覆抽出 JSON。失敗就 raise ValueError。

    支援：純 JSON、```json code fence```、前後夾雜文字。
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 搵第一個 { 或 [ 開始，逐個可能嘅結尾試
    pairs = [("{", "}"), ("[", "]")]
    pairs.sort(key=lambda pc: s.find(pc[0]) if s.find(pc[0]) != -1 else len(s))
    for open_ch, close_ch in pairs:
        start = s.find(open_ch)
        end = s.rfind(close_ch)
        while start != -1 and end > start:
            try:
                return json.loads(s[start : end + 1])
            except json.JSONDecodeError:
                end = s.rfind(close_ch, start, end)
    raise ValueError("no valid JSON found")


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(_clean_str(v) for v in value if _clean_str(v))
    return str(value).strip()


def normalize_category(value: Any) -> str:
    v = _clean_str(value).lower()
    if v in CATEGORIES:
        return v
    aliases = {
        "餐廳": "restaurant", "食肆": "restaurant", "food": "restaurant",
        "咖啡": "cafe", "coffee": "cafe", "café": "cafe",
        "甜品": "dessert", "sweets": "dessert", "bakery": "dessert",
        "景點": "attraction", "sight": "attraction", "sightseeing": "attraction",
        "商店": "shop", "購物": "shop", "shopping": "shop", "store": "shop",
    }
    return aliases.get(v, "other")


def normalize_confidence(value: Any) -> str:
    return _CONFIDENCE_ALIASES.get(_clean_str(value).lower(), "中")


def _to_int_list(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[int] = []
    for item in items:
        try:
            n = int(str(item).strip().lstrip("#"))
        except (TypeError, ValueError):
            continue
        if n not in out:
            out.append(n)
    return out


def normalize_place(raw: dict) -> dict | None:
    """將一個模型回傳嘅 place 整理成固定 schema；冇名就回傳 None。"""
    if not isinstance(raw, dict):
        return None
    name = _clean_str(raw.get("name"))
    name_zh = _clean_str(raw.get("name_zh"))
    if not name and not name_zh:
        return None
    src = raw.get("source_images", raw.get("source_image", raw.get("source_image_index")))
    return {
        "name": name or name_zh,
        "name_zh": name_zh,
        "category": normalize_category(raw.get("category")),
        "area": _clean_str(raw.get("area")),
        "address": _clean_str(raw.get("address")),
        "opening_hours": _clean_str(raw.get("opening_hours")),
        "notes": _clean_str(raw.get("notes")),
        "confidence": normalize_confidence(raw.get("confidence")),
        "source_images": _to_int_list(src),
    }


def parse_places_response(text: str | None) -> list[dict]:
    """解析「識別地點」回覆，回傳已整理 + 去重嘅 places list。"""
    data = extract_json(text)
    if isinstance(data, dict):
        items = data.get("places", [])
    elif isinstance(data, list):
        items = data
    else:
        items = []
    places = [p for p in (normalize_place(i) for i in items or []) if p]
    return dedupe_places(places)


# ---------------------------------------------------------------- dedupe
def _norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s\-_·・'\"’`.,，。()（）\[\]【】「」]", "", s)


def _merge(a: dict, b: dict) -> dict:
    merged = dict(a)
    for key in ("name_zh", "area", "address", "opening_hours"):
        if not merged.get(key) and b.get(key):
            merged[key] = b[key]
    if b.get("notes") and b["notes"] not in merged.get("notes", ""):
        merged["notes"] = "；".join(x for x in (merged.get("notes"), b["notes"]) if x)
    if merged.get("category") == "other" and b.get("category") != "other":
        merged["category"] = b["category"]
    rank = {"高": 0, "中": 1, "低": 2}
    if rank.get(b.get("confidence"), 1) < rank.get(merged.get("confidence"), 1):
        merged["confidence"] = b["confidence"]
    merged["source_images"] = sorted(set(a.get("source_images", [])) | set(b.get("source_images", [])))
    return merged


def dedupe_places(places: Iterable[dict]) -> list[dict]:
    """用原文名或中文名（正規化後）去重，合併資料。保留首次出現次序。"""
    result: list[dict] = []
    for place in places:
        keys = {k for k in (_norm_key(place.get("name", "")), _norm_key(place.get("name_zh", ""))) if k}
        match = None
        for idx, existing in enumerate(result):
            ekeys = {k for k in (_norm_key(existing.get("name", "")), _norm_key(existing.get("name_zh", ""))) if k}
            if keys & ekeys:
                match = idx
                break
        if match is None:
            result.append(dict(place))
        else:
            result[match] = _merge(result[match], place)
    return result


def assign_display_numbers(keys: Iterable[str], labels: dict[str, int], next_num: int) -> int:
    """俾未見過嘅 key 分配穩定圖片編號（由 next_num 起）。

    已有編號唔會改。會直接改 labels，並回傳下一個未用編號。
    """
    for key in keys:
        if key not in labels:
            labels[key] = next_num
            next_num += 1
    return next_num


def map_batch_source_images(places: Iterable[dict], display_numbers: list[int]) -> None:
    """將模型回傳嘅 1-based batch 位置，對應呢批實際送出嘅圖片編號。

    display_numbers 同送俾模型嘅圖片順序一致。超出範圍嘅編號會捨棄。
    """
    for place in places:
        mapped: list[int] = []
        for idx in place.get("source_images") or []:
            # bool 係 int 子類，唔好當圖片編號。
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            if 1 <= idx <= len(display_numbers):
                number = display_numbers[idx - 1]
                if number not in mapped:
                    mapped.append(number)
        place["source_images"] = mapped


def escape_markdown_label(text: str) -> str:
    """跳脫 Markdown link label 入面會截斷連結嘅字元。"""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


# ---------------------------------------------------------------- map links
def place_query(place: dict) -> str:
    """Google Maps 搜尋字串：名稱 + 地址（冇地址就用地區）。"""
    name = place.get("name") or place.get("name_zh") or ""
    loc = place.get("address") or place.get("area") or ""
    return " ".join(x for x in (name, loc) if x).strip()


def maps_search_url(place: dict) -> str:
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": 1, "query": place_query(place)}, quote_via=quote
    )


MAX_WAYPOINTS = 9  # Google Maps URL 最多大約 9 個中途站


def maps_directions_url(queries: list[str]) -> str | None:
    """由一日嘅地點 list 生成 Google Maps 路線連結；少於 2 個地點回傳 None。"""
    stops = [q for q in queries if q]
    if len(stops) < 2:
        return None
    origin, destination = stops[0], stops[-1]
    middle = stops[1:-1][:MAX_WAYPOINTS]
    params = {"api": 1, "origin": origin, "destination": destination}
    if middle:
        params["waypoints"] = "|".join(middle)
    return "https://www.google.com/maps/dir/?" + urlencode(params, quote_via=quote)


# ---------------------------------------------------------------- itinerary
def parse_itinerary_response(text: str | None) -> dict:
    """解析行程 JSON，整理成 {title, days:[{day, area, theme, items:[...]}], tips:[...]}"""
    data = extract_json(text)
    if isinstance(data, list):
        data = {"days": data}
    if not isinstance(data, dict):
        raise ValueError("itinerary is not an object")
    days_out = []
    for i, day in enumerate(data.get("days") or [], start=1):
        if not isinstance(day, dict):
            continue
        items = []
        for item in day.get("items") or []:
            if not isinstance(item, dict):
                continue
            place = _clean_str(item.get("place_name") or item.get("name"))
            activity = _clean_str(item.get("activity"))
            if not place and not activity:
                continue
            items.append({
                "time": _clean_str(item.get("time")),
                "place_name": place,
                "activity": activity,
                "is_meal": bool(item.get("is_meal", False)),
                "maps_query": _clean_str(item.get("maps_query")) or place,
            })
        try:
            day_no = int(day.get("day", i))
        except (TypeError, ValueError):
            day_no = i
        days_out.append({
            "day": day_no,
            "area": _clean_str(day.get("area")),
            "theme": _clean_str(day.get("theme")),
            "items": items,
        })
    if not days_out:
        raise ValueError("itinerary has no days")
    tips = data.get("tips") or []
    if isinstance(tips, str):
        tips = [tips]
    return {
        "title": _clean_str(data.get("title")) or "我嘅懶人行程",
        "days": days_out,
        "tips": [_clean_str(t) for t in tips if _clean_str(t)],
    }


def day_directions_url(day: dict) -> str | None:
    return maps_directions_url([it.get("maps_query") or it.get("place_name") for it in day.get("items", [])])


def itinerary_to_markdown(itinerary: dict) -> str:
    lines = [f"# {itinerary.get('title') or '我嘅懶人行程'}", ""]
    for day in itinerary.get("days", []):
        header = f"## Day {day.get('day')}"
        if day.get("area"):
            header += f"｜{day['area']}"
        lines.append(header)
        if day.get("theme"):
            lines.append(f"_{day['theme']}_")
        lines.append("")
        for it in day.get("items", []):
            meal = "🍽️ " if it.get("is_meal") else ""
            time = f"**{it['time']}** " if it.get("time") else ""
            name = it.get("place_name") or ""
            link = ""
            if it.get("maps_query"):
                link = f" ([地圖]({maps_search_url({'name': it['maps_query']})}))"
            activity = f" — {it['activity']}" if it.get("activity") else ""
            lines.append(f"- {time}{meal}{name}{activity}{link}")
        route = day_directions_url(day)
        if route:
            lines.append("")
            lines.append(f"🗺️ [當日路線（Google Maps）]({route})")
        lines.append("")
    if itinerary.get("tips"):
        lines.append("## 小貼士")
        lines.extend(f"- {t}" for t in itinerary["tips"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

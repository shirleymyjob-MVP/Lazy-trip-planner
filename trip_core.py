"""純邏輯工具：圖片處理、JSON 解析、去重、Google Maps 連結、行程格式化。

呢個 module 唔 import streamlit 或 Gemini，方便單元測試。
"""
from __future__ import annotations

import io
import ipaddress
import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from PIL import Image, ImageOps

# 截圖只要睇到店名／地址。1280 長邊已經夠 OCR，又少啲 token，Gemini 會快啲。
MAX_IMAGE_SIDE = 1280
JPEG_QUALITY = 80

# QA 快樂路徑：上傳（自動認地方）或撳「認地方」→ 撳「生成行程」。
HAPPY_PATH_TAPS = 2
HAPPY_PATH_CAPTION = (
    f"手動步數（快樂路徑）：{HAPPY_PATH_TAPS}。"
    "① 上傳截圖（自動認地方）或貼店名／Google Maps 連結後撳「認地方」"
    "② 撳「生成行程」（預設日本、3 日、地點全選）。"
    "地址、營業時間、評分喺地點卡同行程入面，唔使離開呢版去 Google Maps 對基本資料。"
)

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


_RATING_EMPTY = {
    "", "未知", "不詳", "不確定", "唔肯定", "未有", "無", "n/a", "na", "none", "null", "-", "—",
}


def format_rating(value: Any) -> str:
    """整理評分成卡上可讀字串。冇把握或空值回傳空字串。"""
    v = _clean_str(value)
    v = re.sub(r"^(評分|rating)\s*[:：]\s*", "", v, flags=re.IGNORECASE).strip()
    if v.lower() in _RATING_EMPTY:
        return ""
    if len(v) > 80:
        v = v[:80].rstrip()
    if re.fullmatch(r"\d(?:\.\d{1,2})?", v):
        return f"{v}/5"
    return v


def place_fact_lines(place: dict) -> list[str]:
    """地點卡固定四行：地區、地址、營業時間、評分。缺資料都要顯示，唔好逼用家開 Maps。"""
    area = place.get("area") or "未有"
    address = place.get("address") or "未有"
    hours = place.get("opening_hours") or "未有"
    rating = format_rating(place.get("rating") or "") or "未有"
    return [
        f"📍 地區：{area}",
        f"🏠 地址：{address}",
        f"🕒 營業時間：{hours}",
        f"⭐ 評分：{rating}",
    ]


def seed_place(name: str, **extra: Any) -> dict | None:
    """用家貼上嘅店名／地圖搜尋字，先做成一張未補資料嘅地點卡。"""
    raw = {
        "name": name,
        "name_zh": extra.get("name_zh", ""),
        "category": extra.get("category", "other"),
        "area": extra.get("area", ""),
        "address": extra.get("address", ""),
        "opening_hours": extra.get("opening_hours", ""),
        "rating": extra.get("rating", ""),
        "notes": extra.get("notes", ""),
        "confidence": extra.get("confidence", "中"),
        "source_images": extra.get("source_images", []),
    }
    return normalize_place(raw)


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
        "rating": format_rating(raw.get("rating")),
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
    for key in ("name_zh", "area", "address", "opening_hours", "rating"):
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


# ---------------------------------------------------------------- pasted text / maps links
_URL_RE = re.compile(r"https?://[^\s<>\[\]()]+", re.IGNORECASE)
_SOCIAL_HOSTS = (
    "instagram.com",
    "instagr.am",
    "youtube.com",
    "youtu.be",
    "xiaohongshu.com",
    "xhslink.com",
    "xhs.cn",
    "tiktok.com",
)
# 只會向呢啲 host 發出短連結請求，避免用家貼咗其他網址就去抓。
_FETCH_HOSTS = {
    "maps.app.goo.gl",
    "goo.gl",
    "google.com",
    "www.google.com",
    "maps.google.com",
    "maps.google.co.jp",
    "www.google.co.jp",
    "www.google.com.hk",
}


class _Redirect(Exception):
    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


class _StopRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise _Redirect(newurl)


def _parse_http_url(url: str):
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return parsed


def _hostname(parsed) -> str:
    return (parsed.hostname or "").lower().rstrip(".")


def _host_is(host: str, marker: str) -> bool:
    return host == marker or host.endswith("." + marker)


def _is_social_host(host: str) -> bool:
    return any(_host_is(host, marker) for marker in _SOCIAL_HOSTS)


def _is_short_maps(host: str, path: str) -> bool:
    if host == "maps.app.goo.gl":
        return True
    return host == "goo.gl" and path.startswith("/maps")


def _is_maps_host(host: str, path: str) -> bool:
    if host == "maps.app.goo.gl" or host in {"maps.google.com", "maps.google.co.jp"}:
        return True
    if host == "goo.gl" and path.startswith("/maps"):
        return True
    if host == "google.com" or host.endswith(".google.com") or host.endswith(".google.co.jp"):
        return "/maps" in path
    return False


def _host_allowed_for_fetch(host: str) -> bool:
    if not host or host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return host in _FETCH_HOSTS


def _strip_url_tail(url: str) -> str:
    return url.rstrip(").,，。、；;]>\"'")


def _clean_place_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[\-\*\u2022]+\s*", "", line)
    line = re.sub(r"^\d+[\.\)、]\s*", "", line)
    return line.strip()


def maps_query_from_url(url: str) -> str:
    """由完整 Google Maps URL 抽出搜尋字。短連結或解唔到就回傳空字串。"""
    parsed = _parse_http_url(url)
    if parsed is None:
        return ""
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("query", "q", "destination"):
        values = qs.get(key) or []
        if values and values[0].strip():
            return urllib.parse.unquote_plus(values[0]).strip()
    path = urllib.parse.unquote(parsed.path or "")
    for marker in ("/place/", "/search/"):
        if marker in path:
            seg = path.split(marker, 1)[1].split("/")[0]
            seg = seg.replace("+", " ").strip()
            if seg and seg.lower() not in {"search", "place"}:
                return seg
    return ""


def parse_pasted_places(text: str | None) -> dict:
    """拆用家貼上嘅文字：店名、完整 Maps 連結、短連結、未支援嘅社群連結。

    唔會去抓 Instagram／YouTube／小紅書。
    """
    raw = text or ""
    urls = [_strip_url_tail(url) for url in _URL_RE.findall(raw)]
    leftover = _URL_RE.sub(" ", raw)
    seeds: list[dict] = []
    social: list[str] = []
    short: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        parsed = _parse_http_url(url)
        if parsed is None:
            other.append(url)
            continue
        host = _hostname(parsed)
        path = parsed.path or ""
        if _is_social_host(host):
            social.append(url)
            continue
        if _is_short_maps(host, path):
            short.append(url)
            continue
        if _is_maps_host(host, path):
            query = maps_query_from_url(url)
            place = seed_place(query) if query else None
            if place:
                seeds.append(place)
            else:
                other.append(url)
            continue
        other.append(url)
    for line in leftover.splitlines():
        name = _clean_place_line(line)
        if len(name) < 2:
            continue
        place = seed_place(name)
        if place:
            seeds.append(place)
    return {
        "seeds": dedupe_places(seeds),
        "social_urls": social,
        "short_urls": short,
        "other_urls": other,
    }


def _open_no_redirect(req: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_StopRedirect)
    return opener.open(req, timeout=timeout)


def resolve_maps_short_url(url: str, timeout: float = 4.0, max_hops: int = 4) -> str:
    """跟住 Google Maps 短連結，只允許跳去 Google host。解唔到就 raise ValueError。"""
    current = (url or "").strip()
    for _ in range(max_hops):
        parsed = _parse_http_url(current)
        if parsed is None or not _host_allowed_for_fetch(_hostname(parsed)):
            raise ValueError("blocked url")
        host = _hostname(parsed)
        path = parsed.path or ""
        if not _is_short_maps(host, path) and maps_query_from_url(current):
            return current
        if not _is_short_maps(host, path):
            raise ValueError("not a maps short link")
        req = urllib.request.Request(
            current,
            method="GET",
            headers={"User-Agent": "lazy-trip-planner"},
        )
        try:
            with _open_no_redirect(req, timeout) as resp:
                final = resp.geturl()
        except _Redirect as redirect:
            current = redirect.url
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ValueError("short link failed") from exc
        if maps_query_from_url(final):
            return final
        raise ValueError("no maps query")
    raise ValueError("too many redirects")


def match_place(item: dict, places: Iterable[dict]) -> dict | None:
    """用行程項目嘅店名／maps_query 對返收藏地點，方便喺行程顯示地址、時間、評分。"""
    queries = [_norm_key(item.get("place_name")), _norm_key(item.get("maps_query"))]
    query_set = {q for q in queries if q}
    places = list(places or [])
    for place in places:
        keys = {k for k in (_norm_key(place.get("name", "")), _norm_key(place.get("name_zh", ""))) if k}
        if keys & query_set:
            return place
    best = None
    best_len = 0
    for place in places:
        keys = {k for k in (_norm_key(place.get("name", "")), _norm_key(place.get("name_zh", ""))) if len(k) >= 2}
        for key in keys:
            for query in query_set:
                if key in query or query in key:
                    if len(key) > best_len:
                        best = place
                        best_len = len(key)
    return best


def attach_place_facts(itinerary: dict, places: Iterable[dict]) -> dict:
    """複製行程，並將對得返嘅地址／營業時間／評分寫入每個 item。唔改原物件。"""
    places = list(places or [])
    days = []
    for day in itinerary.get("days") or []:
        items = []
        for item in day.get("items") or []:
            cloned = dict(item)
            match = match_place(cloned, places)
            if match:
                for key in ("address", "opening_hours", "rating", "area"):
                    if not cloned.get(key) and match.get(key):
                        cloned[key] = match[key]
            items.append(cloned)
        new_day = dict(day)
        new_day["items"] = items
        days.append(new_day)
    return {**itinerary, "days": days}


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
            detail = []
            if it.get("address"):
                detail.append(f"地址：{it['address']}")
            elif it.get("area"):
                detail.append(f"地區：{it['area']}")
            if it.get("opening_hours"):
                detail.append(f"營業時間：{it['opening_hours']}")
            if it.get("rating"):
                detail.append(f"評分：{format_rating(it['rating']) or it['rating']}")
            if detail:
                lines.append("  - " + " · ".join(detail))
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

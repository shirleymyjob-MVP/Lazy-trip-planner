"""純邏輯工具：圖片處理、JSON 解析、去重、Google Maps 連結、行程格式化。

呢個 module 唔 import streamlit 或 Gemini，方便單元測試。
"""
from __future__ import annotations

import base64
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

# 兩段分開計，唔好併成一個「而家就要做完」嘅步數。
# 放入行程匣：貼上係 1 步（備註可順手寫，唔使再確認）。iPhone 可能要先撳一下貼上框。
# 稍後排行程：預設全選，撳「開始排行程」係 1 步。改城市／日數／偏好／同行係選項。
CAPTURE_TO_INBOX_TAPS = 1
PLAN_FROM_INBOX_TAPS = 1
HERO_LINE = "貼上你想去的地方，系統會幫你整理清單並安排每日行程，完成後可分享給朋友一起修改。"
OPENING_STEPS = (
    "收集想去的地方",
    "查看／整理我的收藏",
    "一鍵排行程",
    "收集後可邀請朋友一起修改",
)
HAPPY_PATH_CAPTION = HERO_LINE
PAYWALL_NOTE = "收費：待定"
UNVERIFIED = "未核實"
SELF_ATTRIBUTION = "我"
SELF_COLOR = "#4A90D9"
FRIEND_COLOR = "#8A8A8A"
SPARSE_COLLECTION_MIN = 3
FAVOURITE_CATEGORIES = ("美食", "景點", "購物", "美容", "文化體驗", "自然／戶外", "其他")
PLAN_PREF_OPTIONS = ("美食", "景點", "購物", "文化體驗", "自然／戶外")
TRIP_STYLES = (
    ("平衡型", "正常節奏，食與玩均衡"),
    ("網紅打卡型", "多打卡點，節奏快"),
    ("Chill 型", "慢活，長時間享用"),
    ("美食優先", "以餐廳為主"),
)
DEFAULT_TRIP_STYLE = "平衡型"
STYLE_RULES = {
    "平衡型": "每日最多 4 餐，1 至 2 個景點，餐廳停留約 1 至 1.5 小時。唔好成日冇食，亦唔好全日只去餐廳。",
    "網紅打卡型": "多打卡點，節奏較快，仍然要有正餐。",
    "Chill 型": "少站點，停留較長，食飯時間充足。",
    "美食優先": "以餐廳為主，但仍要有非用餐的休息，唔好全日只食。",
}
COLLECT_STATUS_IDLE = "尚未分析"
COLLECT_STATUS_RUNNING = "分析中…"
COLLECT_STATUS_VERIFYING = "資料核實中…"
COLLECT_STATUS_DONE = "已完成"
COLLECT_STATUS_FAIL = "無法辨識"
# ETA 係預算，方便收費前講等待。呢度冇真實 Gemini 時延實測。
PLAN_ENRICH_BUDGET_S = 20
PLAN_ITINERARY_BUDGET_S = 25
FAST_NAME_BUDGET_S = 8

# 目的地清單（2026-10-24 核對，冬季節由 10 月 25 日開始）：
# 國泰 CX 自營香港直飛：東京（HND／NRT）、大阪（KIX）、名古屋（NGO）、福岡（FUK）、札幌（CTS）。
# 香港快運 UO 而家訂到直飛：東京、大阪、名古屋、福岡、沖繩（那霸）、廣島、仙台、高松、小松。
# 石垣係 UO 季節線（2026 年夏飛到 8 月，下一季約 2027 年 3 月先再有票）。
# 未列入：靜岡（2025-10 起停飛）、宮古／下地島（2025-10 後冇班）、鹿兒島、熊本、長崎（冇直航）。
# 京都冇機場，當熱門城市，經大阪關西。
DESTINATION_DEFAULT = "日本（未指定城市）"
DESTINATION_OTHER = "其他（自己填）"
# (畫面選項, 交俾 AI 嘅目的地, 種類)
DESTINATION_OPTIONS: tuple[tuple[str, str, str], ...] = (
    (DESTINATION_DEFAULT, "日本", "default"),
    ("東京（國泰、香港快運直飛）", "東京", "popular"),
    ("大阪（國泰、香港快運直飛）", "大阪", "popular"),
    ("京都（熱門，經大阪關西）", "京都（經大阪關西）", "popular"),
    ("福岡（國泰、香港快運直飛）", "福岡", "popular"),
    ("札幌（國泰直飛新千歲）", "札幌", "popular"),
    ("名古屋（國泰、香港快運直飛）", "名古屋", "popular"),
    ("沖繩・那霸（香港快運直飛）", "沖繩（那霸）", "popular"),
    ("廣島（香港快運直飛）", "廣島", "direct"),
    ("仙台（香港快運直飛）", "仙台", "direct"),
    ("高松（香港快運直飛）", "高松", "direct"),
    ("小松（香港快運直飛）", "小松", "direct"),
    ("石垣（香港快運季節直飛）", "石垣", "direct"),
    (DESTINATION_OTHER, "", "other"),
)
ROUTE_NOTE = (
    "包括熱門城市，同國泰或香港快運由香港直飛嘅日本城市（2026年10月，包括福岡）。"
    "京都經大阪。石垣係季節線。自由文字只係揀「其他」先出現。"
)

PREFERENCE_OPTIONS = ("美食", "甜品／咖啡", "景點", "購物", "文化體驗", "自然／戶外", "其他")
PREFERENCE_DEFAULT = ("美食", "景點")
PARTY_OPTIONS = ("自己一個", "情侶", "朋友", "家庭（有小朋友）", "長輩同行", "其他")
PARTY_DEFAULT = "朋友"
DAY_OPTIONS = tuple(str(n) for n in range(1, 15))
DAY_DEFAULT = "3"
OTHER_CHOICE = "其他"

CATEGORIES = ("restaurant", "cafe", "dessert", "attraction", "shop", "salon", "other")
CATEGORY_LABELS = {
    "restaurant": "🍜 餐廳",
    "cafe": "☕ Cafe",
    "dessert": "🍰 甜品",
    "attraction": "📍 景點",
    "shop": "🛍️ 購物",
    "salon": "💇 美容",
    "other": "✨ 其他",
}
_SALON_RE = re.compile(
    r"美容室|美容院|美髮|美发|理髮|理发|理容室|理容院|美甲|ネイル|ヘアサロン|hair\s*salon|髮型屋|发型屋",
    re.IGNORECASE,
)
# 福岡大名的キアラ。畫面往往只有英文店名，沒有「美容室」三個字。
_SALON_NAME_RE = re.compile(r"kchiara|キアラ|きあら|奇阿拉", re.IGNORECASE)
_SHOPPING_COPY_RE = re.compile(r"選物|購物|逛街|血拼|買手信|手信店|尋寶|特色商品|店舖|店铺")
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


_HOUR_RANGE_RE = re.compile(
    r"\d{1,2}[:：]\d{2}\s*[-–—~～至到]\s*\d{1,2}[:：]\d{2}"
)
_WEEKDAY_RE = re.compile(r"[月火水木金土日]|週|星期|曜|定休|休み|祝")


def hours_specificity(text: str) -> tuple[int, int, int]:
    """分曜日、多過一個時段，比單一 10:00-20:00 更可信。"""
    cleaned = _clean_str(text)
    if not cleaned:
        return (0, 0, 0)
    weekday = 1 if _WEEKDAY_RE.search(cleaned) else 0
    return (weekday, len(_HOUR_RANGE_RE.findall(cleaned)), len(cleaned))


def prefer_printed_facts(place: dict) -> dict:
    """畫面印住嘅分曜日時間同評分，唔好被模型收成嘅單一時段蓋過。"""
    if not isinstance(place, dict):
        return place
    printed_hours = _clean_str(place.get("hours_on_image"))
    if printed_hours and hours_specificity(printed_hours) > hours_specificity(place.get("opening_hours") or ""):
        place["opening_hours"] = printed_hours
    printed_rating = _clean_str(place.get("rating_on_image"))
    if printed_rating:
        shown = format_rating(printed_rating)
        if shown:
            place["rating"] = shown
    return place


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
        "美容": "salon", "美容室": "salon", "美容院": "salon", "美髮": "salon", "美发": "salon",
        "理髮": "salon", "理发": "salon", "理容": "salon", "美甲": "salon", "salon": "salon",
    }
    return aliases.get(v, "other")


def looks_like_salon(place: dict | None) -> bool:
    """美容室／美髮／美甲唔係商店。店名、備註或類別有這些字就當 salon。

    kchiara／キアラ 是福岡大名的美容室。模型常常只回傳店名，並把類別寫成 shop。
    """
    if not isinstance(place, dict):
        return False
    blob = " ".join(
        _clean_str(place.get(key))
        for key in ("name", "name_zh", "name_original", "notes", "category", "fav_category")
    )
    if _SALON_RE.search(blob) or _SALON_NAME_RE.search(blob):
        return True
    return normalize_category(place.get("category")) == "salon"


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


_CITY_HINTS = (
    ("福岡", "福岡"),
    ("博多", "福岡"),
    ("fukuoka", "福岡"),
    ("hakata", "福岡"),
    ("東京", "東京"),
    ("tokyo", "東京"),
    ("大阪", "大阪"),
    ("osaka", "大阪"),
    ("京都", "京都"),
    ("kyoto", "京都"),
    ("札幌", "札幌"),
    ("北海道", "札幌"),
    ("sapporo", "札幌"),
    ("名古屋", "名古屋"),
    ("nagoya", "名古屋"),
    ("廣島", "廣島"),
    ("広島", "廣島"),
    ("hiroshima", "廣島"),
    ("仙台", "仙台"),
    ("sendai", "仙台"),
    ("高松", "高松"),
    ("takamatsu", "高松"),
    ("小松", "小松"),
    ("komatsu", "小松"),
    ("石垣", "石垣"),
    ("ishigaki", "石垣"),
    ("那霸", "沖繩"),
    ("沖繩", "沖繩"),
    ("沖縄", "沖繩"),
    ("okinawa", "沖繩"),
    ("naha", "沖繩"),
)
# 區名／英文別名。今泉、博多、Hakata 都歸福岡，唔使用家先揀。
_DISTRICT_HINTS = (
    ("今泉", "福岡"),
    ("中洲", "福岡"),
    ("天神", "福岡"),
    ("博多", "福岡"),
    ("hakata", "福岡"),
    ("渋谷", "東京"),
    ("澀谷", "東京"),
    ("新宿", "東京"),
    ("淺草", "東京"),
    ("浅草", "東京"),
    ("銀座", "東京"),
    ("道頓堀", "大阪"),
    ("心齋橋", "大阪"),
    ("心斎橋", "大阪"),
    ("難波", "大阪"),
    ("梅田", "大阪"),
    ("祇園", "京都"),
    ("嵐山", "京都"),
    ("國際通", "沖繩"),
    ("薄野", "札幌"),
)
UNSORTED_REGION = "未分類"
REGION_CONFIDENT = "高"
REGION_LOW = "低"
REGION_MANUAL = "手動"
QUICK_REGION_FIXES = ("福岡", "東京", "大阪", "京都", "札幌", "名古屋", "沖繩", "廣島")


def _region_blob(places: Iterable[dict] | None, remark: str = "") -> str:
    parts = [remark or ""]
    for place in places or []:
        parts.extend(
            str(place.get(key) or "")
            for key in ("area", "address", "name", "name_zh", "notes")
        )
    text = " ".join(parts)
    # 「東京都」入面有「京都」兩個字，唔好因此當京都。
    text = text.replace("東京都", "東京")
    return text.lower()


def _region_hits(places: Iterable[dict] | None, remark: str = "") -> dict[str, int]:
    blob = _region_blob(places, remark)
    hits: dict[str, int] = {}
    for needle, city in (*_DISTRICT_HINTS, *_CITY_HINTS):
        if needle.lower() in blob:
            hits[city] = hits.get(city, 0) + 1
    return hits


def _ordered_regions(hits: dict[str, int]) -> list[str]:
    ordered = [city for city in QUICK_REGION_FIXES if city in hits]
    for city in hits:
        if city not in ordered:
            ordered.append(city)
    return ordered


def classify_regions(places: Iterable[dict] | None = None, remark: str = "") -> tuple[list[str], str]:
    """估到嘅地區可以多過一個（連鎖店例如一蘭）。完全估唔到先係低信心。"""
    hits = _region_hits(places, remark)
    if not hits:
        return [], REGION_LOW
    return _ordered_regions(hits), REGION_CONFIDENT


def classify_region(places: Iterable[dict] | None = None, remark: str = "") -> tuple[str, str]:
    """只得一個明確地區先算高信心。幾個地區一齊出現就交俾 classify_regions，呢度當未鎖定單一城市。"""
    hits = _region_hits(places, remark)
    if len(hits) == 1:
        return next(iter(hits)), REGION_CONFIDENT
    if not hits:
        return "", REGION_LOW
    best = max(hits.values())
    winners = [city for city, count in hits.items() if count == best]
    if len(winners) == 1 and best >= 2:
        return winners[0], REGION_CONFIDENT
    return "", REGION_LOW


def infer_city(places: Iterable[dict]) -> str:
    """高信心先返回城市。博多、今泉、福岡縣都算福岡。估唔準就空字串。"""
    city, confidence = classify_region(places)
    if confidence != REGION_CONFIDENT:
        return ""
    return city


def inbox_region_names(item: dict) -> list[str]:
    """一張截圖可以屬於幾個地區。低信心先至係未分類。"""
    confidence = item.get("region_confidence")
    if confidence == REGION_LOW:
        return [UNSORTED_REGION]
    regions = [str(name).strip() for name in (item.get("regions") or []) if str(name).strip()]
    if not regions:
        city = (item.get("city") or "").strip()
        if city:
            regions = [city]
    if not regions:
        return [UNSORTED_REGION]
    return regions


def inbox_region_name(item: dict) -> str:
    """分組時用嘅第一個地區。未分類就係未分類。"""
    names = [name for name in inbox_region_names(item) if name != UNSORTED_REGION]
    if not names:
        return UNSORTED_REGION
    return names[0]


def group_inbox(items: Iterable[dict]) -> list[tuple[str, list[dict]]]:
    """按地區分組。多地區嘅項目會出現喺每個區。未分類放最後。"""
    buckets: dict[str, list[dict]] = {}
    for item in items or []:
        for name in inbox_region_names(item):
            buckets.setdefault(name, []).append(item)

    def sort_key(name: str) -> tuple:
        if name == UNSORTED_REGION:
            return (2, 0, name)
        if name in QUICK_REGION_FIXES:
            return (0, QUICK_REGION_FIXES.index(name), name)
        return (1, 0, name)

    return [(name, buckets[name]) for name in sorted(buckets, key=sort_key)]


def shared_inbox_region(items: Iterable[dict]) -> str:
    """揀咗嘅項目如果交集得一個已分類地區，就跟嗰個地區，唔使再揀城市。"""
    sets = []
    for item in items or []:
        names = {name for name in inbox_region_names(item) if name != UNSORTED_REGION}
        if names:
            sets.append(names)
    if not sets:
        return ""
    common = set.intersection(*sets)
    if len(common) == 1:
        return next(iter(common))
    return ""


def collect_status(item: dict) -> str:
    """收集卡狀態。永遠有字，唔好留空。"""
    analysis = item.get("analysis") or "idle"
    if analysis in ("idle", ""):
        return COLLECT_STATUS_IDLE
    if analysis == "pending":
        return COLLECT_STATUS_RUNNING
    if analysis == "failed":
        return COLLECT_STATUS_FAIL
    places = item.get("places") or []
    if not places:
        return COLLECT_STATUS_FAIL
    if item.get("facts") == "pending":
        return COLLECT_STATUS_VERIFYING
    return COLLECT_STATUS_DONE


def favourite_category(place: dict) -> str:
    if looks_like_salon(place):
        return "美容"
    raw = _clean_str(place.get("fav_category") or place.get("category"))
    if raw in FAVOURITE_CATEGORIES:
        return raw
    mapped = {
        "restaurant": "美食",
        "cafe": "美食",
        "dessert": "美食",
        "attraction": "景點",
        "shop": "購物",
        "salon": "美容",
    }
    return mapped.get(normalize_category(raw), "其他")


def model_category(place: dict) -> str:
    """交俾排行程嘅類別。美容室唔好再係 shop，否則文案會變成選物店。"""
    if looks_like_salon(place):
        return "salon"
    return normalize_category(place.get("category"))


def rewrite_activity(place_name: str, activity: str, category: str = "") -> str:
    """美容室唔好用購物句。原本已經係做髮型／美容嘅句子就保留。"""
    if not looks_like_salon({"name": place_name, "name_zh": place_name, "category": category}):
        return activity or ""
    text = activity or ""
    if not text or _SHOPPING_COPY_RE.search(text):
        return "預留時間做髮型或美容。"
    return text


def _is_real_shop(fav: dict) -> bool:
    """收藏裡真的是商店。美容室即使曾被標成購物，也不算。"""
    if looks_like_salon(fav):
        return False
    category = _clean_str(fav.get("category"))
    model = _clean_str(fav.get("model_category"))
    if category in ("購物", "shop") or model == "shop":
        return True
    return normalize_category(category) == "shop" or normalize_category(model) == "shop"


def rewrite_trip_title(title: str, favourites: Iterable[dict] | None = None) -> str:
    """收藏裡只有美容室、沒有真商店時，標題不要再寫購物之旅。"""
    text = _clean_str(title)
    if "購物" not in text:
        return text
    favs = [fav for fav in (favourites or []) if isinstance(fav, dict)]
    if not any(looks_like_salon(fav) for fav in favs):
        return text
    if any(_is_real_shop(fav) for fav in favs):
        return text
    text = text.replace("與購物", "").replace("和購物", "")
    text = text.replace("購物之旅", "之旅").replace("購物", "")
    text = re.sub(r"[與和]之旅", "之旅", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" 、，,")
    return text or _clean_str(title)


def place_regions(place: dict, item: dict | None = None) -> list[str]:
    """一間店可以多過一個地區，例如一蘭同時屬福岡市同博多。"""
    found: list[str] = []

    def add(name: str) -> None:
        cleaned = (name or "").strip()
        if not cleaned or cleaned == UNSORTED_REGION or cleaned in found:
            return
        found.append(cleaned)

    for part in re.split(r"[,，、/／]", place.get("area") or ""):
        add(part)
    if item:
        for name in item.get("regions") or []:
            add(str(name))
        if not found:
            add(item.get("city") or "")
    return found or [UNSORTED_REGION]


def attribution_label(added_by: str, viewer: str) -> tuple[str, str]:
    """收藏卡用。自己係（我, self），其他人係（顯示名, friend）。"""
    viewer_name = (viewer or "").strip() or "旅人"
    added = (added_by or "").strip()
    if not added or added == SELF_ATTRIBUTION or added == viewer_name:
        return SELF_ATTRIBUTION, "self"
    return added, "friend"


def attribution_html(added_by: str, viewer: str) -> str:
    import html

    label, kind = attribution_label(added_by, viewer)
    color = SELF_COLOR if kind == "self" else FRIEND_COLOR
    weight = "700" if kind == "self" else "500"
    return f'<span class="who-{kind}" style="color:{color};font-weight:{weight};">{html.escape(label)}</span>'


def public_fact(value: Any, *, rating: bool = False) -> str:
    if rating:
        shown = format_rating(value)
    else:
        shown = _clean_str(value)
        if shown in _RATING_EMPTY or shown == UNVERIFIED:
            shown = ""
    return shown or UNVERIFIED


def recognition_headline(item: dict) -> str:
    """行程匣即時狀態：認緊、店名，或者認唔到。"""
    if item.get("analysis") == "pending":
        return "認緊"
    titles = []
    for place in item.get("places") or []:
        title = (place.get("name_zh") or place.get("name") or "").strip()
        if title and title not in titles:
            titles.append(title)
    if titles:
        return "、".join(titles)
    return "認唔到"


def place_title(place: dict) -> str:
    return (place.get("name_zh") or place.get("name") or "").strip()


def place_is_complete(place: dict) -> bool:
    """收費前樣本要齊：名、地址、營業時間、評分。"""
    return bool(
        place_title(place)
        and (place.get("address") or "").strip()
        and (place.get("opening_hours") or "").strip()
        and (place.get("rating") or "").strip()
    )


def first_complete_place(places: Iterable[dict] | None) -> dict | None:
    for place in places or []:
        if place_is_complete(place):
            return place
    return None


def places_needing_facts(places: Iterable[dict] | None) -> list[dict]:
    return [place for place in places or [] if place and not place_is_complete(place)]


def estimate_plan_seconds(places_needing_facts: int) -> int:
    """排行程前顯示嘅預計秒數。預算，唔係 Gemini 實測。"""
    enrich = PLAN_ENRICH_BUDGET_S if places_needing_facts else 0
    return enrich + PLAN_ITINERARY_BUDGET_S


def decode_clipboard_payload(payload: Any, *, max_chars: int = 12_000_000) -> tuple[str, bytes, str] | None:
    """將貼上元件交返嚟嘅 JSON 變成 (id, jpeg, 備註)。唔係圖片或太大就回傳 None。"""
    if not isinstance(payload, dict):
        return None
    paste_id = str(payload.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{16,64}", paste_id):
        return None
    remark = str(payload.get("remark") or "").strip()[:200]
    raw = payload.get("data") or ""
    if not isinstance(raw, str) or not raw or len(raw) > max_chars:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return None
    if not data or len(data) > 8_000_000:
        return None
    try:
        jpeg = prepare_image(data)
    except Exception:  # noqa: BLE001 - 剪貼簿可以係任何位元組
        return None
    return paste_id, jpeg, remark


def destination_labels() -> list[str]:
    return [label for label, _dest, _kind in DESTINATION_OPTIONS]


def _destination_row(label: str) -> tuple[str, str, str] | None:
    for row in DESTINATION_OPTIONS:
        if row[0] == label:
            return row
    return None


def compose_trip_request(
    destination_label: str,
    *,
    destination_other: str = "",
    days: str | int = DAY_DEFAULT,
    preferences: Iterable[str] | None = None,
    preference_other: str = "",
    party: str = PARTY_DEFAULT,
    party_other: str = "",
    start_date: str = "",
    remarks: str = "",
) -> dict:
    """將畫面選項收成分析同排行程用嘅字串。自由文字只係揀咗「其他」先用。"""
    row = _destination_row(destination_label or DESTINATION_DEFAULT)
    if row is None or row[2] == "other":
        destination = (destination_other or "").strip() or "日本"
    else:
        destination = row[1]
    try:
        day_count = int(str(days).strip())
    except (TypeError, ValueError):
        day_count = int(DAY_DEFAULT)
    day_count = max(1, min(14, day_count))
    chosen = list(preferences) if preferences is not None else list(PREFERENCE_DEFAULT)
    pref_bits: list[str] = []
    for item in chosen:
        if item == OTHER_CHOICE:
            extra = (preference_other or "").strip()
            pref_bits.append(extra or OTHER_CHOICE)
        elif item:
            pref_bits.append(str(item))
    if (party or "") == OTHER_CHOICE:
        party_text = (party_other or "").strip() or OTHER_CHOICE
    else:
        party_text = (party or "").strip() or PARTY_DEFAULT
    notes: list[str] = []
    if (start_date or "").strip():
        notes.append(f"出發日期：{start_date.strip()}")
    if OTHER_CHOICE in chosen and (preference_other or "").strip():
        notes.append(preference_other.strip())
    if (party or "") == OTHER_CHOICE and (party_other or "").strip():
        notes.append(f"同行：{party_other.strip()}")
    return {
        "destination": destination,
        "days": day_count,
        "preferences": "、".join(pref_bits) or "（冇特別偏好）",
        "party": party_text,
        "notes": "；".join(notes),
        "remarks": (remarks or "").strip(),
        "start_date": (start_date or "").strip(),
    }


_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def caption_mark(index: int) -> str:
    if 1 <= index <= len(_CIRCLED_DIGITS):
        return _CIRCLED_DIGITS[index - 1]
    return str(index)


def normalize_caption_index(value: Any) -> int:
    if value is None or value is False:
        return 0
    text = _clean_str(value)
    if not text:
        return 0
    if text in _CIRCLED_DIGITS:
        return _CIRCLED_DIGITS.index(text) + 1
    chars: list[str] = []
    for ch in text:
        if ch in _CIRCLED_DIGITS:
            chars.append(str(_CIRCLED_DIGITS.index(ch) + 1))
        elif "０" <= ch <= "９":
            chars.append(chr(ord(ch) - ord("０") + ord("0")))
        else:
            chars.append(ch)
    match = re.search(r"\d+", "".join(chars))
    if not match:
        return 0
    number = int(match.group())
    if 1 <= number <= 30:
        return number
    return 0


def _normalize_remark_digits(text: str) -> str:
    chars: list[str] = []
    for ch in text:
        if ch in _CIRCLED_DIGITS:
            chars.append(str(_CIRCLED_DIGITS.index(ch) + 1))
            chars.append(" ")
        elif "０" <= ch <= "９":
            chars.append(chr(ord(ch) - ord("０") + ord("0")))
        else:
            chars.append(ch)
    return "".join(chars)


def _numbers_from_fragment(fragment: str) -> list[int]:
    found: list[int] = []
    for raw in re.findall(r"\d+", fragment or ""):
        number = int(raw)
        if 1 <= number <= 30 and number not in found:
            found.append(number)
    return found


_EXCLUDE_NUMBERS = re.compile(
    r"(?:唔要|不要|唔需|不需要|排除|除外|去掉|刪除|删掉|skip)"
    r"[^0-9]{0,12}"
    r"((?:\d+\s*[、，,和及同&\s]*)+)",
    re.IGNORECASE,
)
_INCLUDE_NUMBERS = re.compile(
    r"(?:只要|只係|只留|僅留|僅要|僅|只想|想要)"
    r"[^0-9]{0,16}"
    r"((?:\d+\s*[、，,和及同&\s]*)+)",
    re.IGNORECASE,
)
_CAPTION_NUMBERS = re.compile(
    r"(?:圖中|圖入面|圖入邊|圖裏|圖里|圖片中|編號|caption)"
    r"[^0-9]{0,8}"
    r"((?:\d+\s*[、，,和及同&\s]*)+)",
    re.IGNORECASE,
)


def _collect_numbers(pattern: re.Pattern, source: str) -> tuple[list[int], list[tuple[int, int]]]:
    numbers: list[int] = []
    spans: list[tuple[int, int]] = []
    for match in pattern.finditer(source):
        for number in _numbers_from_fragment(match.group(1)):
            if number not in numbers:
                numbers.append(number)
        spans.append(match.span())
    return numbers, spans


def parse_caption_filter(remarks: str | None) -> dict | None:
    """由備註抽出要留或要剔除嘅圖中編號。冇指示就回傳 None。

    「只要圖中 1、4」「圖中1，4」「①④」→ keep。
    「唔要／不要 2、3」→ drop，唔會當成要留。
    """
    raw = (remarks or "").strip()
    if not raw:
        return None
    text = _normalize_remark_digits(raw)
    drop, drop_spans = _collect_numbers(_EXCLUDE_NUMBERS, text)
    masked = list(text)
    for start, end in drop_spans:
        for pos in range(start, end):
            masked[pos] = " "
    masked_text = "".join(masked)
    keep, _spans = _collect_numbers(_INCLUDE_NUMBERS, masked_text)
    if not keep:
        keep, _spans = _collect_numbers(_CAPTION_NUMBERS, masked_text)
    if not keep and not drop:
        leftover = re.sub(r"[\d\s、，,和及同&／/．.]+", "", text)
        if leftover == "" and re.search(r"\d", text):
            keep = _numbers_from_fragment(text)
    if not keep and not drop:
        return None
    spec: dict = {}
    if keep:
        spec["keep"] = set(keep)
    if drop:
        spec["drop"] = set(drop)
    return spec


def apply_caption_filter(places: Iterable[dict], remarks: str | None) -> tuple[list[dict], str]:
    """按備註篩編號。認到嘅地方完全冇 caption_index 時唔好清空，只加警告。"""
    items = [dict(place) for place in places or []]
    spec = parse_caption_filter(remarks)
    if not spec:
        return items, ""
    if not any(int(place.get("caption_index") or 0) for place in items):
        return items, "備註提到圖中編號，但認到嘅地方未有編號，所以未有篩走。請再睇下清單。"
    keep = spec.get("keep")
    drop = spec.get("drop") or set()
    filtered: list[dict] = []
    for place in items:
        index = int(place.get("caption_index") or 0)
        if keep is not None and index not in keep:
            continue
        if index and index in drop:
            continue
        filtered.append(place)
    if not filtered:
        return items, "按備註篩完冇剩餘地方，已保留全部結果。可以改備註再分析。"
    if len(filtered) == len(items):
        return filtered, ""
    if keep is not None:
        marks = "、".join(caption_mark(n) for n in sorted(keep))
        return filtered, f"已按備註只留圖中 {marks}。"
    marks = "、".join(caption_mark(n) for n in sorted(drop))
    return filtered, f"已按備註拿走路圖中 {marks}。"


_BUILDING_RE = re.compile(r"階|ビル|セシオ|大廈|大厦", re.IGNORECASE)
_VISIBLE_ADDR_RE = re.compile(
    r"\d{1,4}(?:[-－]\d{1,4}){1,3}\s*[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]{0,24}(?:階|ビル|セシオ)"
)
_GARBLED_SCRIPT_RE = re.compile(r"[\u0E00-\u0E7F\u0600-\u06FF\u0590-\u05FF]")


def address_specificity(address: str) -> tuple[int, int, int]:
    """樓層／大廈名優先，其次先至係門牌數字。縣名較長唔算更具體。"""
    text = _clean_str(address)
    building = 1 if _BUILDING_RE.search(text) else 0
    return (building, len(re.findall(r"\d+", text)), len(text) if building else 0)


def prefer_visible_address(guessed: str, visible: str) -> str:
    """畫面印住嘅地址（例如 1-12-8 セシオ大名5階）唔好輸俾估出嚟嘅鄰近門牌。"""
    guessed = _clean_str(guessed)
    visible = _clean_str(visible)
    if not visible:
        return guessed
    if not guessed or address_specificity(visible) > address_specificity(guessed):
        return visible
    return guessed


def extract_visible_address(*texts: str) -> str:
    for text in texts:
        match = _VISIBLE_ADDR_RE.search(_clean_str(text))
        if match:
            return match.group(0).strip()
    return ""


def apply_visible_address(place: dict) -> dict:
    if not isinstance(place, dict):
        return place
    visible = _clean_str(place.get("address_on_image")) or extract_visible_address(
        place.get("notes") or "",
        place.get("address") or "",
    )
    if visible:
        place["address_on_image"] = visible
        place["address"] = prefer_visible_address(place.get("address") or "", visible)
    return place


def garbled_place_name(name: str) -> bool:
    """泰文、阿拉伯文等夾喺店名入面，係模型亂碼，唔係日本店名。"""
    return bool(_GARBLED_SCRIPT_RE.search(name or ""))


# IG Reels 標題句。中央大字店名（博多もつ將）有假名，唔好當標題丟掉。
_LISTICLE_TITLE_RE = re.compile(r"不吃會後悔|不吃会后悔|必吃|必去|攻略|合集|的美食|老闆$|老板$")
_COUNT_TITLE_RE = re.compile(r"\d+\s*間")
_KANA_RE = re.compile(r"[ぁ-んァ-ヶ]")


def is_listicle_title(name: str) -> bool:
    """「福岡5間」「不吃會後悔的美食」係系列標題，唔係店名。"""
    text = _clean_str(name)
    if not text:
        return True
    if _KANA_RE.search(text):
        return False
    if _LISTICLE_TITLE_RE.search(text):
        return True
    return bool(_COUNT_TITLE_RE.search(text) and len(text) <= 16)


def place_is_listicle_title(place: dict) -> bool:
    labels = [_clean_str(place.get("name")), _clean_str(place.get("name_zh"))]
    labels = [label for label in labels if label]
    if not labels:
        return True
    return all(is_listicle_title(label) for label in labels)


def keep_shop_names(places: Iterable[dict]) -> list[dict]:
    """有真正店名時丟掉系列標題。全部都係標題就原樣交還，等下一輪再讀大字。"""
    rows = [dict(place) for place in places or [] if isinstance(place, dict)]
    shops = [place for place in rows if not place_is_listicle_title(place)]
    return shops or rows


def normalize_place(raw: dict) -> dict | None:
    """將一個模型回傳嘅 place 整理成固定 schema；冇名就回傳 None。"""
    if not isinstance(raw, dict):
        return None
    name = _clean_str(raw.get("name"))
    name_zh = _clean_str(raw.get("name_zh"))
    if not name and not name_zh:
        return None
    src = raw.get("source_images", raw.get("source_image", raw.get("source_image_index")))
    category = normalize_category(raw.get("category"))
    place = {
        "name": name or name_zh,
        "name_zh": name_zh,
        "category": category,
        "area": _clean_str(raw.get("area")),
        "address": _clean_str(raw.get("address")),
        "opening_hours": _clean_str(raw.get("opening_hours")),
        "rating": format_rating(raw.get("rating")),
        "notes": _clean_str(raw.get("notes")),
        "confidence": normalize_confidence(raw.get("confidence")),
        "source_images": _to_int_list(src),
        "caption_index": normalize_caption_index(raw.get("caption_index")),
        "address_on_image": _clean_str(raw.get("address_on_image")),
        "hours_on_image": _clean_str(raw.get("hours_on_image")),
        "rating_on_image": _clean_str(raw.get("rating_on_image")),
    }
    apply_visible_address(place)
    prefer_printed_facts(place)
    if looks_like_salon(place):
        place["category"] = "salon"
    return place


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
    for key in (
        "name_zh", "area", "opening_hours", "rating", "address_on_image",
        "hours_on_image", "rating_on_image",
    ):
        if not merged.get(key) and b.get(key):
            merged[key] = b[key]
    if hours_specificity(b.get("hours_on_image") or "") > hours_specificity(merged.get("hours_on_image") or ""):
        merged["hours_on_image"] = b.get("hours_on_image")
    if hours_specificity(b.get("opening_hours") or "") > hours_specificity(merged.get("opening_hours") or ""):
        merged["opening_hours"] = b.get("opening_hours")
    if b.get("rating_on_image") and not merged.get("rating_on_image"):
        merged["rating_on_image"] = b.get("rating_on_image")
    prefer_printed_facts(merged)
    merged["address"] = prefer_visible_address(merged.get("address") or "", b.get("address") or "")
    apply_visible_address(merged)
    if b.get("notes") and b["notes"] not in merged.get("notes", ""):
        merged["notes"] = "；".join(x for x in (merged.get("notes"), b["notes"]) if x)
    if looks_like_salon(merged) or looks_like_salon(b) or b.get("category") == "salon":
        merged["category"] = "salon"
    elif merged.get("category") == "other" and b.get("category") not in (None, "", "other"):
        merged["category"] = b["category"]
    if not merged.get("caption_index") and b.get("caption_index"):
        merged["caption_index"] = b["caption_index"]
    rank = {"高": 0, "中": 1, "低": 2}
    if rank.get(b.get("confidence"), 1) < rank.get(merged.get("confidence"), 1):
        merged["confidence"] = b["confidence"]
    merged["source_images"] = sorted(set(a.get("source_images", [])) | set(b.get("source_images", [])))
    return merged


def dedupe_places(places: Iterable[dict]) -> list[dict]:
    """同名、或 kchiara／キアラ 呢類別名，合併成一間。保留首次出現次序。"""
    result: list[dict] = []
    for place in places:
        match = None
        for idx, existing in enumerate(result):
            if names_match(existing, place):
                match = idx
                break
        if match is None:
            result.append(dict(place))
        else:
            result[match] = _merge(result[match], place)
    return result


def merge_known_places(base: Iterable[dict], incoming: Iterable[dict]) -> list[dict]:
    """只補已有店名嘅地址、時間、評分，唔好把別張截圖嘅店併入嚟。"""
    result = [dict(place) for place in base or []]
    for extra in incoming or []:
        keys = {k for k in (_norm_key(extra.get("name", "")), _norm_key(extra.get("name_zh", ""))) if k}
        if not keys:
            continue
        for idx, existing in enumerate(result):
            ekeys = {k for k in (_norm_key(existing.get("name", "")), _norm_key(existing.get("name_zh", ""))) if k}
            if keys & ekeys or names_match(existing, extra):
                result[idx] = _merge(existing, extra)
                break
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
                "transit": _clean_str(item.get("transit")),
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


def _address_parts(address: str) -> tuple[str, list[int]]:
    text = unicodedata.normalize("NFKC", address or "").lower()
    text = text.replace("縣", "県").replace("區", "区")
    text = re.sub(r"[\s　]", "", text)
    nums = [int(n) for n in re.findall(r"\d+", text)]
    stem = re.sub(r"[\d\-－ー−‐]+", "", text)
    for token in ("日本", "福岡県", "福岡縣", "東京都", "大阪府", "京都府", "北海道"):
        stem = stem.replace(token, "")
    return stem, nums


def _same_street(stem_a: str, stem_b: str) -> bool:
    """街名相同，或較短的一邊是較長地址的街名後綴（大名 ⊂ …中央区大名）。"""
    if not stem_a or not stem_b:
        return False
    if stem_a == stem_b:
        return True
    short, long_ = (stem_a, stem_b) if len(stem_a) <= len(stem_b) else (stem_b, stem_a)
    return len(short) >= 2 and long_.endswith(short)


def addresses_near(left: str, right: str) -> bool:
    """同一條街。門牌只差一號，或其中一邊沒寫門牌（大名 vs 大名1-10-14），都當漂移。"""
    stem_a, nums_a = _address_parts(left)
    stem_b, nums_b = _address_parts(right)
    if not _same_street(stem_a, stem_b):
        return False
    if not nums_a or not nums_b:
        return True
    if len(nums_a) != len(nums_b) or nums_a[:-1] != nums_b[:-1]:
        return False
    return abs(nums_a[-1] - nums_b[-1]) <= 1


def _record_name_keys(record: dict) -> set[str]:
    return {
        key for key in (
            _norm_key(record.get("name", "")),
            _norm_key(record.get("name_zh", "")),
            _norm_key(record.get("name_original", "")),
        ) if key
    }


def _record_image_shas(record: dict) -> set[str]:
    found = {str(sha) for sha in (record.get("image_shas") or []) if sha}
    if record.get("image_sha"):
        found.add(str(record["image_sha"]))
    return found


def _shares_salon_alias(left: dict, right: dict) -> bool:
    """kchiara 與キアラ是同一間美容室，即使字面互不包含。"""
    blob_a = " ".join(_record_name_keys(left))
    blob_b = " ".join(_record_name_keys(right))
    return bool(_SALON_NAME_RE.search(blob_a) and _SALON_NAME_RE.search(blob_b))


def _primary_name_key(record: dict) -> str:
    """卡上顯示的名字。不要用另一個欄位的舊值把兩間不同的店併在一起。"""
    return _norm_key(record.get("name") or record.get("name_zh") or record.get("name_original") or "")


def names_match(left: dict, right: dict) -> bool:
    if _shares_salon_alias(left, right):
        return True
    key_a = _primary_name_key(left)
    key_b = _primary_name_key(right)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    short, long = (key_a, key_b) if len(key_a) <= len(key_b) else (key_b, key_a)
    return len(short) >= 4 and short in long


def same_favourite(left: dict, right: dict) -> bool:
    """同名，或同一張截圖再核對出嚟嘅相近地址，都係同一張收藏卡。

    一張圖裡有多間店時，不能只因共用圖片就把不同店併成一張。
    """
    if names_match(left, right):
        return True
    if not (_record_image_shas(left) & _record_image_shas(right)):
        return False
    if addresses_near(left.get("address") or "", right.get("address") or ""):
        return True
    return bool(left.get("sole_image") and right.get("sole_image"))


def _favourite_keys(fav: dict) -> set[str]:
    return {
        key for key in (
            _norm_key(fav.get("name", "")),
            _norm_key(fav.get("name_zh", "")),
            _norm_key(fav.get("name_original", "")),
        ) if key
    }


def match_favourite(place_name: str, favourites: Iterable[dict] | None) -> dict | None:
    key = _norm_key(place_name)
    if not key:
        return None
    exact = [fav for fav in favourites or [] if key in _favourite_keys(fav)]
    if exact:
        return exact[0]
    loose = []
    for fav in favourites or []:
        keys = _favourite_keys(fav)
        if any(len(min(key, other, key=len)) >= 2 and (key in other or other in key) for other in keys):
            loose.append(fav)
    if len(loose) == 1:
        return loose[0]
    return None


def present_itinerary(itinerary: dict, favourites: Iterable[dict] | None, *, allow_ai: bool) -> dict:
    """標明來自收藏定 AI 建議，並補上營業時間同評分。"""
    days = []
    for day in itinerary.get("days") or []:
        items = []
        for stop in day.get("items") or []:
            fav = match_favourite(stop.get("place_name") or "", favourites)
            ai = fav is None
            category = (fav or {}).get("category") or (fav or {}).get("model_category") or ""
            if garbled_place_name(stop.get("place_name") or "") and ai:
                continue
            items.append({
                **stop,
                "ai": ai,
                "hidden": ai and not allow_ai,
                "added_by": (fav or {}).get("added_by") or "",
                "activity": rewrite_activity(stop.get("place_name") or "", stop.get("activity") or "", category),
                "opening_hours": public_fact((fav or {}).get("opening_hours") or stop.get("opening_hours")),
                "rating": public_fact((fav or {}).get("rating") or stop.get("rating"), rating=True),
            })
        days.append({**day, "items": items})
    return {
        **itinerary,
        "title": rewrite_trip_title(itinerary.get("title") or "", favourites),
        "days": isolate_cloned_days(days),
    }


def isolate_cloned_days(days: list[dict]) -> list[dict]:
    """模型把同一清單複製到每一日時，後面的日子不要再顯示第一日的內容。"""
    seen: list[tuple[str, ...]] = []
    isolated = []
    for day in days or []:
        signature = tuple(
            _norm_key(item.get("place_name") or "")
            for item in day.get("items") or []
            if _norm_key(item.get("place_name") or "")
        )
        if signature and signature in seen:
            isolated.append({**day, "items": []})
            continue
        if signature:
            seen.append(signature)
        isolated.append(day)
    return isolated


def fallback_trip_title(region: str, days: int) -> str:
    place = (region or "").strip() or "日本"
    return f"{place}{days}日遊"


def style_planning_note(style: str, *, allow_ai: bool) -> str:
    chosen = style if style in STYLE_RULES else DEFAULT_TRIP_STYLE
    blurb = dict(TRIP_STYLES).get(chosen, "")
    rule = STYLE_RULES[chosen]
    extra = (
        "可以加入清單以外的地點，呢啲係 AI 建議。"
        if allow_ai
        else "只可以排清單入面的地方，唔好加入新店。"
    )
    return f"行程風格：{chosen}。{blurb}。{rule}{extra}"


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

"""懶人出行 — Streamlit entry point.

收集截圖（先不分析）→ 開始分析（店名，再核實地址／時間／評分）→ 我的收藏
→ 一鍵排行程 → 結果。收費維持待定，不擋住排程。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import gemini_client
import trip_core
import trip_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lazy_trip_planner")

# Cloud 有時新 app.py 對住舊 trip_core。新流程先有嘅常數缺咗就用畫面預設，唔好整頁爆。
DEFAULT_TRIP_STYLE = getattr(trip_core, "DEFAULT_TRIP_STYLE", "平衡型")
HERO_LINE = getattr(
    trip_core,
    "HERO_LINE",
    "貼上你想去的地方，系統會幫你整理清單並安排每日行程，完成後可分享給朋友一起修改。",
)
OPENING_STEPS = getattr(
    trip_core,
    "OPENING_STEPS",
    (
        "收集想去的地方",
        "查看／整理我的收藏",
        "一鍵排行程",
        "收集後可邀請朋友一起修改",
    ),
)
PAYWALL_NOTE = getattr(trip_core, "PAYWALL_NOTE", "收費：待定")
UNVERIFIED = getattr(trip_core, "UNVERIFIED", "未核實")
SPARSE_COLLECTION_MIN = getattr(trip_core, "SPARSE_COLLECTION_MIN", 3)
FAVOURITE_CATEGORIES = getattr(
    trip_core,
    "FAVOURITE_CATEGORIES",
    ("美食", "景點", "購物", "文化體驗", "自然／戶外", "其他"),
)
PLAN_PREF_OPTIONS = getattr(
    trip_core,
    "PLAN_PREF_OPTIONS",
    ("美食", "景點", "購物", "文化體驗", "自然／戶外"),
)
TRIP_STYLES = getattr(
    trip_core,
    "TRIP_STYLES",
    (
        ("平衡型", "正常節奏，食與玩均衡"),
        ("網紅打卡型", "多打卡點，節奏快"),
        ("Chill 型", "慢活，長時間享用"),
        ("美食優先", "以餐廳為主"),
    ),
)
COLLECT_STATUS_IDLE = getattr(trip_core, "COLLECT_STATUS_IDLE", "尚未分析")
COLLECT_STATUS_RUNNING = getattr(trip_core, "COLLECT_STATUS_RUNNING", "分析中…")
COLLECT_STATUS_VERIFYING = getattr(trip_core, "COLLECT_STATUS_VERIFYING", "資料核實中…")
COLLECT_STATUS_DONE = getattr(trip_core, "COLLECT_STATUS_DONE", "已完成")
COLLECT_STATUS_FAIL = getattr(trip_core, "COLLECT_STATUS_FAIL", "無法辨識")


def _build_sha() -> str:
    """俾 UAT 對住 build: 確認 Streamlit 載緊邊個 commit。"""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(Path(__file__).parent),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        sha = ""
    return sha or "unknown"


def _collect_status(item: dict) -> str:
    """舊 trip_core 冇 collect_status 時當尚未分析，唔好整頁爆。"""
    fn = getattr(trip_core, "collect_status", None)
    if not callable(fn):
        return COLLECT_STATUS_IDLE
    return fn(item)


def _call_trip_core(name: str, *args, default):
    """舊 trip_core 冇新 helper 時用 default，避免 init 期間整頁爆。"""
    fn = getattr(trip_core, name, None)
    if not callable(fn):
        return default
    return fn(*args)


st.set_page_config(page_title="懶人出行 ✈️", page_icon="✈️", layout="centered")
# init 之後先畫頁尾的話，AttributeError 會令 UAT 睇唔到 SHA。
st.caption(f"build: {_build_sha()}")
st.markdown(
    """
    <style>
      .stApp { background: linear-gradient(180deg, #FFF8F5 0%, #FFFFFF 240px); }
      h1, h2, h3 { color: #2C2C2C; }
      div.stButton > button[kind="primary"] {
        background: #FF7A6E;
        border: 1px solid #FF7A6E;
        color: #FFFFFF;
      }
      div.stButton > button[kind="primary"]:hover {
        background: #E85D52;
        border-color: #E85D52;
        color: #FFFFFF;
      }
      div.stButton > button[kind="secondary"] {
        background: #FFFFFF;
        color: #E85D52;
        border: 1.5px solid #FF7A6E;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

MAX_IMAGES_PER_UPLOAD = 10
# 認完店名之後一次過搵公開資料。多過呢個數就留待下一輪，避免一次 prompt 太長。
AUTO_ENRICH_CAP = 12
_PASTE = components.declare_component(
    "inbox_paste",
    path=str(Path(__file__).parent / "components" / "inbox_paste"),
)


# ---------------------------------------------------------------- config
def get_secret(key: str, default=None):
    """讀 st.secrets；本地冇 secrets.toml 都唔會爆。"""
    try:
        return st.secrets.get(key, default)
    except Exception:  # noqa: BLE001 - StreamlitSecretNotFoundError 等
        return default


def get_int_secret(key: str, default: int) -> int:
    try:
        return int(get_secret(key, default))
    except (TypeError, ValueError):
        logger.warning("Secret %s is not an int, using default %s", key, default)
        return default


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


API_KEY = get_secret("GEMINI_API_KEY")
# 認店名、核對、排日程都用 lite，唔跟 GEMINI_MODEL（3.8-flash 排日程會拖到 120 秒）。
ITINERARY_MODEL = getattr(gemini_client, "DEFAULT_ITINERARY_MODEL", "gemini-3.5-flash-lite")
# 每個 browser session 最多幾多次成功嘅 AI 動作（分析入匣、開始排行程）。
# 失敗、SDK 重試、同一輪入面幾張圖唔另計。
MAX_ANALYSES = get_int_secret("MAX_ANALYSES_PER_SESSION", 10)
TIMEOUT_MS = _clamp_int(
    get_int_secret("GEMINI_TIMEOUT_MS", getattr(gemini_client, "DEFAULT_TIMEOUT_MS", 120_000)),
    10_000,
    180_000,
)
NAMES_MODEL = getattr(gemini_client, "DEFAULT_NAMES_MODEL", "gemini-3.5-flash-lite")
ENRICH_MODEL = getattr(gemini_client, "DEFAULT_ENRICH_MODEL", "gemini-3.5-flash-lite")
# 免費額度預設唔叫 Maps。Secret 或環境變數 GEMINI_USE_MAPS=1 先開啟。
_MAPS_FLAG = get_secret("GEMINI_USE_MAPS", os.environ.get("GEMINI_USE_MAPS", ""))
USE_MAPS = str(_MAPS_FLAG or "").strip().lower() in {"1", "true", "yes", "on"}
RETRY_ATTEMPTS = _clamp_int(
    get_int_secret("GEMINI_RETRY_ATTEMPTS", getattr(gemini_client, "DEFAULT_RETRY_ATTEMPTS", 2)),
    1,
    4,
)


@st.cache_resource(show_spinner=False)
def get_client(api_key: str, timeout_ms: int, retry_attempts: int):
    return gemini_client.make_client(api_key, timeout_ms=timeout_ms, retry_attempts=retry_attempts)


def _client():
    return get_client(API_KEY, TIMEOUT_MS, RETRY_ATTEMPTS)


# ---------------------------------------------------------------- state
def init_state():
    defaults = {
        "inbox": [],
        "itinerary": None,
        "itinerary_places": [],
        "usage": 0,
        "batch": 0,
        "next_image_num": 1,
        "seen_upload_ids": set(),
        "seen_paste_ids": set(),
        "notice": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("destination_choice", trip_core.DESTINATION_DEFAULT)
    st.session_state.setdefault("days_choice", trip_core.DAY_DEFAULT)
    st.session_state.setdefault("pref_pills", list(trip_core.PREFERENCE_DEFAULT))
    st.session_state.setdefault("party_pills", trip_core.PARTY_DEFAULT)
    st.session_state.setdefault("destination_other", "")
    st.session_state.setdefault("pref_other", "")
    st.session_state.setdefault("party_other", "")
    st.session_state.setdefault("upload_remark", "")
    st.session_state.setdefault("inbox_region_filter", "全部")
    st.session_state.setdefault("display_name", "旅人")
    st.session_state.setdefault("members", [])
    st.session_state.setdefault("screen", "A")
    st.session_state.setdefault("favourites", [])
    st.session_state.setdefault("removed_fav_ids", [])
    st.session_state.setdefault("trip_style", DEFAULT_TRIP_STYLE)
    st.session_state.setdefault("allow_ai", True)
    st.session_state.setdefault("day_count", 3)
    st.session_state.setdefault("show_invite", False)


def remaining_quota() -> int:
    return max(0, MAX_ANALYSES - st.session_state.usage)


def clear_inbox():
    st.session_state.inbox = []
    st.session_state.itinerary = None
    st.session_state.itinerary_places = []
    st.session_state.seen_upload_ids = set()
    st.session_state.seen_paste_ids = set()
    st.session_state.notice = None
    st.session_state.next_image_num = 1
    st.session_state.inbox_region_filter = "全部"
    st.session_state.batch = st.session_state.get("batch", 0) + 1


def _upload_key(uploaded_file) -> str:
    file_id = getattr(uploaded_file, "file_id", None)
    if file_id:
        return str(file_id)
    return hashlib.sha256(uploaded_file.getvalue()).hexdigest()


def _viewer_name() -> str:
    return (st.session_state.get("display_name") or "旅人").strip() or "旅人"


def _new_item(image: bytes, remark: str, *, batch: bool = False) -> dict:
    number = st.session_state.next_image_num
    st.session_state.next_image_num = number + 1
    return {
        "id": uuid.uuid4().hex,
        "label": f"截圖 {number}",
        "image": image,
        "remark": (remark or "").strip(),
        "status": "未排程",
        "analysis": "idle",
        "facts": "",
        "batch": batch,
        "places": [],
        "city": "",
        "regions": [],
        "region_confidence": trip_core.REGION_LOW,
        "warning": "",
        "added_by": _viewer_name(),
    }


def _date_text(value) -> str:
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _trip_request() -> dict:
    prefs = st.session_state.get("pref_pills")
    if prefs is None:
        prefs = list(trip_core.PREFERENCE_DEFAULT)
    return trip_core.compose_trip_request(
        st.session_state.get("destination_choice") or trip_core.DESTINATION_DEFAULT,
        destination_other=st.session_state.get("destination_other") or "",
        days=st.session_state.get("days_choice") or trip_core.DAY_DEFAULT,
        preferences=list(prefs),
        preference_other=st.session_state.get("pref_other") or "",
        party=st.session_state.get("party_pills") or trip_core.PARTY_DEFAULT,
        party_other=st.session_state.get("party_other") or "",
        start_date=_date_text(st.session_state.get("start_date")),
        remarks="",
    )


def _render_notice():
    notice = st.session_state.get("notice")
    if not notice:
        return
    level, text = notice
    renderer = {"error": st.error, "warning": st.warning, "success": st.success, "info": st.info}.get(level, st.info)
    renderer(text)


def _apply_region(item: dict) -> None:
    """用截圖文字同備註標地區。用家一撳改過嘅，分析唔好覆寫。"""
    if item.get("region_confidence") == trip_core.REGION_MANUAL:
        return
    regions, confidence = trip_core.classify_regions(item.get("places") or [], item.get("remark") or "")
    item["regions"] = regions
    item["city"] = regions[0] if regions else ""
    item["region_confidence"] = confidence


def _fail_items(items: list[dict], message: str) -> None:
    for item in items:
        item["analysis"] = "failed"
        item["warning"] = message
        _apply_region(item)


def _distribute_places(items: list[dict], places: list[dict]) -> None:
    buckets: list[list[dict]] = [[] for _ in items]
    for place in places:
        indexes = [
            idx for idx in place.get("source_images") or []
            if isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= len(items)
        ]
        if not indexes and len(items) == 1:
            indexes = [1]
        for idx in indexes:
            copied = dict(place)
            copied["source_images"] = [idx]
            buckets[idx - 1].append(copied)
    for item, found in zip(items, buckets):
        item["places"] = trip_core.dedupe_places(found)
        item["analysis"] = "done"
        _apply_region(item)
    _queue_fact_lookup(items)


def _recognize_pending():
    pending = [item for item in st.session_state.inbox if item.get("analysis") == "pending"]
    if not pending:
        return
    if not API_KEY:
        _fail_items(pending, "AI 未設定，未有分析。截圖已留喺行程匣。")
        st.session_state.notice = ("error", "AI 服務未設定好，行程匣未有認到地方。")
        return
    if remaining_quota() <= 0:
        _fail_items(pending, "今個 session 嘅次數用完，未有分析。")
        st.session_state.notice = ("warning", "今個 session 嘅免費次數用完喇 🙏 之後會推出登入／付費版本。")
        return

    groups: list[tuple[str, list[dict]]] = []
    for item in pending:
        remark = item.get("remark") or ""
        if groups and groups[-1][0] == remark:
            groups[-1][1].append(item)
        else:
            groups.append((remark, [item]))

    succeeded = False
    warnings: list[str] = []
    with st.status(f"認緊 {len(pending)} 張新截圖嘅店名…", expanded=True) as status:
        status.write("先辨識店名同地區，接著核實地址、營業時間、評分。")
        for remark, items in groups:
            images = [item["image"] for item in items]
            try:
                raw = gemini_client.recognize_names(
                    _client(),
                    NAMES_MODEL,
                    images,
                    remarks=remark,
                    destination="日本",
                    preferences="",
                    party="",
                )
                result = raw if isinstance(raw, gemini_client.AnalysisResult) else gemini_client.AnalysisResult(list(raw), [])
            except gemini_client.GeminiError as err:
                logger.info("Inbox analysis failed: %s", err)
                _fail_items(items, str(err))
                warnings.append(str(err))
                continue
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected inbox analysis error")
                message = "分析途中出咗意外，請再試一次。"
                _fail_items(items, message)
                warnings.append(message)
                continue
            _distribute_places(items, result.places)
            note = " ".join(result.warnings)
            for item in items:
                item["warning"] = note
            if note:
                warnings.append(note)
            succeeded = True
        if succeeded:
            status.update(label="行程匣更新咗", state="complete")
        elif warnings:
            status.update(label="分析失敗", state="error")
        else:
            status.update(label="分析完成", state="complete")
    if succeeded:
        st.session_state.usage += 1
    if warnings:
        st.session_state.notice = ("warning", " ".join(dict.fromkeys(warnings)))
    elif succeeded:
        st.session_state.notice = None


def _selected_items() -> list[dict]:
    chosen = []
    for item in st.session_state.inbox:
        if st.session_state.get(f"pick_{item['id']}", True):
            chosen.append(item)
    return chosen


def _plan_destination(request: dict, items: list[dict]) -> str:
    """目的地仍然係「日本」時，跟行程匣唯一已分類地區，唔使再揀城市。"""
    destination = request["destination"]
    if destination == "日本":
        shared = trip_core.shared_inbox_region(items)
        if shared:
            return shared
    return destination


def _plan_notes(request: dict, items: list[dict]) -> str:
    bits = [request["notes"]] if request.get("notes") else []
    remarks = [f"{item['label']}：{item['remark']}" for item in items if item.get("remark")]
    if remarks:
        bits.append("截圖備註 " + "；".join(remarks))
    return "；".join(bits)


_TRIP_FIELDS = (
    "destination_choice", "days_choice", "pref_pills", "party_pills",
    "destination_other", "pref_other", "party_other",
)


def _sync_recognize() -> bool:
    return os.environ.get("LAZY_TRIP_SYNC_RECOGNIZE") == "1"


def _ensure_share() -> None:
    query = st.query_params.get("share", "")
    if isinstance(query, list):
        query = query[0] if query else ""
    token = st.session_state.get("share_token") or ""
    if not re.fullmatch(r"[0-9a-f]{8,32}", token):
        token = query if re.fullmatch(r"[0-9a-f]{8,32}", query or "") else trip_store.new_token()
    st.session_state.share_token = token
    if query != token:
        st.query_params["share"] = token


def _share_url() -> str:
    try:
        current = st.context.url or ""
    except Exception:  # noqa: BLE001
        current = ""
    base = current.split("?", 1)[0] if current else "http://localhost:8501/"
    return f"{base}?share={st.session_state.share_token}"


def _apply_loaded_trip(loaded: dict) -> None:
    st.session_state.inbox = list(loaded.get("inbox") or [])
    st.session_state.itinerary = loaded.get("itinerary")
    st.session_state.itinerary_places = list(loaded.get("itinerary_places") or [])
    st.session_state.next_image_num = int(loaded.get("next_image_num") or 1)
    st.session_state.members = list(loaded.get("members") or [])
    for key in _TRIP_FIELDS:
        if loaded.get(key) is not None:
            st.session_state[key] = loaded[key]
    st.session_state.favourites = list(loaded.get("favourites") or [])
    st.session_state.removed_fav_ids = list(loaded.get("removed_fav_ids") or [])
    if loaded.get("trip_style"):
        st.session_state.trip_style = loaded["trip_style"]
    if "allow_ai" in loaded:
        st.session_state.allow_ai = bool(loaded.get("allow_ai"))
    if loaded.get("day_count"):
        st.session_state.day_count = int(loaded["day_count"])
    picks = loaded.get("picks") or {}
    for item in st.session_state.inbox:
        st.session_state[f"pick_{item['id']}"] = bool(picks.get(item["id"], True))
        st.session_state[f"remark_{item['id']}"] = item.get("remark") or ""
        names = [name for name in trip_core.inbox_region_names(item) if name != trip_core.UNSORTED_REGION]
        st.session_state[f"region_fix_{item['id']}"] = names
    st.session_state.last_digest = trip_store.digest({
        "inbox": st.session_state.inbox,
        "itinerary": st.session_state.itinerary,
        "itinerary_places": st.session_state.itinerary_places,
        "next_image_num": st.session_state.next_image_num,
        "picks": {item["id"]: bool(picks.get(item["id"], True)) for item in st.session_state.inbox},
        "members": st.session_state.members,
        **{key: st.session_state.get(key) for key in _TRIP_FIELDS},
    })


def _load_shared_trip() -> None:
    token = st.session_state.share_token
    try:
        mt = trip_store.mtime(token)
    except ValueError:
        return
    if mt == st.session_state.get("seen_mtime"):
        return
    st.session_state.seen_mtime = mt
    if not mt:
        st.session_state.last_digest = ""
        return
    loaded = trip_store.load_trip(token)
    if loaded:
        _apply_loaded_trip(loaded)


def _snapshot() -> dict:
    inbox = []
    picks = {}
    for item in st.session_state.inbox:
        remark_key = f"remark_{item['id']}"
        if remark_key in st.session_state:
            item["remark"] = (st.session_state.get(remark_key) or "").strip()
        picks[item["id"]] = bool(st.session_state.get(f"pick_{item['id']}", True))
        inbox.append(item)
    name = (st.session_state.get("display_name") or "旅人").strip() or "旅人"
    members = sorted({*(st.session_state.get("members") or []), name})
    st.session_state.members = members
    return {
        "token": st.session_state.share_token,
        "inbox": inbox,
        "itinerary": st.session_state.itinerary,
        "itinerary_places": st.session_state.itinerary_places,
        "next_image_num": st.session_state.next_image_num,
        "picks": picks,
        "members": members,
        "favourites": list(st.session_state.get("favourites") or []),
        "removed_fav_ids": list(st.session_state.get("removed_fav_ids") or []),
        "trip_style": st.session_state.get("trip_style") or DEFAULT_TRIP_STYLE,
        "allow_ai": bool(st.session_state.get("allow_ai")),
        "day_count": int(st.session_state.get("day_count") or 3),
        **{key: st.session_state.get(key) for key in _TRIP_FIELDS},
    }


def _persist_shared_trip() -> None:
    snapshot = _snapshot()
    current = trip_store.digest(snapshot)
    if current == st.session_state.get("last_digest"):
        return
    try:
        st.session_state.seen_mtime = trip_store.save_trip(snapshot)
    except ValueError:
        logger.exception("Could not save shared trip")
        return
    st.session_state.last_digest = current


def _remember_region_fix(item_id: str) -> None:
    choice = st.session_state.get(f"region_fix_{item_id}") or []
    if isinstance(choice, str):
        choice = [choice]
    for item in st.session_state.inbox:
        if item.get("id") == item_id:
            item["regions"] = list(choice)
            item["city"] = choice[0] if choice else ""
            item["region_confidence"] = trip_core.REGION_MANUAL
            return


def _remember_remark(item_id: str) -> None:
    text = (st.session_state.get(f"remark_{item_id}") or "").strip()
    for item in st.session_state.inbox:
        if item.get("id") == item_id:
            item["remark"] = text
            _apply_region(item)
            return


def _overlay_enriched(items: list[dict], enriched: list[dict]) -> None:
    for item in items:
        item["places"] = trip_core.merge_known_places(item.get("places") or [], enriched)
        _apply_region(item)


def _place_keys(place: dict) -> set[str]:
    return {
        key for key in (
            trip_core._norm_key(place.get("name", "")),
            trip_core._norm_key(place.get("name_zh", "")),
        ) if key
    }


def _queue_fact_lookup(items: list[dict]) -> None:
    """店名已經出咗。未齊地址／時間／評分嘅，下一輪先一次過搵，唔好阻住個名出現。"""
    for item in items:
        if trip_core.places_needing_facts(item.get("places") or []):
            item["facts"] = "pending"
        else:
            item["facts"] = "done"


def _items_touching(items: list[dict], keys: set[str]) -> list[dict]:
    touched = []
    for item in items:
        for place in trip_core.places_needing_facts(item.get("places") or []):
            if _place_keys(place) & keys:
                touched.append(item)
                break
    return touched


def _settle_fact_state(items: list[dict], batch_keys: set[str]) -> None:
    for item in items:
        waiting = [
            place for place in trip_core.places_needing_facts(item.get("places") or [])
            if not (_place_keys(place) & batch_keys)
        ]
        item["facts"] = "pending" if waiting else "done"


def _note_fact_failure(message: str) -> None:
    if message == gemini_client.CROWD_RATE_LIMIT_MESSAGE:
        st.session_state.notice = ("error", message)
        return
    bits = [gemini_client.ENRICH_INCOMPLETE_WARNING]
    if not USE_MAPS:
        bits.append(gemini_client.MAPS_UNAVAILABLE_WARNING)
    if message and message not in bits and gemini_client.CROWD_RATE_LIMIT_MESSAGE not in message:
        bits.append(message)
    st.session_state.notice = ("warning", " ".join(bits))


def _enrich_inbox_facts() -> None:
    """認完名之後一次過文字核對。唔另計 session 次數；Maps 仍然跟 GEMINI_USE_MAPS。"""
    pending_items = [
        item for item in st.session_state.inbox
        if item.get("facts") == "pending" and item.get("analysis") == "done"
    ]
    if not pending_items:
        return
    if not API_KEY:
        for item in pending_items:
            item["facts"] = "failed"
        return
    seeds = trip_core.places_needing_facts(trip_core.dedupe_places(
        place for item in pending_items for place in (item.get("places") or [])
    ))
    batch = seeds[:AUTO_ENRICH_CAP]
    if not batch:
        for item in pending_items:
            item["facts"] = "done"
        return
    batch_keys: set[str] = set()
    for place in batch:
        batch_keys |= _place_keys(place)
    targeted = _items_touching(pending_items, batch_keys)
    if not targeted:
        for item in pending_items:
            item["facts"] = "done"
        return
    try:
        with st.status("資料核實中…", expanded=True) as status:
            status.write("用店名搵公開地址、營業時間、評分。")
            result = _enrich_result(batch)
            _overlay_enriched(targeted, result.places)
            _settle_fact_state(targeted, batch_keys)
            status.update(label="資料更新咗", state="complete")
    except gemini_client.GeminiError as err:
        logger.info("Inbox fact lookup failed: %s", err)
        for item in targeted:
            item["facts"] = "failed"
        _note_fact_failure(str(err))
        return
    except Exception:  # noqa: BLE001
        logger.exception("Inbox fact lookup failed")
        for item in targeted:
            item["facts"] = "failed"
        st.session_state.notice = ("warning", "認資料失敗，請再試一次。")
        return
    filled = any(
        trip_core.place_is_complete(place)
        for item in targeted
        for place in (item.get("places") or [])
        if _place_keys(place) & batch_keys
    )
    if result.warnings and not filled:
        bits = []
        for warning in result.warnings:
            if warning and warning not in bits:
                bits.append(warning)
        st.session_state.notice = ("warning", " ".join(bits))


def _enrich_result(places: list[dict]) -> gemini_client.AnalysisResult:
    raw = gemini_client.enrich_places(_client(), ENRICH_MODEL, places, use_maps=USE_MAPS)
    if isinstance(raw, gemini_client.AnalysisResult):
        return raw
    return gemini_client.AnalysisResult(list(raw), [])


def _enrich_places(places: list[dict]) -> list[dict]:
    return _enrich_result(places).places


@st.fragment(run_every=0.8)
def _recognize_later() -> None:
    if st.session_state.pop("_skip_recognize_fragment", False):
        return
    if any(item.get("analysis") == "pending" for item in st.session_state.inbox):
        _recognize_pending()
        _publish_favourites()
        st.rerun()
    if any(item.get("facts") == "pending" for item in st.session_state.inbox):
        _enrich_inbox_facts()
        _publish_favourites()
        st.rerun()


def _ensure_fav_id(place: dict) -> str:
    if not place.get("fav_id"):
        place["fav_id"] = uuid.uuid4().hex[:16]
    return str(place["fav_id"])


def _favourite_record(place: dict, item: dict) -> dict:
    name = (place.get("name_zh") or place.get("name") or "").strip()
    original = (place.get("name") or name).strip()
    return {
        "id": _ensure_fav_id(place),
        "source_item": item.get("id") or "",
        "name": name or original,
        "name_zh": place.get("name_zh") or "",
        "name_original": original,
        "category": _call_trip_core("favourite_category", place, default="其他"),
        "model_category": place.get("category") or "other",
        "regions": _call_trip_core("place_regions", place, item, default=[]),
        "address": (place.get("address") or "").strip(),
        "opening_hours": (place.get("opening_hours") or "").strip(),
        "rating": place.get("rating") or "",
        "added_by": (item.get("added_by") or "").strip() or "旅人",
        "edited": False,
    }


def _publish_favourites() -> None:
    removed = set(st.session_state.get("removed_fav_ids") or [])
    previous = [dict(fav) for fav in st.session_state.get("favourites") or [] if fav.get("id") not in removed]
    by_id = {fav["id"]: fav for fav in previous}
    for item in st.session_state.inbox:
        if _collect_status(item) != COLLECT_STATUS_DONE:
            continue
        for place in item.get("places") or []:
            fid = _ensure_fav_id(place)
            if fid in removed:
                continue
            fresh = _favourite_record(place, item)
            prev = by_id.get(fid)
            if prev:
                for field in ("address", "opening_hours", "rating"):
                    if not (fresh.get(field) or "").strip() and (prev.get(field) or "").strip():
                        fresh[field] = prev[field]
                if prev.get("edited"):
                    fresh["name"] = prev.get("name") or fresh["name"]
                    fresh["regions"] = list(prev.get("regions") or fresh["regions"])
                    fresh["category"] = prev.get("category") or fresh["category"]
                    fresh["edited"] = True
            by_id[fid] = fresh
    ordered: list[dict] = []
    seen: set[str] = set()
    for fav in previous:
        fid = fav.get("id")
        if fid in by_id and fid not in seen:
            ordered.append(by_id[fid])
            seen.add(fid)
    for fid, fav in by_id.items():
        if fid not in seen:
            ordered.append(fav)
    st.session_state.favourites = ordered


def _drop_favourites_for(item_id: str) -> None:
    st.session_state.favourites = [
        fav for fav in st.session_state.get("favourites") or []
        if fav.get("source_item") != item_id
    ]


def _sync_remarks() -> None:
    for item in st.session_state.inbox:
        key = f"remark_{item['id']}"
        if key in st.session_state:
            item["remark"] = (st.session_state.get(key) or "").strip()


def _kick_analysis() -> None:
    _sync_remarks()
    if _sync_recognize() or not API_KEY or remaining_quota() <= 0:
        _recognize_pending()
    if _sync_recognize():
        _enrich_inbox_facts()
    _publish_favourites()


def _mark_for_analysis(item_ids: set[str]) -> None:
    _sync_remarks()
    started = False
    for item in st.session_state.inbox:
        if item.get("id") in item_ids and item.get("analysis") in ("idle", "failed"):
            item["analysis"] = "pending"
            item["facts"] = ""
            item["warning"] = ""
            item["places"] = []
            started = True
    if started:
        _drop_started_favourites(item_ids)
        st.rerun()


def _drop_started_favourites(item_ids: set[str]) -> None:
    for item_id in item_ids:
        _drop_favourites_for(item_id)


def _apply_manual_name(item: dict, name: str) -> None:
    place = trip_core.seed_place(name, area=item.get("city") or "", category="other")
    if not place:
        return
    _ensure_fav_id(place)
    item["places"] = [place]
    item["analysis"] = "done"
    item["facts"] = "pending"
    item["warning"] = ""
    _apply_region(item)


def _places_from_favourites(favs: list[dict]) -> list[dict]:
    places = []
    for fav in favs:
        places.append({
            "name": fav.get("name_original") or fav.get("name") or "",
            "name_zh": fav.get("name") or "",
            "category": fav.get("model_category") or "other",
            "area": "、".join(fav.get("regions") or []),
            "address": fav.get("address") or "",
            "opening_hours": fav.get("opening_hours") or "",
            "rating": fav.get("rating") or "",
            "added_by": fav.get("added_by") or "",
        })
    return places


def _favourite_destination(favs: list[dict]) -> str:
    sets = []
    for fav in favs:
        names = {
            name for name in (fav.get("regions") or [])
            if name and name != trip_core.UNSORTED_REGION
        }
        if names:
            sets.append(names)
    if not sets:
        return "日本"
    common = set.intersection(*sets)
    if len(common) == 1:
        return next(iter(common))
    return "日本"


def _allow_ai_suggestions(favs: list[dict]) -> bool:
    if len(favs) >= SPARSE_COLLECTION_MIN:
        return False
    return bool(st.session_state.get("allow_ai", True))


def _go(screen: str) -> None:
    st.session_state.screen = screen
    st.rerun()


def _render_nav() -> None:
    labels = (("A", "開場"), ("B", "收集"), ("C", "收藏"), ("D", "排程"), ("E", "結果"))
    columns = st.columns(len(labels))
    current = st.session_state.get("screen") or "A"
    for column, (screen, label) in zip(columns, labels):
        with column:
            if st.button(
                label,
                key=f"nav_{screen}",
                type="primary" if current == screen else "secondary",
                width="stretch",
            ):
                _go(screen)


def _render_invite() -> None:
    st.markdown("**邀請朋友一起收集**")
    st.caption("朋友打開後可加入同一收藏清單，一起貼上截圖並修改。")
    st.code(_share_url(), language=None)


def _thumb(item: dict) -> None:
    if item.get("image"):
        st.image(item["image"], width="stretch")
    else:
        st.caption(item.get("label") or "截圖")


def _fact_caption(place: dict) -> str:
    address = trip_core.public_fact(place.get("address"))
    hours = trip_core.public_fact(place.get("opening_hours"))
    rating = trip_core.public_fact(place.get("rating"), rating=True)
    return f"📍 {address}\n\n🕒 {hours} · ⭐ {rating}"


def _ingest_collect_inputs() -> None:
    pasted = _PASTE(key=f"inbox_paste_{st.session_state.batch}", default=None)
    decoded = trip_core.decode_clipboard_payload(pasted)
    if decoded and decoded[0] not in st.session_state.seen_paste_ids:
        paste_id, image, remark = decoded
        seen = set(st.session_state.seen_paste_ids)
        seen.add(paste_id)
        st.session_state.seen_paste_ids = seen
        st.session_state.inbox = list(st.session_state.inbox) + [_new_item(image, remark, batch=False)]

    uploaded = st.file_uploader(
        "批量上傳相簿圖片（可多張）",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.batch}",
    )
    uploaded = list(uploaded or [])
    if len(uploaded) > MAX_IMAGES_PER_UPLOAD:
        st.warning(f"一次最多收 {MAX_IMAGES_PER_UPLOAD} 張，今次只用頭 {MAX_IMAGES_PER_UPLOAD} 張。")
        uploaded = uploaded[:MAX_IMAGES_PER_UPLOAD]
    seen_uploads = set(st.session_state.seen_upload_ids)
    fresh = []
    for file in uploaded:
        key = _upload_key(file)
        if key in seen_uploads:
            continue
        seen_uploads.add(key)
        try:
            image = trip_core.prepare_image(file.getvalue())
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read image %s", getattr(file, "name", "?"))
            st.warning(f"「{getattr(file, 'name', '圖片')}」讀不到，已略過。")
            continue
        fresh.append(image)
    batch = len(fresh) > 1
    if fresh:
        st.session_state.inbox = list(st.session_state.inbox) + [
            _new_item(image, "", batch=batch) for image in fresh
        ]
    st.session_state.seen_upload_ids = seen_uploads


def _render_collect_card(item: dict) -> None:
    status = _collect_status(item)
    with st.container(border=True):
        left, right = st.columns([1, 2])
        with left:
            _thumb(item)
        with right:
            st.markdown(f"**{status}**")
            st.caption(f"加入：{item.get('added_by') or '未記名'}")
            if status == COLLECT_STATUS_IDLE and not item.get("batch"):
                remark_key = f"remark_{item['id']}"
                if remark_key not in st.session_state:
                    st.session_state[remark_key] = item.get("remark") or ""
                st.text_input("備註（可選）", key=remark_key, placeholder="例如：想吃晚餐、需要訂位…")
                if st.button("開始分析", key=f"analyze_{item['id']}", type="primary"):
                    _mark_for_analysis({item["id"]})
            elif status == COLLECT_STATUS_IDLE and item.get("batch"):
                st.caption("批量項目：不設個別備註")
            elif status == COLLECT_STATUS_RUNNING:
                st.caption("正在識別截圖…")
            elif status == COLLECT_STATUS_VERIFYING:
                for place in item.get("places") or []:
                    title = place.get("name_zh") or place.get("name")
                    st.markdown(f"**{title}**")
                    regions = "、".join(trip_core.place_regions(place, item))
                    st.caption(regions)
                st.caption("資料核實中…（地址／營業時間／評分）")
            elif status == COLLECT_STATUS_FAIL:
                st.markdown("**無法辨識這張圖片**")
                st.caption(item.get("warning") or "可能是風景或截圖過於模糊。")
                if st.button("重試", key=f"retry_{item['id']}"):
                    item["places"] = []
                    item["facts"] = ""
                    item["warning"] = ""
                    _drop_favourites_for(item["id"])
                    _mark_for_analysis({item["id"]})
                if st.button("手動輸入店名", key=f"manual_open_{item['id']}"):
                    st.session_state.manual_id = item["id"]
                    st.rerun()
                if st.session_state.get("manual_id") == item["id"]:
                    manual = st.text_input("店舖名稱", key=f"manual_name_{item['id']}", placeholder="輸入正確店名")
                    if st.button("確認店名", key=f"manual_ok_{item['id']}", type="primary"):
                        if (manual or "").strip():
                            _apply_manual_name(item, manual.strip())
                            st.session_state.manual_id = None
                            st.rerun()
                        else:
                            st.warning("請輸入店名")
            else:
                for place in item.get("places") or []:
                    title = place.get("name_zh") or place.get("name")
                    st.markdown(f"**{title}**")
                    st.caption("、".join(trip_core.place_regions(place, item)))
                    st.markdown(_fact_caption(place))
                if st.button("改名", key=f"rename_open_{item['id']}"):
                    st.session_state.renaming_id = item["id"]
                    st.rerun()
                if st.session_state.get("renaming_id") == item["id"]:
                    renamed = st.text_input("店舖名稱", key=f"rename_name_{item['id']}")
                    if st.button("儲存店名", key=f"rename_ok_{item['id']}", type="primary"):
                        cleaned = (renamed or "").strip()
                        if cleaned and item.get("places"):
                            place = item["places"][0]
                            place["name"] = cleaned
                            place["name_zh"] = cleaned
                            for fav in st.session_state.favourites:
                                if fav.get("id") == place.get("fav_id"):
                                    fav["name"] = cleaned
                                    fav["edited"] = True
                            st.session_state.renaming_id = None
                            _publish_favourites()
                            st.rerun()


def _render_collect() -> None:
    st.subheader("收集想去的地方")
    st.caption("貼上截圖，系統會幫你識別並分類。貼上後先排隊，不會自動分析。")
    st.caption("將 IG／小紅書／YouTube 截圖貼上或批量上傳。")
    _ingest_collect_inputs()
    _kick_analysis()
    pending = [item for item in st.session_state.inbox if _collect_status(item) != COLLECT_STATUS_DONE]
    done = [item for item in st.session_state.inbox if _collect_status(item) == COLLECT_STATUS_DONE]
    st.markdown(f"**待分析 {len(pending)}**")
    if not pending:
        st.caption("尚未有待分析項目。")
    for item in pending:
        _render_collect_card(item)
    idle_ids = {item["id"] for item in pending if item.get("analysis") in ("idle", "failed")}
    if idle_ids and st.button("開始分析全部待處理", key="analyze_all", type="secondary"):
        _mark_for_analysis(idle_ids)
    st.markdown("**已處理**")
    if not done:
        st.caption("分析完成後會顯示在這裡，並加入收藏。")
    for item in done:
        _render_collect_card(item)
    if st.button("查看我的收藏 →", type="primary", key="to_collection"):
        _go("C")


def _filtered_favourites() -> list[dict]:
    favs = list(st.session_state.get("favourites") or [])
    regions = []
    categories = []
    for fav in favs:
        for region in fav.get("regions") or []:
            if region not in regions:
                regions.append(region)
        category = fav.get("category") or ""
        if category and category not in categories:
            categories.append(category)
    options = ["全部", *regions, *categories]
    if st.session_state.get("fav_filter") not in options:
        st.session_state["fav_filter"] = "全部"
    picked = st.pills("篩選", options, key="fav_filter") or "全部"
    if picked == "全部":
        return favs
    return [
        fav for fav in favs
        if picked in (fav.get("regions") or []) or fav.get("category") == picked
    ]


def _render_favourite(fav: dict) -> None:
    with st.container(border=True):
        source = next((item for item in st.session_state.inbox if item.get("id") == fav.get("source_item")), None)
        left, right = st.columns([1, 3])
        with left:
            if source:
                _thumb(source)
            else:
                st.caption("📍")
        with right:
            st.markdown(f"**{fav.get('name') or '未命名'}**")
            st.markdown(trip_core.attribution_html(fav.get("added_by") or "", _viewer_name()), unsafe_allow_html=True)
            tags = " ".join(
                [f"`{fav.get('category') or '其他'}`"]
                + [f"`{region}`" for region in fav.get("regions") or []]
            )
            st.markdown(tags)
            st.markdown(
                f"📍 {trip_core.public_fact(fav.get('address'))}\n\n"
                f"🕒 {trip_core.public_fact(fav.get('opening_hours'))} · "
                f"⭐ {trip_core.public_fact(fav.get('rating'), rating=True)}"
            )
            if st.button("✎ 編輯", key=f"edit_{fav['id']}"):
                st.session_state.editing_fav = fav["id"]
                st.rerun()
            if st.session_state.get("editing_fav") == fav["id"]:
                options = list(dict.fromkeys([*(fav.get("regions") or []), *trip_core.QUICK_REGION_FIXES]))
                region_key = f"edit_regions_{fav['id']}"
                if region_key not in st.session_state:
                    st.session_state[region_key] = list(fav.get("regions") or [])
                st.pills("地區（可多選）", options, selection_mode="multi", key=region_key)
                cat_key = f"edit_cat_{fav['id']}"
                if cat_key not in st.session_state:
                    st.session_state[cat_key] = fav.get("category") or "其他"
                st.selectbox("類別", list(FAVOURITE_CATEGORIES), key=cat_key)
                save_col, remove_col = st.columns(2)
                with save_col:
                    if st.button("儲存", key=f"save_{fav['id']}", type="primary"):
                        chosen = st.session_state.get(region_key) or []
                        if isinstance(chosen, str):
                            chosen = [chosen]
                        fav["regions"] = list(chosen) or fav.get("regions") or [trip_core.UNSORTED_REGION]
                        fav["category"] = st.session_state.get(cat_key) or fav.get("category")
                        fav["edited"] = True
                        st.session_state.editing_fav = None
                        st.rerun()
                with remove_col:
                    if st.button("移除", key=f"remove_{fav['id']}"):
                        removed = list(st.session_state.get("removed_fav_ids") or [])
                        removed.append(fav["id"])
                        st.session_state.removed_fav_ids = removed
                        st.session_state.favourites = [
                            row for row in st.session_state.favourites if row.get("id") != fav["id"]
                        ]
                        st.session_state.editing_fav = None
                        st.rerun()


def _render_collection() -> None:
    st.subheader("我的收藏")
    st.caption("這裡只有你和朋友收藏的地點，排行程時會用這些。")
    _render_invite()
    favs = list(st.session_state.get("favourites") or [])
    if not favs:
        st.info("尚未有收藏。請先在「收集」分析截圖，或手動輸入店名。")
    else:
        st.caption("右側 ✎ 可改地區、類別或移除。每一張卡都會標明是你還是朋友加入。")
        visible = _filtered_favourites()
        if not visible:
            st.caption("此篩選暫時沒有地點。")
        for fav in visible:
            _render_favourite(fav)
    if st.button("一鍵排行程 →", type="primary", key="to_plan"):
        _go("D")
    st.caption(PAYWALL_NOTE)


def _render_plan() -> None:
    st.subheader("一鍵排行程")
    favs = list(st.session_state.get("favourites") or [])
    st.markdown("**出發日與日數**")
    day_col, count_col = st.columns(2)
    with day_col:
        st.date_input("出發日", value=None, key="start_date")
    with count_col:
        st.number_input("日數", min_value=1, max_value=14, step=1, key="day_count")
    raw_prefs = list(st.session_state.get("pref_pills") or [])
    clean_prefs = [item for item in raw_prefs if item in PLAN_PREF_OPTIONS]
    if clean_prefs != raw_prefs:
        st.session_state.pref_pills = clean_prefs or ["美食", "景點"]
    st.pills(
        "你想去哪些類型的地方？（可多選）",
        list(PLAN_PREF_OPTIONS),
        selection_mode="multi",
        key="pref_pills",
    )
    style_labels = [name for name, _blurb in TRIP_STYLES]
    if st.session_state.get("trip_style") not in style_labels:
        st.session_state.trip_style = DEFAULT_TRIP_STYLE
    st.radio(
        "行程風格（單選）",
        style_labels,
        captions=[blurb for _name, blurb in TRIP_STYLES],
        key="trip_style",
    )
    allow_ai = _allow_ai_suggestions(favs)
    if len(favs) < SPARSE_COLLECTION_MIN:
        st.markdown("**目前收藏的地方較少，是否需要 AI 協助建議其他地點？**")
        yes_col, no_col = st.columns(2)
        with yes_col:
            if st.button("需要", key="ai_yes", type="primary" if st.session_state.get("allow_ai", True) else "secondary"):
                st.session_state.allow_ai = True
                st.rerun()
        with no_col:
            if st.button(
                "不用，只用我的收藏",
                key="ai_no",
                type="primary" if not st.session_state.get("allow_ai", True) else "secondary",
            ):
                st.session_state.allow_ai = False
                st.rerun()
        allow_ai = bool(st.session_state.get("allow_ai", True))
    has_date = bool(st.session_state.get("start_date"))
    can_plan = bool(API_KEY) and remaining_quota() > 0 and has_date and (bool(favs) or allow_ai)
    if not has_date:
        st.caption("請先選擇出發日與日數。")
    elif not favs and not allow_ai:
        st.caption("收藏是空的。可以允許 AI 建議，或先回去收集。")
    elif not API_KEY:
        st.caption("AI 未設定，未可以排行程。")
    elif remaining_quota() <= 0:
        st.caption("今次可用次數已用完。")
    submitted = st.button("開始排行程", type="primary", disabled=not can_plan, key="plan_go")
    if not submitted:
        return
    style = st.session_state.get("trip_style") or DEFAULT_TRIP_STYLE
    request = trip_core.compose_trip_request(
        trip_core.DESTINATION_DEFAULT,
        days=st.session_state.get("day_count") or 3,
        preferences=list(st.session_state.get("pref_pills") or []),
        party=trip_core.PARTY_DEFAULT,
        start_date=_date_text(st.session_state.get("start_date")),
    )
    destination = _favourite_destination(favs)
    if destination == "日本":
        destination = request["destination"]
    notes = "；".join(
        bit for bit in (
            request.get("notes") or "",
            trip_core.style_planning_note(style, allow_ai=allow_ai),
        ) if bit
    )
    places = _places_from_favourites(favs)
    plan_error = ""
    with st.status("正在安排行程…", expanded=True) as status:
        try:
            missing = trip_core.places_needing_facts(places)
            if missing:
                status.write("核對未齊的地址、營業時間、評分。")
                enriched = _enrich_places(missing)
                for extra in enriched:
                    fav = trip_core.match_favourite(extra.get("name_zh") or extra.get("name") or "", favs)
                    if not fav:
                        continue
                    for field in ("address", "opening_hours", "rating"):
                        if extra.get(field) and not (fav.get(field) or "").strip():
                            fav[field] = extra[field]
                places = _places_from_favourites(favs)
            itinerary = gemini_client.generate_itinerary(
                _client(),
                ITINERARY_MODEL,
                places,
                request["days"],
                destination,
                notes,
                preferences=request["preferences"],
                party=request["party"],
            )
        except gemini_client.GeminiError as err:
            plan_error = str(err)
            status.update(label=f"排行程失敗：{plan_error}", state="error")
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error during itinerary generation")
            plan_error = "排行程途中出了意外，請再試一次。"
            status.update(label=f"排行程失敗：{plan_error}", state="error")
        else:
            if (itinerary.get("title") or "") in ("", "我嘅懶人行程"):
                itinerary["title"] = trip_core.fallback_trip_title(destination, request["days"])
            st.session_state.usage += 1
            st.session_state.itinerary = itinerary
            st.session_state.itinerary_places = places
            st.session_state.planned_style = style
            st.session_state.planned_allow_ai = allow_ai
            st.session_state.screen = "E"
            st.session_state.notice = None
            status.update(label="行程排好了", state="complete")
    if plan_error:
        st.session_state.notice = ("error", plan_error)
        st.error(plan_error)
    elif st.session_state.get("screen") == "E":
        st.rerun()


def _render_results() -> None:
    itinerary = st.session_state.get("itinerary")
    style = st.session_state.get("planned_style") or st.session_state.get("trip_style") or DEFAULT_TRIP_STYLE
    if not itinerary:
        st.subheader("行程結果")
        st.caption("尚未安排行程。")
        if st.button("前往排行程", key="back_to_plan"):
            _go("D")
        return
    shown = trip_core.present_itinerary(
        itinerary,
        st.session_state.get("favourites") or [],
        allow_ai=bool(st.session_state.get("planned_allow_ai", True)),
    )
    st.subheader(shown.get("title") or "行程結果")
    st.markdown(f"**已按「{style}」安排**")
    days = shown.get("days") or []
    labels = [f"Day {day.get('day')}" for day in days]
    if labels:
        if st.session_state.get("day_tab") not in labels:
            st.session_state["day_tab"] = labels[0]
        picked = st.pills("行程日", labels, key="day_tab") or labels[0]
        day = next((item for item in days if f"Day {item.get('day')}" == picked), days[0])
        visible = [stop for stop in day.get("items") or [] if not stop.get("hidden")]
        own = [stop for stop in visible if not stop.get("ai")]
        if not own and any(stop.get("hidden") for stop in day.get("items") or []):
            st.caption("這一天主要依賴 AI 建議；你選擇了只用收藏，因此內容較少。")
            if st.button("改為加入 AI 建議", key="enable_ai_from_results"):
                st.session_state.allow_ai = True
                st.session_state.planned_allow_ai = True
                st.rerun()
        for stop in visible:
            if stop.get("transit"):
                st.caption(f"🚇 {stop['transit']}")
            with st.container(border=True):
                if stop.get("time"):
                    st.markdown(f"**{stop['time']}**")
                st.markdown(f"**{stop.get('place_name') or ''}**")
                if stop.get("ai"):
                    st.markdown(
                        '<span class="who-ai" style="color:#8A6A20;font-weight:600;">AI 建議</span>',
                        unsafe_allow_html=True,
                    )
                else:
                    who = trip_core.attribution_html(stop.get("added_by") or "", _viewer_name())
                    st.markdown(
                        f'<span style="color:#E85D52;font-weight:600;">來自收藏</span> {who}',
                        unsafe_allow_html=True,
                    )
                if stop.get("activity"):
                    st.caption(stop["activity"])
                st.caption(
                    f"🕒 {stop.get('opening_hours') or UNVERIFIED} · "
                    f"⭐ {stop.get('rating') or UNVERIFIED}"
                )
    st.markdown("**分享行程**")
    st.code(_share_url(), language=None)
    st.caption("連結可供朋友瀏覽；編輯權限與收藏收件匣分開。")


def _render_opening() -> None:
    st.title("懶人出行 ✈️")
    st.markdown(HERO_LINE)
    steps = "\n".join(f"{index}. {label}" for index, label in enumerate(OPENING_STEPS, start=1))
    st.markdown(steps)
    st.text_input("你的顯示名", key="display_name")
    members = "、".join(st.session_state.get("members") or []) or _viewer_name()
    st.caption(f"目前一起修改：{members}")
    if st.button("開始收集想去的地方", type="primary", key="start_collect"):
        _go("B")
    if st.button("先複製邀請連結", key="open_invite"):
        st.session_state.show_invite = not st.session_state.get("show_invite")
        st.rerun()
    if st.session_state.get("show_invite"):
        _render_invite()


init_state()
_ensure_share()
_load_shared_trip()
_publish_favourites()

_render_nav()
_render_notice()
screen = st.session_state.get("screen") or "A"
if not API_KEY:
    st.error("AI 服務未設定好，暫時認不到地方，也排不了行程。（管理員請在 Streamlit Secrets 設定 GEMINI_API_KEY）")
if screen == "A":
    _render_opening()
elif screen == "B":
    _render_collect()
elif screen == "C":
    _render_collection()
elif screen == "D":
    _render_plan()
else:
    _render_results()

if not _sync_recognize():
    if any(
        item.get("analysis") == "pending" or item.get("facts") == "pending"
        for item in st.session_state.inbox
    ):
        st.session_state._skip_recognize_fragment = True
    _recognize_later()

st.divider()
st.caption(
    f"原型版本 | 今次還可使用 {remaining_quota()}/{MAX_ANALYSES} 次 | build: {_build_sha()}"
)
_persist_shared_trip()

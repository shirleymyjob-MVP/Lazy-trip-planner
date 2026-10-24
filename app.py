"""懶人行程規劃 — Streamlit entry point.

MVP：貼上截圖放入行程匣（可寫備註，未填行程都收）→ 稍後先選目的地／日數／偏好
→ 揀行程匣項目 → 按營業時間、距離同偏好排行程。
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import gemini_client
import trip_core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lazy_trip_planner")

st.set_page_config(page_title="懶人行程規劃 (原型)", page_icon="✈️", layout="centered")

MAX_IMAGES_PER_UPLOAD = 10
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
MODEL = get_secret("GEMINI_MODEL", gemini_client.DEFAULT_MODEL) or gemini_client.DEFAULT_MODEL
# 每個 browser session 最多幾多次成功嘅 AI 動作（分析入匣、開始排行程）。
# 失敗、SDK 重試、同一輪入面幾張圖唔另計。
MAX_ANALYSES = get_int_secret("MAX_ANALYSES_PER_SESSION", 10)
TIMEOUT_MS = _clamp_int(
    get_int_secret("GEMINI_TIMEOUT_MS", gemini_client.DEFAULT_TIMEOUT_MS),
    10_000,
    180_000,
)
RETRY_ATTEMPTS = _clamp_int(
    get_int_secret("GEMINI_RETRY_ATTEMPTS", gemini_client.DEFAULT_RETRY_ATTEMPTS),
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


def _new_item(image: bytes, remark: str) -> dict:
    number = st.session_state.next_image_num
    st.session_state.next_image_num = number + 1
    return {
        "id": uuid.uuid4().hex,
        "label": f"截圖 {number}",
        "image": image,
        "remark": (remark or "").strip(),
        "status": "未排程",
        "analysis": "pending",
        "places": [],
        "city": "",
        "region_confidence": trip_core.REGION_LOW,
        "warning": "",
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
    city, confidence = trip_core.classify_region(item.get("places") or [], item.get("remark") or "")
    item["city"] = city
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


def _analyze_pending():
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
    with st.status(f"AI 睇緊行程匣入面 {len(pending)} 張新截圖…", expanded=True) as status:
        status.write("備註會交俾分析。多過 3 張會分批，失敗會再試。")
        for remark, items in groups:
            images = [item["image"] for item in items]
            try:
                raw = gemini_client.analyze_screenshots(
                    _client(),
                    MODEL,
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


init_state()

# ---------------------------------------------------------------- header
st.title("✈️ 懶人行程規劃（原型版）")
st.caption("先將截圖貼進行程匣，日期同偏好可以稍後先揀。唔會擷取 Instagram／YouTube／小紅書連結。")
st.caption(trip_core.HAPPY_PATH_CAPTION)

with st.expander("點樣用？", expanded=not st.session_state.inbox):
    st.markdown(
        f"""
1. **行程匣**：喺 IG 見到地方就截圖，iPhone 撳「複製並刪除」，返嚟呢度貼上。桌面用 Cmd/Ctrl+V，或者撳「從剪貼簿貼上」。備註可寫「只要圖中 1、4」。貼上唔使揀地區。
2. 已儲存嘅圖片先用下面嘅上傳（次要）。分析完會按畫面同文字自動標地區，例如福岡、博多、今泉。睇唔準就係未分類，可以之後一撳改。
3. **開始排行程**係之後先做：日數、偏好、同行有預設。如果揀咗嘅行程匣都係同一個地區，會跟嗰個地區，唔使再揀城市。想改先用下拉。會用營業時間、距離同上面嘅選擇。

_原型版：每個 session 最多 {MAX_ANALYSES} 次成功嘅 AI 動作（失敗同自動重試唔計）。截圖只會用嚟即時分析。_
"""
    )

if not API_KEY:
    st.error("AI 服務未設定好，暫時認唔到地方同排唔到行程。（管理員請喺 Streamlit Secrets 設定 GEMINI_API_KEY）")

# ---------------------------------------------------------------- inbox first
st.subheader("1. 行程匣")
st.caption("貼上同上傳都唔使揀地區。分析完會按畫面同文字自動標，例如福岡、博多、今泉。睇唔準就係未分類，可以之後一撳改。")
st.caption("未填目的地、日期、偏好都可以收截圖。貼上係主要入口；上傳只係已經儲存咗檔案先用。")

pasted = _PASTE(key=f"inbox_paste_{st.session_state.batch}", default=None)
decoded_paste = trip_core.decode_clipboard_payload(pasted)
if decoded_paste and decoded_paste[0] not in st.session_state.seen_paste_ids:
    paste_id, image, remark = decoded_paste
    seen = set(st.session_state.seen_paste_ids)
    seen.add(paste_id)
    st.session_state.seen_paste_ids = seen
    st.session_state.inbox = list(st.session_state.inbox) + [_new_item(image, remark)]

st.text_input(
    "上傳備註（可留空，只適用於下面已儲存嘅圖片）",
    key="upload_remark",
    placeholder="例如：只要圖中 1、4",
)
uploaded = st.file_uploader(
    "或者上傳已儲存嘅圖片（次要。貼上唔使先儲存相簿）",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.batch}",
)
uploaded = list(uploaded or [])
if len(uploaded) > MAX_IMAGES_PER_UPLOAD:
    st.warning(f"一次最多收 {MAX_IMAGES_PER_UPLOAD} 張，今次只用頭 {MAX_IMAGES_PER_UPLOAD} 張。")
    uploaded = uploaded[:MAX_IMAGES_PER_UPLOAD]
upload_remark = st.session_state.get("upload_remark") or ""
seen_uploads = set(st.session_state.seen_upload_ids)
new_items: list[dict] = []
for file in uploaded:
    key = _upload_key(file)
    if key in seen_uploads:
        continue
    seen_uploads.add(key)
    try:
        image = trip_core.prepare_image(file.getvalue())
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read image %s", getattr(file, "name", "?"))
        st.warning(f"「{getattr(file, 'name', '圖片')}」讀唔到，可能唔係圖片檔，已略過。")
        continue
    new_items.append(_new_item(image, upload_remark))
if new_items:
    st.session_state.inbox = list(st.session_state.inbox) + new_items
st.session_state.seen_upload_ids = seen_uploads

if st.session_state.inbox and st.button("清空行程匣", key=f"clear_{st.session_state.batch}"):
    clear_inbox()
    st.rerun()

_analyze_pending()
failed = [item for item in st.session_state.inbox if item.get("analysis") == "failed"]
if failed and API_KEY and remaining_quota() > 0:
    if st.button("再試一次分析", type="primary", key=f"retry_{st.session_state.batch}"):
        for item in failed:
            item["analysis"] = "pending"
            item["warning"] = ""
        st.session_state.notice = None
        _analyze_pending()
_render_notice()

def _remember_region_fix(item_id: str) -> None:
    choice = st.session_state.get(f"region_fix_{item_id}")
    if not choice:
        return
    for item in st.session_state.inbox:
        if item.get("id") == item_id:
            item["city"] = choice
            item["region_confidence"] = trip_core.REGION_MANUAL
            return


def _render_inbox_card(item: dict) -> None:
    with st.container(border=True):
        pick_key = f"pick_{item['id']}"
        if pick_key not in st.session_state:
            st.session_state[pick_key] = True
        st.checkbox(f"排行程時包括 {item['label']}", key=pick_key)
        left, right = st.columns([1, 2])
        with left:
            st.image(item["image"], caption=item["label"], width="stretch")
        with right:
            region = trip_core.inbox_region_name(item)
            st.markdown(f"**{item['label']}** · 狀態：{item['status']}")
            st.markdown(f"地區：{region}")
            st.markdown(f"備註：{item['remark'] or '（冇）'}")
            fix_label = "可以一撳改（唔改都得）" if region == trip_core.UNSORTED_REGION else "唔岩可以一撳改"
            st.pills(
                fix_label,
                list(trip_core.QUICK_REGION_FIXES),
                key=f"region_fix_{item['id']}",
                on_change=_remember_region_fix,
                args=(item["id"],),
                wrap=True,
            )
            if item.get("analysis") == "pending":
                st.caption("分析中")
            elif item.get("analysis") == "failed":
                st.caption(item.get("warning") or "分析失敗")
            elif not item.get("places"):
                st.caption(item.get("warning") or "未認到地方")
            else:
                if item.get("warning"):
                    st.caption(item["warning"])
                for place in item["places"]:
                    title = place.get("name_zh") or place.get("name")
                    if place.get("caption_index"):
                        title = f"{trip_core.caption_mark(place['caption_index'])} {title}"
                    st.markdown(f"**{title}**")
                    if place.get("name_zh") and place.get("name") and place["name"] != place["name_zh"]:
                        st.caption(place["name"])
                    st.markdown("\n\n".join(trip_core.place_fact_lines(place)))


if not st.session_state.inbox:
    st.info("行程匣未有截圖。貼上一張就會出現喺呢度，狀態係未排程。")
else:
    st.caption("每張卡有認到嘅地方、地區、備註、狀態。未分類可以之後一撳改，唔改都得。未排程＝未放入行程。")
    groups = trip_core.group_inbox(st.session_state.inbox)
    region_names = [name for name, _grouped in groups]
    filter_options = ["全部", *region_names]
    if st.session_state.get("inbox_region_filter") not in filter_options:
        st.session_state["inbox_region_filter"] = "全部"
    counts = {name: len(grouped) for name, grouped in groups}
    counts["全部"] = len(st.session_state.inbox)
    picked = st.pills(
        "按地區睇",
        filter_options,
        key="inbox_region_filter",
        format_func=lambda name: f"{name}（{counts.get(name, 0)}）",
        wrap=True,
    )
    visible = groups if not picked or picked == "全部" else [
        (name, grouped) for name, grouped in groups if name == picked
    ]
    for name, grouped in visible:
        st.markdown(f"**{name}** · {len(grouped)} 張")
        for item in grouped:
            _render_inbox_card(item)

# ---------------------------------------------------------------- plan later
st.subheader("2. 開始排行程")
st.caption("呢段係稍後先做。預設可以唔改。只會用上面剔咗嘅行程匣項目。")
st.caption(trip_core.ROUTE_NOTE)
dest_col, day_col = st.columns(2)
with dest_col:
    st.selectbox("目的地", trip_core.destination_labels(), key="destination_choice")
with day_col:
    st.selectbox("玩幾多日？", list(trip_core.DAY_OPTIONS), key="days_choice")
if "start_date" not in st.session_state:
    st.date_input("出發日期（可唔填）", value=None, key="start_date")
else:
    st.date_input("出發日期（可唔填）", key="start_date")
if st.session_state.get("destination_choice") == trip_core.DESTINATION_OTHER:
    st.text_input("其他目的地", key="destination_other", placeholder="例如：韓國首爾")
st.pills(
    "偏好（可多選）",
    list(trip_core.PREFERENCE_OPTIONS),
    selection_mode="multi",
    key="pref_pills",
)
if trip_core.OTHER_CHOICE in (st.session_state.get("pref_pills") or []):
    st.text_input("其他偏好", key="pref_other", placeholder="例如：唔想行太多")
st.pills("同行", list(trip_core.PARTY_OPTIONS), key="party_pills")
if st.session_state.get("party_pills") == trip_core.OTHER_CHOICE:
    st.text_input("其他同行", key="party_other", placeholder="例如：兩代同遊")

selected_items = _selected_items()
selected_places = trip_core.dedupe_places(
    place for item in selected_items for place in (item.get("places") or [])
)
plan_request = _trip_request()
plan_destination = _plan_destination(plan_request, selected_items)
can_plan = bool(API_KEY) and bool(selected_places) and remaining_quota() > 0
if not selected_places:
    st.caption("行程匣未有可排嘅地方，所以下面個掣未撳得。")
elif not API_KEY:
    st.caption("AI 未設定，未可以排行程。")
elif remaining_quota() <= 0:
    st.caption("今個 session 次數用完，未可以排行程。")
elif plan_request["destination"] == "日本" and plan_destination != "日本":
    st.caption(f"今次會跟行程匣地區「{plan_destination}」排。上面目的地下拉可以改。")
submitted = st.button(
    "✨ 開始排行程",
    type="primary",
    disabled=not can_plan,
    key=f"plan_{st.session_state.batch}",
)
if submitted:
    request = plan_request
    if not selected_places:
        st.warning("最少要有一個認到嘅地方先排到行程。")
    elif remaining_quota() == 0:
        st.warning("今個 session 嘅免費次數用完喇 🙏 之後會推出登入／付費版本。")
    else:
        with st.status("AI 排緊行程…", expanded=True) as status:
            status.write(f"會用目的地{plan_destination}、日數、偏好、同行，再按營業時間同距離排。")
            try:
                itinerary = gemini_client.generate_itinerary(
                    _client(),
                    MODEL,
                    selected_places,
                    request["days"],
                    plan_destination,
                    _plan_notes(request, selected_items),
                    preferences=request["preferences"],
                    party=request["party"],
                )
            except gemini_client.GeminiError as err:
                status.update(label="排行程失敗", state="error")
                st.session_state.notice = ("error", str(err))
                st.error(str(err))
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected error during itinerary generation")
                st.session_state.notice = ("error", "排行程途中出咗意外，請再試一次。")
                st.error("排行程途中出咗意外，請再試一次。")
            else:
                st.session_state.usage += 1
                st.session_state.itinerary = itinerary
                st.session_state.itinerary_places = selected_places
                for item in selected_items:
                    if item.get("places"):
                        item["status"] = "已排程"
                st.session_state.notice = None
                status.update(label="行程排好喇", state="complete")

# ---------------------------------------------------------------- itinerary view
itinerary = st.session_state.itinerary
if itinerary:
    fact_source = st.session_state.itinerary_places
    shown = trip_core.attach_place_facts(itinerary, fact_source)
    st.divider()
    st.subheader(f"🧳 {shown['title']}")
    st.caption("對得返行程匣地點嘅，下面會直接寫地址、營業時間、評分。")
    for day in shown["days"]:
        with st.container(border=True):
            title = f"**Day {day['day']}**"
            if day["area"]:
                title += f"｜{day['area']}"
            st.markdown(title)
            if day["theme"]:
                st.caption(day["theme"])
            for stop in day["items"]:
                icon = "🍽️ " if stop["is_meal"] else ""
                time = f"`{stop['time']}` " if stop["time"] else ""
                map_url = trip_core.maps_search_url({"name": stop["maps_query"]}) if stop["maps_query"] else None
                if map_url and stop["place_name"]:
                    name = f"[{trip_core.escape_markdown_label(stop['place_name'])}]({map_url})"
                else:
                    name = stop["place_name"]
                activity = f" — {stop['activity']}" if stop["activity"] else ""
                st.markdown(f"- {time}{icon}{name}{activity}")
                bits = []
                if stop.get("address"):
                    bits.append(f"🏠 {stop['address']}")
                elif stop.get("area"):
                    bits.append(f"📍 {stop['area']}")
                if stop.get("opening_hours"):
                    bits.append(f"🕒 {stop['opening_hours']}")
                if stop.get("rating"):
                    bits.append(f"⭐ {stop['rating']}")
                if bits:
                    st.caption(" · ".join(bits))
            route = trip_core.day_directions_url(day)
            if route:
                st.link_button("🗺️ 當日路線（Google Maps）", route)
    if shown["tips"]:
        st.markdown("**小貼士**")
        for tip in shown["tips"]:
            st.markdown(f"- {tip}")
    st.download_button(
        "⬇️ 下載行程（Markdown）",
        data=trip_core.itinerary_to_markdown(shown),
        file_name="itinerary.md",
        mime="text/markdown",
        width="stretch",
    )
    st.caption("地址、營業時間、評分來自截圖同 AI 有把握先填嘅資料，基本核對唔使離開呢版。Maps 只係想導航或睇街景先開。")

st.divider()
st.caption(f"原型版本 | 行程匣 MVP | 今個 session 仲有 {remaining_quota()}/{MAX_ANALYSES} 次")

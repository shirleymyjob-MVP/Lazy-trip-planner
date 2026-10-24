"""懶人行程規劃 — Streamlit entry point.

流程：上傳多張旅遊片截圖 → Gemini 識別地點（structured JSON）→ 地點卡片 + Google Maps
→ 揀想去嘅地方 → 生成 N 日行程 → 下載 Markdown。
"""
from __future__ import annotations

import hashlib
import logging

import streamlit as st

import gemini_client
import trip_core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lazy_trip_planner")

st.set_page_config(page_title="懶人行程規劃 (原型)", page_icon="✈️", layout="centered")

MAX_IMAGES_PER_UPLOAD = 10


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


API_KEY = get_secret("GEMINI_API_KEY")
MODEL = get_secret("GEMINI_MODEL", gemini_client.DEFAULT_MODEL) or gemini_client.DEFAULT_MODEL
# 每個 browser session 最多幾多次 AI 呼叫（分析截圖 + 生成行程都計一次）。
# 注意：呢個只係簡單嘅 per-session 限制，重新整理頁面就會重置；
# 真正嘅 per-user 限額 / 收費需要之後加登入（login）同後端記錄。
MAX_ANALYSES = get_int_secret("MAX_ANALYSES_PER_SESSION", 10)


@st.cache_resource(show_spinner=False)
def get_client(api_key: str):
    return gemini_client.make_client(api_key)


# ---------------------------------------------------------------- state
def init_state():
    defaults = {
        "places": [],          # 去重後嘅地點
        "itinerary": None,
        "usage": 0,            # 今個 session 用咗幾多次 AI
        "batch": 0,            # 每次清除 +1，用嚟重設 checkbox / uploader key
        # file id -> 畫面編號。分析完再 rerun 都唔好改編號，先至同卡片「圖 #n」對得上。
        "image_labels": {},
        "next_image_num": 1,
        "prepared_images": {},  # file id -> JPEG bytes，失敗就係 None
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def remaining_quota() -> int:
    return max(0, MAX_ANALYSES - st.session_state.usage)


def reset_all():
    for k in ("places", "itinerary", "image_labels", "next_image_num", "prepared_images"):
        st.session_state.pop(k, None)
    st.session_state.batch = st.session_state.get("batch", 0) + 1


def _upload_key(uploaded_file) -> str:
    file_id = getattr(uploaded_file, "file_id", None)
    if file_id:
        return str(file_id)
    return hashlib.sha256(uploaded_file.getvalue()).hexdigest()


init_state()

# ---------------------------------------------------------------- header
st.title("✈️ 懶人行程規劃（原型版）")
st.caption("睇 IG／YouTube 旅遊片截咗一堆圖？丟晒上嚟，AI 幫你認店名、整地圖連結、排埋行程。")

with st.expander("點樣用？", expanded=not st.session_state.places):
    st.markdown(
        f"""
1. 上傳最多 **{MAX_IMAGES_PER_UPLOAD} 張**截圖（有餐廳／景點／店名嘅畫面）
2. 撳 **分析截圖**，AI 會列出認到嘅地方，每個都有 Google Maps 連結
3. 剔走唔想去嘅地方，揀日數，撳 **生成行程**
4. 下載行程（Markdown），貼落 Notes／WhatsApp 都得

_原型版：每個 session 最多用 {MAX_ANALYSES} 次 AI。截圖只會用嚟即時分析，你手機其他資料唔會被接觸。_
"""
    )

if not API_KEY:
    st.error("AI 服務未設定好，暫時用唔到分析功能。（管理員請喺 Streamlit Secrets 設定 GEMINI_API_KEY）")

# ---------------------------------------------------------------- upload
uploaded = st.file_uploader(
    "上傳截圖（可以一次揀多張）",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.batch}",
)
uploaded = uploaded or []

if len(uploaded) > MAX_IMAGES_PER_UPLOAD:
    st.warning(f"一次最多分析 {MAX_IMAGES_PER_UPLOAD} 張，今次只會用頭 {MAX_IMAGES_PER_UPLOAD} 張。")
    uploaded = uploaded[:MAX_IMAGES_PER_UPLOAD]

# (file key, JPEG bytes) for files that decoded. Numbers are assigned afterwards
# so a bad file does not consume a screenshot number.
decoded: list[tuple[str, bytes]] = []
kept_images: dict = {}
if uploaded:
    cache: dict = dict(st.session_state.prepared_images)
    for f in uploaded:
        key = _upload_key(f)
        if key not in cache:
            try:
                cache[key] = trip_core.prepare_image(f.getvalue())
            except Exception:  # noqa: BLE001
                logger.exception("Failed to read image %s", getattr(f, "name", "?"))
                cache[key] = None
        kept_images[key] = cache[key]
        if cache[key] is None:
            st.warning(f"「{f.name}」讀唔到，可能唔係圖片檔，已略過。")
            continue
        decoded.append((key, cache[key]))
    labels = dict(st.session_state.image_labels)
    st.session_state.next_image_num = trip_core.assign_display_numbers(
        [key for key, _ in decoded], labels, st.session_state.next_image_num
    )
    st.session_state.image_labels = labels
    if decoded:
        cols = st.columns(min(len(decoded), 5))
        for i, (key, data) in enumerate(decoded):
            with cols[i % len(cols)]:
                st.image(data, caption=f"#{labels[key]}", width="stretch")
st.session_state.prepared_images = kept_images

prepared = [(key, st.session_state.image_labels[key], data) for key, data in decoded]
can_analyze = bool(prepared) and bool(API_KEY) and remaining_quota() > 0
if uploaded and remaining_quota() == 0:
    st.warning("今個 session 嘅免費次數用完喇 🙏 之後會推出登入／付費版本。")

if st.button("🔍 分析截圖", type="primary", disabled=not can_analyze):
    images = [data for _key, _num, data in prepared]
    display_numbers = [num for _key, num, _data in prepared]
    if images:
        with st.spinner(f"AI 睇緊 {len(images)} 張截圖..."):
            try:
                st.session_state.usage += 1
                new_places = gemini_client.analyze_screenshots(get_client(API_KEY), MODEL, images)
            except gemini_client.GeminiError as err:
                st.error(str(err))
                new_places = None
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected error during analysis")
                st.error("分析途中出咗意外，請再試一次。")
                new_places = None
        if new_places is not None:
            trip_core.map_batch_source_images(new_places, display_numbers)
            before = len(st.session_state.places)
            st.session_state.places = trip_core.dedupe_places(st.session_state.places + new_places)
            added = len(st.session_state.places) - before
            st.session_state.itinerary = None
            if new_places:
                st.success(f"認到 {len(new_places)} 個地方（新增 {added} 個）。")
            else:
                st.info("呢批截圖認唔到具體地方，試下上傳有店名／招牌／地址嘅畫面。")

# ---------------------------------------------------------------- places
places = st.session_state.places
if places:
    st.divider()
    head_l, head_r = st.columns([3, 1])
    head_l.subheader(f"📍 認到嘅地方（{len(places)}）")
    if head_r.button("清除全部", type="tertiary"):
        reset_all()
        st.rerun()
    st.caption("剔走唔想去嘅地方，之後排行程只會用有剔嘅。")

    for i, p in enumerate(places):
        with st.container(border=True):
            c1, c2 = st.columns([5, 2])
            with c1:
                st.checkbox(
                    f"**{p['name_zh'] or p['name']}**",
                    value=True,
                    key=f"sel_{st.session_state.batch}_{i}_{p['name']}",
                )
                if p["name_zh"] and p["name"] != p["name_zh"]:
                    st.caption(p["name"])
                meta = [trip_core.CATEGORY_LABELS.get(p["category"], p["category"])]
                if p["area"]:
                    meta.append(p["area"])
                meta.append(f"信心：{p['confidence']}")
                if p["source_images"]:
                    meta.append("圖 " + ", ".join(f"#{n}" for n in p["source_images"]))
                st.markdown(" · ".join(meta))
                if p["address"]:
                    st.markdown(f"🏠 {p['address']}")
                if p["opening_hours"]:
                    st.markdown(f"🕒 {p['opening_hours']}")
                if p["notes"]:
                    st.caption(p["notes"])
            with c2:
                st.link_button("Google Maps", trip_core.maps_search_url(p), width="stretch")

    selected = [
        p for i, p in enumerate(places)
        if st.session_state.get(f"sel_{st.session_state.batch}_{i}_{p['name']}", True)
    ]

    # ------------------------------------------------------------ itinerary form
    st.divider()
    st.subheader("🗓️ 幫我排行程")
    with st.form("itinerary_form"):
        days = st.number_input("玩幾多日？", min_value=1, max_value=14, value=3, step=1)
        destination = st.text_input("目的地（可留空，AI 會自己估）", placeholder="例如：東京、首爾、台北")
        notes = st.text_area(
            "其他要求（可留空）",
            placeholder="例如：第一日下晝先到、唔想太趕、有小朋友、想食多啲甜品",
        )
        submitted = st.form_submit_button(
            f"✨ 用 {len(selected)} 個地方生成行程",
            type="primary",
            disabled=not API_KEY,
        )
    if submitted:
        if not selected:
            st.warning("最少要剔一個地方先排到行程。")
        elif remaining_quota() == 0:
            st.warning("今個 session 嘅免費次數用完喇 🙏 之後會推出登入／付費版本。")
        else:
            with st.spinner("AI 排緊行程..."):
                try:
                    st.session_state.usage += 1
                    st.session_state.itinerary = gemini_client.generate_itinerary(
                        get_client(API_KEY), MODEL, selected, int(days), destination, notes
                    )
                except gemini_client.GeminiError as err:
                    st.error(str(err))
                except Exception:  # noqa: BLE001
                    logger.exception("Unexpected error during itinerary generation")
                    st.error("排行程途中出咗意外，請再試一次。")

# ---------------------------------------------------------------- itinerary view
itinerary = st.session_state.itinerary
if itinerary:
    st.divider()
    st.subheader(f"🧳 {itinerary['title']}")
    for day in itinerary["days"]:
        with st.container(border=True):
            title = f"**Day {day['day']}**"
            if day["area"]:
                title += f"｜{day['area']}"
            st.markdown(title)
            if day["theme"]:
                st.caption(day["theme"])
            for it in day["items"]:
                icon = "🍽️ " if it["is_meal"] else ""
                time = f"`{it['time']}` " if it["time"] else ""
                map_url = trip_core.maps_search_url({"name": it["maps_query"]}) if it["maps_query"] else None
                if map_url and it["place_name"]:
                    # 店名若含 ] 會截斷 Markdown link，地圖連結會指錯地方。
                    name = f"[{trip_core.escape_markdown_label(it['place_name'])}]({map_url})"
                else:
                    name = it["place_name"]
                activity = f" — {it['activity']}" if it["activity"] else ""
                st.markdown(f"- {time}{icon}{name}{activity}")
            route = trip_core.day_directions_url(day)
            if route:
                st.link_button("🗺️ 當日路線（Google Maps）", route)
    if itinerary["tips"]:
        st.markdown("**小貼士**")
        for t in itinerary["tips"]:
            st.markdown(f"- {t}")
    st.download_button(
        "⬇️ 下載行程（Markdown）",
        data=trip_core.itinerary_to_markdown(itinerary),
        file_name="itinerary.md",
        mime="text/markdown",
        width="stretch",
    )
    st.caption("⚠️ 營業時間、地址由 AI 估計，出發前請再喺 Google Maps 確認。")

st.divider()
st.caption(f"原型版本 | Gemini 睇圖分析 | 今個 session 仲有 {remaining_quota()}/{MAX_ANALYSES} 次")

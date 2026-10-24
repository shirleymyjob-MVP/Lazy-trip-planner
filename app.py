"""懶人行程規劃 — Streamlit entry point.

流程：上傳截圖（自動認地方）或貼店名／Google Maps 連結 → 地點卡顯示
地址、營業時間、評分 → 揀地方 → 生成行程 → 下載 Markdown。
預設日本，其他目的地可以改。
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
JAPAN_PRESETS = ("日本", "東京", "大阪", "京都", "北海道")


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
# 每個 browser session 最多幾多次成功嘅 AI 動作（分析截圖、認地方、生成行程）。
# 失敗、SDK 重試、分批唔另計。重新整理會重置；真正 per-user 限額要之後先加登入。
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
        "places": [],
        "itinerary": None,
        "itinerary_places": [],
        "usage": 0,
        "batch": 0,
        "image_labels": {},
        "next_image_num": 1,
        "prepared_images": {},
        "analyzed_keys": set(),
        "analysis_block": set(),
        "notice": None,  # (level, text) 失敗／警告先留低，避免下一輪 rerun 唔見
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("destination_field", "日本")


def remaining_quota() -> int:
    return max(0, MAX_ANALYSES - st.session_state.usage)


def reset_all():
    for key in (
        "places", "itinerary", "itinerary_places", "image_labels", "next_image_num",
        "prepared_images", "analyzed_keys", "analysis_block", "notice",
    ):
        st.session_state.pop(key, None)
    st.session_state.batch = st.session_state.get("batch", 0) + 1


def _upload_key(uploaded_file) -> str:
    file_id = getattr(uploaded_file, "file_id", None)
    if file_id:
        return str(file_id)
    return hashlib.sha256(uploaded_file.getvalue()).hexdigest()


def _coerce_analysis(raw) -> gemini_client.AnalysisResult:
    if isinstance(raw, gemini_client.AnalysisResult):
        return raw
    if isinstance(raw, list):
        return gemini_client.AnalysisResult(places=list(raw), warnings=[])
    raise gemini_client.GeminiError("AI 回覆格式有問題，請再試一次。")


def _render_notice():
    notice = st.session_state.get("notice")
    if not notice:
        return
    level, text = notice
    renderer = {"error": st.error, "warning": st.warning, "success": st.success, "info": st.info}.get(level, st.info)
    renderer(text)


def _remember_places(new_places: list[dict]) -> int:
    before = len(st.session_state.places)
    st.session_state.places = trip_core.dedupe_places(st.session_state.places + new_places)
    st.session_state.itinerary = None
    st.session_state.itinerary_places = []
    return len(st.session_state.places) - before


def _analyze_pending(pending: list[tuple[str, int, bytes]]):
    images = [data for _key, _num, data in pending]
    display_numbers = [num for _key, num, _data in pending]
    keys = [key for key, _num, _data in pending]
    with st.status(f"AI 睇緊 {len(images)} 張截圖（唔穩會自動重試）…", expanded=True) as status:
        status.write("圖片已縮細。多過 3 張會分批同時處理，失敗會再試。")
        try:
            raw = gemini_client.analyze_screenshots(_client(), MODEL, images)
            result = _coerce_analysis(raw)
        except gemini_client.GeminiError as err:
            logger.info("Analysis failed: %s", err)
            status.update(label="分析失敗", state="error")
            st.session_state.notice = ("error", str(err))
            blocked = set(st.session_state.analysis_block)
            blocked.update(keys)
            st.session_state.analysis_block = blocked
            return
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error during analysis")
            status.update(label="分析失敗", state="error")
            st.session_state.notice = ("error", "分析途中出咗意外，請再試一次。")
            blocked = set(st.session_state.analysis_block)
            blocked.update(keys)
            st.session_state.analysis_block = blocked
            return
        status.update(
            label=f"認到 {len(result.places)} 個地方" if result.places else "分析完成",
            state="complete",
        )
    st.session_state.usage += 1
    done = set(st.session_state.analyzed_keys)
    done.update(keys)
    st.session_state.analyzed_keys = done
    blocked = set(st.session_state.analysis_block)
    blocked.difference_update(keys)
    st.session_state.analysis_block = blocked
    trip_core.map_batch_source_images(result.places, display_numbers)
    added = _remember_places(result.places)
    if result.warnings:
        st.session_state.notice = ("warning", " ".join(result.warnings))
    else:
        st.session_state.notice = None
    if result.places:
        st.success(f"認到 {len(result.places)} 個地方（新增 {added} 個）。地址、營業時間、評分喺下面嘅卡。")
    else:
        st.info("呢批截圖認唔到具體地方，試下上傳有店名／招牌／地址嘅畫面。")


def _add_pasted(text: str):
    parsed = trip_core.parse_pasted_places(text)
    unresolved: list[str] = []
    for url in parsed["short_urls"]:
        try:
            final = trip_core.resolve_maps_short_url(url)
        except Exception:  # noqa: BLE001
            logger.info("Could not resolve maps short link", exc_info=True)
            unresolved.append(url)
            continue
        query = trip_core.maps_query_from_url(final)
        place = trip_core.seed_place(query) if query else None
        if place:
            parsed["seeds"].append(place)
        else:
            unresolved.append(url)
    messages: list[str] = []
    if parsed["social_urls"]:
        messages.append(
            "Instagram／YouTube／小紅書連結未有穩定擷取。請上傳該頁截圖，或者貼店名／Google Maps 連結。"
        )
    if parsed["other_urls"] or unresolved:
        messages.append("有啲連結解唔到地點。請貼店名，或者完整 Google Maps 連結。")
    seeds = trip_core.dedupe_places(parsed["seeds"])
    if not seeds:
        st.session_state.notice = (
            "warning",
            " ".join(messages) or "未認到地方。一行一個店名，或者貼 Google Maps 連結。",
        )
        return
    enriched = seeds
    if API_KEY and remaining_quota() > 0:
        with st.status("幫貼上嘅地方補地址、營業時間、評分…", expanded=True) as status:
            try:
                raw = gemini_client.enrich_places(_client(), MODEL, seeds)
                result = _coerce_analysis(raw)
            except gemini_client.GeminiError as err:
                status.update(label="AI 補充失敗", state="error")
                messages.append(f"{err} 已先用你貼上嘅名稱。")
            except Exception:  # noqa: BLE001
                logger.exception("enrich failed")
                status.update(label="AI 補充失敗", state="error")
                messages.append("AI 補充失敗，已先用你貼上嘅名稱。")
            else:
                enriched = result.places or seeds
                messages.extend(result.warnings)
                st.session_state.usage += 1
                status.update(label=f"整理咗 {len(enriched)} 個地方", state="complete")
    elif not API_KEY:
        messages.append("AI 未設定，只列出你貼上嘅名稱；地址、時間、評分要有 API key 先補到。")
    else:
        messages.append("今個 session 嘅次數用完，已先列出名稱，未有再問 AI 補資料。")
    added = _remember_places(enriched)
    st.session_state.notice = ("warning", " ".join(messages)) if messages else None
    st.success(f"加入 {added} 個地方。卡上有地址、營業時間、評分（未有就係對唔到，唔使跳去 Maps）。")


init_state()

# ---------------------------------------------------------------- header
st.title("✈️ 懶人行程規劃（原型版）")
st.caption("日本行程為主：上傳 IG／YouTube／小紅書截圖，或者貼店名／Google Maps 連結。其他目的地一樣改得。")
st.caption(trip_core.HAPPY_PATH_CAPTION)

with st.expander("點樣用？", expanded=not st.session_state.places):
    st.markdown(
        f"""
1. 上傳最多 **{MAX_IMAGES_PER_UPLOAD} 張**截圖。上傳完會**自動認地方**，唔使再撳分析。
2. 或者貼店名／Google Maps 連結（一行一個），撳 **認地方**。社群連結請改上傳截圖。
3. 地點卡已經有地區、地址、營業時間、評分。剔走唔想去嘅，目的地預設日本。
4. 撳 **生成行程**，下載 Markdown。

_原型版：每個 session 最多 {MAX_ANALYSES} 次成功嘅 AI 動作（失敗同自動重試唔計）。截圖只會用嚟即時分析。_
"""
    )

if not API_KEY:
    st.error("AI 服務未設定好，暫時用唔到分析功能。（管理員請喺 Streamlit Secrets 設定 GEMINI_API_KEY）")

# ---------------------------------------------------------------- upload + paste
uploaded = st.file_uploader(
    "上傳截圖（可以一次揀多張，上傳完會自動分析）",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.batch}",
)
uploaded = uploaded or []

pasted = st.text_area(
    "或者貼店名／Google Maps 連結（一行一個）",
    placeholder="一蘭拉麵 澀谷\nhttps://www.google.com/maps/search/?api=1&query=淺草寺",
    key=f"paste_{st.session_state.batch}",
    height=110,
)
if st.button("認地方", type="primary", disabled=not (pasted or "").strip(), key=f"paste_btn_{st.session_state.batch}"):
    _add_pasted(pasted)

if len(uploaded) > MAX_IMAGES_PER_UPLOAD:
    st.warning(f"一次最多分析 {MAX_IMAGES_PER_UPLOAD} 張，今次只會用頭 {MAX_IMAGES_PER_UPLOAD} 張。")
    uploaded = uploaded[:MAX_IMAGES_PER_UPLOAD]

decoded: list[tuple[str, bytes]] = []
kept_images: dict = {}
if uploaded:
    cache: dict = dict(st.session_state.prepared_images)
    for file in uploaded:
        key = _upload_key(file)
        if key not in cache:
            try:
                cache[key] = trip_core.prepare_image(file.getvalue())
            except Exception:  # noqa: BLE001
                logger.exception("Failed to read image %s", getattr(file, "name", "?"))
                cache[key] = None
        kept_images[key] = cache[key]
        if cache[key] is None:
            st.warning(f"「{file.name}」讀唔到，可能唔係圖片檔，已略過。")
            continue
        decoded.append((key, cache[key]))
    labels = dict(st.session_state.image_labels)
    st.session_state.next_image_num = trip_core.assign_display_numbers(
        [key for key, _ in decoded], labels, st.session_state.next_image_num
    )
    st.session_state.image_labels = labels
    if decoded:
        cols = st.columns(min(len(decoded), 5))
        for index, (key, data) in enumerate(decoded):
            with cols[index % len(cols)]:
                st.image(data, caption=f"#{labels[key]}", width="stretch")
st.session_state.prepared_images = kept_images

prepared = [(key, st.session_state.image_labels[key], data) for key, data in decoded]
pending = [
    item for item in prepared
    if item[0] not in st.session_state.analyzed_keys and item[0] not in st.session_state.analysis_block
]
if pending and API_KEY and remaining_quota() > 0:
    _analyze_pending(pending)
    pending = [
        item for item in prepared
        if item[0] not in st.session_state.analyzed_keys and item[0] not in st.session_state.analysis_block
    ]

_render_notice()

blocked_items = [item for item in prepared if item[0] in st.session_state.analysis_block]
analyzed_all = bool(prepared) and all(item[0] in st.session_state.analyzed_keys for item in prepared)
if blocked_items and API_KEY and remaining_quota() > 0:
    if st.button("再試一次分析", type="primary", key=f"retry_{st.session_state.batch}"):
        blocked = set(st.session_state.analysis_block)
        for key, _num, _data in blocked_items:
            blocked.discard(key)
        st.session_state.analysis_block = blocked
        st.rerun()
elif analyzed_all and API_KEY and remaining_quota() > 0:
    if st.button("再分析呢批截圖", key=f"reanalyze_{st.session_state.batch}"):
        done = set(st.session_state.analyzed_keys)
        for key, _num, _data in prepared:
            done.discard(key)
        st.session_state.analyzed_keys = done
        st.rerun()
elif not API_KEY or (prepared and remaining_quota() == 0) or not prepared:
    if prepared and remaining_quota() == 0:
        st.warning("今個 session 嘅免費次數用完喇 🙏 之後會推出登入／付費版本。")
    # 有 API key 又未上傳時，上傳本身就會自動分析，唔使一個撳唔到嘅掣。
    # 冇 key，或者次數用完仲有圖，先顯示停用嘅分析入口。
    show_disabled = (not API_KEY) or (prepared and remaining_quota() == 0)
    if show_disabled:
        st.button("🔍 分析截圖", type="primary", disabled=True, key=f"analyze_disabled_{st.session_state.batch}")

# ---------------------------------------------------------------- places
places = st.session_state.places
if places:
    st.divider()
    head_l, head_r = st.columns([3, 1])
    head_l.subheader(f"📍 認到嘅地方（{len(places)}）")
    if head_r.button("清除全部", type="tertiary"):
        reset_all()
        st.rerun()
    st.caption("每張卡都有地區、地址、營業時間、評分。未有＝截圖冇寫、AI 亦未有把握，唔使為咗呢四樣開 Google Maps。Maps 掣只係導航。")
    st.caption("剔走唔想去嘅地方。預設全選，可以直接生成行程。")

    for index, place in enumerate(places):
        with st.container(border=True):
            left, right = st.columns([5, 2])
            with left:
                st.checkbox(
                    f"**{place['name_zh'] or place['name']}**",
                    value=True,
                    key=f"sel_{st.session_state.batch}_{index}_{place['name']}",
                )
                if place["name_zh"] and place["name"] != place["name_zh"]:
                    st.caption(place["name"])
                meta = [trip_core.CATEGORY_LABELS.get(place["category"], place["category"])]
                meta.append(f"信心：{place['confidence']}")
                if place["source_images"]:
                    meta.append("圖 " + ", ".join(f"#{n}" for n in place["source_images"]))
                st.markdown(" · ".join(meta))
                st.markdown("\n\n".join(trip_core.place_fact_lines(place)))
                if place["notes"]:
                    st.caption(place["notes"])
            with right:
                st.link_button("Google Maps", trip_core.maps_search_url(place), width="stretch")

    selected = [
        place for index, place in enumerate(places)
        if st.session_state.get(f"sel_{st.session_state.batch}_{index}_{place['name']}", True)
    ]

    st.divider()
    st.subheader("🗓️ 幫我排行程")
    st.caption("目的地預設日本、日數預設 3。想指定城市先揀下面，否則直接生成。")
    preset_cols = st.columns(len(JAPAN_PRESETS))
    for col, name in zip(preset_cols, JAPAN_PRESETS):
        if col.button(name, key=f"preset_{st.session_state.batch}_{name}"):
            if st.session_state.get("destination_field") != name:
                st.session_state.destination_field = name
                st.rerun()
    with st.form("itinerary_form"):
        days = st.number_input("玩幾多日？", min_value=1, max_value=14, value=3, step=1)
        destination = st.text_input(
            "目的地",
            key="destination_field",
            placeholder="日本、東京、大阪，或其他國家",
        )
        notes = st.text_area(
            "其他要求（可留空）",
            placeholder="例如：第一日下晝先到、唔想行太多、想食多啲拉麵、帶小朋友",
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
            with st.status("AI 排緊行程…", expanded=True) as status:
                status.write("會按地區分日，並盡量避開清單入面已知嘅非營業時間。")
                try:
                    itinerary = gemini_client.generate_itinerary(
                        _client(), MODEL, selected, int(days), destination, notes
                    )
                except gemini_client.GeminiError as err:
                    status.update(label="排行程失敗", state="error")
                    st.session_state.notice = ("error", str(err))
                    st.error(str(err))
                except Exception:  # noqa: BLE001
                    logger.exception("Unexpected error during itinerary generation")
                    status.update(label="排行程失敗", state="error")
                    st.session_state.notice = ("error", "排行程途中出咗意外，請再試一次。")
                    st.error("排行程途中出咗意外，請再試一次。")
                else:
                    st.session_state.usage += 1
                    st.session_state.itinerary = itinerary
                    st.session_state.itinerary_places = selected
                    st.session_state.notice = None
                    status.update(label="行程排好喇", state="complete")

# ---------------------------------------------------------------- itinerary view
itinerary = st.session_state.itinerary
if itinerary:
    fact_source = st.session_state.itinerary_places or places
    shown = trip_core.attach_place_facts(itinerary, fact_source)
    st.divider()
    st.subheader(f"🧳 {shown['title']}")
    st.caption("對得返收藏地點嘅，下面會直接寫地址、營業時間、評分。")
    for day in shown["days"]:
        with st.container(border=True):
            title = f"**Day {day['day']}**"
            if day["area"]:
                title += f"｜{day['area']}"
            st.markdown(title)
            if day["theme"]:
                st.caption(day["theme"])
            for item in day["items"]:
                icon = "🍽️ " if item["is_meal"] else ""
                time = f"`{item['time']}` " if item["time"] else ""
                map_url = trip_core.maps_search_url({"name": item["maps_query"]}) if item["maps_query"] else None
                if map_url and item["place_name"]:
                    name = f"[{trip_core.escape_markdown_label(item['place_name'])}]({map_url})"
                else:
                    name = item["place_name"]
                activity = f" — {item['activity']}" if item["activity"] else ""
                st.markdown(f"- {time}{icon}{name}{activity}")
                bits = []
                if item.get("address"):
                    bits.append(f"🏠 {item['address']}")
                elif item.get("area"):
                    bits.append(f"📍 {item['area']}")
                if item.get("opening_hours"):
                    bits.append(f"🕒 {item['opening_hours']}")
                if item.get("rating"):
                    bits.append(f"⭐ {item['rating']}")
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
st.caption(f"原型版本 | 日本預設 | 今個 session 仲有 {remaining_quota()}/{MAX_ANALYSES} 次")

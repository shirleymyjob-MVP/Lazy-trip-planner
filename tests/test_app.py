"""Streamlit AppTest：新流程用 mock 代替真 Gemini。"""
import hashlib
import inspect
import io
import json
import os
import sys
from datetime import date
from unittest import mock

from PIL import Image
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
APP = os.path.join(ROOT, "app.py")
_CONFLICT_BANNER = "呢份行程可能已喺另一個分頁、工作階段或成員度更新。你未儲存嘅更改未寫入。"
_CONFLICT_RELOAD_NOTE = "撳「重新載入已儲存的行程」會放棄呢度未儲存嘅更改，改為顯示已儲存的版本。"
_TOKYO = "東京（國泰、香港快運直飛）"

import pytest

import auth_gate  # noqa: E402
import gemini_client  # noqa: E402
import trip_core  # noqa: E402
import trip_store  # noqa: E402


@pytest.fixture(autouse=True)
def _signed_in_member(monkeypatch):
    """既有畫面測試以已登入會員身份跑。

    AppTest 的 LocalScriptRunner 把 st.user 寫死，公開 API 不能設 is_logged_in。
    這裡只係測試替身，應用程式本身沒有訪客旁路。
    """
    monkeypatch.setattr(auth_gate, "_authlib_ready", lambda: True)
    monkeypatch.setattr(
        auth_gate,
        "_auth_section",
        lambda: {
            "redirect_uri": "http://localhost:8501/oauth2callback",
            "cookie_secret": "test-cookie-secret",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "server_metadata_url": auth_gate.GOOGLE_SERVER_METADATA_URL,
        },
    )
    monkeypatch.setattr(
        "streamlit.user_info._get_user_info",
        lambda: {
            "is_logged_in": True,
            "sub": "test-member-sub",
            "email_verified": True,
        },
    )


def _png(color: str) -> bytes:
    img = Image.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _page(at: AppTest) -> str:
    return "\n".join(block.value for block in list(at.title) + list(at.markdown) + list(at.caption))


def _button(at: AppTest, label: str):
    found = [button for button in at.button if button.label == label]
    assert found, label
    return found[0]


def _nav(at: AppTest):
    found = [pill for pill in at.pills if pill.label == "步驟"]
    assert found, "找不到步驟 pills"
    return found[0]


def _open_screen(at: AppTest, label: str):
    nav = _nav(at)
    if nav.value != label:
        nav.set_value(label).run()
    assert not at.exception
    return at


def _select(at: AppTest, label: str):
    return [box for box in at.selectbox if label in box.label][0]


def _names():
    return trip_core.parse_places_response(json.dumps({
        "places": [{
            "name": "太宰府天滿宮", "name_zh": "太宰府天滿宮", "category": "attraction",
            "area": "福岡", "confidence": "高", "source_images": [1],
        }]
    }, ensure_ascii=False))


def _facts():
    return trip_core.parse_places_response(json.dumps({
        "places": [{
            "name": "太宰府天滿宮", "name_zh": "太宰府天滿宮", "category": "attraction",
            "area": "福岡", "address": "福岡縣太宰府市宰府4-7-1",
            "opening_hours": "06:00-19:00", "rating": "4.5/5",
            "confidence": "高", "source_images": [1],
        }]
    }, ensure_ascii=False))


def _fav(**overrides) -> dict:
    row = {
        "id": "fav1",
        "source_item": "",
        "name": "太宰府天滿宮",
        "name_zh": "太宰府天滿宮",
        "name_original": "太宰府天滿宮",
        "category": "景點",
        "model_category": "attraction",
        "regions": ["福岡"],
        "address": "福岡縣太宰府市宰府4-7-1",
        "opening_hours": "06:00-19:00",
        "rating": "4.5/5",
        "added_by": "旅人",
        "edited": False,
    }
    row.update(overrides)
    return row


def test_opening_shows_the_four_steps_and_build_without_a_paywall():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    page = _page(at)
    assert "懶人出行" in page
    assert "歡迎回來，旅人！" in page
    assert "進行中" not in page
    assert trip_core.HERO_LINE in page
    for step in trip_core.OPENING_STEPS:
        assert step in page
    assert any("build:" in caption.value for caption in at.caption)
    assert any("AI 服務未設定好" in error.value for error in at.error)
    logout = _button(at, "登出")
    assert logout.label == "登出"
    assert not logout.disabled
    start = _button(at, "開始收集想去的地方")
    assert not start.disabled
    assert not any(button.label == "開始排行程" for button in at.button)
    assert not at.download_button
    assert any(block.value == "你仲未有行程" for block in at.info)


def test_logout_css_stays_tappable_without_covering_the_desktop_column():
    """手機要見到完整「登出」並可點；桌面唔好變成整行大掣。"""
    css = open(APP, encoding="utf-8").read()
    start = css.index("div.st-key-auth_logout {")
    block = css[start:css.index("div[data-testid=\"stColumn\"]", start)]
    assert "st-key-auth_logout_missing_sub" not in block
    assert "[class*=\"st-key-auth_logout\"]" not in block
    assert "min-height: 44px" in block
    assert "white-space: nowrap" in block
    assert "3.75rem" in block
    assert "width: auto" in block
    button_rules = block.split("button {", 1)[1]
    assert "\n        width: 100%" not in button_rules
    assert "min-width: 4.75rem" in button_rules


def test_upload_waits_for_the_analyze_button_then_fills_facts():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.run()
    with mock.patch.object(gemini_client, "recognize_names", side_effect=AssertionError("too soon")), \
         mock.patch.object(gemini_client, "enrich_places", side_effect=AssertionError("too soon")):
        at.file_uploader[0].upload("dazaifu.png", _png("red"), "image/png").run()
    assert not at.exception
    assert at.session_state["inbox"][0]["analysis"] == "idle"
    assert at.session_state["usage"] == 0
    assert "尚未分析" in _page(at)
    assert any(box.label == "備註（可選）" for box in at.text_input)
    with mock.patch.object(
        gemini_client, "recognize_names",
        return_value=gemini_client.AnalysisResult(_names(), []),
    ), mock.patch.object(
        gemini_client, "enrich_places",
        return_value=gemini_client.AnalysisResult(_facts(), []),
    ) as enrich:
        _button(at, "開始分析").click().run()
        assert enrich.call_count == 1
        _button(at, "查看我的收藏 →").click().run()
        assert enrich.call_count == 1
    assert not at.exception
    assert enrich.call_args.kwargs.get("use_maps") is False
    assert enrich.call_args.args[1] == "gemini-3.5-flash-lite"
    assert at.session_state["usage"] == 1
    assert at.session_state["inbox"][0]["facts"] == "done"
    page = _page(at)
    assert "宰府4-7-1" in page
    assert "06:00-19:00" in page
    assert "4.5/5" in page
    assert "who-self" in page and "#4A90D9" in page and ">我<" in page
    assert trip_core.PAYWALL_NOTE in page
    assert "未核實" not in page
    assert not any(button.label == "先核對一個樣本" for button in at.button)


def test_batch_upload_has_one_analyze_all_button_and_no_remarks():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.run()
    with mock.patch.object(gemini_client, "recognize_names", side_effect=AssertionError("too soon")):
        uploader = at.file_uploader[0]
        uploader.upload("a.png", _png("red"), "image/png")
        uploader.upload("b.png", _png("blue"), "image/png")
        uploader.upload("c.png", _png("green"), "image/png").run()
    assert not at.exception
    inbox = at.session_state["inbox"]
    assert len(inbox) == 3
    assert all(item["batch"] for item in inbox)
    assert all(item["analysis"] == "idle" for item in inbox)
    assert not any(box.label == "備註（可選）" for box in at.text_input)
    assert "批量項目：不設個別備註" in _page(at)
    found = trip_core.parse_places_response(json.dumps({
        "places": [
            {"name": "一蘭", "name_zh": "一蘭", "category": "restaurant", "area": "福岡", "source_images": [1]},
            {"name": "博多站", "name_zh": "博多站", "category": "attraction", "area": "福岡", "source_images": [2]},
        ]
    }, ensure_ascii=False))
    with mock.patch.object(
        gemini_client, "recognize_names",
        return_value=gemini_client.AnalysisResult(found, []),
    ) as recognize, mock.patch.object(
        gemini_client, "enrich_places",
        return_value=gemini_client.AnalysisResult(found, []),
    ):
        _button(at, "開始分析全部待處理").click().run()
    assert not at.exception
    recognize.assert_called_once()
    assert len(recognize.call_args.args[2]) == 3
    assert at.session_state["usage"] == 1


def test_failed_analysis_can_retry_or_take_a_manual_name():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.run()
    at.file_uploader[0].upload("blur.png", _png("white"), "image/png").run()
    with mock.patch.object(
        gemini_client, "recognize_names",
        side_effect=gemini_client.GeminiError("測試失敗"),
    ):
        _button(at, "開始分析").click().run()
    assert not at.exception
    assert at.session_state["usage"] == 0
    assert at.session_state["inbox"][0]["analysis"] == "failed"
    assert "無法辨識" in _page(at)
    assert any(button.label == "重試" for button in at.button)
    with mock.patch.object(
        gemini_client, "enrich_places",
        return_value=gemini_client.AnalysisResult(_facts(), []),
    ) as enrich:
        _button(at, "手動輸入店名").click().run()
        name = [box for box in at.text_input if box.label == "店名"][0]
        name.input("太宰府天滿宮").run()
        _button(at, "確認店名").click().run()
    assert not at.exception
    assert enrich.call_count == 1
    assert at.session_state["usage"] == 0
    assert at.session_state["favourites"][0]["name"] == "太宰府天滿宮"
    assert at.session_state["favourites"][0]["added_by"] == "旅人"
    assert "宰府4-7-1" in _page(at)


def test_collection_card_colors_me_and_friend_then_edits_region():
    mine = _fav(id="mine", name="太宰府天滿宮", added_by="旅人", regions=["太宰府"], category="景點")
    friend = _fav(
        id="friend",
        name="运河城 Hakata",
        name_original="运河城 Hakata",
        added_by="小美",
        regions=["福岡市", "博多"],
        category="購物",
        address="福岡縣福岡市博多區住吉1-2",
    )
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "C"
    at.session_state["display_name"] = "旅人"
    at.session_state["favourites"] = [mine, friend]
    at.run()
    assert not at.exception
    page = _page(at)
    assert page.count("who-self") >= 1
    assert page.count("who-friend") >= 1
    assert "#4A90D9" in page and ">我<" in page
    assert "#8A8A8A" in page and "小美" in page
    assert "运河城 Hakata" in page and "太宰府天滿宮" in page
    _button(at, "✎").click().run()
    region = [pill for pill in at.pills if "地區" in pill.proto.label][0]
    region.set_value(["太宰府", "福岡"]).run()
    _select(at, "分類").set_value("文化體驗").run()
    renamed = [box for box in at.text_input if box.label == "店名"][0]
    renamed.input("太宰府天滿宮（本殿）").run()
    _button(at, "儲存").click().run()
    assert not at.exception
    saved = at.session_state["favourites"][0]
    assert saved["edited"] is True
    assert saved["name"] == "太宰府天滿宮（本殿）"
    assert saved["category"] == "文化體驗"
    assert "福岡" in saved["regions"] and "太宰府" in saved["regions"]
    assert "太宰府天滿宮（本殿）" in _page(at)
    assert "文化體驗" in _page(at)
    _button(at, "✎").click().run()
    remove = [button for button in at.button if button.label == "移除"][0]
    remove.click().run()
    assert not any(fav["id"] == "mine" for fav in at.session_state["favourites"])
    assert any(fav["added_by"] == "小美" for fav in at.session_state["favourites"])


def test_plan_requires_a_start_date_and_labels_collection_versus_ai():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "D"
    at.session_state["favourites"] = [_fav(added_by="小美")]
    at.session_state["allow_ai"] = True
    at.run()
    assert not at.exception
    assert _button(at, "開始排行程").disabled
    assert "請先選擇出發日" in _page(at)
    assert "是否需要 AI 協助" in _page(at)
    at.session_state["start_date"] = date(2026, 11, 14)
    at.run()
    assert not _button(at, "開始排行程").disabled
    fake = {
        "title": "福岡3日遊",
        "days": [{"day": 1, "area": "福岡", "theme": "", "items": [
            {
                "time": "09:00", "place_name": "太宰府天滿宮", "activity": "求學問運，約 1.5 小時。",
                "is_meal": False, "maps_query": "太宰府天滿宮", "transit": "",
            },
            {
                "time": "11:30", "place_name": "大濠公園", "activity": "湖邊散步。",
                "is_meal": False, "maps_query": "大濠公園", "transit": "西鉄電車 約 25 分鐘",
            },
        ]}],
        "tips": [],
    }
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake) as gen:
        _button(at, "開始排行程").click().run()
    assert not at.exception
    assert gen.call_args.args[1] == "gemini-3.5-flash-lite"
    assert gen.call_args.args[3] == 3
    assert gen.call_args.args[4] == "福岡"
    notes = gen.call_args.args[5]
    assert "2026-11-14" in notes
    assert "平衡型" in notes
    assert "可以加入清單以外" in notes
    page = _page(at)
    assert "已按「平衡型」安排" in page
    assert "來自收藏" in page
    assert "AI 建議" in page
    assert "小美" in page and "#8A8A8A" in page
    assert "西鉄電車 約 25 分鐘" in page
    assert "🚇" in page
    assert "🚶" not in page
    assert "06:00-19:00" in page
    assert "未核實" in page
    assert at.session_state["usage"] == 1
    assert at.session_state["screen"] == "E"
    assert not at.download_button
    from urllib.parse import unquote

    maps = [button for button in at.link_button if button.value == "喺 Google 睇行程"]
    assert maps, "results page should link to Google Maps"
    assert maps[0].proto.url.startswith("https://www.google.com/maps/dir/")
    decoded = unquote(maps[0].proto.url)
    assert "太宰府天滿宮" in decoded and "大濠公園" in decoded
    assert page.count("Google 地圖") >= 2
    assert "maps/search" in page
    assert "query=%E5%A4%AA%E5%AE%B0%E5%BA%9C%E5%A4%A9%E6%BB%BF%E5%AE%AE" in page or "太宰府天滿宮" in page


def test_plan_failure_does_not_spend_a_turn():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "D"
    at.session_state["start_date"] = date(2026, 11, 14)
    at.session_state["favourites"] = [_fav(), _fav(id="b", name="一蘭"), _fav(id="c", name="福岡塔")]
    at.run()
    assert "是否需要 AI 協助" in _page(at)
    message = "AI 回應太慢（已自動重試）。請少傳幾張圖，或者過一陣再試。"
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", side_effect=gemini_client.GeminiError(message)):
        submit = _button(at, "開始排行程")
        assert not submit.disabled
        submit.click().run()
    assert not at.exception
    shown = "\n".join(block.value for block in list(at.error) + list(at.warning))
    assert message in shown
    assert at.session_state["usage"] == 0
    assert at.session_state["itinerary"] is None


def test_same_member_reloads_screenshot_and_share_query_is_ignored():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.query_params["share"] = "abc123def456"
    at.session_state["display_name"] = "Ada"
    at.session_state["screen"] = "B"
    at.run()
    assert at.query_params.get("share") == "abc123def456"
    assert "share_token" not in at.session_state
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _open_screen(at, "收集")
    with mock.patch.object(gemini_client, "recognize_names", side_effect=AssertionError("no analyze")):
        at.file_uploader[0].upload("shared.png", _png("red"), "image/png").run()
    assert at.session_state["inbox"][0]["added_by"] == "Ada"
    image = at.session_state["inbox"][0]["image"]
    assert image
    assert "尚未分析" in _page(at)
    other = AppTest.from_file(APP, default_timeout=30)
    other.secrets["GEMINI_API_KEY"] = "fake"
    other.query_params["share"] = "abc123def456"
    other.session_state["display_name"] = "Bo"
    other.session_state["screen"] = "B"
    other.run()
    assert not other.exception
    assert other.query_params.get("share") == "abc123def456"
    assert other.session_state["inbox"][0]["added_by"] == "Ada"
    assert other.session_state["inbox"][0]["image"] == image
    assert "加入：Ada" in _page(other)
    assert "test-service-role-key" not in _page(other)


def test_collect_page_has_helper_and_no_clipboard_button():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "B"
    at.run()
    assert not at.exception
    page = _page(at)
    assert "貼上截圖，系統會幫你識別並分類。" in page
    assert "待分析" in page and "已處理" in page
    assert "查看我的收藏 →" in [button.label for button in at.button]
    assert "從剪貼簿貼上" not in [button.label for button in at.button]
    assert "貼上截圖" not in [button.label for button in at.button]
    assert at.file_uploader[0].label == "從相簿或相機加入"
    assert "相簿或相機" in (at.file_uploader[0].help or "")
    source = open(APP, encoding="utf-8").read()
    assert 'label="邀請朋友一起收集"' not in source
    assert "先複製邀請連結" not in source
    assert 'st.query_params["share"]' not in source
    assert "朋友打開後可加入同一收藏清單" not in source
    assert 'content: "從相簿或相機加入"' in source
    assert "stFileUploaderDropzoneInstructions" in source
    assert 'label_visibility="collapsed"' in source
    assert "mini-thumb-wrap" in source
    assert "grid-template-columns: 56px minmax(0, 1fr)" in source
    assert "max-height: 80px" not in source


def test_same_screenshot_publishes_one_favourite():
    image = _png("red")

    def item(item_id, name, address):
        return {
            "id": item_id,
            "image": image,
            "analysis": "done",
            "facts": "done",
            "places": [{
                "name": name,
                "name_zh": name,
                "category": "shop",
                "area": "福岡",
                "address": address,
                "opening_hours": "11:00-20:00",
                "rating": "4.2/5",
            }],
            "added_by": "旅人",
            "remark": "",
            "batch": False,
        }

    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "C"
    at.session_state["inbox"] = [
        item("a", "kchiara", "福岡市中央區大名1-10-15"),
        item("b", "キアラ", "福岡縣福岡市中央區大名1-10-14"),
    ]
    at.run()
    assert not at.exception
    favs = at.session_state["favourites"]
    assert len(favs) == 1
    assert "1-10-15" in favs[0]["address"]
    assert favs[0]["category"] == "美容"
    assert not any(button.label == "從剪貼簿貼上" for button in at.button)


def test_saved_kchiara_cards_collapse_even_with_different_ids():
    """UAT：同一張 kchiara.png 分析兩次，地址和時間字串略有不同，仍然只能有一張美容卡。"""
    image = _png("red")

    def place(fav_id, address, hours):
        return {
            "fav_id": fav_id,
            "name": "kchiara",
            "name_zh": "kchiara",
            "category": "shop",
            "area": "福岡",
            "address": address,
            "opening_hours": hours,
            "rating": "4.2/5",
        }

    def item(item_id, fav):
        return {
            "id": item_id,
            "image": image,
            "analysis": "done",
            "facts": "done",
            "places": [fav],
            "added_by": "旅人",
            "remark": "",
            "batch": False,
        }

    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "C"
    at.session_state["favourites"] = [
        {
            "id": "card-a",
            "name": "kchiara",
            "name_original": "kchiara",
            "category": "購物",
            "address": "福岡県福岡市中央区大名1-10-14",
            "opening_hours": "12:00-19:00",
            "rating": "4.2/5",
            "added_by": "旅人",
            "regions": ["福岡"],
        },
        {
            "id": "card-b",
            "name": "kchiara",
            "name_original": "kchiara",
            "category": "購物",
            "address": "福岡県福岡市中央区大名",
            "opening_hours": "11:00-20:00",
            "rating": "4.2/5",
            "added_by": "旅人",
            "regions": ["福岡"],
        },
    ]
    at.session_state["inbox"] = [
        item("a", place("card-a", "福岡県福岡市中央区大名1-10-14", "12:00-19:00")),
        item("b", place("card-b", "福岡県福岡市中央区大名", "11:00-20:00")),
    ]
    at.run()
    assert not at.exception
    favs = at.session_state["favourites"]
    assert len(favs) == 1
    assert favs[0]["category"] == "美容"
    assert favs[0]["model_category"] == "salon"
    assert "1-10-14" in favs[0]["address"]
    page = _page(at)
    assert page.count("kchiara") == 1
    assert "購物" not in page


def test_one_photo_with_two_places_stays_two_favourites():
    image = _png("blue")
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "C"
    at.session_state["inbox"] = [{
        "id": "mix",
        "image": image,
        "analysis": "done",
        "facts": "done",
        "places": [
            {
                "name": "一蘭", "name_zh": "一蘭", "category": "restaurant",
                "area": "福岡", "address": "福岡市博多區中洲1-1-1",
            },
            {
                "name": "太宰府天滿宮", "name_zh": "太宰府天滿宮", "category": "attraction",
                "area": "太宰府", "address": "福岡縣太宰府市宰府4-7-1",
            },
        ],
        "added_by": "旅人",
        "remark": "",
        "batch": False,
    }]
    at.run()
    assert not at.exception
    names = {fav["name"] for fav in at.session_state["favourites"]}
    assert names == {"一蘭", "太宰府天滿宮"}


def test_ai_suggestion_is_enriched_instead_of_staying_unverified():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "D"
    at.session_state["start_date"] = date(2026, 11, 14)
    at.session_state["favourites"] = [_fav(added_by="小美")]
    at.session_state["allow_ai"] = True
    at.run()
    fake = {
        "title": "福岡3日遊",
        "days": [{"day": 1, "area": "福岡", "theme": "", "items": [
            {
                "time": "09:00", "place_name": "太宰府天滿宮", "activity": "求學問運。",
                "is_meal": False, "maps_query": "太宰府天滿宮", "transit": "",
            },
            {
                "time": "16:00", "place_name": "大濠公園", "activity": "湖邊散步。",
                "is_meal": False, "maps_query": "大濠公園", "transit": "步行 約 8 分鐘",
            },
        ]}],
        "tips": [],
    }
    enriched = [{
        "name": "大濠公園",
        "name_zh": "大濠公園",
        "category": "attraction",
        "area": "福岡",
        "address": "福岡市中央區大濠公園",
        "opening_hours": "09:00-18:00",
        "rating": "4.4/5",
    }]
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake), \
         mock.patch.object(
             gemini_client, "enrich_places",
             return_value=gemini_client.AnalysisResult(enriched, []),
         ) as enrich:
        _button(at, "開始排行程").click().run()
    assert not at.exception
    assert enrich.call_count == 1
    page = _page(at)
    assert "09:00-18:00" in page
    assert "4.4/5" in page
    assert "AI 建議" in page
    assert "未核實" not in page
    assert "🚶" in page
    assert "🚇" not in page
    assert at.session_state["usage"] == 1


def test_generic_ai_shell_is_replaced_before_the_results_page():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "D"
    at.session_state["start_date"] = date(2026, 11, 14)
    at.session_state["favourites"] = [_fav(added_by="小美")]
    at.session_state["allow_ai"] = True
    at.run()
    fake = {
        "title": "福岡一日",
        "days": [{"day": 1, "area": "福岡", "items": [
            {
                "time": "09:00", "place_name": "太宰府天滿宮", "activity": "參拜。",
                "is_meal": False, "maps_query": "太宰府天滿宮", "transit": "",
            },
            {
                "time": "12:30", "place_name": "天神網紅餐廳 (AI建議)", "activity": "打卡食午餐。",
                "is_meal": True, "maps_query": "天神", "transit": "地鐵 約 25 分鐘",
            },
        ]}],
        "tips": [],
    }
    nearby = gemini_client.AnalysisResult([{
        "name": "一蘭 天神店",
        "name_zh": "一蘭 天神店",
        "category": "restaurant",
        "area": "天神",
        "address": "福岡市中央區天神2-1-1",
        "opening_hours": "11:00-22:00",
        "rating": "4.3/5",
        "notes": "天神站附近，有公開評分。",
    }], [])
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake), \
         mock.patch.object(gemini_client, "suggest_nearby", return_value=nearby) as nearby_call:
        _button(at, "開始排行程").click().run()
    assert not at.exception
    assert nearby_call.call_count == 1
    page = _page(at)
    assert "天神網紅餐廳" not in page
    assert "打卡熱點" not in page
    assert "一蘭 天神店" in page
    assert "福岡市中央區天神2-1-1" in page
    assert "11:00-22:00" in page
    assert "4.3/5" in page
    assert "AI 建議" in page
    assert page.count("Google 地圖") >= 2
    saved = at.session_state["itinerary"]["days"][0]["items"]
    assert [item["place_name"] for item in saved] == ["太宰府天滿宮", "一蘭 天神店"]


def test_missing_trip_style_constant_still_opens_and_shows_build(monkeypatch):
    """舊 trip_core 冇新常數時，開場唔可以 AttributeError，而且 build 要先畫出嚟。"""
    for name in (
        "DEFAULT_TRIP_STYLE",
        "HERO_LINE",
        "OPENING_STEPS",
        "COLLECT_STATUS_IDLE",
        "COLLECT_STATUS_DONE",
        "PAYWALL_NOTE",
        "SPARSE_COLLECTION_MIN",
    ):
        monkeypatch.delattr(trip_core, name, raising=False)
    monkeypatch.delattr(trip_core, "collect_status", raising=False)
    monkeypatch.delattr(trip_core, "favourite_category", raising=False)
    monkeypatch.delattr(trip_core, "place_regions", raising=False)
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["inbox"] = [{
        "id": "stale",
        "analysis": "done",
        "facts": "done",
        "places": [{"name": "太宰府天滿宮", "name_zh": "太宰府天滿宮"}],
        "remark": "",
        "batch": False,
        "added_by": "旅人",
    }]
    at.run()
    assert not at.exception
    assert at.caption
    assert at.caption[0].value.startswith("build:")
    assert at.session_state["trip_style"] == "平衡型"
    page = _page(at)
    assert "貼上你想去的地方" in page
    assert "收集想去的地方" in page
    _button(at, "開始收集想去的地方")
    assert "懶人出行" in page


ADDR_15 = "福岡県福岡市中央区大名1-10-15"
ADDR_14 = "福岡県福岡市中央区大名1-10-14"


def test_one_digit_address_drift_collapses_even_when_trip_core_refuses(monkeypatch):
    """同一張 kchiara.png：大名1-10-15 同 1-10-14、同一圖片指紋，載入後只能剩一張美容卡。

    6b92 把 same_favourite / looks_like_salon 放喺 trip_core。Cloud 若只換到新 app.py，
    舊模組會當兩張卡唔同、類別保持購物。呢條路徑必須由 app.py 自己收埋。
    """
    monkeypatch.setattr(trip_core, "same_favourite", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(trip_core, "looks_like_salon", lambda *_args, **_kwargs: False)
    image = _png("red")
    fingerprint = hashlib.sha256(image).hexdigest()[:16]

    def card(card_id: str, address: str) -> dict:
        return {
            "id": card_id,
            "source_item": card_id,
            "name": "kchiara",
            "name_zh": "kchiara",
            "name_original": "kchiara",
            "category": "購物",
            "model_category": "shop",
            "address": address,
            "opening_hours": "12:00-19:00",
            "rating": "4.2/5",
            "added_by": "旅人",
            "regions": ["福岡", "大名"],
            "image_sha": fingerprint,
            "image_shas": [fingerprint],
            "sole_image": True,
            "edited": False,
        }

    def item(item_id: str, address: str) -> dict:
        return {
            "id": item_id,
            "image": image,
            "analysis": "done",
            "facts": "done",
            "places": [{
                "fav_id": item_id,
                "name": "kchiara",
                "name_zh": "kchiara",
                "category": "shop",
                "area": "福岡",
                "address": address,
                "opening_hours": "12:00-19:00",
                "rating": "4.2/5",
            }],
            "added_by": "旅人",
            "remark": "",
            "batch": False,
        }

    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "C"
    at.session_state["favourites"] = [card("card-15", ADDR_15), card("card-14", ADDR_14)]
    at.session_state["inbox"] = [item("card-15", ADDR_15), item("card-14", ADDR_14)]
    at.run()
    assert not at.exception
    favs = at.session_state["favourites"]
    assert len(favs) == 1
    assert favs[0]["category"] == "美容"
    assert favs[0]["model_category"] == "salon"
    assert favs[0]["address"] == ADDR_15
    assert favs[0]["opening_hours"] == "12:00-19:00"
    assert fingerprint in (favs[0].get("image_shas") or [])
    page = _page(at)
    assert page.count("kchiara") == 1
    assert "美容" in page
    assert "購物" not in page


def test_clicking_analyze_twice_on_the_same_photo_publishes_one_salon(monkeypatch):
    """開始分析兩次先至係守門員的路徑。舊 trip_core 拒絕合併時，第二次仍要收成一張美容卡。"""
    monkeypatch.setattr(trip_core, "same_favourite", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(trip_core, "looks_like_salon", lambda *_args, **_kwargs: False)
    image = _png("pink")
    addresses = [ADDR_15, ADDR_14]
    calls = {"n": 0}

    def recognize(*_args, **_kwargs):
        places = trip_core.parse_places_response(json.dumps({"places": [{
            "name": "kchiara",
            "name_zh": "kchiara",
            "category": "shop",
            "area": "福岡",
            "confidence": "高",
            "source_images": [1],
            "caption_index": 0,
        }]}, ensure_ascii=False))
        return gemini_client.AnalysisResult(places, [])

    def enrich(_client, _model, places, **_kwargs):
        address = addresses[min(calls["n"], len(addresses) - 1)]
        calls["n"] += 1
        seed = dict(places[0])
        seed.update({
            "address": address,
            "opening_hours": "12:00-19:00",
            "rating": "4.2/5",
            "category": "shop",
        })
        return gemini_client.AnalysisResult([seed], [])

    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.session_state["inbox"] = [
        {
            "id": "shot-a",
            "label": "截圖 1",
            "image": image,
            "remark": "",
            "analysis": "idle",
            "facts": "",
            "batch": False,
            "places": [],
            "city": "",
            "regions": [],
            "region_confidence": "低",
            "warning": "",
            "added_by": "旅人",
        },
        {
            "id": "shot-b",
            "label": "截圖 2",
            "image": image,
            "remark": "",
            "analysis": "idle",
            "facts": "",
            "batch": False,
            "places": [],
            "city": "",
            "regions": [],
            "region_confidence": "低",
            "warning": "",
            "added_by": "旅人",
        },
    ]
    at.run()
    assert not at.exception
    with mock.patch.object(gemini_client, "recognize_names", side_effect=recognize), \
         mock.patch.object(gemini_client, "enrich_places", side_effect=enrich):
        _button(at, "開始分析").click().run()
        assert len(at.session_state["favourites"]) == 1
        _button(at, "開始分析").click().run()
    assert not at.exception
    favs = at.session_state["favourites"]
    assert len(favs) == 1
    assert favs[0]["category"] == "美容"
    assert favs[0]["opening_hours"] == "12:00-19:00"
    assert favs[0]["address"] in {ADDR_15, ADDR_14}
    assert calls["n"] == 2


def test_kchiara_shopping_blurb_normalizes_to_salon_unless_a_real_shop_remains(monkeypatch):
    """收藏係購物、標題係購物之旅、文案係特色選物店。正規化後要變美容，除非仲有真商店。"""
    monkeypatch.setattr(trip_core, "looks_like_salon", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(trip_core, "garbled_place_name", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(trip_core, "rewrite_trip_title", lambda title, favourites=None: title)
    monkeypatch.setattr(trip_core, "rewrite_activity", lambda _name, activity, category="": activity)

    salon = {
        "id": "kchiara",
        "name": "kchiara",
        "name_original": "kchiara",
        "category": "購物",
        "model_category": "shop",
        "address": ADDR_15,
        "opening_hours": "12:00-19:00",
        "rating": "4.2/5",
        "added_by": "旅人",
        "regions": ["福岡"],
        "edited": False,
    }
    edited = _fav(
        id="edited",
        name="太宰府天滿宮",
        name_zh="太宰府天滿宮",
        name_original="太宰府天滿宮",
        category="文化體驗",
        edited=True,
    )
    itinerary = {
        "title": "福岡 3 日平衡美食購物之旅",
        "days": [{"day": 1, "items": [
            {
                "time": "14:00",
                "place_name": "kchiara",
                "activity": "探索大名區特色選物店，停留約 1.5 小時",
                "transit": "",
            },
            {
                "time": "16:00",
                "place_name": "Café Del ม",
                "activity": "下午茶",
                "transit": "步行約 5 分鐘",
            },
        ]}],
    }
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "E"
    at.session_state["favourites"] = [salon, edited]
    at.session_state["itinerary"] = itinerary
    at.run()
    assert not at.exception
    by_id = {fav["id"]: fav for fav in at.session_state["favourites"]}
    assert by_id["kchiara"]["category"] == "美容"
    assert by_id["edited"]["category"] == "文化體驗"
    page = _page(at)
    title = " ".join(block.value for block in at.subheader)
    assert "美容" in page
    assert "預留時間做髮型或美容" in page
    assert "選物" not in page
    assert "購物" not in title
    assert "美食" in title
    assert "Café Del" not in page
    assert "ม" not in page

    shop = _fav(
        id="muji",
        name="無印良品",
        name_zh="無印良品",
        name_original="無印良品",
        category="購物",
        model_category="shop",
        address="福岡市博多區",
    )
    at.session_state["favourites"] = [salon, shop]
    at.session_state["itinerary"] = {
        "title": "福岡 3 日平衡美食購物之旅",
        "days": [{"day": 1, "items": [
            {
                "time": "14:00",
                "place_name": "kchiara",
                "activity": "探索大名區特色選物店，停留約 1.5 小時",
                "transit": "",
            },
            {
                "time": "16:00",
                "place_name": "無印良品",
                "activity": "參觀特色選物店",
                "transit": "",
            },
        ]}],
    }
    at.run()
    assert not at.exception
    page = _page(at)
    title = " ".join(block.value for block in at.subheader)
    assert "購物" in title
    assert "選物" in page
    assert "預留時間做髮型或美容" in page
    saved = {fav["id"]: fav for fav in at.session_state["favourites"]}
    assert saved["kchiara"]["category"] == "美容"
    assert saved["muji"]["category"] == "購物"


def test_day_tabs_show_only_the_selected_day_even_if_day_numbers_repeat():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "E"
    at.session_state["favourites"] = [
        _fav(id="hakata", name="博多站", name_zh="博多站", name_original="博多站", address="博多站"),
        _fav(id="dazaifu", name="太宰府天滿宮", name_zh="太宰府天滿宮", name_original="太宰府天滿宮"),
    ]
    at.session_state["itinerary"] = {
        "title": "福岡 2 日遊",
        "days": [
            {"day": 1, "items": [{"time": "10:00", "place_name": "博多站", "activity": "集合", "transit": ""}]},
            {"day": 1, "items": [{"time": "11:00", "place_name": "太宰府天滿宮", "activity": "參拜", "transit": ""}]},
        ],
    }
    at.run()
    assert not at.exception
    page = _page(at)
    assert "博多站" in page
    assert "太宰府天滿宮" not in page
    day_pills = [pill for pill in at.pills if "行程日" in pill.proto.label]
    assert day_pills and list(day_pills[0].options) == ["Day 1", "Day 2"]
    day_pills[0].set_value("Day 2").run()
    assert not at.exception
    page = _page(at)
    assert "太宰府天滿宮" in page
    assert "博多站" not in page


def test_cloned_day_does_not_repeat_day_one():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "E"
    stops = [
        {"time": "10:00", "place_name": "博多站", "activity": "集合", "transit": ""},
        {"time": "12:00", "place_name": "一蘭", "activity": "午餐", "transit": "步行約 8 分鐘"},
    ]
    at.session_state["favourites"] = [
        _fav(id="hakata", name="博多站", name_zh="博多站", name_original="博多站", address="博多"),
        _fav(id="ichiran", name="一蘭", name_zh="一蘭", name_original="一蘭", address="中洲", category="美食"),
    ]
    at.session_state["itinerary"] = {
        "title": "福岡 2 日遊",
        "days": [
            {"day": 1, "items": [dict(stop) for stop in stops]},
            {"day": 2, "items": [dict(stop) for stop in stops]},
        ],
    }
    at.run()
    assert not at.exception
    assert "博多站" in _page(at)
    day_pills = [pill for pill in at.pills if "行程日" in pill.proto.label][0]
    day_pills.set_value("Day 2").run()
    page = _page(at)
    assert "這一天未有安排" in page
    assert "博多站" not in page
    assert "一蘭" not in page


def test_nav_is_small_step_chips_not_a_full_width_button():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    nav = [pill for pill in at.pills if pill.label == "步驟"]
    assert nav, "step chips should be st.pills"
    assert nav[0].options == ["首頁", "旅行夾", "收集", "收藏", "排程", "行程"]
    assert not any(button.label == "開場" for button in at.button)
    nav[0].set_value("收集").run()
    assert at.session_state["screen"] == "B"
    assert "待分析" in _page(at) and "已處理" in _page(at)


def test_done_cards_put_the_newest_on_top_and_zoom_opens():
    image = _png("red")

    def done(item_id, name, stamp):
        return {
            "id": item_id,
            "image": image,
            "analysis": "done",
            "facts": "done",
            "places": [{
                "name": name,
                "name_zh": name,
                "category": "restaurant",
                "area": "福岡",
                "address": "福岡市",
                "opening_hours": "11:00-20:00",
                "rating": "4.0/5",
            }],
            "added_by": "旅人",
            "remark": "",
            "batch": False,
            "added_at": stamp,
        }

    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.session_state["inbox"] = [
        done("old", "舊店一蘭", 10),
        done("new", "新店鐵板燒", 99),
    ]
    at.run()
    assert not at.exception
    page = _page(at)
    assert page.index("新店鐵板燒") < page.index("舊店一蘭")
    assert any(button.label == "⤢" for button in at.button)
    assert not any(button.label == "✎ 編輯" for button in at.button)
    _button(at, "⤢").click().run()
    assert not at.exception
    assert any(button.label == "關閉" for button in at.button)
    _button(at, "查看我的收藏 →").click().run()
    assert not at.exception
    assert not any(button.label == "關閉" for button in at.button)


def test_unrelated_buttons_do_not_open_the_zoom_dialog():
    """放大只可以由縮圖或 ⤢ 打開。其他頁的常用掣唔可以彈 dialog。"""
    image = _png("blue")
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["zoom_image"] = image
    at.session_state["screen"] = "A"
    at.run()
    assert not at.exception
    assert not any(button.label == "關閉" for button in at.button)
    assert not any(button.label == "先複製邀請連結" for button in at.button)

    _button(at, "開始收集想去的地方").click().run()
    assert at.session_state["screen"] == "B"
    assert not any(button.label == "關閉" for button in at.button)

    at.session_state["inbox"] = [{
        "id": "shot1",
        "image": image,
        "analysis": "done",
        "facts": "done",
        "places": [{
            "name": "kchiara",
            "name_zh": "kchiara",
            "category": "salon",
            "area": "福岡",
            "address": "大名",
            "opening_hours": "月-金 11:00-20:00",
            "rating": "4.5/5",
        }],
        "added_by": "旅人",
        "remark": "",
        "batch": False,
        "added_at": 1,
    }]
    at.session_state["favourites"] = [{
        "id": "fav1",
        "name": "kchiara",
        "category": "美容",
        "regions": ["福岡"],
        "address": "大名",
        "opening_hours": "月-金 11:00-20:00",
        "rating": "4.5/5",
        "added_by": "旅人",
        "source_item": "shot1",
    }]
    at.session_state["screen"] = "B"
    at.run()
    assert any(button.label == "⤢" for button in at.button)
    _button(at, "✎").click().run()
    assert not any(button.label == "關閉" for button in at.button)

    _button(at, "放大").click().run()
    assert any(button.label == "關閉" for button in at.button)
    _button(at, "關閉").click().run()
    assert not any(button.label == "關閉" for button in at.button)

    nav = [pill for pill in at.pills if pill.label == "步驟"][0]
    for label, screen in (("收藏", "C"), ("排程", "D"), ("行程", "E"), ("首頁", "A")):
        nav.set_value(label).run()
        assert at.session_state["screen"] == screen
        assert not at.exception
        assert not any(button.label == "關閉" for button in at.button)
        if screen == "C":
            _button(at, "✎").click().run()
            assert not any(button.label == "關閉" for button in at.button)
        nav = [pill for pill in at.pills if pill.label == "步驟"][0]


def test_favourites_keep_unstamped_order_and_pencil_stays_a_button():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "C"
    at.session_state["favourites"] = [
        _fav(id="first", name="先加入"),
        _fav(id="second", name="後加入"),
    ]
    at.run()
    assert not at.exception
    page = _page(at)
    assert page.index("先加入") < page.index("後加入")
    assert any(button.label == "✎" for button in at.button)
    assert not any("編輯" == button.label for button in at.button)


def test_processed_card_edit_updates_name_category_and_area():
    image = _png("red")
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.session_state["inbox"] = [{
        "id": "done1",
        "image": image,
        "analysis": "done",
        "facts": "done",
        "places": [{
            "fav_id": "fav-done",
            "name": "舊店名",
            "name_zh": "舊店名",
            "category": "shop",
            "area": "福岡",
            "address": "福岡市博多區",
            "opening_hours": "11:00-20:00",
            "rating": "4.0/5",
        }],
        "regions": ["福岡"],
        "city": "福岡",
        "region_confidence": "高",
        "added_by": "旅人",
        "remark": "",
        "batch": False,
        "added_at": 5,
    }]
    at.session_state["favourites"] = [_fav(
        id="fav-done",
        source_item="done1",
        name="舊店名",
        name_original="舊店名",
        category="購物",
        model_category="shop",
        regions=["福岡"],
    )]
    at.run()
    assert not at.exception
    _button(at, "✎").click().run()
    [box for box in at.text_input if box.label == "店名"][0].input("新店名").run()
    _select(at, "分類").set_value("景點").run()
    region = [pill for pill in at.pills if "地區" in pill.proto.label][0]
    region.set_value(["大阪"]).run()
    _button(at, "儲存").click().run()
    assert not at.exception
    place = at.session_state["inbox"][0]["places"][0]
    assert place["name"] == "新店名"
    assert place["fav_category"] == "景點"
    assert place["category_locked"] is True
    fav = next(row for row in at.session_state["favourites"] if row["id"] == "fav-done")
    assert fav["name"] == "新店名"
    assert fav["category"] == "景點"
    assert fav["edited"] is True
    assert "大阪" in fav["regions"]
    page = _page(at)
    assert "新店名" in page
    assert "景點" in page
    assert "大阪" in page


def test_unrecognised_item_can_choose_category_and_area():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.run()
    at.file_uploader[0].upload("blur.png", _png("white"), "image/png").run()
    with mock.patch.object(
        gemini_client, "recognize_names",
        side_effect=gemini_client.GeminiError("測試失敗"),
    ):
        _button(at, "開始分析").click().run()
    _button(at, "手動輸入店名").click().run()
    assert any(box.label == "分類" for box in at.selectbox)
    assert any(box.label == "地區" for box in at.selectbox)
    [box for box in at.text_input if box.label == "店名"][0].input("一蘭 天神店").run()
    _select(at, "分類").set_value("美食").run()
    _select(at, "地區").set_value("福岡").run()
    with mock.patch.object(
        gemini_client, "enrich_places",
        return_value=gemini_client.AnalysisResult([{
            "name": "一蘭 天神店",
            "name_zh": "一蘭 天神店",
            "category": "restaurant",
            "area": "福岡",
            "address": "福岡市中央區天神",
            "opening_hours": "11:00-22:00",
            "rating": "4.2/5",
        }], []),
    ):
        _button(at, "確認店名").click().run()
    assert not at.exception
    fav = at.session_state["favourites"][0]
    assert fav["name"] == "一蘭 天神店"
    assert fav["category"] == "美食"
    assert "福岡" in fav["regions"]
    assert trip_core.UNSORTED_REGION not in (fav.get("regions") or [])
    assert trip_core.UNSORTED_REGION not in _page(at)


def test_manual_add_sets_name_category_and_area():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["screen"] = "C"
    at.run()
    _button(at, "手動新增").click().run()
    [box for box in at.text_input if box.label == "店名"][0].input("太宰府天滿宮").run()
    _select(at, "分類").set_value("景點").run()
    _select(at, "地區").set_value("福岡").run()
    _button(at, "加入收藏").click().run()
    assert not at.exception
    fav = at.session_state["favourites"][0]
    assert fav["name"] == "太宰府天滿宮"
    assert fav["category"] == "景點"
    assert fav["edited"] is True
    assert fav["regions"] == ["福岡"]
    page = _page(at)
    assert "太宰府天滿宮" in page
    assert "景點" in page
    assert "福岡" in page
    assert trip_core.UNSORTED_REGION not in page


def test_ai_toggle_stays_visible_and_nearby_suggestions_are_verified():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "D"
    at.session_state["start_date"] = date(2026, 11, 14)
    at.session_state["allow_ai"] = True
    at.session_state["favourites"] = [
        _fav(id="a", name="太宰府天滿宮"),
        _fav(id="b", name="一蘭", name_original="一蘭", category="美食", model_category="restaurant"),
        _fav(id="c", name="福岡塔", name_original="福岡塔", category="景點", model_category="attraction"),
    ]
    at.run()
    assert "是否需要 AI 協助" in _page(at)
    assert any(button.label == "需要" for button in at.button)
    assert any(button.label == "不用，只用我的收藏" for button in at.button)
    fake = {
        "title": "福岡3日遊",
        "days": [{"day": 1, "area": "福岡", "items": [
            {
                "time": "09:00", "place_name": "太宰府天滿宮", "activity": "參拜。",
                "is_meal": False, "maps_query": "太宰府天滿宮", "transit": "",
            },
        ]}],
        "tips": [],
    }
    nearby = gemini_client.AnalysisResult([
        {
            "name": "大濠公園",
            "name_zh": "大濠公園",
            "category": "attraction",
            "area": "福岡",
            "address": "福岡市中央區大濠公園1-2",
            "opening_hours": "09:00-18:00",
            "rating": "4.4/5",
            "notes": "公園可以散步。",
        },
        {
            "name": "天神網紅餐廳",
            "name_zh": "天神網紅餐廳",
            "category": "restaurant",
            "area": "天神",
            "address": "福岡市",
            "opening_hours": "11:00-21:00",
            "rating": "4.9/5",
        },
    ], [])
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake), \
         mock.patch.object(gemini_client, "suggest_nearby", return_value=nearby) as nearby_call:
        _button(at, "開始排行程").click().run()
    assert not at.exception
    assert nearby_call.call_count == 1
    anchors = nearby_call.call_args.args[2]
    assert any("太宰府天滿宮" in (place.get("name") or "") for place in anchors)
    page = _page(at)
    assert "大濠公園" in page
    assert "天神網紅餐廳" not in page
    assert "打卡熱點" not in page
    assert page.count("Google 地圖") >= 2
    assert "maps/search" in page
    assert trip_core.NEARBY_UNVERIFIED_NOTE not in page
    saved = [item["place_name"] for item in at.session_state["itinerary"]["days"][0]["items"]]
    assert saved == ["太宰府天滿宮", "大濠公園"]

    empty = AppTest.from_file(APP, default_timeout=30)
    empty.secrets["GEMINI_API_KEY"] = "fake"
    empty.session_state["screen"] = "D"
    empty.session_state["start_date"] = date(2026, 11, 14)
    empty.session_state["allow_ai"] = True
    empty.session_state["favourites"] = [_fav()]
    empty.run()
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake), \
         mock.patch.object(
             gemini_client, "suggest_nearby",
             return_value=gemini_client.AnalysisResult([{
                 "name": "市中心打卡熱點",
                 "address": "福岡市",
                 "rating": "4.8/5",
                 "opening_hours": "10:00-18:00",
             }], []),
         ):
        _button(empty, "開始排行程").click().run()
    assert not empty.exception
    assert "市中心打卡熱點" not in _page(empty)
    assert trip_core.NEARBY_UNVERIFIED_NOTE in _page(empty)


def _joined(blocks) -> str:
    return "\n".join(block.value for block in blocks)


def test_share_query_does_not_open_another_members_trip(monkeypatch, memory_supabase):
    holder = {"sub": "user-a"}
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: holder["sub"])
    created = trip_store.create_trip()
    item_id = "ab" * 16
    trip_store.save_trip({
        "trip_id": created["id"],
        "version": created["version"],
        "title": "甲的行程",
        "inbox": [{
            "id": item_id,
            "label": "截圖 1",
            "remark": "",
            "status": "未排程",
            "analysis": "done",
            "places": [{"name": "秘密拉麵", "name_zh": "秘密拉麵"}],
            "added_by": "Ada",
            "image": b"\xff\xd8secret",
        }],
        "favourites": [],
        "itinerary": None,
    })
    holder["sub"] = "user-b"
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.query_params["share"] = "abc123def456"
    at.session_state["trip_id"] = created["id"]
    at.session_state["screen"] = "B"
    at.run()
    assert not at.exception
    assert "秘密拉麵" not in _page(at)
    assert at.session_state.get("trip_id") != created["id"]
    assert at.query_params.get("share") == "abc123def456"
    assert "share_token" not in at.session_state
    assert [row for row in memory_supabase.rows if row.get("owner_sub") == "user-b"] == []
    path = f"user-a/{created['id']}/{item_id}.jpg"
    assert memory_supabase.objects[path] == b"\xff\xd8secret"
    holder["sub"] = "user-a"
    loaded = trip_store.load_trip(created["id"])
    assert loaded["inbox"][0]["places"][0]["name"] == "秘密拉麵"


def test_trip_switcher_stops_at_the_configured_cap(monkeypatch, memory_supabase):
    monkeypatch.setenv("MAX_TRIPS_PER_USER", "2")
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    assert not at.exception
    assert memory_supabase.inserts == []
    _open_screen(at, "旅行夾")
    assert "你仲未有行程" in _page(at)
    assert any(button.label == "建立新旅行夾" for button in at.button)
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    assert len(memory_supabase.inserts) == 1
    assert any("行程 1" in button.label for button in at.button)
    assert any(button.label == "建立新旅行夾" for button in at.button)
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    labels = [button.label for button in at.button]
    assert any(label == "行程 1" or label.startswith("行程 1") for label in labels)
    assert any("行程 2" in label for label in labels)
    assert "建立新旅行夾" not in labels
    assert "每位會員最多可儲存 2 個行程" in _page(at)
    assert len(memory_supabase.inserts) == 2


def test_image_cap_stops_extra_uploads(monkeypatch):
    monkeypatch.setenv("MAX_IMAGES_PER_USER", "1")
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.run()
    uploader = at.file_uploader[0]
    uploader.upload("a.png", _png("red"), "image/png")
    uploader.upload("b.png", _png("blue"), "image/png").run()
    assert not at.exception
    assert len(at.session_state["inbox"]) == 1
    assert "每位會員最多可儲存 1 張圖片" in _joined(at.warning)


def test_missing_supabase_config_stops_without_local_fallback(monkeypatch, memory_supabase, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setattr(trip_store, "_lookup_secret", lambda _path: None)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AssertionError("should not connect")

    monkeypatch.setattr(trip_store, "_connect", boom)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.query_params["share"] = "abc123def456"
    at.run()
    assert not at.exception
    text = _joined(at.error)
    assert "行程儲存未設定好" in text
    assert "test-service-role-key" not in text
    assert "abc123def456" not in text
    labels = [button.label for button in at.button]
    assert "開始收集想去的地方" not in labels
    assert calls["n"] == 0
    assert memory_supabase.rows == []
    assert memory_supabase.uploads == []
    assert not (tmp_path / "data").exists()


def test_supabase_secrets_are_enough_and_stay_off_the_page(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("MAX_TRIPS_PER_USER", raising=False)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.secrets["supabase"] = {
        "url": "https://from-secrets.supabase.co",
        "service_role_key": "secret-value-from-secrets",
    }
    at.secrets["limits"] = {"max_trips_per_user": 1, "max_images_per_user": 1}
    at.run()
    assert not at.exception
    assert "行程儲存未設定好" not in _joined(at.error)
    _open_screen(at, "旅行夾")
    assert any(button.label == "建立新旅行夾" for button in at.button)
    assert not any("行程 1" in button.label for button in at.button)
    blob = _page(at) + "\n" + _joined(list(at.error) + list(at.warning) + list(at.caption))
    assert "secret-value-from-secrets" not in blob
    assert "test-service-role-key" not in blob
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    assert any("行程 1" in button.label for button in at.button)
    assert not any(button.label == "建立新旅行夾" for button in at.button)
    blob = _page(at) + "\n" + _joined(list(at.error) + list(at.warning) + list(at.caption) + list(at.info) + list(at.success))
    assert "secret-value-from-secrets" not in blob
    assert "test-service-role-key" not in blob


def test_connection_failure_refuses_without_leaking_or_local_files(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def boom():
        raise RuntimeError("connect apikey=super-secret-token https://db.internal")

    monkeypatch.setattr(trip_store, "_connect", boom)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.query_params["share"] = "abc123def456"
    at.run()
    assert not at.exception
    text = _joined(at.error)
    assert "暫時連不到行程儲存" in text
    assert "super-secret-token" not in text
    assert "db.internal" not in text
    assert "開始收集想去的地方" not in [button.label for button in at.button]
    assert not (tmp_path / "data").exists()


_UNSAVED_NOTICE = "尚未儲存。按「建立新旅行夾」之後先會保留。"


def _feedback(at: AppTest) -> str:
    blocks = list(at.error) + list(at.warning) + list(at.info) + list(at.success) + list(at.caption)
    return "\n".join(block.value for block in blocks)


def _saved_messages(text: str) -> bool:
    return "已儲存" in text or "儲存成功" in text or "已保存" in text


def test_new_account_load_does_not_create_a_trip(memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    for _ in range(3):
        at.run()
    assert not at.exception
    assert memory_supabase.inserts == []
    assert memory_supabase.rows == []
    assert at.session_state.get("trip_id") in ("", None)
    assert list(at.session_state.get("trip_catalog") or []) == []
    assert "開始收集想去的地方" in [button.label for button in at.button]
    assert not any(button.label == "建立新旅行夾" for button in at.button)
    _open_screen(at, "旅行夾")
    assert "你仲未有行程" in _page(at)
    assert any(button.label == "建立新旅行夾" for button in at.button)
    assert not any("行程 1" in button.label for button in at.button)
    assert not list(at.success)
    assert not _saved_messages(_feedback(at))


def test_create_trip_once_stays_one_across_reruns(memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    assert memory_supabase.inserts == []
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    assert len(memory_supabase.rows) == 1
    assert len(memory_supabase.inserts) == 1
    trip_id = at.session_state["trip_id"]
    assert trip_id
    for _ in range(3):
        at.run()
    assert not at.exception
    assert len(memory_supabase.rows) == 1
    assert len(memory_supabase.inserts) == 1
    assert at.session_state["trip_id"] == trip_id
    assert any("行程 1" in button.label for button in at.button)


def test_create_trip_replay_and_reentry_insert_once(monkeypatch, memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    at.session_state["_skip_next_create"] = True
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    assert memory_supabase.inserts == []
    assert at.session_state.get("_skip_next_create") in (False, None, "")

    real = trip_store.create_trip
    state = {"n": 0}

    def wrapped():
        state["n"] += 1
        if state["n"] > 1:
            raise AssertionError("create_trip re-entered")
        created = real()
        fn = None
        frame = inspect.currentframe()
        while frame:
            if "_create_new_trip" in frame.f_globals:
                fn = frame.f_globals["_create_new_trip"]
                break
            frame = frame.f_back
        assert fn is not None
        fn()
        return created

    monkeypatch.setattr(trip_store, "create_trip", wrapped)
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    assert state["n"] == 1
    assert len(memory_supabase.rows) == 1
    assert len(memory_supabase.inserts) == 1


def test_existing_member_loads_own_trips_unchanged(monkeypatch, memory_supabase):
    holder = {"sub": "user-a"}
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: holder["sub"])
    image = b"\xff\xd8osaka-photo"
    item_id = "cd" * 16
    first = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": first["id"],
        "version": first["version"],
        "title": "大阪",
        "inbox": [{
            "id": item_id,
            "label": "截圖 1",
            "remark": "",
            "status": "未排程",
            "analysis": "done",
            "facts": "done",
            "places": [{"name": "秘密拉麵", "name_zh": "秘密拉麵"}],
            "added_by": "旅人",
            "image": image,
        }],
        "favourites": [_fav(id="fav-osaka", name="秘密拉麵")],
        "members": ["旅人"],
        "itinerary": None,
    })
    second = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": second["id"],
        "version": second["version"],
        "title": "京都",
        "inbox": [],
        "favourites": [_fav(id="fav-kyoto", name="清水寺")],
        "members": ["旅人"],
        "itinerary": None,
    })
    before = {
        row["id"]: json.dumps(row["payload"], ensure_ascii=False, sort_keys=True)
        for row in memory_supabase.rows
    }
    stamps = {row["id"]: row["updated_at"] for row in memory_supabase.rows}
    inserts_before = len(memory_supabase.inserts)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["trip_id"] = second["id"]
    at.session_state["screen"] = "C"
    at.run()
    for _ in range(2):
        at.run()
    assert not at.exception
    assert len(memory_supabase.inserts) == inserts_before
    assert at.session_state["trip_id"] == second["id"]
    assert "清水寺" in _page(at)
    assert "秘密拉麵" not in _page(at)
    assert not any("建立新旅行夾" == button.label for button in at.button)
    _open_screen(at, "旅行夾")
    labels = [button.label for button in at.button]
    assert any(label.startswith("京都") and "目前" in label for label in labels)
    assert any(label == "大阪" or label.startswith("大阪") for label in labels)
    after = {
        row["id"]: json.dumps(row["payload"], ensure_ascii=False, sort_keys=True)
        for row in memory_supabase.rows
    }
    assert after == before
    assert {row["id"]: row["updated_at"] for row in memory_supabase.rows} == stamps
    assert memory_supabase.objects[f"user-a/{first['id']}/{item_id}.jpg"] == image
    assert not list(at.success)


def _inbox_rows(memory_supabase):
    return [
        row for row in memory_supabase.rows
        if (row.get("payload") or {}).get("kind") == "inbox"
    ]


def test_collection_inbox_saves_without_a_trip_and_adopt_clears_it(monkeypatch, memory_supabase):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "test-member-sub")
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "B"
    at.run()
    for _ in range(2):
        at.run()
    assert not at.exception
    assert memory_supabase.inserts == []
    assert trip_store.list_trips() == []
    with mock.patch.object(gemini_client, "recognize_names", side_effect=AssertionError("no analyze")):
        at.file_uploader[0].upload("place.png", _png("red"), "image/png").run()
    assert not at.exception
    assert len(_inbox_rows(memory_supabase)) == 1
    assert trip_store.list_trips() == []
    assert "你仲未有行程" not in _page(at)
    assert not any("行程 1" in button.label for button in at.button)
    _open_screen(at, "旅行夾")
    assert "你仲未有行程" in _page(at)
    _open_screen(at, "收集")
    assert _UNSAVED_NOTICE not in _feedback(at)
    assert not list(at.success)
    assert not _saved_messages(_feedback(at))
    image = at.session_state["inbox"][0]["image"]
    item_id = at.session_state["inbox"][0]["id"]
    inbox_id = _inbox_rows(memory_supabase)[0]["id"]
    assert memory_supabase.objects[f"test-member-sub/{inbox_id}/{item_id}.jpg"] == image
    for _ in range(2):
        at.run()
    assert len(_inbox_rows(memory_supabase)) == 1
    assert len(trip_store.list_trips()) == 0

    fresh = AppTest.from_file(APP, default_timeout=30)
    fresh.secrets["GEMINI_API_KEY"] = "fake"
    fresh.session_state["screen"] = "B"
    fresh.run()
    assert not fresh.exception
    assert fresh.session_state["inbox"][0]["image"] == image
    assert fresh.session_state.get("trip_id") in ("", None)
    assert len(_inbox_rows(memory_supabase)) == 1
    assert _UNSAVED_NOTICE not in _feedback(fresh)
    _button(fresh, "查看我的收藏 →").click().run()
    assert fresh.session_state["screen"] == "C"
    _open_screen(fresh, "旅行夾")
    _button(fresh, "建立新旅行夾").click().run()
    assert not fresh.exception
    assert _inbox_rows(memory_supabase) == []
    assert len(trip_store.list_trips()) == 1
    trip_id = fresh.session_state["trip_id"]
    assert trip_id != inbox_id
    loaded = trip_store.load_trip(trip_id)
    assert loaded.get("kind") != "inbox"
    assert loaded["inbox"][0]["image"] == image
    assert memory_supabase.objects[f"test-member-sub/{trip_id}/{item_id}.jpg"] == image
    assert f"test-member-sub/{inbox_id}/{item_id}.jpg" not in memory_supabase.objects
    assert not list(fresh.success)
    assert not _saved_messages(_feedback(fresh))
    for _ in range(2):
        fresh.run()
    assert len(trip_store.list_trips()) == 1
    assert _inbox_rows(memory_supabase) == []


def test_inbox_save_failure_is_an_error_not_success(monkeypatch, memory_supabase):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "test-member-sub")

    def boom(_snapshot):
        raise trip_store.TripStoreError("行程內容未能儲存，請稍後再試。")

    monkeypatch.setattr(trip_store, "save_collection_inbox", boom)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "C"
    at.session_state["favourites"] = [_fav(name="秘密拉麵")]
    at.session_state["inbox"] = [{
        "id": "ef" * 16,
        "label": "截圖 1",
        "image": b"\xff\xd8pending-photo",
        "remark": "",
        "status": "未排程",
        "analysis": "done",
        "facts": "done",
        "places": [{"name": "秘密拉麵", "name_zh": "秘密拉麵"}],
        "added_by": "旅人",
    }]
    at.run()
    assert not at.exception
    text = _feedback(at)
    assert "行程內容未能儲存，請稍後再試。" in _joined(at.error)
    assert _UNSAVED_NOTICE not in text
    assert not list(at.success)
    assert not _saved_messages(text)
    assert at.session_state["favourites"][0]["name"] == "秘密拉麵"
    assert at.session_state["inbox"][0]["image"] == b"\xff\xd8pending-photo"
    assert memory_supabase.rows == []
    assert memory_supabase.objects == {}
    assert at.session_state.get("trip_id") in ("", None)
    assert "你仲未有行程" not in _page(at)
    assert any("我的收藏" in block.value for block in at.subheader)


def test_cross_user_load_does_not_create_or_leak(monkeypatch, memory_supabase):
    holder = {"sub": "user-a"}
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: holder["sub"])
    image = b"\xff\xd8user-a-only"
    item_id = "aa" * 16
    created = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": created["id"],
        "version": created["version"],
        "title": "甲的行程",
        "inbox": [{
            "id": item_id,
            "label": "截圖 1",
            "remark": "",
            "status": "未排程",
            "analysis": "done",
            "places": [{"name": "秘密拉麵", "name_zh": "秘密拉麵"}],
            "added_by": "Ada",
            "image": image,
        }],
        "favourites": [_fav(id="secret", name="秘密拉麵")],
        "itinerary": None,
    })
    before = json.dumps(memory_supabase.rows[0]["payload"], ensure_ascii=False, sort_keys=True)
    stamp = memory_supabase.rows[0]["updated_at"]
    holder["sub"] = "user-b"
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "C"
    at.session_state["trip_id"] = created["id"]
    at.run()
    for _ in range(2):
        at.run()
    assert not at.exception
    assert [row for row in memory_supabase.rows if row.get("owner_sub") == "user-b"] == []
    assert at.session_state.get("trip_id") in ("", None)
    assert "秘密拉麵" not in _page(at)
    assert "你仲未有行程" not in _page(at)
    _open_screen(at, "旅行夾")
    assert "你仲未有行程" in _page(at)
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    own = [row for row in memory_supabase.rows if row.get("owner_sub") == "user-b"]
    assert len(own) == 1
    blob = json.dumps(own[0]["payload"], ensure_ascii=False)
    assert "秘密拉麵" not in blob
    assert at.session_state["trip_id"] == own[0]["id"]
    assert at.session_state["trip_id"] != created["id"]
    holder["sub"] = "user-a"
    kept = next(row for row in memory_supabase.rows if row["id"] == created["id"])
    assert kept["updated_at"] == stamp
    assert json.dumps(kept["payload"], ensure_ascii=False, sort_keys=True) == before
    assert memory_supabase.objects[f"user-a/{created['id']}/{item_id}.jpg"] == image
    loaded = trip_store.load_trip(created["id"])
    assert loaded["inbox"][0]["places"][0]["name"] == "秘密拉麵"


def test_cleanup_failure_switches_to_the_new_trip_without_asking_again(monkeypatch, memory_supabase, caplog):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "test-member-sub")
    image = b"\xff\xd8cleanup-pending"
    item_id = "ab" * 8
    trip_store.save_collection_inbox({
        "title": "收藏",
        "inbox": [{
            "id": item_id,
            "label": "截圖 1",
            "remark": "",
            "status": "未排程",
            "analysis": "done",
            "facts": "done",
            "places": [{"name": "嚴島神社", "name_zh": "嚴島神社"}],
            "added_by": "旅人",
            "image": image,
        }],
        "favourites": [{"id": "fav", "name": "嚴島神社"}],
        "itinerary": None,
    })
    inbox_id = trip_store.collection_inbox_id()
    real_remove = trip_store._remove_owned

    def flaky(owner, paths):
        if any(inbox_id in path for path in paths):
            raise trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE)
        return real_remove(owner, paths)

    monkeypatch.setattr(trip_store, "_remove_owned", flaky)
    caplog.set_level("WARNING", logger="lazy_trip_planner.store")
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "C"
    at.run()
    assert not at.exception
    assert at.session_state.get("trip_id") in ("", None)
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    assert not at.exception
    trip_id = at.session_state["trip_id"]
    assert trip_id and trip_id != inbox_id
    assert len(trip_store.list_trips()) == 1
    loaded = trip_store.load_trip(trip_id)
    assert loaded["inbox"][0]["image"] == image
    assert loaded["inbox"][0]["id"] == item_id
    warning = _joined(at.warning)
    assert "新行程已儲存。舊收集匣暫時未清理，唔使再新增。" in warning
    feedback = _feedback(at)
    assert "請再試一次" not in feedback
    assert "收藏未能完成整理" not in feedback
    assert "請再試一次" not in _joined(at.error)
    pending_logs = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("Collection inbox cleanup pending")
    ]
    assert any(
        message == f"Collection inbox cleanup pending inbox={inbox_id} trip={trip_id} (TripStoreError)"
        for message in pending_logs
    )
    assert all("test-member-sub" not in message for message in pending_logs)
    assert all(image.hex() not in message for message in pending_logs)

    item = at.session_state["inbox"][0]
    item["remark"] = "改過備註"
    at.session_state[f"remark_{item['id']}"] = "改過備註"
    at.session_state["last_digest"] = ""
    at.run()
    assert trip_store.load_trip(trip_id)["inbox"][0]["remark"] == "改過備註"
    edited_stamp = trip_store.load_trip(trip_id)["updated_at"]
    _button(at, "建立新旅行夾").click().run()
    assert len(trip_store.list_trips()) == 1
    assert at.session_state["trip_id"] == trip_id
    kept = trip_store.load_trip(trip_id)
    assert kept["inbox"][0]["remark"] == "改過備註"
    assert kept["inbox"][0]["image"] == image
    assert kept["updated_at"] == edited_stamp
    at.run()
    assert len(trip_store.list_trips()) == 1
    assert trip_store.load_trip(trip_id)["inbox"][0]["remark"] == "改過備註"
    assert trip_store.load_trip(trip_id)["updated_at"] == edited_stamp
    assert "請再試一次" not in _feedback(at)

    before_objects = dict(memory_supabase.objects)
    fresh = AppTest.from_file(APP, default_timeout=30)
    fresh.secrets["GEMINI_API_KEY"] = "fake"
    fresh.session_state["screen"] = "C"
    fresh.run()
    assert not fresh.exception
    assert fresh.session_state["trip_id"] == trip_id
    assert fresh.session_state.get("collection_inbox_id") in ("", None)
    assert fresh.session_state["inbox"][0]["remark"] == "改過備註"
    assert fresh.session_state["inbox"][0]["image"] == image
    assert [row["id"] for row in trip_store.list_trips()] == [trip_id]
    assert trip_store.collection_inbox_id() == inbox_id
    assert inbox_id not in [row["id"] for row in fresh.session_state.get("trip_catalog") or []]
    assert "你仲未有行程" not in _page(fresh)
    assert memory_supabase.objects == before_objects
    assert len([row for row in memory_supabase.rows if row.get("id") != inbox_id]) == 1

    monkeypatch.setattr(trip_store, "_remove_owned", real_remove)
    trip_store.release_adopted_collection_inbox()
    assert trip_store.load_collection_inbox() is None
    assert trip_store.has_pending_collection_inbox() is False
    final = trip_store.load_trip(trip_id)
    assert final["inbox"][0]["remark"] == "改過備註"
    assert final["inbox"][0]["image"] == image
    assert len(trip_store.list_trips()) == 1


def test_remote_edit_keeps_unsaved_local_until_reload(monkeypatch, memory_supabase):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "user-a")
    image = b"\xff\xd8keep-me"
    item_id = "ab" * 8
    created = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": created["id"],
        "version": created["version"],
        "title": "大阪",
        "inbox": [{
            "id": item_id,
            "label": "截圖 1",
            "remark": "",
            "status": "未排程",
            "analysis": "done",
            "facts": "done",
            "places": [{"name": "秘密拉麵", "name_zh": "秘密拉麵"}],
            "added_by": "旅人",
            "image": image,
        }],
        "favourites": [_fav(id="fav-osaka", name="秘密拉麵")],
        "members": ["旅人"],
        "itinerary": None,
    })
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["trip_id"] = created["id"]
    at.session_state["screen"] = "C"
    at.run()
    assert not at.exception
    assert at.session_state["trip_title"] == "大阪"
    row = memory_supabase.rows[0]
    payload = dict(row["payload"])
    payload["title"] = "遠端已改"
    row["payload"] = payload
    row["version"] = int(row["version"]) + 1
    row["updated_at"] = f"{row['updated_at']}-remote"
    at.session_state["trip_title"] = "我未儲存"
    at.session_state["last_digest"] = "local-dirty"
    at.run()
    assert not at.exception
    assert at.session_state["trip_title"] == "我未儲存"
    assert at.session_state["inbox"][0]["image"] == image
    assert _CONFLICT_BANNER in _joined(at.error)
    assert any(button.label == "重新載入已儲存的行程" for button in at.button)
    assert trip_store.load_trip(created["id"])["title"] == "遠端已改"
    assert memory_supabase.objects[f"user-a/{created['id']}/{item_id}.jpg"] == image
    at.run()
    assert at.session_state["trip_title"] == "我未儲存"
    assert trip_store.load_trip(created["id"])["title"] == "遠端已改"
    _button(at, "重新載入已儲存的行程").click().run()
    assert not at.exception
    assert at.session_state["trip_title"] == "遠端已改"
    assert not at.session_state.get("trip_conflict")
    assert trip_store.load_trip(created["id"])["title"] == "遠端已改"
    assert memory_supabase.objects[f"user-a/{created['id']}/{item_id}.jpg"] == image


def test_invite_opens_shared_workspace_and_dates_persist(monkeypatch, memory_supabase):
    holder = {"sub": "owner-sub"}
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: holder["sub"])
    created = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": created["id"],
        "version": created["version"],
        "title": "福岡旅行",
        "inbox": [],
        "favourites": [_fav(name="太宰府天滿宮")],
        "itinerary": None,
    })
    invite = trip_store.create_invite(created["id"])
    holder["sub"] = "member-sub"
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.query_params["invite"] = invite["token"]
    at.session_state["display_name"] = "朋友 A"
    at.run()
    assert not at.exception
    assert at.session_state["trip_id"] == created["id"]
    assert at.session_state["screen"] == "F"
    assert _nav(at).value == "旅行夾"
    page = _page(at)
    assert "福岡旅行" in page
    assert "共同旅行" in page
    assert "member-sub" not in page
    assert "owner-sub" not in page
    assert "lazy-trip-planner.app" not in page
    labels = [button.label for button in at.button]
    assert "前往收集" in labels
    assert "前往收藏" in labels
    assert "前往行程" in labels
    assert "撤銷邀請連結" not in labels
    assert "刪除行程" not in labels
    _button(at, "⚙").click().run()
    labels = [button.label for button in at.button]
    assert "離開行程" in labels
    assert "邀請朋友" not in labels
    _button(at, "前往收藏").click().run()
    assert at.session_state["screen"] == "C"
    assert "太宰府天滿宮" in _page(at)

    holder["sub"] = "owner-sub"
    owner = AppTest.from_file(APP, default_timeout=30)
    owner.secrets["GEMINI_API_KEY"] = "fake"
    owner.session_state["trip_id"] = created["id"]
    owner.session_state["display_name"] = "行程主人"
    owner.run()
    assert not owner.exception
    _open_screen(owner, "旅行夾")
    _button(owner, "⚙").click().run()
    _button(owner, "邀請朋友").click().run()
    owner_labels = [button.label for button in owner.button]
    assert "撤銷邀請連結" in owner_labels or "重新產生邀請連結" in owner_labels
    assert "https://lazy-trip-planner.streamlit.app/?invite=" in _page(owner) or any(
        "重新產生邀請連結" == button.label for button in owner.button
    )
    start = [box for box in owner.text_input if box.label == "旅行開始"][0]
    start.input("2026-03-15").run()
    end = [box for box in owner.text_input if box.label == "旅行結束"][0]
    end.input("2026-03-18").run()
    assert not owner.exception
    loaded = trip_store.load_trip(created["id"])
    assert loaded["travel_start"] == "2026-03-15"
    assert loaded["travel_end"] == "2026-03-18"
    assert "4日3夜" in _page(owner)
    assert "owner-sub" not in _page(owner)


_SCREENS = ("首頁", "旅行夾", "收集", "收藏", "排程", "行程")


def test_folder_block_is_only_on_the_folder_screen_and_alerts_stay(monkeypatch):
    """旅行夾只出現喺自己嗰頁。邀請失敗同清理警告每一頁都要見到。"""
    monkeypatch.setattr(trip_store, "has_pending_collection_inbox", lambda: True)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["invite_error"] = "邀請連結無效、已過期或已撤銷。"
    at.session_state["inbox_cleanup_warning"] = "新行程已儲存。舊收集匣暫時未清理，唔使再新增。"
    at.run()
    assert not at.exception
    for label in _SCREENS:
        _open_screen(at, label)
        page = _page(at)
        errors = _joined(at.error)
        warnings = _joined(at.warning)
        assert "邀請連結無效、已過期或已撤銷。" in errors
        assert "新行程已儲存。舊收集匣暫時未清理，唔使再新增。" in warnings
        assert _nav(at).value == label
        if label == "旅行夾":
            assert "我的私人收藏" in page
            assert "共同旅行" in page
            assert any(button.label == "建立新旅行夾" for button in at.button)
        else:
            assert "我的私人收藏" not in page
            assert "建立新旅行夾" not in [button.label for button in at.button]
            assert "前往收集" not in [button.label for button in at.button]
            assert not any(box.label == "行程名稱" for box in at.text_input)
    assert at.session_state["nav_chip"] == "行程"
    assert at.session_state["screen"] == "E"


def test_conflict_alert_stays_on_every_screen(monkeypatch, memory_supabase):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "user-a")
    created = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": created["id"],
        "version": created["version"],
        "title": "大阪",
        "inbox": [],
        "favourites": [],
        "members": ["旅人"],
        "itinerary": None,
    })
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["trip_id"] = created["id"]
    at.session_state["screen"] = "C"
    at.run()
    row = memory_supabase.rows[0]
    payload = dict(row["payload"])
    payload["title"] = "遠端已改"
    row["payload"] = payload
    row["version"] = int(row["version"]) + 1
    at.session_state["trip_title"] = "我未儲存"
    at.session_state["last_digest"] = "local-dirty"
    at.run()
    assert _CONFLICT_BANNER in _joined(at.error)
    for label in ("首頁", "收集", "排程", "行程", "旅行夾"):
        _open_screen(at, label)
        assert _CONFLICT_BANNER in _joined(at.error)
        assert at.session_state["trip_title"] == "我未儲存"
    assert trip_store.load_trip(created["id"])["title"] == "遠端已改"


def test_saved_inputs_survive_every_screen_and_are_not_reset_to_defaults(memory_supabase):
    """離開畫面再入返，行程名、日期、顯示名、日數、風格、偏好都唔可以變回預設。

    舊程式：顯示名的 widget 只喺首頁，離開之後 session 會變返「旅人」。
    日數、風格、偏好的 widget 只喺排程，離開之後會寫低 3／平衡型／美食+景點，覆蓋已儲存行程。
    行程名同日期當時因為旅行夾每頁都畫，所以未覆寫；收埋旅行夾之後一樣會中招。
    """
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    trip_id = at.session_state["trip_id"]
    _open_screen(at, "首頁")
    [box for box in at.text_input if box.label == "你的顯示名"][0].input("阿強").run()
    assert at.session_state["display_name"] == "阿強"
    _open_screen(at, "旅行夾")
    [box for box in at.text_input if box.label == "行程名稱"][0].input("大阪行").run()
    [box for box in at.text_input if box.label == "旅行開始"][0].input("2026-03-15").run()
    [box for box in at.text_input if box.label == "旅行結束"][0].input("2026-03-18").run()
    _open_screen(at, "排程")
    [box for box in at.number_input if "日數" in box.label][0].set_value(7).run()
    [box for box in at.radio if "行程風格" in box.label][0].set_value("Chill 型").run()
    [box for box in at.pills if "類型" in box.label][0].set_value(["美食"]).run()
    assert not at.exception
    before = trip_store.load_trip(trip_id)
    assert before["title"] == "大阪行"
    assert before["travel_start"] == "2026-03-15"
    assert before["travel_end"] == "2026-03-18"
    assert before["day_count"] == 7
    assert before["trip_style"] == "Chill 型"
    assert before["pref_pills"] == ["美食"]
    assert "阿強" in before["members"]
    for label in _SCREENS:
        _open_screen(at, label)
        at.run()
        assert not at.exception
    assert at.session_state["display_name"] == "阿強"
    assert at.session_state["trip_title"] == "大阪行"
    assert at.session_state["travel_start"] == "2026-03-15"
    assert at.session_state["travel_end"] == "2026-03-18"
    assert at.session_state["day_count"] == 7
    assert at.session_state["trip_style"] == "Chill 型"
    assert list(at.session_state["pref_pills"]) == ["美食"]
    saved = trip_store.load_trip(trip_id)
    assert saved["title"] == "大阪行"
    assert saved["travel_start"] == "2026-03-15"
    assert saved["travel_end"] == "2026-03-18"
    assert saved["day_count"] == 7
    assert saved["trip_style"] == "Chill 型"
    assert saved["pref_pills"] == ["美食"]
    assert "阿強" in saved["members"]
    assert saved["members"] != ["旅人"]
    _open_screen(at, "旅行夾")
    assert [box for box in at.text_input if box.label == "行程名稱"][0].value == "大阪行"
    assert [box for box in at.text_input if box.label == "旅行開始"][0].value == "2026-03-15"
    assert [box for box in at.text_input if box.label == "旅行結束"][0].value == "2026-03-18"
    _open_screen(at, "首頁")
    assert [box for box in at.text_input if box.label == "你的顯示名"][0].value == "阿強"
    _open_screen(at, "排程")
    assert [box for box in at.number_input if "日數" in box.label][0].value == 7
    assert [box for box in at.radio if "行程風格" in box.label][0].value == "Chill 型"
    assert [box for box in at.pills if "類型" in box.label][0].value == ["美食"]
    assert "目前行程：大阪行" in _page(at)


def test_switching_trips_updates_the_folder_inputs(memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    [box for box in at.text_input if box.label == "行程名稱"][0].input("大阪行").run()
    [box for box in at.text_input if box.label == "旅行開始"][0].input("2026-04-01").run()
    [box for box in at.text_input if box.label == "旅行結束"][0].input("2026-04-03").run()
    _button(at, "建立新旅行夾").click().run()
    assert at.session_state["screen"] == "F"
    assert _nav(at).value == "旅行夾"
    assert [box for box in at.text_input if box.label == "行程名稱"][0].value != "大阪行"
    _button(at, "大阪行").click().run()
    assert not at.exception
    assert at.session_state["screen"] == "F"
    assert _nav(at).value == "旅行夾"
    assert at.session_state["trip_title"] == "大阪行"
    assert at.session_state["travel_start"] == "2026-04-01"
    assert at.session_state["travel_end"] == "2026-04-03"
    assert [box for box in at.text_input if box.label == "行程名稱"][0].value == "大阪行"
    assert [box for box in at.text_input if box.label == "旅行開始"][0].value == "2026-04-01"
    assert [box for box in at.text_input if box.label == "旅行結束"][0].value == "2026-04-03"


def test_folder_actions_land_on_the_folder_screen_and_workspace_links_still_go(monkeypatch, memory_supabase):
    holder = {"sub": "owner-sub"}
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: holder["sub"])
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    assert at.session_state["screen"] == "A"
    assert "我的私人收藏" not in _page(at)
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    assert at.session_state["screen"] == "F"
    assert _nav(at).value == "旅行夾"
    _button(at, "前往收集").click().run()
    assert at.session_state["screen"] == "B"
    assert _nav(at).value == "收集"
    assert "我的私人收藏" not in _page(at)
    _open_screen(at, "旅行夾")
    _button(at, "前往收藏").click().run()
    assert at.session_state["screen"] == "C"
    assert _nav(at).value == "收藏"
    _open_screen(at, "旅行夾")
    _button(at, "前往行程").click().run()
    assert at.session_state["screen"] == "D"
    assert _nav(at).value == "排程"
    _open_screen(at, "旅行夾")
    _button(at, "⚙").click().run()
    assert at.session_state["screen"] == "F"
    assert _nav(at).value == "旅行夾"
    _button(at, "邀請朋友").click().run()
    assert at.session_state["screen"] == "F"
    assert any(block.value == "邀請朋友加入" for block in at.subheader)
    _button(at, "返回行程").click().run()
    assert at.session_state["screen"] == "F"
    assert _nav(at).value == "旅行夾"
    assert not any(block.value == "邀請朋友加入" for block in at.subheader)
    assert "前往收集" in [button.label for button in at.button]
    if not any(button.label == "刪除行程" for button in at.button):
        _button(at, "⚙").click().run()
    _button(at, "刪除行程").click().run()
    assert at.session_state["screen"] == "F"
    _button(at, "確認刪除").click().run()
    assert not at.exception
    assert at.session_state["screen"] == "F"
    assert _nav(at).value == "旅行夾"
    assert trip_store.list_trips() == []
    assert "你仲未有行程" in _page(at)

    holder["sub"] = "owner-sub"
    created = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": created["id"],
        "version": created["version"],
        "title": "共同大阪",
        "inbox": [],
        "favourites": [],
        "itinerary": None,
    })
    invite = trip_store.create_invite(created["id"])
    holder["sub"] = "member-sub"
    member = AppTest.from_file(APP, default_timeout=30)
    member.secrets["GEMINI_API_KEY"] = "fake"
    member.query_params["invite"] = invite["token"]
    member.session_state["display_name"] = "朋友 A"
    member.run()
    assert member.session_state["screen"] == "F"
    assert _nav(member).value == "旅行夾"
    holder["sub"] = "owner-sub"
    owner = AppTest.from_file(APP, default_timeout=30)
    owner.secrets["GEMINI_API_KEY"] = "fake"
    owner.session_state["trip_id"] = created["id"]
    owner.run()
    _open_screen(owner, "旅行夾")
    _button(owner, "⚙").click().run()
    remove = [button for button in owner.button if button.label.startswith("移除 ")]
    assert remove, [button.label for button in owner.button]
    remove[0].click().run()
    assert owner.session_state["screen"] == "F"
    _button(owner, "確認移除").click().run()
    assert not owner.exception
    assert owner.session_state["screen"] == "F"
    assert _nav(owner).value == "旅行夾"

    holder["sub"] = "owner-sub"
    other = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": other["id"],
        "version": other["version"],
        "title": "共同京都",
        "inbox": [],
        "favourites": [],
        "itinerary": None,
    })
    other_invite = trip_store.create_invite(other["id"])
    holder["sub"] = "member-sub"
    leaver = AppTest.from_file(APP, default_timeout=30)
    leaver.secrets["GEMINI_API_KEY"] = "fake"
    leaver.query_params["invite"] = other_invite["token"]
    leaver.session_state["display_name"] = "朋友 A"
    leaver.run()
    assert leaver.session_state["screen"] == "F"
    _button(leaver, "⚙").click().run()
    _button(leaver, "離開行程").click().run()
    assert leaver.session_state["screen"] == "F"
    _button(leaver, "確認離開").click().run()
    assert not leaver.exception
    assert leaver.session_state["screen"] == "F"
    assert _nav(leaver).value == "旅行夾"
    assert leaver.session_state.get("trip_id") != other["id"]


def test_skip_next_create_is_cleared_off_the_folder_screen(memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    at.session_state["_skip_next_create"] = True
    at.session_state["screen"] = "B"
    at.run()
    assert at.session_state.get("_skip_next_create") in (False, None, "")
    assert memory_supabase.inserts == []
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    assert len(memory_supabase.inserts) == 1
    _button(at, "建立新旅行夾").click().run()
    assert len(memory_supabase.inserts) == 2


def test_opening_private_inbox_still_goes_to_collect(monkeypatch, memory_supabase):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "test-member-sub")
    created = trip_store.create_trip()
    trip_store.save_collection_inbox({
        "title": "收藏",
        "inbox": [],
        "favourites": [_fav(name="太宰府天滿宮")],
        "itinerary": None,
    })
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["trip_id"] = created["id"]
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "私人收藏匣").click().run()
    assert not at.exception
    assert at.session_state["screen"] == "B"
    assert _nav(at).value == "收集"
    assert at.session_state.get("view_inbox") is True
    assert "我的私人收藏" not in _page(at)


def _destination_box(at: AppTest):
    found = [box for box in at.selectbox if "選擇目的地" in box.label]
    assert found, [box.label for box in at.selectbox]
    return found[0]


def _ready_plan(at: AppTest, favs: list[dict] | None = None) -> AppTest:
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "D"
    at.session_state["favourites"] = list(favs) if favs is not None else [_fav()]
    at.session_state["start_date"] = date(2026, 11, 14)
    at.session_state["allow_ai"] = True
    at.run()
    assert not at.exception
    return at


def _fake_itinerary() -> dict:
    return {"title": "測試行程", "days": [{"day": 1, "area": "", "theme": "", "items": []}], "tips": []}


def test_plan_destination_choice_is_what_the_itinerary_receives():
    at = _ready_plan(AppTest.from_file(APP, default_timeout=30))
    assert "選擇目的地／地區" in _page(at)
    assert "自動判斷：福岡" in _page(at)
    assert _destination_box(at).value == trip_core.DESTINATION_DEFAULT
    _destination_box(at).set_value(_TOKYO).run()
    assert at.session_state["destination_choice"] == _TOKYO
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=_fake_itinerary()) as gen:
        _button(at, "開始排行程").click().run()
    assert gen.call_args.args[4] == "東京"

    other = _ready_plan(AppTest.from_file(APP, default_timeout=30))
    _destination_box(other).set_value(trip_core.DESTINATION_OTHER).run()
    [box for box in other.text_input if box.label == "其他目的地"][0].input("首爾").run()
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=_fake_itinerary()) as gen:
        _button(other, "開始排行程").click().run()
    assert gen.call_args.args[4] == "首爾"


_OTHER_DESTINATION_REQUIRED = "請先填寫其他目的地，先可以開始 AI 排程。"


def test_blank_custom_destination_blocks_planning_until_text_is_entered(monkeypatch):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "test-member-sub")
    for blank in ("", "   "):
        at = AppTest.from_file(APP, default_timeout=30)
        at.secrets["GEMINI_API_KEY"] = "fake"
        at.run()
        _open_screen(at, "旅行夾")
        _button(at, "建立新旅行夾").click().run()
        _open_screen(at, "排程")
        at.session_state["favourites"] = [_fav()]
        at.session_state["start_date"] = date(2026, 11, 14)
        at.run()
        _destination_box(at).set_value(trip_core.DESTINATION_OTHER).run()
        typed = [box for box in at.text_input if box.label == "其他目的地"][0]
        if blank != (typed.value or ""):
            typed.input(blank).run()
        assert _OTHER_DESTINATION_REQUIRED in _joined(at.warning)
        submit = _button(at, "開始排行程")
        assert submit.disabled
        before = trip_store.load_trip(at.session_state["trip_id"])
        with mock.patch.object(
            gemini_client, "generate_itinerary", side_effect=AssertionError("should not plan"),
        ) as gen:
            at.run()
        assert not at.exception
        assert gen.call_count == 0
        assert at.session_state.get("itinerary") is None
        assert at.session_state["usage"] == 0
        after = trip_store.load_trip(at.session_state["trip_id"])
        assert after["version"] == before["version"]
        assert after.get("itinerary") == before.get("itinerary")
        assert after.get("updated_at") == before.get("updated_at")

    ready = _ready_plan(AppTest.from_file(APP, default_timeout=30))
    _destination_box(ready).set_value(trip_core.DESTINATION_OTHER).run()
    [box for box in ready.text_input if box.label == "其他目的地"][0].input("  韓國首爾  ").run()
    assert _OTHER_DESTINATION_REQUIRED not in _joined(ready.warning)
    assert not _button(ready, "開始排行程").disabled
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=_fake_itinerary()) as gen:
        _button(ready, "開始排行程").click().run()
    assert gen.call_args.args[4] == "韓國首爾"


def test_explicit_destination_survives_every_screen_and_is_saved():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _open_screen(at, "排程")
    _destination_box(at).set_value(_TOKYO).run()
    for label in _SCREENS:
        _open_screen(at, label)
        at.run()
    assert at.session_state["destination_choice"] == _TOKYO
    loaded = trip_store.load_trip(at.session_state["trip_id"])
    assert loaded["destination_choice"] == _TOKYO
    assert loaded["destination_choice"] != trip_core.DESTINATION_DEFAULT
    _open_screen(at, "排程")
    assert _destination_box(at).value == _TOKYO


def test_switching_trips_restores_each_saved_destination():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _open_screen(at, "排程")
    _destination_box(at).set_value(_TOKYO).run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _open_screen(at, "排程")
    assert _destination_box(at).value == trip_core.DESTINATION_DEFAULT
    _open_screen(at, "旅行夾")
    _button(at, "行程 1").click().run()
    _open_screen(at, "排程")
    assert at.session_state["destination_choice"] == _TOKYO
    assert _destination_box(at).value == _TOKYO


def test_old_payload_without_a_destination_stays_on_auto(monkeypatch):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "test-member-sub")
    created = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": created["id"],
        "version": created["version"],
        "title": "舊行程",
        "inbox": [],
        "favourites": [_fav()],
        "itinerary": None,
    })
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["trip_id"] = created["id"]
    at.session_state["screen"] = "D"
    at.session_state["start_date"] = date(2026, 11, 14)
    at.run()
    assert _destination_box(at).value == trip_core.DESTINATION_DEFAULT
    assert "自動判斷：福岡" in _page(at)
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=_fake_itinerary()) as gen:
        _button(at, "開始排行程").click().run()
    assert gen.call_args.args[4] == "福岡"

    stored = trip_store.create_trip()
    trip_store.save_trip({
        "trip_id": stored["id"],
        "version": stored["version"],
        "title": "寫明預設",
        "destination_choice": trip_core.DESTINATION_DEFAULT,
        "inbox": [],
        "favourites": [_fav()],
        "itinerary": None,
    })
    again = AppTest.from_file(APP, default_timeout=30)
    again.secrets["GEMINI_API_KEY"] = "fake"
    again.session_state["trip_id"] = stored["id"]
    again.session_state["screen"] = "D"
    again.session_state["start_date"] = date(2026, 11, 14)
    again.run()
    assert again.session_state["destination_choice"] == trip_core.DESTINATION_DEFAULT
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=_fake_itinerary()) as gen:
        _button(again, "開始排行程").click().run()
    assert gen.call_args.args[4] == "福岡"


def test_second_session_edit_shows_the_new_banner_and_is_not_saved(monkeypatch):
    monkeypatch.setattr(auth_gate, "get_member_id", lambda: "test-member-sub")
    first = AppTest.from_file(APP, default_timeout=30)
    first.secrets["GEMINI_API_KEY"] = "fake"
    first.run()
    _open_screen(first, "旅行夾")
    _button(first, "建立新旅行夾").click().run()
    trip_id = first.session_state["trip_id"]
    _open_screen(first, "排程")
    [box for box in first.number_input if "日數" in box.label][0].set_value(6).run()
    assert not any(_CONFLICT_BANNER in error.value for error in first.error)
    other = AppTest.from_file(APP, default_timeout=30)
    other.secrets["GEMINI_API_KEY"] = "fake"
    other.session_state["trip_id"] = trip_id
    other.session_state["screen"] = "D"
    other.run()
    [box for box in first.number_input if "日數" in box.label][0].set_value(8).run()
    [box for box in other.radio if "行程風格" in box.label][0].set_value("Chill 型").run()
    assert _CONFLICT_BANNER in _joined(other.error)
    assert _CONFLICT_RELOAD_NOTE in _page(other)
    assert other.session_state["trip_style"] == "Chill 型"
    loaded = trip_store.load_trip(trip_id)
    assert loaded["day_count"] == 8
    assert loaded["trip_style"] != "Chill 型"
    [box for box in other.number_input if "日數" in box.label][0].set_value(9).run()
    assert _CONFLICT_BANNER in _joined(other.error)
    assert trip_store.load_trip(trip_id)["day_count"] == 8
    with pytest.raises(trip_store.TripConflictError) as caught:
        trip_store.save_trip({
            "trip_id": trip_id,
            "version": 1,
            "title": "唔好寫",
            "inbox": [],
            "favourites": [],
            "itinerary": None,
        })
    assert caught.value.args[0] == trip_store.MSG_CONFLICT


def _unsaved_text(at: AppTest) -> str:
    return _joined(at.error) + "\n" + _page(at)


_GENERATED_TITLE = "福岡9日網紅打卡極速快閃遊"


def _generated_itinerary() -> dict:
    return {
        "title": _GENERATED_TITLE,
        "days": [{"day": 1, "area": "福岡", "theme": "", "items": [
            {"time": "09:00", "place_name": "太宰府天滿宮", "activity": "參拜",
             "is_meal": False, "maps_query": "太宰府天滿宮", "transit": ""},
        ]}],
        "tips": [],
    }


def _plan_with_trip(at: AppTest) -> AppTest:
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _open_screen(at, "排程")
    at.session_state["favourites"] = [_fav()]
    at.session_state["start_date"] = date(2026, 11, 14)
    at.run()
    assert not _button(at, "開始排行程").disabled
    return at


def _mock_plan(gen):
    return mock.patch.object(gemini_client, "make_client", return_value=object()), \
        mock.patch.object(gemini_client, "generate_itinerary", side_effect=gen), \
        mock.patch.object(gemini_client, "suggest_nearby", return_value=gemini_client.AnalysisResult([], []))


def _nine_day_itinerary() -> dict:
    one = _generated_itinerary()
    stop = one["days"][0]["items"]
    one["days"] = [
        {"day": day, "area": "福岡", "theme": "打卡", "items": stop}
        for day in range(1, 10)
    ]
    return one


def test_single_session_generation_saves_before_opening_results():
    at = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))
    patches = _mock_plan(lambda *args, **kwargs: _nine_day_itinerary())
    with patches[0], patches[1] as gen, patches[2]:
        _button(at, "開始排行程").click().run()
    assert gen.call_count == 1
    assert not at.exception
    assert at.session_state["screen"] == "E"
    assert not at.session_state.get("trip_conflict")
    assert not at.session_state.get("itinerary_unsaved")
    assert "尚未儲存" not in _unsaved_text(at)
    assert _CONFLICT_BANNER not in _joined(at.error)
    loaded = trip_store.load_trip(at.session_state["trip_id"])
    assert loaded["itinerary"]["title"] == _GENERATED_TITLE
    assert len(loaded["itinerary"]["days"]) == 9
    assert at.session_state["seen_version"] == loaded["version"]
    assert "再試儲存" not in [button.label for button in at.button]


def test_generation_conflict_keeps_itinerary_and_does_not_overwrite(memory_supabase):
    at = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))
    trip_id = at.session_state["trip_id"]

    def other_writer(*_args, **_kwargs):
        row = next(item for item in memory_supabase.rows if item["id"] == trip_id)
        row["version"] = int(row["version"]) + 1
        payload = dict(row["payload"])
        payload["day_count"] = 2
        payload["itinerary"] = None
        row["payload"] = payload
        row["updated_at"] = str(row.get("updated_at") or "") + "X"
        return _generated_itinerary()

    patches = _mock_plan(other_writer)
    with patches[0], patches[1], patches[2]:
        _button(at, "開始排行程").click().run()
    assert not at.exception
    assert at.session_state["screen"] == "D"
    assert at.session_state.get("trip_conflict")
    assert at.session_state.get("itinerary_unsaved")
    assert at.session_state["itinerary"]["title"] == _GENERATED_TITLE
    assert "（SAVE-CONFLICT）" in _joined(at.error)
    assert "另一個分頁、工作階段或成員" in _unsaved_text(at)
    assert _CONFLICT_BANNER in _joined(at.error)
    assert "再試儲存" not in [button.label for button in at.button]
    loaded = trip_store.load_trip(trip_id)
    assert loaded.get("itinerary") in (None, {})
    assert loaded["day_count"] == 2
    _open_screen(at, "行程")
    assert at.session_state["itinerary"]["title"] == _GENERATED_TITLE
    assert "（SAVE-CONFLICT）" in _joined(at.error)
    assert loaded.get("itinerary") in (None, {})


def test_generation_save_error_keeps_itinerary_without_pretending_saved(monkeypatch):
    at = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))
    trip_id = at.session_state["trip_id"]
    before = trip_store.load_trip(trip_id)
    real = trip_store.save_trip

    def fail_once(snapshot):
        fail_once.calls += 1
        raise trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE, reason="unavailable")

    fail_once.calls = 0
    monkeypatch.setattr(trip_store, "save_trip", fail_once)
    patches = _mock_plan(lambda *_args, **_kwargs: _generated_itinerary())
    with patches[0], patches[1], patches[2]:
        _button(at, "開始排行程").click().run()
    assert not at.exception
    assert fail_once.calls == 1
    assert at.session_state["screen"] == "D"
    assert at.session_state["itinerary"]["title"] == _GENERATED_TITLE
    assert at.session_state.get("itinerary_unsaved")
    assert "（SAVE-NET）" in _joined(at.error)
    assert "連線、逾時或伺服器錯誤" in _unsaved_text(at)
    assert "另一個分頁" not in _unsaved_text(at)
    assert "再試儲存" not in [button.label for button in at.button]
    monkeypatch.setattr(trip_store, "save_trip", real)
    loaded = trip_store.load_trip(trip_id)
    assert loaded.get("itinerary") == before.get("itinerary")
    assert loaded["version"] == before["version"]


def _assert_save_code(at: AppTest, code: str, *, conflict_words: bool) -> None:
    text = _unsaved_text(at)
    assert f"（{code}）" in _joined(at.error)
    assert "AI 行程已生成，但尚未儲存" in _joined(at.error)
    if conflict_words:
        assert "另一個分頁、工作階段或成員" in text
    else:
        assert "另一個分頁" not in text
    assert "再試儲存" not in [button.label for button in at.button]
    assert at.session_state["screen"] == "D"
    assert at.session_state["itinerary"]["title"] == _GENERATED_TITLE


def test_each_save_failure_code_is_shown_without_guessing_conflict(monkeypatch):
    monkeypatch.setenv("MAX_TRIPS_PER_USER", "20")
    cases = [
        (trip_store.TripStoreError(trip_store.MSG_CONTENT), "SAVE-CONTENT", False),
        (trip_store.TripStoreError(trip_store.MSG_DENIED), "SAVE-DENIED", False),
        (
            trip_store.TripStoreError(
                trip_store.MSG_UNAVAILABLE,
                reason="update0",
                expected_version=4,
                remote_version=4,
            ),
            "SAVE-UPDATE0",
            False,
        ),
        (
            trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE, reason="unavailable"),
            "SAVE-NET",
            False,
        ),
        (trip_store.TripStoreError("未能歸類的儲存問題"), "SAVE-UNKNOWN", False),
    ]
    real_save = trip_store.save_trip
    for exc, code, conflict_words in cases:
        at = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))

        def fail(_snapshot, exc=exc):
            raise exc

        monkeypatch.setattr(trip_store, "save_trip", fail)
        patches = _mock_plan(lambda *_args, **_kwargs: _generated_itinerary())
        with patches[0], patches[1], patches[2]:
            _button(at, "開始排行程").click().run()
        assert not at.exception
        _assert_save_code(at, code, conflict_words=conflict_words)
        monkeypatch.setattr(trip_store, "save_trip", real_save)


def test_already_conflict_and_missing_trip_use_their_own_codes():
    already = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))

    def mark_conflict(*_args, **_kwargs):
        import streamlit as st
        st.session_state.trip_conflict = True
        return _generated_itinerary()

    patches = _mock_plan(mark_conflict)
    with patches[0], patches[1], patches[2]:
        _button(already, "開始排行程").click().run()
    _assert_save_code(already, "SAVE-ALREADY", conflict_words=True)
    assert "已經有未解決的版本衝突" in _unsaved_text(already)
    assert already.session_state.get("trip_conflict")

    missing = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))

    def drop_trip(*_args, **_kwargs):
        import streamlit as st
        st.session_state.trip_id = ""
        st.session_state._inbox_save_inflight = True
        return _generated_itinerary()

    patches = _mock_plan(drop_trip)
    with patches[0], patches[1], patches[2]:
        _button(missing, "開始排行程").click().run()
    _assert_save_code(missing, "SAVE-STATE", conflict_words=False)


def test_save_failure_log_has_codes_and_no_sensitive_data(caplog):
    at = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))
    trip_id = at.session_state["trip_id"]
    at.session_state["favourites"] = [_fav(name="NameMarker顯示名", added_by="mail-marker@example.com")]
    at.session_state["display_name"] = "NameMarker顯示名"

    def fail(snapshot):
        assert snapshot.get("trip_id") == trip_id
        raise trip_store.TripStoreError(trip_store.MSG_CONTENT)

    caplog.set_level("WARNING", logger="lazy_trip_planner")
    with mock.patch.object(trip_store, "save_trip", side_effect=fail):
        patches = _mock_plan(lambda *_args, **_kwargs: {
            **_generated_itinerary(),
            "title": "TitleMarker行程標題",
        })
        with patches[0], patches[1], patches[2]:
            _button(at, "開始排行程").click().run()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "save_failed code=SAVE-CONTENT" in logged
    assert "exc=TripStoreError" in logged
    assert "payload_bytes=" in logged
    assert "trip_hash=" in logged
    for secret in (
        trip_id,
        "NameMarker顯示名",
        "mail-marker@example.com",
        "TitleMarker行程標題",
        "太宰府天滿宮",
        "test-member-sub",
        "fake",
    ):
        assert secret not in logged


def test_home_prefers_google_display_name(monkeypatch):
    monkeypatch.setattr(auth_gate, "get_display_name", lambda: "阿花")
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    assert "歡迎回來，阿花！" in _page(at)
    assert "進行中" not in _page(at)


def test_home_sorts_visible_trips_and_in_progress_uses_saved_dates(memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _button(at, "建立新旅行夾").click().run()
    for row in memory_supabase.rows:
        title = (row.get("payload") or {}).get("title")
        if title == "行程 1":
            row["updated_at"] = "2026-12-01T00:00:00+00:00"
        elif title == "行程 2":
            row["updated_at"] = "2020-01-01T00:00:00+00:00"
    _open_screen(at, "首頁")
    labels = [button.label for button in at.button]
    assert labels.index("行程 1") < labels.index("行程 2（目前）")
    assert "進行中" not in _page(at)
    _open_screen(at, "旅行夾")
    today = date.today().isoformat()
    [box for box in at.text_input if box.label == "旅行開始"][0].input(today).run()
    [box for box in at.text_input if box.label == "旅行結束"][0].input(today).run()
    _open_screen(at, "首頁")
    assert "進行中" in _page(at)
    assert "行程 2" in _page(at)
    _button(at, "前往收集").click().run()
    assert at.session_state["screen"] == "B"
    _open_screen(at, "旅行夾")
    [box for box in at.text_input if box.label == "旅行開始"][0].input("2020-01-01").run()
    [box for box in at.text_input if box.label == "旅行結束"][0].input("2020-01-02").run()
    _open_screen(at, "首頁")
    assert "進行中" not in _page(at)
    assert "前往排程" not in [button.label for button in at.button]


def test_workspace_schedule_card_opens_plan_without_removing_results(memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _button(at, "前往排程").click().run()
    assert at.session_state["screen"] == "D"
    _open_screen(at, "旅行夾")
    _button(at, "前往行程").click().run()
    assert at.session_state["screen"] == "D"


def test_collect_progress_counts_real_statuses_without_new_recognition(memory_supabase, monkeypatch):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    at.session_state["inbox"] = [
        {"id": "done-one", "analysis": "done", "facts": "done", "places": [{"name": "甲"}], "batch": True},
        {"id": "fail-one", "analysis": "failed", "places": [], "batch": True, "warning": "未能辨識店名"},
    ]
    monkeypatch.setattr(
        gemini_client,
        "recognize_names",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不應再識別")),
    )
    _open_screen(at, "收集")
    assert not at.exception
    page = _page(at)
    assert "已完成 1／2" in page
    assert "1 張未能辨識" in page
    assert "重試" in [button.label for button in at.button]
    _open_screen(at, "收藏")
    assert "未有文字搜尋" in _page(at)
    assert "跨行程複製" in _page(at)
    assert not any(box.label == "搜尋" for box in at.text_input)


def test_invite_panel_keeps_manual_join_wording(memory_supabase):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    _open_screen(at, "旅行夾")
    _button(at, "建立新旅行夾").click().run()
    _button(at, "⚙").click().run()
    _button(at, "邀請朋友").click().run()
    assert any(block.value == "邀請朋友加入" for block in at.subheader)
    assert "而家未有登入後自動加入" in _page(at)
    assert any(button.label == "產生邀請連結" for button in at.button)
    assert any(button.label == "返回行程" for button in at.button)


def test_plan_lists_all_favourites_and_travel_detail_keeps_the_day(monkeypatch):
    at = _plan_with_trip(AppTest.from_file(APP, default_timeout=30))
    page = _page(at)
    assert "太宰府天滿宮" in page
    assert "全部收藏都會交俾排程" in page
    assert "尚未修正" in page
    assert not list(at.checkbox)
    calls = {"n": 0}

    def gen(*_args, **_kwargs):
        calls["n"] += 1
        return _generated_itinerary()

    monkeypatch.setattr(gemini_client, "make_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(gemini_client, "generate_itinerary", gen)
    monkeypatch.setattr(
        gemini_client,
        "suggest_nearby",
        lambda *_args, **_kwargs: gemini_client.AnalysisResult([], []),
    )
    _button(at, "開始排行程").click().run()
    assert calls["n"] == 1
    assert at.session_state["screen"] == "E"
    assert "手動調整時間" in _page(at)
    assert "上移" not in [button.label for button in at.button]
    assert "（SAVE-" not in _joined(at.error)
    _button(at, "旅行中查看").click().run()
    assert calls["n"] == 1
    travel_page = _page(at)
    assert "太宰府天滿宮" in travel_page
    assert "°C" not in travel_page
    assert "加入行程" not in [button.label for button in at.button]
    kept = at.session_state.get("day_tab")
    _button(at, "查看詳情：太宰府天滿宮").click().run()
    assert at.session_state.get("day_tab") == kept
    assert "營業時間" in _page(at)
    _button(at, "返回時間線").click().run()
    assert at.session_state.get("day_tab") == kept
    assert not at.session_state.get("travel_detail")
    _button(at, "返回行程結果").click().run()
    assert not at.session_state.get("travel_view")
    assert at.session_state.get("day_tab") == kept
    assert calls["n"] == 1

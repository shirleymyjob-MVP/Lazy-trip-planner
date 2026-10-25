"""Streamlit AppTest：新流程用 mock 代替真 Gemini。"""
import hashlib
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

import gemini_client  # noqa: E402
import trip_core  # noqa: E402


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
    assert trip_core.HERO_LINE in page
    for step in trip_core.OPENING_STEPS:
        assert step in page
    assert any("build:" in caption.value for caption in at.caption)
    assert any("AI 服務未設定好" in error.value for error in at.error)
    start = _button(at, "開始收集想去的地方")
    assert not start.disabled
    assert not any(button.label == "開始排行程" for button in at.button)
    assert not at.download_button


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
        name = [box for box in at.text_input if box.label == "店舖名稱"][0]
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
    _button(at, "✎ 編輯").click().run()
    region = [pill for pill in at.pills if "地區" in pill.proto.label][0]
    region.set_value(["太宰府", "福岡"]).run()
    _select(at, "類別").set_value("文化體驗").run()
    _button(at, "儲存").click().run()
    assert not at.exception
    saved = at.session_state["favourites"][0]
    assert saved["edited"] is True
    assert saved["category"] == "文化體驗"
    assert "福岡" in saved["regions"] and "太宰府" in saved["regions"]
    _button(at, "✎ 編輯").click().run()
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


def test_plan_failure_does_not_spend_a_turn():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["screen"] = "D"
    at.session_state["start_date"] = date(2026, 11, 14)
    at.session_state["favourites"] = [_fav(), _fav(id="b", name="一蘭"), _fav(id="c", name="福岡塔")]
    at.run()
    assert "是否需要 AI 協助" not in _page(at)
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


def test_shared_inbox_keeps_who_added_the_screenshot():
    token = "ab" * 6
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.query_params["share"] = token
    at.session_state["display_name"] = "Ada"
    at.session_state["screen"] = "B"
    at.run()
    with mock.patch.object(gemini_client, "recognize_names", side_effect=AssertionError("no analyze")):
        at.file_uploader[0].upload("shared.png", _png("red"), "image/png").run()
    assert at.session_state["inbox"][0]["added_by"] == "Ada"
    assert "尚未分析" in _page(at)
    other = AppTest.from_file(APP, default_timeout=30)
    other.secrets["GEMINI_API_KEY"] = "fake"
    other.query_params["share"] = token
    other.session_state["display_name"] = "Bo"
    other.session_state["screen"] = "B"
    other.run()
    assert not other.exception
    assert other.session_state["inbox"][0]["added_by"] == "Ada"
    assert "加入：Ada" in _page(other)


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
    assert at.file_uploader[0].label == "批量上傳"


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

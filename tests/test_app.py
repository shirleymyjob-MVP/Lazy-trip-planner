"""Streamlit AppTest：新流程用 mock 代替真 Gemini。"""
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

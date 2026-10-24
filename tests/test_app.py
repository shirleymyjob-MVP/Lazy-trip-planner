"""Streamlit AppTest：用 mock 代替真 Gemini。"""
import io
import json
import os
import sys
from unittest import mock

from PIL import Image
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
APP = os.path.join(ROOT, "app.py")

import gemini_client  # noqa: E402
import trip_core  # noqa: E402
from test_trip_core import ITIN, PLACES_JSON  # noqa: E402


def _png(color: str) -> bytes:
    img = Image.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _select(at: AppTest, label: str):
    return [box for box in at.selectbox if label in box.label][0]


def _pill(at: AppTest, label: str):
    return [pill for pill in at.pills if label in pill.proto.label][0]


def _inbox_item(place: dict, *, item_id: str, label: str, remark: str = "", city: str = "東京") -> dict:
    return {
        "id": item_id,
        "label": label,
        "image": _png("white"),
        "remark": remark,
        "status": "未排程",
        "analysis": "done",
        "places": [place],
        "city": city,
        "warning": "",
    }


def test_app_without_key_shows_friendly_error():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("AI 服務未設定好" in e.value for e in at.error)
    assert any("開始排行程" in b.label and b.disabled for b in at.button)
    assert any("放入行程匣" in c.value and "稍後排行程" in c.value for c in at.caption)
    assert any(str(trip_core.CAPTURE_TO_INBOX_TAPS) in c.value and "行程匣" in c.value for c in at.caption)
    heads = [h.value for h in at.subheader]
    assert heads == ["1. 行程匣", "2. 開始排行程"]
    assert not any("認地方" in b.label for b in at.button)
    assert not any("貼" in area.label and "店名" in area.label for area in at.text_area)


def test_app_plan_from_inbox_uses_trip_basics():
    places = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.secrets["MAX_ANALYSES_PER_SESSION"] = 2
    at.session_state["inbox"] = [
        _inbox_item(places[0], item_id="a" * 32, label="截圖 1", remark="只要圖中 1、4", city="東京"),
        _inbox_item(places[1], item_id="b" * 32, label="截圖 2", city="東京"),
    ]
    at.run()
    assert not at.exception
    heads = [h.value for h in at.subheader]
    assert heads.index("1. 行程匣") < heads.index("2. 開始排行程")
    page = "\n".join(m.value for m in at.markdown)
    assert "東京都渋谷区神南1-22-7" in page
    assert "24小時" in page
    assert "狀態：未排程" in page
    assert "城市：東京" in page
    assert "只要圖中 1、4" in page
    assert _select(at, "目的地").value == trip_core.DESTINATION_DEFAULT
    assert _select(at, "幾多日").value == "3"
    at.checkbox[1].uncheck().run()

    fake_itin = trip_core.parse_itinerary_response(json.dumps(ITIN, ensure_ascii=False))
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake_itin) as gen:
        _select(at, "目的地").set_value("東京（國泰、香港快運直飛）")
        _select(at, "幾多日").set_value("2")
        _pill(at, "同行").set_value("家庭（有小朋友）")
        _pill(at, "偏好").set_value(["美食", "景點", "其他"]).run()
        other = [box for box in at.text_input if "其他偏好" in box.label][0]
        other.input("唔想行太多")
        submit = [b for b in at.button if b.label == "✨ 開始排行程"][0]
        assert not submit.disabled
        submit.click().run()
        assert not at.exception
        args = gen.call_args.args
        assert len(args[2]) == 1 and args[3] == 2 and args[4] == "東京"
        assert "唔想行太多" in args[5]
        assert "只要圖中 1、4" in args[5]
        assert gen.call_args.kwargs["party"] == "家庭（有小朋友）"
        assert "美食" in gen.call_args.kwargs["preferences"]
    assert at.session_state["itinerary"]["title"] == "東京 3 日懶人遊"
    assert at.session_state["usage"] == 1
    assert at.session_state["inbox"][0]["status"] == "已排程"
    assert at.session_state["inbox"][1]["status"] == "未排程"
    at.run()
    assert at.session_state["itinerary"] is not None
    assert any("仲有 1/2 次" in c.value for c in at.caption)


def test_unreadable_upload_is_skipped():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    with mock.patch.object(gemini_client, "analyze_screenshots", return_value=[]) as analyze:
        at.file_uploader[0].upload("bad.png", b"not-an-image", "image/png")
        at.file_uploader[0].upload("ok.png", _png("green"), "image/png").run()
    assert not at.exception
    assert any("讀唔到" in w.value for w in at.warning)
    assert [item["label"] for item in at.session_state["inbox"]] == ["截圖 1"]
    assert at.session_state["inbox"][0]["status"] == "未排程"
    analyze.assert_called_once()
    assert at.session_state["usage"] == 1


def test_upload_maps_second_image_and_does_not_reanalyze():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    found = trip_core.parse_places_response(json.dumps({
        "places": [{
            "name": "博多炉端 魚男", "name_zh": "博多爐端 魚男", "category": "restaurant",
            "area": "中洲, 福岡", "address": "福岡縣福岡市中央區今泉1-4-23",
            "opening_hours": "18:00-23:00", "rating": "4.5",
            "confidence": "高", "source_images": [2],
        }]
    }, ensure_ascii=False))
    with mock.patch.object(
        gemini_client, "analyze_screenshots",
        return_value=gemini_client.AnalysisResult(found, []),
    ):
        at.file_uploader[0].upload("a.png", _png("red"), "image/png")
        at.file_uploader[0].upload("b.png", _png("blue"), "image/png").run()
    assert not at.exception
    inbox = at.session_state["inbox"]
    assert [item["label"] for item in inbox] == ["截圖 1", "截圖 2"]
    assert inbox[0]["places"] == []
    assert inbox[1]["places"][0]["name"] == "博多炉端 魚男"
    assert inbox[1]["city"] == "福岡"
    assert inbox[1]["status"] == "未排程"
    page = "\n".join(m.value for m in at.markdown)
    assert "今泉1-4-23" in page and "18:00-23:00" in page and "4.5/5" in page
    with mock.patch.object(gemini_client, "analyze_screenshots", side_effect=AssertionError("reanalyzed")):
        at.run()
    assert not at.exception
    assert [item["label"] for item in at.session_state["inbox"]] == ["截圖 1", "截圖 2"]


def test_upload_remark_is_passed_into_analysis():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    remark = [box for box in at.text_input if "上傳備註" in box.label][0]
    remark.input("只要圖中 1、4").run()
    seen = {}

    def fake_analyze(_client, _model, images, **kwargs):
        seen["kwargs"] = kwargs
        seen["images"] = len(images)
        return gemini_client.AnalysisResult([], [])

    with mock.patch.object(gemini_client, "analyze_screenshots", side_effect=fake_analyze):
        at.file_uploader[0].upload("ig.png", _png("orange"), "image/png").run()
    assert seen["images"] == 1
    assert seen["kwargs"]["remarks"] == "只要圖中 1、4"
    assert seen["kwargs"]["destination"] == "日本"
    assert at.session_state["inbox"][0]["remark"] == "只要圖中 1、4"
    assert at.session_state["inbox"][0]["status"] == "未排程"
    with mock.patch.object(gemini_client, "analyze_screenshots", side_effect=AssertionError("reanalyzed")):
        at.run()
    assert not at.exception


def test_bracket_in_place_name_does_not_break_map_link():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["itinerary"] = {
        "title": "測試",
        "days": [{
            "day": 1,
            "area": "",
            "theme": "",
            "items": [{
                "time": "10:00",
                "place_name": "Cafe [Borderless]",
                "activity": "行下",
                "is_meal": False,
                "maps_query": "teamLab Borderless",
            }],
        }],
        "tips": [],
    }
    at.session_state["itinerary_places"] = []
    at.run()
    assert not at.exception
    md = "\n".join(m.value for m in at.markdown)
    assert "[Cafe \\[Borderless\\]](https://www.google.com/maps/search/" in md
    assert "teamLab%20Borderless" in md


def test_analysis_failure_does_not_spend_quota():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.secrets["MAX_ANALYSES_PER_SESSION"] = 2
    at.run()
    with mock.patch.object(
        gemini_client, "analyze_screenshots", side_effect=gemini_client.GeminiError("測試失敗"),
    ):
        at.file_uploader[0].upload("ok.png", _png("red"), "image/png").run()
    assert not at.exception
    assert at.session_state["usage"] == 0
    assert at.session_state["inbox"][0]["status"] == "未排程"
    assert at.session_state["inbox"][0]["analysis"] == "failed"
    shown = [getattr(block, "value", "") for block in list(at.error) + list(at.warning) + list(at.caption)]
    assert any("測試失敗" in text for text in shown)
    assert any("再試一次分析" in b.label and not b.disabled for b in at.button)
    assert any("仲有 2/2 次" in c.value for c in at.caption)


def test_fukuoka_is_a_plan_option_without_blocking_inbox():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    assert _select(at, "目的地").value == trip_core.DESTINATION_DEFAULT
    assert "福岡" in " ".join(_select(at, "目的地").options)
    _select(at, "目的地").set_value("福岡（國泰、香港快運直飛）").run()
    assert not at.exception
    assert _select(at, "目的地").value == "福岡（國泰、香港快運直飛）"
    _select(at, "目的地").set_value(trip_core.DESTINATION_OTHER).run()
    assert any(box.label == "其他目的地" for box in at.text_input)


def test_plan_button_stays_disabled_until_inbox_has_places():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    assert any(b.label == "✨ 開始排行程" and b.disabled for b in at.button)
    assert any("行程匣未有截圖" in info.value for info in at.info)

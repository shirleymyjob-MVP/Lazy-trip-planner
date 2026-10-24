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


def test_app_without_key_shows_friendly_error():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("AI 服務未設定好" in e.value for e in at.error)
    assert any("分析截圖" in b.label and b.disabled for b in at.button)
    assert any("手動步數" in c.value and str(trip_core.HAPPY_PATH_TAPS) in c.value for c in at.caption)


def test_app_places_and_itinerary_flow():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.secrets["MAX_ANALYSES_PER_SESSION"] = 2
    at.session_state["places"] = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    at.run()
    assert not at.exception
    assert len(at.checkbox) == 2
    heads = [h.value for h in at.subheader]
    assert heads.index("1. 行程要求") < heads.index("2. 提供來源") < heads.index("3. 生成行程")
    assert at.text_input[0].value == "日本"
    assert int(at.number_input[0].value) == 3
    page = "\n".join(m.value for m in at.markdown)
    assert "東京都渋谷区神南1-22-7" in page
    assert "24小時" in page
    assert "評分：未有" in page
    # 剔走第二個地方
    at.checkbox[1].uncheck().run()

    fake_itin = trip_core.parse_itinerary_response(json.dumps(ITIN, ensure_ascii=False))
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake_itin) as gen:
        at.number_input[0].set_value(2)
        at.text_input[0].input("東京")
        pref = [area for area in at.text_area if "偏好" in area.label][0]
        pref.input("唔想行太多")
        submit = [b for b in at.button if "生成行程" in b.label][0]
        submit.click().run()
        assert not at.exception
        args = gen.call_args.args
        assert len(args[2]) == 1 and args[3] == 2 and args[4] == "東京" and args[5] == "唔想行太多"
    assert at.session_state["itinerary"]["title"] == "東京 3 日懶人遊"
    assert at.session_state["usage"] == 1
    md = "\n".join(m.value for m in at.markdown)
    assert "一蘭拉麵" in md
    # rerun 唔會唔見結果
    at.run()
    assert at.session_state["itinerary"] is not None
    assert any("仲有 1/2 次" in c.value for c in at.caption)


def _png(color: str) -> bytes:
    img = Image.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _captions(at: AppTest) -> list[str]:
    return [caption for image in at.image for caption in image.captions]


def test_unreadable_upload_is_skipped():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    with mock.patch.object(gemini_client, "analyze_screenshots", return_value=[]) as analyze:
        at.file_uploader[0].upload("bad.png", b"not-an-image", "image/png")
        at.file_uploader[0].upload("ok.png", _png("green"), "image/png").run()
    assert not at.exception
    assert any("讀唔到" in w.value for w in at.warning)
    # 壞檔唔佔編號，睇到嘅圖由 #1 開始。上傳完會自動分析，唔使再撳。
    assert _captions(at) == ["#1"]
    analyze.assert_called_once()
    assert at.session_state["usage"] == 1


def test_screenshot_numbers_stay_stable_after_rerun():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    found = trip_core.parse_places_response(json.dumps({
        "places": [{
            "name": "Cafe A", "name_zh": "咖啡A", "category": "cafe",
            "area": "澀谷", "address": "東京都渋谷区", "opening_hours": "10:00-19:00",
            "rating": "4.5", "confidence": "高", "source_images": [2],
        }]
    }, ensure_ascii=False))
    with mock.patch.object(gemini_client, "analyze_screenshots", return_value=found):
        at.file_uploader[0].upload("a.png", _png("red"), "image/png")
        at.file_uploader[0].upload("b.png", _png("blue"), "image/png").run()
    assert not at.exception
    assert at.session_state["places"][0]["source_images"] == [2]
    assert at.session_state["places"][0]["rating"] == "4.5/5"
    assert _captions(at) == ["#1", "#2"]
    page = "\n".join(m.value for m in at.markdown)
    assert "東京都渋谷区" in page and "10:00-19:00" in page and "4.5/5" in page
    # 舊邏輯會喺下次 rerun 用已增加嘅 image_count，將同一批圖改成 #3、#4。
    # 同一批圖亦唔應該再打一次 AI。
    with mock.patch.object(gemini_client, "analyze_screenshots", side_effect=AssertionError("reanalyzed")):
        at.run()
    assert not at.exception
    assert _captions(at) == ["#1", "#2"]
    assert at.session_state["places"][0]["source_images"] == [2]


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
    assert any("測試失敗" in e.value for e in at.error)
    assert any("再試一次分析" in b.label and not b.disabled for b in at.button)
    assert any("仲有 2/2 次" in c.value for c in at.caption)


def test_requirements_render_before_sources():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    assert not at.exception
    heads = [h.value for h in at.subheader]
    assert heads == ["1. 行程要求", "2. 提供來源"]
    assert at.text_input[0].value == "日本"
    assert int(at.number_input[0].value) == 3
    labels = [area.label for area in at.text_area]
    assert labels.index(next(label for label in labels if "偏好" in label)) < labels.index(
        next(label for label in labels if "貼" in label)
    )
    assert any("行程要求" in c.value and "提供來源" in c.value for c in at.caption)
    assert any("手動步數" in c.value and str(trip_core.HAPPY_PATH_TAPS) in c.value for c in at.caption)
    assert not any("生成行程" in b.label for b in at.button)


def test_japan_preset_fills_destination():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.session_state["places"] = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    at.run()
    assert at.text_input[0].value == "日本"
    osaka = [b for b in at.button if b.label == "大阪"][0]
    osaka.click().run()
    assert not at.exception
    assert at.text_input[0].value == "大阪"


def test_paste_maps_link_enriches_and_skips_social():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    text = (
        "https://www.google.com/maps/search/?api=1&query=%E6%B7%BA%E8%8D%89%E5%AF%BA\n"
        "https://www.instagram.com/p/abc"
    )
    paste = [area for area in at.text_area if "貼" in area.label][0]
    paste.input(text).run()
    enriched = trip_core.parse_places_response(json.dumps({"places": [{
        "name": "淺草寺", "name_zh": "淺草寺", "category": "attraction",
        "area": "淺草, 東京", "address": "東京都台東區淺草2-3-1",
        "opening_hours": "6:00–17:00", "rating": "4.4/5",
        "confidence": "高", "source_images": [],
    }]}, ensure_ascii=False))
    with mock.patch.object(
        gemini_client, "enrich_places", return_value=gemini_client.AnalysisResult(enriched, []),
    ) as enrich:
        button = [b for b in at.button if b.label == "認地方"][0]
        assert not button.disabled
        button.click().run()
    assert not at.exception
    enrich.assert_called_once()
    assert at.session_state["usage"] == 1
    blob = "\n".join(m.value for m in at.markdown) + "\n" + "\n".join(w.value for w in at.warning)
    assert "東京都台東區淺草2-3-1" in blob
    assert "4.4/5" in blob
    assert "Instagram" in blob

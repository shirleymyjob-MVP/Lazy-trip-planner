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
    assert at.button[0].disabled  # 分析掣


def test_app_places_and_itinerary_flow():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.secrets["MAX_ANALYSES_PER_SESSION"] = 2
    at.session_state["places"] = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    at.run()
    assert not at.exception
    assert len(at.checkbox) == 2
    # 剔走第二個地方
    at.checkbox[1].uncheck().run()

    fake_itin = trip_core.parse_itinerary_response(json.dumps(ITIN, ensure_ascii=False))
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "generate_itinerary", return_value=fake_itin) as gen:
        at.number_input[0].set_value(2)
        at.text_input[0].input("東京")
        submit = [b for b in at.button if "生成行程" in b.label][0]
        submit.click().run()
        assert not at.exception
        args = gen.call_args.args
        assert len(args[2]) == 1 and args[3] == 2 and args[4] == "東京"
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
    at.file_uploader[0].upload("bad.png", b"not-an-image", "image/png")
    at.file_uploader[0].upload("ok.png", _png("green"), "image/png").run()
    assert not at.exception
    assert any("讀唔到" in w.value for w in at.warning)
    # 壞檔唔佔編號，睇到嘅圖由 #1 開始，而且分析掣仍然可用。
    assert _captions(at) == ["#1"]
    assert not at.button[0].disabled


def test_screenshot_numbers_stay_stable_after_rerun():
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["GEMINI_API_KEY"] = "fake"
    at.run()
    at.file_uploader[0].upload("a.png", _png("red"), "image/png")
    at.file_uploader[0].upload("b.png", _png("blue"), "image/png").run()
    assert not at.exception
    assert _captions(at) == ["#1", "#2"]

    found = trip_core.parse_places_response(json.dumps({
        "places": [{
            "name": "Cafe A", "name_zh": "咖啡A", "category": "cafe",
            "area": "澀谷", "confidence": "高", "source_images": [2],
        }]
    }, ensure_ascii=False))
    with mock.patch.object(gemini_client, "make_client", return_value=object()), \
         mock.patch.object(gemini_client, "analyze_screenshots", return_value=found):
        at.button[0].click().run()
    assert not at.exception
    assert at.session_state["places"][0]["source_images"] == [2]
    assert _captions(at) == ["#1", "#2"]
    # 舊邏輯會喺下次 rerun 用已增加嘅 image_count，將同一批圖改成 #3、#4。
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

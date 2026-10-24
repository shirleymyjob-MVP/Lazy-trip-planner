"""Streamlit AppTest：用 mock 代替真 Gemini。"""
import json
import os
import sys
from unittest import mock

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

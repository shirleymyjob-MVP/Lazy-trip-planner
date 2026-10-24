import io
import json
import os
import sys
from unittest import mock

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_client  # noqa: E402
import trip_core  # noqa: E402

PLACES_JSON = {
    "places": [
        {"name": "一蘭 渋谷店", "name_zh": "一蘭拉麵 澀谷店", "category": "restaurant",
         "area": "澀谷, 東京", "address": "東京都渋谷区神南1-22-7", "opening_hours": "24小時",
         "notes": "豚骨拉麵", "confidence": "高", "source_images": [1]},
        {"name": "一蘭渋谷店", "name_zh": "", "category": "other", "area": "",
         "address": "", "opening_hours": "", "notes": "要排隊", "confidence": "低", "source_images": [3]},
        {"name": "Blue Bottle Coffee", "name_zh": "藍瓶咖啡", "category": "Coffee",
         "area": "清澄白河, 東京", "confidence": "medium", "source_images": "2"},
        {"name": "", "name_zh": "", "category": "cafe"},
    ]
}


def test_parse_places_dedupes_and_normalizes():
    places = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    assert len(places) == 2
    ichiran = places[0]
    assert ichiran["source_images"] == [1, 3]
    assert ichiran["confidence"] == "高"
    assert "要排隊" in ichiran["notes"]
    bb = places[1]
    assert bb["category"] == "cafe"
    assert bb["confidence"] == "中"
    assert bb["source_images"] == [2]


def test_parse_places_fallbacks():
    fenced = "好，以下係結果：\n```json\n" + json.dumps(PLACES_JSON) + "\n```\n希望幫到你"
    assert len(trip_core.parse_places_response(fenced)) == 2
    bare_list = "Result: " + json.dumps(PLACES_JSON["places"]) + " end"
    assert len(trip_core.parse_places_response(bare_list)) == 2
    for bad in ("", None, "sorry I can't", "{not json"):
        try:
            trip_core.parse_places_response(bad)
            assert False, "should raise"
        except ValueError:
            pass


def test_maps_search_url_encodes():
    url = trip_core.maps_search_url({"name": "一蘭 渋谷店", "address": "東京都渋谷区 1-22 & 7"})
    assert url.startswith("https://www.google.com/maps/search/?api=1&query=")
    assert " " not in url and "&7" not in url
    assert "%E4%B8%80%E8%98%AD%20" in url
    # 冇地址就用 area
    assert "Shibuya" in trip_core.maps_search_url({"name": "X", "area": "Shibuya"})


def test_directions_url():
    assert trip_core.maps_directions_url(["A"]) is None
    url = trip_core.maps_directions_url(["A place", "B", "C|x", "D"])
    assert url.startswith("https://www.google.com/maps/dir/?api=1&origin=A%20place&destination=D")
    assert "waypoints=B%7CC%7Cx" in url
    many = trip_core.maps_directions_url([str(i) for i in range(20)])
    assert many.count("%7C") == trip_core.MAX_WAYPOINTS - 1


ITIN = {
    "title": "東京 3 日懶人遊",
    "days": [
        {"day": 1, "area": "澀谷", "theme": "食拉麵", "items": [
            {"time": "12:00", "place_name": "一蘭拉麵", "activity": "食午餐", "is_meal": True,
             "maps_query": "一蘭 渋谷店 澀谷"},
            {"time": "15:00", "place_name": "藍瓶咖啡", "activity": "飲咖啡", "is_meal": False,
             "maps_query": "Blue Bottle Coffee 清澄白河"},
        ]},
        {"area": "淺草", "items": [{"time": "10:00", "place_name": "淺草寺", "activity": "", "is_meal": False}]},
    ],
    "tips": "記得買 Suica",
}


def test_itinerary_parse_and_markdown():
    it = trip_core.parse_itinerary_response("```json\n" + json.dumps(ITIN, ensure_ascii=False) + "\n```")
    assert it["days"][1]["day"] == 2
    assert it["days"][1]["items"][0]["maps_query"] == "淺草寺"
    assert it["tips"] == ["記得買 Suica"]
    md = trip_core.itinerary_to_markdown(it)
    assert md.startswith("# 東京 3 日懶人遊")
    assert "## Day 1｜澀谷" in md
    assert "**12:00** 🍽️ 一蘭拉麵 — 食午餐" in md
    assert "https://www.google.com/maps/dir/?api=1" in md  # Day 1 有 2 個點
    assert md.count("maps/dir") == 1  # Day 2 只有 1 個點
    assert "## 小貼士" in md


def test_itinerary_invalid():
    for bad in ('{"days": []}', "nope"):
        try:
            trip_core.parse_itinerary_response(bad)
            assert False
        except ValueError:
            pass


def _img_bytes(mode, size, fmt="PNG"):
    img = Image.new(mode, size)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_display_numbers_stay_with_the_file_and_map_batch_positions():
    labels: dict[str, int] = {}
    nxt = trip_core.assign_display_numbers(["a", "b"], labels, 1)
    assert labels == {"a": 1, "b": 2} and nxt == 3
    # 同一批檔再出現唔好改編號；新檔先用下一個號。
    nxt = trip_core.assign_display_numbers(["b", "c"], labels, nxt)
    assert labels["b"] == 2 and labels["c"] == 3 and nxt == 4

    places = [
        {"source_images": [1, 2]},
        {"source_images": [2, 0, 9, True, "x"]},
    ]
    # 呢批實際送出嘅係畫面 #3 同 #4（例如上一批已經用咗 1、2）。
    trip_core.map_batch_source_images(places, [3, 4])
    assert places[0]["source_images"] == [3, 4]
    assert places[1]["source_images"] == [4]


def test_escape_markdown_label():
    assert trip_core.escape_markdown_label("Cafe [Borderless]") == "Cafe \\[Borderless\\]"
    assert trip_core.escape_markdown_label("a\\b") == "a\\\\b"


def test_prepare_image_modes_and_resize():
    for mode in ("RGBA", "P", "LA", "L", "RGB"):
        out = trip_core.prepare_image(_img_bytes(mode, (3200, 1000)))
        img = Image.open(io.BytesIO(out))
        assert img.format == "JPEG" and img.mode == "RGB"
        assert max(img.size) == 1600
    small = Image.open(io.BytesIO(trip_core.prepare_image(_img_bytes("RGB", (400, 300)))))
    assert small.size == (400, 300)


# ---------------------------------------------------------------- gemini_client with mock
class _Resp:
    def __init__(self, text):
        self.text = text


def test_analyze_screenshots_mocked():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps(PLACES_JSON, ensure_ascii=False))
    jpeg = trip_core.prepare_image(_img_bytes("RGB", (10, 10)))
    places = gemini_client.analyze_screenshots(client, "m", [jpeg, jpeg])
    assert len(places) == 2
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "m"
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_schema is gemini_client.PLACES_SCHEMA
    parts = kwargs["contents"]
    assert parts[1] == "圖片 #1" and parts[3] == "圖片 #2"
    assert parts[2].inline_data.mime_type == "image/jpeg"
    assert "日本旅行助手" not in parts[0]


def test_analyze_errors_are_friendly():
    from google.genai import errors

    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp("totally not json")
    try:
        gemini_client.analyze_screenshots(client, "m", [b"x"])
        assert False
    except gemini_client.GeminiError as e:
        assert "格式" in str(e)

    client.models.generate_content.side_effect = errors.ClientError(
        429, {"error": {"code": 429, "message": "secret internal detail", "status": "RESOURCE_EXHAUSTED"}}
    )
    try:
        gemini_client.analyze_screenshots(client, "m", [b"x"])
        assert False
    except gemini_client.GeminiError as e:
        assert "secret internal detail" not in str(e)
        assert "過一陣" in str(e)


def test_generate_itinerary_mocked():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps(ITIN, ensure_ascii=False))
    places = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    it = gemini_client.generate_itinerary(client, "m", places, 3, "東京", "唔想太趕")
    assert it["title"] == "東京 3 日懶人遊"
    prompt = client.models.generate_content.call_args.kwargs["contents"]
    assert "3 日" in prompt and "東京" in prompt and "唔想太趕" in prompt


def test_invalid_key_message():
    from google.genai import errors

    client = mock.MagicMock()
    client.models.generate_content.side_effect = errors.ClientError(
        400, {"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}}
    )
    try:
        gemini_client.analyze_screenshots(client, "m", [b"x"])
        assert False
    except gemini_client.GeminiError as e:
        assert "API Key" in str(e)

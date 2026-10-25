import io
import json
import os
import sys
from unittest import mock

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_client  # noqa: E402
import trip_core  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_maps_grounding_flag():
    gemini_client._maps_grounding_disabled = False
    yield
    gemini_client._maps_grounding_disabled = False

PLACES_JSON = {
    "places": [
        {"name": "一蘭 渋谷店", "name_zh": "一蘭拉麵 澀谷店", "category": "restaurant",
         "area": "澀谷, 東京", "address": "東京都渋谷区神南1-22-7", "opening_hours": "24小時",
         "notes": "豚骨拉麵", "rating": "4.2/5", "confidence": "高", "source_images": [1]},
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
    assert ichiran["rating"] == "4.2/5"
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
        assert max(img.size) == trip_core.MAX_IMAGE_SIDE
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
    result = gemini_client.analyze_screenshots(client, "m", [jpeg, jpeg])
    assert len(result.places) == 2
    assert result.places[0]["rating"] == "4.2/5"
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "m"
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_schema is gemini_client.PLACES_SCHEMA
    assert kwargs["config"].thinking_config.thinking_level == "LOW"
    assert kwargs["config"].thinking_config.thinking_budget is None
    parts = kwargs["contents"]
    assert parts[1] == "圖片 #1" and parts[3] == "圖片 #2"
    assert parts[2].inline_data.mime_type == "image/jpeg"
    assert "日本" in parts[0] and "唔好硬套日本" in parts[0]
    assert "評分" in parts[0]


def test_recognize_names_is_the_fast_path():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps({
        "places": [{
            "name": "一蘭", "name_zh": "一蘭", "category": "restaurant",
            "area": "福岡、東京", "confidence": "高",
            "source_images": [1], "caption_index": 0,
        }]
    }, ensure_ascii=False))
    result = gemini_client.recognize_names(client, "m", [b"jpeg"], remarks="只要圖中 1、4")
    assert result.places[0]["name_zh"] == "一蘭"
    assert result.places[0]["address"] == ""
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "m"
    config = kwargs["config"]
    assert config.response_schema is gemini_client.NAMES_SCHEMA
    assert config.thinking_config.thinking_level == "LOW"
    assert config.thinking_config.thinking_budget is None
    assert config.http_options is None
    prompt = kwargs["contents"][0]
    assert "只係快認店名" in prompt
    assert "只要圖中 1、4" in prompt
    assert "博多もつ將" in prompt
    assert client.models.generate_content.call_count == 1


def test_center_overlay_shop_name_survives_an_empty_first_pass():
    """食物特寫加中央大字：第一輪交空 list 或系列標題時，再讀一次大字。"""
    fixture_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures", "hakata_motsu_overlay.json",
    )
    with open(fixture_path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    for line in fixture["visible_text"]:
        if line == fixture["expected_name"]:
            assert not trip_core.is_listicle_title(line)
        else:
            assert trip_core.is_listicle_title(line)
    assert trip_core.keep_shop_names([
        {"name": "福岡5間", "name_zh": "福岡5間"},
        {"name": fixture["expected_name"], "name_zh": fixture["expected_name"]},
    ])[0]["name"] == fixture["expected_name"]

    client = mock.MagicMock()
    client.models.generate_content.side_effect = [
        _Resp(json.dumps({"places": []}, ensure_ascii=False)),
        _Resp(json.dumps({"places": [{
            "name": "福岡5間",
            "name_zh": "福岡5間",
            "category": "other",
            "area": "福岡",
            "confidence": "低",
            "source_images": [1],
            "caption_index": 0,
        }, {
            "name": fixture["expected_name"],
            "name_zh": fixture["expected_name"],
            "category": fixture["expected_category"],
            "area": fixture["expected_area"],
            "confidence": "高",
            "source_images": [1],
            "caption_index": 0,
        }]}, ensure_ascii=False)),
    ]
    result = gemini_client.recognize_names(client, "m", [b"jpeg"], retry_sleep=0)
    assert [place["name"] for place in result.places] == [fixture["expected_name"]]
    assert result.places[0]["area"] == fixture["expected_area"]
    assert client.models.generate_content.call_count == 2
    overlay_prompt = client.models.generate_content.call_args_list[1].kwargs["contents"][0]
    assert "博多もつ將" in overlay_prompt
    assert "上一輪冇抽出店名" in overlay_prompt
    assert "風景" in overlay_prompt


def test_recognize_names_defaults_to_lite_model_once():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp("not json")
    try:
        gemini_client.recognize_names(client, None, [b"jpeg"], retry_sleep=0)
        assert False
    except gemini_client.GeminiError as err:
        assert "格式" in str(err)
    assert client.models.generate_content.call_count == 1
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-3.5-flash-lite"
    assert kwargs["model"] == gemini_client.DEFAULT_NAMES_MODEL
    assert gemini_client.DEFAULT_MODEL == "gemini-3.8-flash"
    assert gemini_client.DEFAULT_TIMEOUT_MS == 120_000
    assert kwargs["config"].thinking_config.thinking_level == "LOW"
    assert kwargs["config"].thinking_config.thinking_budget is None


def test_analyze_errors_are_friendly():
    from google.genai import errors

    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp("totally not json")
    try:
        gemini_client.analyze_screenshots(client, "m", [b"x"], retry_sleep=0)
        assert False
    except gemini_client.GeminiError as e:
        assert "格式" in str(e)
    assert client.models.generate_content.call_count == 2  # 壞 JSON 會再試一次

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
    assert "4.2/5" in prompt
    assert "唔好硬改成日本" in prompt
    assert client.models.generate_content.call_args.kwargs["model"] == "m"
    http_options = client.models.generate_content.call_args.kwargs["config"].http_options
    assert http_options.timeout == gemini_client.ITINERARY_TIMEOUT_MS
    assert http_options.retry_options.attempts == 1
    assert client.models.generate_content.call_args.kwargs["config"].thinking_config.thinking_level == "LOW"


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


def test_rating_hours_and_place_facts():
    assert trip_core.format_rating(4.5) == "4.5/5"
    assert trip_core.format_rating("評分：未知") == ""
    lines = trip_core.place_fact_lines({"area": "", "address": "", "opening_hours": "", "rating": ""})
    assert lines == ["📍 地區：未有", "🏠 地址：未有", "🕒 營業時間：未有", "⭐ 評分：未有"]
    filled = trip_core.place_fact_lines({
        "area": "淺草, 東京", "address": "東京都台東區", "opening_hours": "6:00-17:00", "rating": "4.4/5",
    })
    assert "淺草, 東京" in filled[0] and "東京都台東區" in filled[1]
    assert "6:00-17:00" in filled[2] and "4.4/5" in filled[3]


def test_parse_pasted_places_names_maps_and_social():
    text = """
    - 一蘭拉麵 澀谷
    1. 藍瓶咖啡
    https://www.google.com/maps/search/?api=1&query=%E6%B7%BA%E8%8D%89%E5%AF%BA
    https://www.google.com/maps/place/Ichiran+Shibuya/@35.66,139.70,17z
    https://maps.app.goo.gl/abcDEF
    https://www.instagram.com/reel/abc
    https://youtu.be/xyz
    https://www.xiaohongshu.com/explore/123
    """
    parsed = trip_core.parse_pasted_places(text)
    names = {p["name"] for p in parsed["seeds"]}
    assert "一蘭拉麵 澀谷" in names
    assert "藍瓶咖啡" in names
    assert "淺草寺" in names
    assert "Ichiran Shibuya" in names
    assert parsed["short_urls"] == ["https://maps.app.goo.gl/abcDEF"]
    assert len(parsed["social_urls"]) == 3
    assert all("instagram" in u or "youtu" in u or "xiaohongshu" in u for u in parsed["social_urls"])


def test_resolve_maps_short_url_stays_on_google():
    def fake_open(req, timeout):
        raise trip_core._Redirect("https://www.google.com/maps/place/Ichiran+Shibuya")

    with mock.patch.object(trip_core, "_open_no_redirect", fake_open):
        final = trip_core.resolve_maps_short_url("https://maps.app.goo.gl/abc")
    assert "Ichiran+Shibuya" in final

    def blocked(req, timeout):
        raise trip_core._Redirect("http://169.254.169.254/latest/meta-data")

    with mock.patch.object(trip_core, "_open_no_redirect", blocked):
        try:
            trip_core.resolve_maps_short_url("https://maps.app.goo.gl/abc")
            assert False, "should reject non-google redirect"
        except ValueError:
            pass
    try:
        trip_core.resolve_maps_short_url("https://example.com/place")
        assert False, "should reject non-maps host"
    except ValueError:
        pass


def test_attach_place_facts_onto_itinerary():
    places = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    it = trip_core.parse_itinerary_response(json.dumps(ITIN, ensure_ascii=False))
    shown = trip_core.attach_place_facts(it, places)
    lunch = shown["days"][0]["items"][0]
    assert "神南" in lunch["address"]
    assert lunch["opening_hours"] == "24小時"
    assert lunch["rating"] == "4.2/5"
    assert "address" not in it["days"][0]["items"][0]
    md = trip_core.itinerary_to_markdown(shown)
    assert "地址：東京都渋谷区神南1-22-7" in md
    assert "營業時間：24小時" in md
    assert "評分：4.2/5" in md
    assert trip_core.CAPTURE_TO_INBOX_TAPS == 1
    assert trip_core.PLAN_FROM_INBOX_TAPS == 1
    assert trip_core.HAPPY_PATH_CAPTION == trip_core.HERO_LINE
    assert trip_core.OPENING_STEPS[0] == "收集想去的地方"
    assert trip_core.OPENING_STEPS[2] == "一鍵排行程"
    assert "邀請朋友" in trip_core.OPENING_STEPS[3]
    assert trip_core.PAYWALL_NOTE == "收費：待定"
    assert "貼店名" not in trip_core.HAPPY_PATH_CAPTION


def test_empty_response_is_retried_then_parsed():
    client = mock.MagicMock()
    client.models.generate_content.side_effect = [
        _Resp("  "),
        _Resp(json.dumps(PLACES_JSON, ensure_ascii=False)),
    ]
    result = gemini_client.analyze_screenshots(client, "m", [b"x"], retry_sleep=0)
    assert len(result.places) == 2
    assert client.models.generate_content.call_count == 2


def test_partial_chunk_failure_keeps_other_places():
    from google.genai import errors

    client = mock.MagicMock()
    calls = {"n": 0}

    def side_effect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise errors.ServerError(
                503, {"error": {"code": 503, "message": "secret-backend", "status": "UNAVAILABLE"}}
            )
        return _Resp(json.dumps({
            "places": [{
                "name": "淺草寺", "name_zh": "淺草寺", "category": "attraction",
                "area": "淺草, 東京", "address": "東京都台東區淺草2-3-1",
                "opening_hours": "06:00-17:00", "rating": "4.5/5",
                "confidence": "高", "source_images": [1],
            }]
        }, ensure_ascii=False))

    client.models.generate_content.side_effect = side_effect
    result = gemini_client.analyze_screenshots(
        client, "m", [b"a", b"b"], chunk_size=1, max_workers=1, parse_attempts=1, retry_sleep=0,
    )
    assert [p["name"] for p in result.places] == ["淺草寺"]
    assert result.places[0]["source_images"] == [2]
    assert result.warnings and "secret-backend" not in result.warnings[0]
    assert "再分析" in result.warnings[0]


def test_timeout_message_hides_internals():
    import httpx

    client = mock.MagicMock()
    request = httpx.Request("POST", "https://example.test/gemini")
    client.models.generate_content.side_effect = httpx.ReadTimeout("The read operation timed out", request=request)
    try:
        gemini_client.analyze_screenshots(client, "m", [b"x"], parse_attempts=1, retry_sleep=0)
        assert False
    except gemini_client.GeminiError as e:
        assert "太慢" in str(e)
        assert "read operation" not in str(e)


def test_make_client_sets_timeout_and_retries():
    client = gemini_client.make_client("fake-key")
    opts = client._api_client._http_options
    assert opts.timeout == gemini_client.DEFAULT_TIMEOUT_MS
    assert opts.retry_options.attempts == gemini_client.DEFAULT_RETRY_ATTEMPTS
    assert 429 in opts.retry_options.http_status_codes
    assert 503 in opts.retry_options.http_status_codes


def test_enrich_places_keeps_seed_when_model_adds_facts():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps({
        "places": [{
            "name": "淺草寺", "name_zh": "淺草寺", "category": "attraction",
            "area": "淺草, 東京", "address": "東京都台東區淺草2-3-1",
            "opening_hours": "6:00-17:00", "rating": "4.4/5",
            "confidence": "高", "source_images": [],
        }]
    }, ensure_ascii=False))
    seeds = [trip_core.seed_place("淺草寺")]
    result = gemini_client.enrich_places(client, "m", seeds, retry_sleep=0)
    assert len(result.places) == 1
    assert result.places[0]["address"] == "東京都台東區淺草2-3-1"
    assert result.places[0]["rating"] == "4.4/5"
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "m"
    prompt = kwargs["contents"]
    assert "淺草寺" in prompt and "唔好硬套日本" in prompt
    assert "address" in prompt and "opening_hours" in prompt and "rating" in prompt
    assert "一定要三項都填" in prompt
    assert "只填你有把握" not in prompt
    config = kwargs["config"]
    assert config.thinking_config.thinking_level == "LOW"
    assert config.thinking_config.thinking_budget is None
    assert config.response_schema is gemini_client.ENRICH_SCHEMA
    assert "唔肯定就空字串" not in config.response_schema.properties["places"].items.properties["address"].description
    assert "唔肯定就空字串" in gemini_client.PLACES_SCHEMA.properties["places"].items.properties["address"].description
    assert config.tools is None
    assert config.tool_config is None
    assert config.http_options.timeout == gemini_client.ENRICH_TIMEOUT_MS
    assert config.http_options.retry_options.attempts == 1


def _enrich_ok_payload():
    return _Resp(json.dumps({
        "places": [{
            "name": "淺草寺", "name_zh": "淺草寺", "category": "attraction",
            "area": "淺草, 東京", "address": "東京都台東區淺草2-3-1",
            "opening_hours": "6:00-17:00", "rating": "4.4/5",
            "confidence": "高", "source_images": [],
        }]
    }, ensure_ascii=False))


def test_enrich_defaults_to_lite_and_retries_fast_5xx_once():
    from google.genai import errors

    client = mock.MagicMock()
    client.models.generate_content.side_effect = [
        errors.ServerError(
            503, {"error": {"code": 503, "message": "backend down", "status": "UNAVAILABLE"}}
        ),
        _enrich_ok_payload(),
    ]
    sleeps = []
    with mock.patch.object(gemini_client, "_sleep", side_effect=lambda seconds: sleeps.append(seconds)):
        result = gemini_client.enrich_places(client, None, [trip_core.seed_place("淺草寺")])
    assert result.places[0]["address"] == "東京都台東區淺草2-3-1"
    assert result.places[0]["opening_hours"] == "6:00-17:00"
    assert result.places[0]["rating"] == "4.4/5"
    assert trip_core.place_is_complete(result.places[0])
    assert client.models.generate_content.call_count == 2
    assert sleeps == [gemini_client.ENRICH_RETRY_SLEEP_S]
    assert 1 <= gemini_client.ENRICH_RETRY_SLEEP_S <= 2
    assert client.models.generate_content.call_args.kwargs["model"] == gemini_client.DEFAULT_ENRICH_MODEL
    assert gemini_client.DEFAULT_ENRICH_MODEL == "gemini-3.5-flash-lite"


def test_enrich_does_not_retry_a_slow_5xx():
    from google.genai import errors

    client = mock.MagicMock()
    client.models.generate_content.side_effect = errors.ServerError(
        503, {"error": {"code": 503, "message": "backend down", "status": "UNAVAILABLE"}}
    )
    with mock.patch.object(gemini_client.time, "monotonic", side_effect=[0.0, 20.0]):
        with mock.patch.object(gemini_client, "_sleep", side_effect=AssertionError("should not sleep")):
            try:
                gemini_client.enrich_places(client, "", [trip_core.seed_place("淺草寺")], retry_sleep=0)
                assert False
            except gemini_client.GeminiError as err:
                assert "唔穩定" in str(err)
                assert "backend down" not in str(err)
    assert client.models.generate_content.call_count == 1


def test_enrich_parses_fenced_json_and_biases_fukuoka():
    client = mock.MagicMock()
    body = json.dumps({
        "places": [{
            "name": "kchiara", "name_zh": "キアラ", "category": "restaurant",
            "area": "福岡", "address": "福岡市中央區",
            "opening_hours": "12:00-22:00", "rating": "4.1/5",
            "confidence": "高", "source_images": [],
        }]
    }, ensure_ascii=False)
    client.models.generate_content.return_value = _Resp(f"```json\n{body}\n```")
    seed = trip_core.seed_place("kchiara", name_zh="キアラ", area="福岡")
    result = gemini_client.enrich_places(client, "m", [seed], retry_sleep=0, use_maps=True)
    assert trip_core.place_is_complete(result.places[0])
    assert result.warnings == []
    config = client.models.generate_content.call_args.kwargs["config"]
    lat_lng = config.tool_config.retrieval_config.lat_lng
    assert lat_lng.latitude == 33.59
    assert lat_lng.longitude == 130.40
    tokyo = trip_core.seed_place("淺草寺", area="東京都台東區")
    gemini_client.enrich_places(client, "m", [tokyo], retry_sleep=0, use_maps=True)
    biased = client.models.generate_content.call_args.kwargs["config"].tool_config.retrieval_config.lat_lng
    assert biased.latitude == 35.68
    assert biased.longitude == 139.76
    mixed = [
        trip_core.seed_place("一蘭", area="福岡"),
        trip_core.seed_place("淺草寺", area="東京"),
    ]
    gemini_client.enrich_places(client, "m", mixed, retry_sleep=0, use_maps=True)
    assert client.models.generate_content.call_args.kwargs["config"].tool_config is None


def test_kchiara_text_enrich_keeps_facts_when_model_rewrites_the_name():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps({
        "places": [{
            "name": "キアラ", "name_zh": "琪亞拉", "category": "restaurant",
            "area": "福岡", "address": "福岡市中央區大名1-1-1",
            "opening_hours": "11:30-22:00", "rating": "4.1/5",
            "confidence": "高", "source_images": [],
        }]
    }, ensure_ascii=False))
    seed = trip_core.seed_place("kchiara", area="福岡")
    result = gemini_client.enrich_places(client, "m", [seed], retry_sleep=0)
    assert result.warnings == []
    assert trip_core.place_is_complete(result.places[0])
    assert result.places[0]["address"] == "福岡市中央區大名1-1-1"
    assert result.places[0]["opening_hours"] == "11:30-22:00"
    assert result.places[0]["rating"] == "4.1/5"
    card = trip_core.merge_known_places([seed], result.places)
    assert trip_core.place_is_complete(card[0])
    assert "未有" not in "\n".join(trip_core.place_fact_lines(card[0]))
    prompt = client.models.generate_content.call_args.kwargs["contents"]
    assert "kchiara" in prompt and "福岡" in prompt and "一定要三項都填" in prompt


def test_batch_text_enrich_matches_a_name_with_a_suffix():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps({
        "places": [
            {
                "name": "太宰府天滿宮", "name_zh": "太宰府天滿宮", "category": "attraction",
                "area": "福岡", "address": "福岡県太宰府市宰府4-7-1",
                "opening_hours": "6:00-19:00", "rating": "4.5/5",
                "confidence": "高", "source_images": [],
            },
            {
                "name": "寶滿宮竈門神社", "name_zh": "寶滿宮竈門神社", "category": "attraction",
                "area": "福岡", "address": "福岡県太宰府市大字内山883",
                "opening_hours": "8:30-16:30", "rating": "4.3/5",
                "confidence": "高", "source_images": [],
            },
            {
                "name": "秋月城跡（福岡）", "name_zh": "", "category": "attraction",
                "area": "福岡", "address": "福岡県朝倉市秋月野鳥",
                "opening_hours": "9:00-17:00", "rating": "4.1/5",
                "confidence": "高", "source_images": [],
            },
        ]
    }, ensure_ascii=False))
    seeds = [
        trip_core.seed_place("太宰府天滿宮", area="福岡"),
        trip_core.seed_place("寶滿宮竈門神社", area="福岡"),
        trip_core.seed_place("秋月城跡", area="福岡"),
    ]
    result = gemini_client.enrich_places(client, "m", seeds, retry_sleep=0)
    assert client.models.generate_content.call_count == 1
    assert result.warnings == []
    cards = trip_core.merge_known_places(seeds, result.places)
    assert [place["name"] for place in cards] == ["太宰府天滿宮", "寶滿宮竈門神社", "秋月城跡"]
    assert all(trip_core.place_is_complete(place) for place in cards)
    assert all("未有" not in "\n".join(trip_core.place_fact_lines(place)) for place in cards)
    assert cards[2]["address"] == "福岡県朝倉市秋月野鳥"
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs.get("tools") is None
    assert "太宰府天滿宮" in kwargs["contents"] and "秋月城跡" in kwargs["contents"]


def test_enrich_warns_when_maps_facts_stay_empty():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps({
        "places": [{
            "name": "kchiara", "name_zh": "キアラ", "category": "restaurant",
            "area": "福岡", "address": "", "opening_hours": "", "rating": "",
            "confidence": "低", "source_images": [],
        }]
    }, ensure_ascii=False))
    seed = trip_core.seed_place("kchiara", name_zh="キアラ", area="福岡")
    result = gemini_client.enrich_places(client, "m", [seed], retry_sleep=0)
    assert not trip_core.place_is_complete(result.places[0])
    assert gemini_client.ENRICH_INCOMPLETE_WARNING in result.warnings
    assert gemini_client.MAPS_UNAVAILABLE_WARNING in result.warnings
    assert client.models.generate_content.call_args.kwargs["config"].tools is None
    assert gemini_client.CROWD_RATE_LIMIT_MESSAGE not in " ".join(result.warnings)


def _quota_error(message: str):
    from google.genai import errors

    return errors.ClientError(
        429, {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}
    )


def test_maps_429_falls_back_to_text_enrich_without_false_rate_limit():
    client = mock.MagicMock()
    client.models.generate_content.side_effect = [
        _quota_error("You exceeded your current quota, please check your plan and billing details."),
        _enrich_ok_payload(),
    ]
    result = gemini_client.enrich_places(
        client, None, [trip_core.seed_place("淺草寺", area="東京")], retry_sleep=0, use_maps=True,
    )
    assert trip_core.place_is_complete(result.places[0])
    assert result.warnings == []
    calls = client.models.generate_content.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["model"] == gemini_client.DEFAULT_ENRICH_MODEL
    assert calls[1].kwargs["model"] == gemini_client.DEFAULT_ENRICH_MODEL
    assert calls[0].kwargs["config"].tools[0].google_maps is not None
    assert calls[0].kwargs["config"].response_schema is None
    assert calls[1].kwargs["config"].tools is None
    assert calls[1].kwargs["config"].response_schema is gemini_client.ENRICH_SCHEMA
    assert "人太多" not in calls[1].kwargs["contents"]

    later = mock.MagicMock()
    later.models.generate_content.return_value = _enrich_ok_payload()
    gemini_client.enrich_places(
        later, "m", [trip_core.seed_place("kchiara", area="福岡")], retry_sleep=0, use_maps=True,
    )
    assert later.models.generate_content.call_count == 1
    assert later.models.generate_content.call_args.kwargs["model"] == "m"
    assert later.models.generate_content.call_args.kwargs["config"].tools is None


def test_maps_429_then_empty_text_warns_instead_of_rate_limit():
    client = mock.MagicMock()
    client.models.generate_content.side_effect = [
        _quota_error("You exceeded your current quota, please check your plan and billing details."),
        _Resp(json.dumps({
            "places": [{
                "name": "kchiara", "name_zh": "キアラ", "category": "restaurant",
                "area": "福岡", "address": "", "opening_hours": "", "rating": "",
                "confidence": "低", "source_images": [],
            }]
        }, ensure_ascii=False)),
    ]
    result = gemini_client.enrich_places(
        client, None, [trip_core.seed_place("kchiara", name_zh="キアラ", area="福岡")],
        retry_sleep=0, use_maps=True,
    )
    assert not trip_core.place_is_complete(result.places[0])
    assert gemini_client.ENRICH_INCOMPLETE_WARNING in result.warnings
    assert gemini_client.MAPS_UNAVAILABLE_WARNING in result.warnings
    assert "而家用緊嘅人太多" not in " ".join(result.warnings)


def test_text_enrich_hard_quota_is_not_retried_as_crowd():
    client = mock.MagicMock()
    client.models.generate_content.side_effect = [
        _quota_error("You exceeded your current quota, please check your plan and billing details."),
        _quota_error("You exceeded your current quota, please check your plan and billing details."),
    ]
    with mock.patch.object(gemini_client, "_sleep", side_effect=AssertionError("should not sleep")):
        try:
            gemini_client.enrich_places(client, None, [trip_core.seed_place("kchiara")], retry_sleep=0)
            assert False
        except gemini_client.GeminiError as err:
            assert "額度滿" in str(err)
            assert "而家用緊嘅人太多" not in str(err)
            assert "過一陣" in str(err)
    assert client.models.generate_content.call_count == 1
    assert client.models.generate_content.call_args.kwargs["config"].tools is None


def test_text_enrich_transient_429_retries_once():
    client = mock.MagicMock()
    client.models.generate_content.side_effect = [
        _quota_error("Resource exhausted. Please retry later."),
        _enrich_ok_payload(),
    ]
    sleeps = []
    with mock.patch.object(gemini_client, "_sleep", side_effect=lambda seconds: sleeps.append(seconds)):
        result = gemini_client.enrich_places(client, None, [trip_core.seed_place("淺草寺")], retry_sleep=0)
    assert trip_core.place_is_complete(result.places[0])
    assert client.models.generate_content.call_count == 2
    assert sleeps == [gemini_client.ENRICH_RATE_RETRY_SLEEP_S]
    assert not calls_use_maps(client, 0) and not calls_use_maps(client, 1)


def calls_use_maps(client, index: int) -> bool:
    tools = client.models.generate_content.call_args_list[index].kwargs["config"].tools
    return bool(tools)


def _fukuoka_places():
    names = [
        (1, "鉄板焼 日和", "鐵板燒 日和", "restaurant"),
        (2, "OLGA LE BON BON DONUTS", "Olga 甜甜圈", "dessert"),
        (3, "Hakata Tempura NAGAOKA", "博多天婦羅 NAGAOKA", "restaurant"),
        (4, "GELATERIA SAKURA", "櫻花雪糕", "dessert"),
    ]
    return [
        {
            "name": name, "name_zh": zh, "category": category,
            "area": "博多, 福岡", "address": "福岡市", "opening_hours": "11:00-20:00",
            "rating": "4.0/5", "confidence": "高", "source_images": [1],
            "caption_index": index,
        }
        for index, name, zh, category in names
    ]


def test_direct_city_list_includes_fukuoka_and_skips_ended_routes():
    labels = " ".join(trip_core.destination_labels())
    for city in ("東京", "大阪", "京都", "福岡", "札幌", "名古屋", "那霸", "廣島", "仙台", "高松", "小松", "石垣"):
        assert city in labels
    for city in ("鹿兒島", "熊本", "長崎", "靜岡", "宮古", "下地島"):
        assert city not in labels
    request = trip_core.compose_trip_request("福岡（國泰、香港快運直飛）")
    assert request["destination"] == "福岡"
    assert request["days"] == 3
    assert request["preferences"] == "美食、景點"
    assert request["party"] == "朋友"
    kyoto = trip_core.compose_trip_request("京都（熱門，經大阪關西）")
    assert "大阪" in kyoto["destination"]
    other = trip_core.compose_trip_request(
        trip_core.DESTINATION_OTHER,
        destination_other="韓國首爾",
        days="2",
        preferences=["購物", "其他"],
        preference_other="唔想行太多",
        party="其他",
        party_other="兩代同遊",
        start_date="2026-11-01",
    )
    assert other["destination"] == "韓國首爾"
    assert other["days"] == 2
    assert other["preferences"] == "購物、唔想行太多"
    assert other["party"] == "兩代同遊"
    assert "出發日期：2026-11-01" in other["notes"]
    assert "唔想行太多" in other["notes"]


def test_caption_filter_keeps_requested_numbers():
    places = _fukuoka_places()
    for remarks in ("只要圖中 1、4", "圖中1，4", "①④"):
        kept, note = trip_core.apply_caption_filter(places, remarks)
        assert [place["caption_index"] for place in kept] == [1, 4], remarks
        assert "①" in note and "④" in note
    dropped, note = trip_core.apply_caption_filter(places, "唔要圖中 2、3")
    assert [place["caption_index"] for place in dropped] == [1, 4]
    assert "拿走路" in note
    unnumbered = [{**place, "caption_index": 0} for place in places]
    kept, note = trip_core.apply_caption_filter(unnumbered, "只要圖中 1、4")
    assert len(kept) == 4
    assert "未有編號" in note
    untouched, note = trip_core.apply_caption_filter(places, "想食拉麵")
    assert len(untouched) == 4 and note == ""


def test_analyze_passes_remarks_and_filters_caption_index():
    client = mock.MagicMock()
    payload = {"places": [
        {**place, "caption_index": place["caption_index"]}
        for place in _fukuoka_places()
    ]}
    client.models.generate_content.return_value = _Resp(json.dumps(payload, ensure_ascii=False))
    result = gemini_client.analyze_screenshots(
        client, "m", [b"x"],
        remarks="只要圖中 1、4",
        destination="福岡",
        preferences="美食、景點",
        party="朋友",
        retry_sleep=0,
    )
    assert [place["name"] for place in result.places] == ["鉄板焼 日和", "GELATERIA SAKURA"]
    assert any("①" in warning for warning in result.warnings)
    prompt = client.models.generate_content.call_args.kwargs["contents"][0]
    assert "只要圖中 1、4" in prompt
    assert "福岡" in prompt
    assert "美食" in prompt
    assert "caption_index" in prompt
    assert "唔好硬套日本" in prompt


def test_generate_itinerary_defaults_to_lite_and_surfaces_timeout():
    import httpx

    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps(ITIN, ensure_ascii=False))
    places = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    gemini_client.generate_itinerary(client, None, places, 3, "福岡")
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-3.5-flash-lite"
    assert kwargs["model"] == gemini_client.DEFAULT_ITINERARY_MODEL
    assert kwargs["config"].http_options.timeout == 45_000

    request = httpx.Request("POST", "https://example.test/gemini")
    client.models.generate_content.side_effect = httpx.ReadTimeout(
        "The read operation timed out", request=request,
    )
    try:
        gemini_client.generate_itinerary(client, "", places, 3, "福岡", retry_sleep=0)
        assert False
    except gemini_client.GeminiError as err:
        assert "太慢" in str(err)
        assert "排行程失敗" not in str(err)
        assert "read operation" not in str(err)


def test_itinerary_prompt_uses_preferences_party_and_hours():
    client = mock.MagicMock()
    client.models.generate_content.return_value = _Resp(json.dumps(ITIN, ensure_ascii=False))
    places = trip_core.parse_places_response(json.dumps(PLACES_JSON, ensure_ascii=False))
    gemini_client.generate_itinerary(
        client, "m", places, 4, "福岡", "出發日期：2026-11-01",
        preferences="美食、景點", party="家庭（有小朋友）",
    )
    prompt = client.models.generate_content.call_args.kwargs["contents"]
    assert "4 日" in prompt and "福岡" in prompt
    assert "美食、景點" in prompt
    assert "家庭（有小朋友）" in prompt
    assert "出發日期：2026-11-01" in prompt
    assert "opening_hours" in prompt
    assert "距離" in prompt


def test_collection_attribution_and_itinerary_sources():
    assert trip_core.collect_status({"analysis": "idle"}) == "尚未分析"
    assert trip_core.collect_status({"analysis": "pending"}) == "分析中…"
    assert trip_core.collect_status({"analysis": "failed"}) == "無法辨識"
    assert trip_core.collect_status({
        "analysis": "done",
        "facts": "pending",
        "places": [{"name": "太宰府天滿宮"}],
    }) == "資料核實中…"
    assert trip_core.collect_status({
        "analysis": "done",
        "facts": "done",
        "places": [{"name": "太宰府天滿宮"}],
    }) == "已完成"
    me_html = trip_core.attribution_html("旅人", "旅人")
    friend_html = trip_core.attribution_html("小美", "旅人")
    assert "who-self" in me_html and "#4A90D9" in me_html and ">我<" in me_html
    assert "who-friend" in friend_html and "#8A8A8A" in friend_html and ">小美<" in friend_html
    favs = [{
        "name": "太宰府天滿宮",
        "name_original": "太宰府天滿宮",
        "added_by": "小美",
        "opening_hours": "06:00-19:00",
        "rating": "4.5/5",
    }]
    shown = trip_core.present_itinerary({
        "title": "福岡3日遊",
        "days": [{"day": 1, "items": [
            {"time": "09:00", "place_name": "太宰府天滿宮", "activity": "參拜", "transit": ""},
            {"time": "12:00", "place_name": "大濠公園", "activity": "散步", "transit": "電車 約 25 分鐘"},
        ]}],
    }, favs, allow_ai=False)
    own, extra = shown["days"][0]["items"]
    assert own["ai"] is False and own["hidden"] is False and own["added_by"] == "小美"
    assert own["opening_hours"] == "06:00-19:00" and own["rating"] == "4.5/5"
    assert extra["ai"] is True and extra["hidden"] is True
    assert extra["opening_hours"] == trip_core.UNVERIFIED
    note = trip_core.style_planning_note("平衡型", allow_ai=False)
    assert "平衡型" in note and "4 餐" in note and "唔好加入新店" in note


def test_salon_is_not_labelled_as_shopping():
    place = trip_core.normalize_place({
        "name": "kchiara",
        "name_zh": "奇阿拉美容室",
        "category": "shop",
        "area": "福岡",
        "notes": "參觀選物店",
    })
    assert place["category"] == "salon"
    assert trip_core.favourite_category(place) == "美容"
    assert trip_core.model_category(place) == "salon"
    activity = trip_core.rewrite_activity("奇阿拉美容室", "參觀選物店，約 1 小時。", "shop")
    assert "選物" not in activity
    assert "美容" in activity
    shop = trip_core.normalize_place({"name": "無印良品", "name_zh": "無印良品", "category": "shop"})
    assert shop["category"] == "shop"
    assert trip_core.favourite_category(shop) == "購物"
    assert "選物" in trip_core.rewrite_activity("無印良品", "參觀選物店。", "shop")


def test_near_duplicate_addresses_are_the_same_favourite():
    first = {
        "name": "kchiara",
        "name_zh": "キアラ",
        "address": "福岡市中央區大名1-10-15",
        "image_shas": ["same-photo"],
    }
    second = {
        "name": "キアラ",
        "address": "福岡縣福岡市中央區大名1-10-14",
        "image_shas": ["same-photo"],
    }
    assert trip_core.addresses_near(first["address"], second["address"])
    assert trip_core.same_favourite(first, second)
    assert trip_core.same_favourite(
        {"name": "太宰府天滿宮", "address": "宰府4-7-1"},
        {"name": "太宰府天滿宮", "address": "宰府4-7-2"},
    )
    assert not trip_core.same_favourite(
        {"name": "一蘭", "address": "福岡市博多區中洲1-1-1", "image_shas": ["same-photo"]},
        {"name": "太宰府天滿宮", "address": "太宰府市宰府4-7-1", "image_shas": ["same-photo"]},
    )


def test_kchiara_stays_one_salon_when_address_or_script_drifts():
    bare = {"name": "kchiara", "category": "shop", "address": "福岡県福岡市中央区大名"}
    numbered = {
        "name": "kchiara",
        "category": "shop",
        "address": "福岡県福岡市中央区大名1-10-14",
    }
    assert trip_core.looks_like_salon(bare)
    assert trip_core.favourite_category(bare) == "美容"
    assert trip_core.model_category(bare) == "salon"
    assert trip_core.addresses_near(bare["address"], numbered["address"])
    assert trip_core.addresses_near("福岡市中央區大名1-10-14", "福岡縣福岡市中央區大名")
    assert trip_core.same_favourite(bare, numbered)
    assert trip_core.same_favourite(
        {"name": "kchiara", "address": numbered["address"]},
        {"name": "キアラ", "address": bare["address"]},
    )
    visit = trip_core.rewrite_activity("kchiara", "參觀人氣選物店，挑選特色商品", "shop")
    assert "選物" not in visit and "美容" in visit
    hunt = trip_core.rewrite_activity("kchiara", "再次造訪大名區特色店舖尋寶", "購物")
    assert "尋寶" not in hunt and "購物" not in hunt
    title = trip_core.rewrite_trip_title(
        "福岡 3 日悠閒美食與購物之旅",
        [{"name": "kchiara", "category": "美容"}],
    )
    assert "購物" not in title and "美食" in title
    kept = trip_core.rewrite_trip_title(
        "福岡 3 日悠閒美食與購物之旅",
        [{"name": "kchiara", "category": "美容"}, {"name": "無印良品", "category": "購物"}],
    )
    assert "購物" in kept
    assert not trip_core.same_favourite(
        {"name": "一蘭", "address": "福岡市", "image_shas": ["shot"], "sole_image": False},
        {"name": "太宰府天滿宮", "address": "太宰府市", "image_shas": ["shot"], "sole_image": False},
    )
    shown = trip_core.present_itinerary({
        "title": "福岡 3 日悠閒美食與購物之旅",
        "days": [{"day": 1, "items": [{
            "time": "12:30",
            "place_name": "kchiara",
            "activity": "再次造訪大名區特色店舖尋寶",
            "transit": "步行約 15 分鐘",
        }]}],
    }, [{"name": "kchiara", "category": "美容", "added_by": "旅人"}], allow_ai=True)
    assert "購物" not in shown["title"]
    assert "尋寶" not in shown["days"][0]["items"][0]["activity"]
    assert "美容" in shown["days"][0]["items"][0]["activity"]


def test_infer_city_fukuoka_from_caption_address():
    places = [{
        "name": "博多炉端 魚男",
        "name_zh": "博多爐端 魚男",
        "area": "中洲, 福岡",
        "address": "福岡縣福岡市中央區今泉 1-4-23",
    }]
    assert trip_core.infer_city(places) == "福岡"
    assert trip_core.infer_city([{"area": "澀谷, 東京", "address": ""}]) == "東京"
    assert trip_core.infer_city([{"name": "某店", "area": ""}]) == ""
    assert trip_core.infer_city([
        {"area": "東京", "address": ""},
        {"area": "福岡", "address": ""},
    ]) == ""


def test_classify_region_from_caption_words_and_group_inbox():
    city, confidence = trip_core.classify_region(remark="今泉")
    assert (city, confidence) == ("福岡", trip_core.REGION_CONFIDENT)
    city, confidence = trip_core.classify_region([{"name": "Hakata Tempura"}])
    assert (city, confidence) == ("福岡", trip_core.REGION_CONFIDENT)
    city, confidence = trip_core.classify_region([{"address": "東京都千代田區"}])
    assert (city, confidence) == ("東京", trip_core.REGION_CONFIDENT)
    city, confidence = trip_core.classify_region([{"address": "京都府京都市"}])
    assert (city, confidence) == ("京都", trip_core.REGION_CONFIDENT)
    city, confidence = trip_core.classify_region([
        {"area": "東京"},
        {"area": "福岡"},
    ])
    assert (city, confidence) == ("", trip_core.REGION_LOW)

    fukuoka = {"city": "福岡", "region_confidence": trip_core.REGION_CONFIDENT}
    tokyo = {"city": "東京", "region_confidence": trip_core.REGION_CONFIDENT}
    unsorted = {"city": "", "region_confidence": trip_core.REGION_LOW}
    manual = {"city": "大阪", "region_confidence": trip_core.REGION_MANUAL}
    grouped = trip_core.group_inbox([unsorted, tokyo, fukuoka, manual])
    assert [name for name, _items in grouped] == ["福岡", "東京", "大阪", "未分類"]
    assert trip_core.inbox_region_name(unsorted) == "未分類"
    assert trip_core.inbox_region_name({"city": "東京"}) == "東京"
    assert trip_core.shared_inbox_region([fukuoka, unsorted]) == "福岡"
    assert trip_core.shared_inbox_region([fukuoka, tokyo]) == ""
    assert trip_core.shared_inbox_region([unsorted]) == ""
    chain = {
        "city": "福岡",
        "regions": ["福岡", "東京"],
        "region_confidence": trip_core.REGION_CONFIDENT,
    }
    regions, confidence = trip_core.classify_regions([
        {"name": "一蘭", "name_zh": "一蘭", "area": "福岡、東京"},
    ])
    assert regions == ["福岡", "東京"]
    assert confidence == trip_core.REGION_CONFIDENT
    assert trip_core.recognition_headline({"analysis": "pending", "places": []}) == "認緊"
    assert trip_core.recognition_headline({"analysis": "done", "places": []}) == "認唔到"
    assert trip_core.recognition_headline({
        "analysis": "done",
        "places": [{"name_zh": "一蘭"}],
    }) == "一蘭"
    grouped_chain = trip_core.group_inbox([chain])
    assert [name for name, _items in grouped_chain] == ["福岡", "東京"]
    assert trip_core.shared_inbox_region([chain, fukuoka]) == "福岡"
    assert trip_core.shared_inbox_region([chain]) == ""
    assert trip_core.place_is_complete({
        "name": "一蘭", "address": "福岡市", "opening_hours": "24小時", "rating": "4.0/5",
    })
    assert not trip_core.place_is_complete({"name": "一蘭"})
    assert trip_core.estimate_plan_seconds(0) == trip_core.PLAN_ITINERARY_BUDGET_S
    assert trip_core.estimate_plan_seconds(2) == (
        trip_core.PLAN_ENRICH_BUDGET_S + trip_core.PLAN_ITINERARY_BUDGET_S
    )


def test_decode_clipboard_payload_accepts_image_and_remark():
    raw = _img_bytes("RGB", (20, 10))
    import base64
    payload = {
        "id": "ab" * 16,
        "remark": "只要圖中 1、4",
        "data": base64.b64encode(raw).decode(),
    }
    decoded = trip_core.decode_clipboard_payload(payload)
    assert decoded is not None
    paste_id, jpeg, remark = decoded
    assert paste_id == payload["id"]
    assert remark == "只要圖中 1、4"
    assert jpeg[:2] == b"\xff\xd8"
    assert trip_core.decode_clipboard_payload({"id": "nope", "data": payload["data"]}) is None
    assert trip_core.decode_clipboard_payload(None) is None


def test_paste_component_is_drop_zone_without_clipboard_button():
    html = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "components", "inbox_paste", "index.html"),
        encoding="utf-8",
    ).read()
    assert "將 IG／小紅書／YouTube 截圖拖入或貼上" in html
    assert "從剪貼簿貼上" not in html
    assert "貼上截圖" not in html
    assert "clipboard.read" not in html
    assert 'contenteditable="true"' in html
    assert "insertFromPaste" in html
    assert "paste" in html
    assert "drop" in html
    assert "streamlit:setComponentValue" in html
    assert "<select" not in html.lower()
    assert "目的地" not in html


def test_copy_link_component_uses_in_page_fallback():
    html = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "components", "copy_link", "index.html"),
        encoding="utf-8",
    ).read()
    assert "複製連結" in html
    assert "execCommand" in html
    assert "writeText" in html
    assert "width: 100%" in html
    assert "#FFE3DE" in html
    assert "flex-direction: row" in html
    assert "flex-wrap: nowrap" in html
    assert "white-space: nowrap" in html
    assert "已複製" in html
    assert "請長按上面的連結再複製" in html
    assert "font-size: 16px" in html


def test_mobile_fix_unlocks_fullscreen_images():
    html = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "components", "mobile_fix", "index.html"),
        encoding="utf-8",
    ).read()
    assert "position" in html
    assert "92vw" in html
    assert "stImage" in html


def test_weekday_hours_and_screenshot_rating_beat_a_flat_window():
    printed = "月-金 11:00-20:00／土 10:00-19:00／日 定休"
    place = trip_core.normalize_place({
        "name": "kchiara",
        "name_zh": "kchiara",
        "category": "salon",
        "area": "福岡",
        "hours_on_image": printed,
        "rating_on_image": "4.5",
        "opening_hours": "10:00-20:00",
        "rating": "4.0/5",
    })
    assert place["opening_hours"] == printed
    assert place["rating"] == "4.5/5"
    assert "hours_on_image" in gemini_client.NAMES_PROMPT
    assert "rating_on_image" in gemini_client.NAMES_PROMPT
    assert "10:00-20:00" in gemini_client.NAMES_PROMPT
    schema = gemini_client.NAMES_SCHEMA.model_dump_json()
    assert "hours_on_image" in schema and "rating_on_image" in schema
    merged = trip_core.merge_known_places(
        [place],
        [trip_core.normalize_place({
            "name": "kchiara",
            "opening_hours": "10:00-20:00",
            "rating": "3.9/5",
            "address": "福岡市中央区大名1-10-15",
        })],
    )
    assert merged[0]["opening_hours"] == printed
    assert merged[0]["rating"] == "4.5/5"
    assert "1-10-15" in merged[0]["address"]
    flat = {"opening_hours": "10:00-20:00", "hours_on_image": ""}
    richer = {"opening_hours": printed, "hours_on_image": printed, "name": "kchiara"}
    upgraded = trip_core.merge_known_places(
        [trip_core.normalize_place({"name": "kchiara", **flat, "category": "salon", "area": "福岡"})],
        [trip_core.normalize_place({"name": "kchiara", **richer, "category": "salon", "area": "福岡"})],
    )
    assert "月" in upgraded[0]["opening_hours"]
    assert upgraded[0]["opening_hours"] != "10:00-20:00"


def test_printed_address_beats_a_nearby_guess_and_ties_keep_the_first():
    guessed = "福岡県福岡市中央区大名1-10-15"
    other = "福岡県福岡市中央区大名1-10-14"
    visible = "1-12-8 セシオ大名5階"
    assert trip_core.prefer_visible_address(guessed, visible) == visible
    assert trip_core.prefer_visible_address(guessed, other) == guessed
    place = trip_core.normalize_place({
        "name": "Café Delu",
        "name_zh": "Café Delu",
        "category": "cafe",
        "area": "福岡",
        "address": guessed,
        "address_on_image": visible,
        "notes": "畫面寫住 1-12-8 セシオ大名5階",
    })
    assert place["address"] == visible
    assert "セシオ" in place["address_on_image"]


def test_garbled_ai_stop_is_dropped_and_cloned_days_go_blank():
    assert trip_core.garbled_place_name("Café Del ม")
    assert not trip_core.garbled_place_name("kchiara")
    assert not trip_core.garbled_place_name("太宰府天滿宮")
    shown = trip_core.present_itinerary({
        "title": "福岡 2 日遊",
        "days": [
            {"day": 1, "items": [
                {"time": "10:00", "place_name": "博多站", "activity": "集合", "transit": ""},
                {"time": "16:00", "place_name": "Café Del ม", "activity": "咖啡", "transit": ""},
            ]},
            {"day": 2, "items": [
                {"time": "10:00", "place_name": "博多站", "activity": "集合", "transit": ""},
                {"time": "16:00", "place_name": "Café Del ม", "activity": "咖啡", "transit": ""},
            ]},
            {"day": 3, "items": [
                {"time": "11:00", "place_name": "太宰府天滿宮", "activity": "參拜", "transit": ""},
            ]},
        ],
    }, [], allow_ai=True)
    assert [stop["place_name"] for stop in shown["days"][0]["items"]] == ["博多站"]
    assert shown["days"][1]["items"] == []
    assert [stop["place_name"] for stop in shown["days"][2]["items"]] == ["太宰府天滿宮"]
    assert trip_core.isolate_cloned_days(shown["days"])[2]["items"]

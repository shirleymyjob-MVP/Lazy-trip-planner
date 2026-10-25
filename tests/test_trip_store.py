"""分享行程匣可以寫入再讀返，包括圖片同第一個加入嘅人。"""
import os

import trip_store


def test_save_and_load_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIP_STORE_DIR", str(tmp_path))
    token = trip_store.new_token()
    snapshot = {
        "token": token,
        "inbox": [{
            "id": "ab" * 16,
            "label": "截圖 1",
            "remark": "今泉",
            "status": "未排程",
            "analysis": "done",
            "places": [{"name": "一蘭", "name_zh": "一蘭"}],
            "city": "福岡",
            "regions": ["福岡", "東京"],
            "region_confidence": "高",
            "warning": "",
            "added_by": "Ada",
            "added_at": 99.5,
            "image": b"\xff\xd8fake",
        }],
        "itinerary": {"title": "試"},
        "itinerary_places": [],
        "next_image_num": 2,
        "picks": {"ab" * 16: True},
        "members": ["Ada"],
        "destination_choice": "日本（未指定城市）",
        "days_choice": "3",
        "pref_pills": ["美食"],
        "party_pills": "朋友",
        "destination_other": "",
        "pref_other": "",
        "party_other": "",
    }
    trip_store.save_trip(snapshot)
    loaded = trip_store.load_trip(token)
    assert loaded is not None
    assert loaded["inbox"][0]["added_by"] == "Ada"
    assert loaded["inbox"][0]["added_at"] == 99.5
    assert loaded["inbox"][0]["regions"] == ["福岡", "東京"]
    assert loaded["inbox"][0]["image"] == b"\xff\xd8fake"
    assert loaded["itinerary"]["title"] == "試"
    assert trip_store.digest(snapshot) == trip_store.digest(loaded)
    assert trip_store.load_trip("cd" * 6) is None
    assert os.path.isdir(tmp_path)

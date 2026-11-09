"""Supabase 行程儲存：擁有者核對、上限、失敗時唔退回本機或分享連結。"""
import os
from pathlib import Path

import pytest

import auth_gate
import trip_store


class _Resp:
    def __init__(self, data):
        self.data = data


class HostileClient:
    """select 會交還別人的列。任何寫入或 Storage 呼叫都應被視為失敗。"""

    def __init__(self, row: dict) -> None:
        self.row = row
        self.writes: list[str] = []
        self.storage_calls: list[str] = []
        self.storage = self

    def from_(self, bucket: str):
        self.storage_calls.append(bucket)
        return self

    def upload(self, *_args, **_kwargs):
        raise AssertionError("upload")

    def download(self, *_args, **_kwargs):
        raise AssertionError("download")

    def remove(self, *_args, **_kwargs):
        raise AssertionError("remove")

    def table(self, name: str):
        assert name == "trips"
        return _HostileQuery(self)


class _HostileQuery:
    def __init__(self, client: HostileClient) -> None:
        self.client = client
        self.op = "select"

    def select(self, *_args, **_kwargs):
        self.op = "select"
        return self

    def insert(self, _row):
        self.op = "insert"
        self.client.writes.append("insert")
        return self

    def update(self, _patch):
        self.op = "update"
        self.client.writes.append("update")
        return self

    def delete(self):
        self.op = "delete"
        self.client.writes.append("delete")
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self.op != "select":
            raise AssertionError(self.op)
        return _Resp([self.client.row])


@pytest.fixture
def use_sub(monkeypatch):
    holder = {"sub": None}

    monkeypatch.setattr(auth_gate, "get_member_id", lambda: holder["sub"])

    def _use(sub):
        holder["sub"] = sub

    return _use


def _item(item_id: str, *, name: str = "一蘭", image: bytes | None = None) -> dict:
    blob = b"\xff\xd8fake" if image is None else image
    return {
        "id": item_id,
        "label": "截圖 1",
        "remark": "今泉",
        "status": "未排程",
        "analysis": "done",
        "places": [{"name": name, "name_zh": name}],
        "city": "福岡",
        "regions": ["福岡", "東京"],
        "region_confidence": "高",
        "warning": "",
        "added_by": "Ada",
        "added_at": 99.5,
        "facts": "done",
        "batch": False,
        "image": blob,
    }


def _snap(trip_id: str, inbox: list[dict] | None = None, **extra) -> dict:
    payload = {
        "trip_id": trip_id,
        "title": "行程 1",
        "inbox": inbox or [],
        "itinerary": {"title": "試"},
        "itinerary_places": [],
        "next_image_num": 2,
        "picks": {},
        "members": ["Ada"],
        "destination_choice": "日本（未指定城市）",
        "days_choice": "3",
        "pref_pills": ["美食"],
        "party_pills": "朋友",
        "destination_other": "",
        "pref_other": "",
        "party_other": "",
        "favourites": [],
        "removed_fav_ids": [],
        "trip_style": "平衡型",
        "allow_ai": True,
        "day_count": 3,
    }
    payload.update(extra)
    return payload


def _clear_limits(monkeypatch):
    monkeypatch.delenv("MAX_TRIPS_PER_USER", raising=False)
    monkeypatch.delenv("MAX_IMAGES_PER_USER", raising=False)
    monkeypatch.setattr(trip_store, "_lookup_secret", lambda _path: None)


def test_save_and_load_round_trip_for_the_same_sub(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    item_id = "ab" * 16
    snapshot = _snap(created["id"], [_item(item_id)])
    snapshot["picks"] = {item_id: True}
    snapshot["owner_sub"] = "user-b"
    snapshot["storage_path"] = "user-b/nope/nope.jpg"
    trip_store.save_trip(snapshot)
    loaded = trip_store.load_trip(created["id"])
    assert loaded["inbox"][0]["added_by"] == "Ada"
    assert loaded["inbox"][0]["added_at"] == 99.5
    assert loaded["inbox"][0]["regions"] == ["福岡", "東京"]
    assert loaded["inbox"][0]["image"] == b"\xff\xd8fake"
    assert loaded["itinerary"]["title"] == "試"
    assert trip_store.digest(snapshot) == trip_store.digest(loaded)
    assert memory_supabase.rows[0]["owner_sub"] == "user-a"
    assert "user-b" not in memory_supabase.rows[0]["payload"].get("inbox", [{}])[0].get("id", "")
    path, blob, options = memory_supabase.uploads[-1]
    assert path == f"user-a/{created['id']}/{item_id}.jpg"
    assert blob == b"\xff\xd8fake"
    assert options["content-type"] == "image/jpeg"
    assert options["upsert"] == "true"
    assert memory_supabase.buckets
    assert set(memory_supabase.buckets) == {"trip-images"}
    assert memory_supabase.update_filters[-1] == {
        "id": created["id"],
        "owner_sub": "user-a",
    }
    assert all(filt.get("owner_sub") == "user-a" for filt in memory_supabase.select_filters)


def test_user_cannot_read_modify_or_delete_another_users_trip(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    item_id = "cd" * 16
    trip_store.save_trip(_snap(
        created["id"],
        [_item(item_id, name="秘密拉麵", image=b"\xff\xd8secret-bytes")],
        title="甲的行程",
    ))
    uploads_before = list(memory_supabase.uploads)
    removes_before = list(memory_supabase.removes)
    path = f"user-a/{created['id']}/{item_id}.jpg"

    use_sub("user-b")
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.load_trip(created["id"])
    assert "秘密" not in str(denied.value)
    assert "user-a" not in str(denied.value)
    assert denied.value.args[0] == trip_store.MSG_DENIED

    with pytest.raises(trip_store.TripStoreError) as denied_save:
        trip_store.save_trip(_snap(
            created["id"],
            [_item(item_id, name="被改寫", image=b"\xff\xd8hijack")],
        ))
    assert "user-a" not in str(denied_save.value)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.delete_trip(created["id"])
    with pytest.raises(trip_store.TripStoreError):
        trip_store.delete_image(created["id"], item_id)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.delete_image_path(path)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.delete_image_path(f"user-b/{created['id']}/{item_id}.jpg")

    assert memory_supabase.uploads == uploads_before
    assert memory_supabase.removes == removes_before
    assert memory_supabase.objects[path] == b"\xff\xd8secret-bytes"
    assert [row["id"] for row in trip_store.list_trips()] != [created["id"]]
    assert trip_store.list_trips() == []

    use_sub("user-a")
    loaded = trip_store.load_trip(created["id"])
    assert loaded["inbox"][0]["places"][0]["name"] == "秘密拉麵"
    assert loaded["inbox"][0]["image"] == b"\xff\xd8secret-bytes"
    assert loaded["title"] == "甲的行程"
    summaries = trip_store.list_trips()
    assert [row["id"] for row in summaries] == [created["id"]]
    assert "inbox" not in summaries[0]


def test_guessed_paths_never_touch_storage(use_sub, monkeypatch):
    use_sub("user-a")
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AssertionError("storage client created")

    monkeypatch.setattr(trip_store, "_connect", boom)
    trip_id = "11111111-1111-4111-8111-111111111111"
    item_id = "ab" * 8
    guesses = [
        f"user-b/{trip_id}/{item_id}.jpg",
        f"user-a/../user-b/{item_id}.jpg",
        f"user-a/{trip_id}/../../{item_id}.jpg",
        f"/{ 'user-a' }/{trip_id}/{item_id}.jpg",
        "",
        f"user-a/{trip_id}/../{item_id}.jpg",
        f"user-a/not-a-uuid/{item_id}.jpg",
        f"user-a/{trip_id}/{item_id}.jpg/extra",
        f"user-a/{trip_id}/..jpg",
        "../user-b/secret.jpg",
        f"user-a\\{trip_id}\\{item_id}.jpg",
    ]
    for path in guesses:
        with pytest.raises(trip_store.TripStoreError):
            trip_store.delete_image_path(path)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.load_trip("not-a-uuid")
    with pytest.raises(trip_store.TripStoreError):
        trip_store.load_trip("../etc/passwd")
    with pytest.raises(trip_store.TripStoreError):
        trip_store.load_trip("abc123def456")
    assert calls["n"] == 0


def test_returned_foreign_row_is_not_trusted(use_sub, monkeypatch):
    use_sub("user-b")
    trip_id = "22222222-2222-4222-8222-222222222222"
    foreign = {
        "id": trip_id,
        "owner_sub": "user-a",
        "payload": {"title": "甲", "inbox": [{"id": "ab" * 8, "has_image": True, "label": "秘密"}]},
        "created_at": "t",
        "updated_at": "t",
    }
    hostile = HostileClient(foreign)
    monkeypatch.setattr(trip_store, "_connect", lambda: hostile)
    for action in (
        lambda: trip_store.load_trip(trip_id),
        lambda: trip_store.save_trip(_snap(trip_id, [_item("ab" * 8, name="改")])),
        lambda: trip_store.delete_trip(trip_id),
        lambda: trip_store.delete_image(trip_id, "ab" * 8),
    ):
        with pytest.raises(trip_store.TripStoreError) as denied:
            action()
        assert denied.value.args[0] == trip_store.MSG_DENIED
        assert "秘密" not in str(denied.value)
    assert hostile.writes == []
    assert hostile.storage_calls == []


def test_same_sub_can_delete_only_own_image(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    keep = "aa" * 8
    drop = "bb" * 8
    trip_store.save_trip(_snap(created["id"], [
        _item(keep, image=b"\xff\xd8keep"),
        _item(drop, image=b"\xff\xd8drop"),
    ]))
    trip_store.delete_image(created["id"], drop)
    loaded = trip_store.load_trip(created["id"])
    images = {item["id"]: item["image"] for item in loaded["inbox"]}
    assert images[keep] == b"\xff\xd8keep"
    assert images[drop] == b""
    assert f"user-a/{created['id']}/{drop}.jpg" not in memory_supabase.objects
    assert memory_supabase.removes[-1] == [f"user-a/{created['id']}/{drop}.jpg"]
    trip_store.delete_trip(created["id"])
    assert memory_supabase.rows == []
    assert f"user-a/{created['id']}/{keep}.jpg" not in memory_supabase.objects


def test_default_trip_cap_is_three_and_image_cap_is_fifty(use_sub, monkeypatch):
    _clear_limits(monkeypatch)
    use_sub("user-a")
    assert trip_store.max_trips() == 3
    assert trip_store.max_images() == 50
    created = [trip_store.create_trip() for _ in range(3)]
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.create_trip()
    assert "3" in str(denied.value)
    assert len(trip_store.list_trips()) == 3

    images = [_item(f"i{index:02d}", image=b"\xff\xd8" + bytes([index])) for index in range(50)]
    trip_store.save_trip(_snap(created[0]["id"], images, title="行程 1"))
    loaded = trip_store.load_trip(created[0]["id"])
    assert len(loaded["inbox"]) == 50
    assert loaded["inbox"][49]["image"].startswith(b"\xff\xd8")
    too_many = images + [_item("i50", image=b"\xff\xd8overflow")]
    with pytest.raises(trip_store.TripStoreError) as image_denied:
        trip_store.save_trip(_snap(created[0]["id"], too_many, title="行程 1"))
    assert "50" in str(image_denied.value)
    assert len(trip_store.load_trip(created[0]["id"])["inbox"]) == 50

    use_sub("user-b")
    assert trip_store.create_trip()["title"] == "行程 1"


def test_configurable_caps_count_across_trips(use_sub, monkeypatch):
    monkeypatch.setattr(trip_store, "_lookup_secret", lambda _path: None)
    monkeypatch.setenv("MAX_TRIPS_PER_USER", "2")
    monkeypatch.setenv("MAX_IMAGES_PER_USER", "3")
    use_sub("user-a")
    assert trip_store.max_trips() == 2
    assert trip_store.max_images() == 3
    first = trip_store.create_trip()
    second = trip_store.create_trip()
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.create_trip()
    assert "2" in str(denied.value)
    trip_store.save_trip(_snap(first["id"], [
        _item("aa", image=b"\xff\xd8aa"),
        _item("bb", image=b"\xff\xd8bb"),
    ]))
    trip_store.save_trip(_snap(second["id"], [_item("cc", image=b"\xff\xd8cc")], title="行程 2"))
    with pytest.raises(trip_store.TripStoreError) as image_denied:
        trip_store.save_trip(_snap(second["id"], [
            _item("cc", image=b"\xff\xd8cc"),
            _item("dd", image=b"\xff\xd8dd"),
        ], title="行程 2"))
    assert "3" in str(image_denied.value)
    assert len(trip_store.load_trip(second["id"])["inbox"]) == 1
    assert trip_store.count_images() == 3


def test_secrets_override_env_limits_and_url(use_sub, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://env.example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "from-env")
    monkeypatch.setenv("MAX_TRIPS_PER_USER", "9")
    monkeypatch.setenv("MAX_IMAGES_PER_USER", "9")

    def lookup(path):
        values = {
            ("supabase", "url"): "https://secrets.example.supabase.co",
            ("supabase", "service_role_key"): "from-secret",
            ("limits", "max_trips_per_user"): 2,
            ("limits", "max_images_per_user"): 4,
        }
        return values.get(tuple(path))

    monkeypatch.setattr(trip_store, "_lookup_secret", lookup)
    url, key = trip_store._require_settings()
    assert url == "https://secrets.example.supabase.co"
    assert key == "from-secret"
    assert trip_store.max_trips() == 2
    assert trip_store.max_images() == 4
    use_sub("user-a")
    trip_store.create_trip()
    trip_store.create_trip()
    with pytest.raises(trip_store.TripStoreError):
        trip_store.create_trip()


def test_missing_settings_invalid_url_and_logged_out_do_not_connect(use_sub, monkeypatch, tmp_path):
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("apikey=super-secret-token")

    monkeypatch.setattr(trip_store, "_connect", boom)
    monkeypatch.setattr(trip_store, "_lookup_secret", lambda _path: None)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    use_sub("user-a")
    with pytest.raises(trip_store.TripStoreError) as missing:
        trip_store.list_trips()
    assert missing.value.args[0] == trip_store.MSG_CONFIG
    assert "super-secret" not in str(missing.value)
    assert calls["n"] == 0

    monkeypatch.setenv("SUPABASE_URL", "http://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "from-env")
    with pytest.raises(trip_store.TripStoreError) as insecure:
        trip_store.load_trip("33333333-3333-4333-8333-333333333333")
    assert insecure.value.args[0] == trip_store.MSG_CONFIG
    assert calls["n"] == 0

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    use_sub(None)
    with pytest.raises(trip_store.TripStoreError) as logged_out:
        trip_store.list_trips()
    assert logged_out.value.args[0] == trip_store.MSG_LOGIN
    with pytest.raises(trip_store.TripStoreError):
        trip_store.save_trip(_snap("33333333-3333-4333-8333-333333333333"))
    assert calls["n"] == 0

    use_sub("user-a")
    os.chdir(tmp_path)
    with pytest.raises(trip_store.TripStoreError) as down:
        trip_store.list_trips()
    assert down.value.args[0] == trip_store.MSG_UNAVAILABLE
    assert "super-secret-token" not in str(down.value)
    assert "apikey" not in str(down.value)
    assert calls["n"] == 1
    assert not (tmp_path / "data").exists()
    assert list(tmp_path.iterdir()) == []


def test_store_and_app_do_not_embed_a_service_role_or_local_share_store():
    store = Path(trip_store.__file__).read_text(encoding="utf-8")
    app = Path(trip_store.__file__).resolve().parent.joinpath("app.py").read_text(encoding="utf-8")
    assert "TRIP_STORE_DIR" not in store
    assert "def store_dir" not in store
    assert "mkdir" not in store
    assert "eyJ" not in store
    assert "your-supabase-service-role-key" not in store
    assert 'st.query_params["share"]' not in app
    assert "eyJ" not in app
    assert "先複製邀請連結" not in app
    assert "邀請朋友一起收集" not in app

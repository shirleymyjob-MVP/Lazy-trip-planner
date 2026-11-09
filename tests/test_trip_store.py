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
    snapshot["version"] = created["version"]
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
    assert loaded["version"] == 2
    assert memory_supabase.rows[0]["version"] == 2
    assert memory_supabase.update_filters[-1] == {
        "id": created["id"],
        "owner_sub": "user-a",
        "version": 1,
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
        version=created["version"],
    ))
    saved_version = memory_supabase.rows[0]["version"]
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
    assert memory_supabase.rows[0]["version"] == saved_version
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
    ], version=created["version"]))
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
    trip_store.save_trip(_snap(created[0]["id"], images, title="行程 1", version=created[0]["version"]))
    loaded = trip_store.load_trip(created[0]["id"])
    assert len(loaded["inbox"]) == 50
    assert loaded["inbox"][49]["image"].startswith(b"\xff\xd8")
    too_many = images + [_item("i50", image=b"\xff\xd8overflow")]
    with pytest.raises(trip_store.TripStoreError) as image_denied:
        trip_store.save_trip(_snap(created[0]["id"], too_many, title="行程 1", version=loaded["version"]))
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
    ], version=first["version"]))
    trip_store.save_trip(_snap(second["id"], [_item("cc", image=b"\xff\xd8cc")], title="行程 2", version=second["version"]))
    second_version = trip_store.load_trip(second["id"])["version"]
    with pytest.raises(trip_store.TripStoreError) as image_denied:
        trip_store.save_trip(_snap(second["id"], [
            _item("cc", image=b"\xff\xd8cc"),
            _item("dd", image=b"\xff\xd8dd"),
        ], title="行程 2", version=second_version))
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


def _inbox_rows(memory_supabase):
    return [row for row in memory_supabase.rows if (row.get("payload") or {}).get("kind") == "inbox"]


def _collection_snapshot(inbox=None, **extra):
    payload = _snap("00000000-0000-4000-8000-000000000000", inbox, **extra)
    payload["title"] = extra.get("title", "收藏")
    return payload


def test_collection_inbox_is_not_created_on_load_and_stays_singular(use_sub, memory_supabase):
    use_sub("user-a")
    assert trip_store.load_collection_inbox() is None
    assert trip_store.collection_inbox_id() == ""
    assert trip_store.list_trips() == []
    assert memory_supabase.inserts == []
    item_id = "ab" * 8
    image = b"\xff\xd8inbox-photo"
    first_inbox = _collection_snapshot(
        [_item(item_id, name="太宰府天滿宮", image=image)],
        favourites=[{"id": "fav1", "name": "太宰府天滿宮"}],
    )
    trip_store.save_collection_inbox(first_inbox)
    second_inbox = _collection_snapshot(
        [_item(item_id, name="太宰府天滿宮", image=image)],
        favourites=[{"id": "fav1", "name": "太宰府天滿宮"}],
    )
    second_inbox["version"] = first_inbox["version"]
    trip_store.save_collection_inbox(second_inbox)
    assert len(_inbox_rows(memory_supabase)) == 1
    assert len(memory_supabase.inserts) == 1
    assert trip_store.list_trips() == []
    loaded = trip_store.load_collection_inbox()
    assert loaded["inbox"][0]["image"] == image
    assert loaded["favourites"][0]["name"] == "太宰府天滿宮"
    inbox_id = loaded["trip_id"]
    assert memory_supabase.objects[f"user-a/{inbox_id}/{item_id}.jpg"] == image
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.load_trip(inbox_id)
    assert denied.value.args[0] == trip_store.MSG_DENIED
    created = trip_store.create_trip()
    assert created["title"] == "行程 1"
    assert "kind" not in memory_supabase.rows[-1]["payload"]
    assert len(trip_store.list_trips()) == 1
    assert len(_inbox_rows(memory_supabase)) == 1


def test_collection_inbox_is_outside_the_trip_cap_and_inside_the_image_cap(use_sub, memory_supabase, monkeypatch):
    monkeypatch.setattr(trip_store, "_lookup_secret", lambda _path: None)
    monkeypatch.setenv("MAX_TRIPS_PER_USER", "1")
    monkeypatch.setenv("MAX_IMAGES_PER_USER", "2")
    use_sub("user-a")
    trip_store.save_collection_inbox(_collection_snapshot([
        _item("aa", image=b"\xff\xd8aa"),
        _item("bb", image=b"\xff\xd8bb"),
    ]))
    assert trip_store.count_images() == 2
    assert trip_store.list_trips() == []
    created = trip_store.create_trip()
    assert created["title"] == "行程 1"
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.create_trip()
    assert "1" in str(denied.value)
    assert len(trip_store.list_trips()) == 1
    with pytest.raises(trip_store.TripStoreError) as image_denied:
        trip_store.save_trip(_snap(
            created["id"],
            [_item("cc", image=b"\xff\xd8cc")],
            title="行程 1",
            version=created["version"],
        ))
    assert "2" in str(image_denied.value)
    assert trip_store.count_images() == 2
    assert len(_inbox_rows(memory_supabase)) == 1


def test_adopt_collection_inbox_copies_then_clears(use_sub, memory_supabase):
    use_sub("user-a")
    item_id = "cd" * 8
    image = b"\xff\xd8adopt-me"
    snapshot = _collection_snapshot(
        [_item(item_id, name="清水寺", image=image)],
        favourites=[{"id": "favk", "name": "清水寺"}],
    )
    trip_store.save_collection_inbox(snapshot)
    inbox_id = trip_store.collection_inbox_id()
    adopted = trip_store.adopt_collection_inbox(snapshot)
    assert adopted["title"] == "行程 1"
    assert adopted["id"] != inbox_id
    assert _inbox_rows(memory_supabase) == []
    assert trip_store.load_collection_inbox() is None
    loaded = trip_store.load_trip(adopted["id"])
    assert loaded.get("kind") != "inbox"
    assert loaded["inbox"][0]["image"] == image
    assert loaded["favourites"][0]["name"] == "清水寺"
    assert memory_supabase.objects == {f"user-a/{adopted['id']}/{item_id}.jpg": image}
    assert len(trip_store.list_trips()) == 1


def test_adopt_rolls_back_when_image_upload_fails_midway(use_sub, memory_supabase, monkeypatch):
    use_sub("user-a")
    first = _item("aa", name="一", image=b"\xff\xd8one")
    second = _item("bb", name="二", image=b"\xff\xd8two")
    snapshot = _collection_snapshot([first, second], favourites=[{"id": "f", "name": "一"}])
    trip_store.save_collection_inbox(snapshot)
    inbox_id = trip_store.collection_inbox_id()
    inbox_paths = {
        f"user-a/{inbox_id}/aa.jpg": b"\xff\xd8one",
        f"user-a/{inbox_id}/bb.jpg": b"\xff\xd8two",
    }
    real = trip_store._upload_owned
    calls = {"n": 0}

    def flaky(owner, path, blob):
        if inbox_id in path:
            return real(owner, path, blob)
        calls["n"] += 1
        if calls["n"] == 2:
            raise trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE)
        return real(owner, path, blob)

    monkeypatch.setattr(trip_store, "_upload_owned", flaky)
    with pytest.raises(trip_store.TripStoreError) as failed:
        trip_store.adopt_collection_inbox(snapshot)
    assert failed.value.args[0] == trip_store.MSG_UNAVAILABLE
    assert memory_supabase.objects == inbox_paths
    assert [row["id"] for row in memory_supabase.rows] == [inbox_id]
    assert trip_store.load_collection_inbox()["inbox"][0]["image"] == b"\xff\xd8one"
    monkeypatch.setattr(trip_store, "_upload_owned", real)
    adopted = trip_store.adopt_collection_inbox(snapshot)
    assert len(trip_store.list_trips()) == 1
    loaded = trip_store.load_trip(adopted["id"])
    assert [item["id"] for item in loaded["inbox"]] == ["aa", "bb"]
    assert [item["image"] for item in loaded["inbox"]] == [b"\xff\xd8one", b"\xff\xd8two"]
    assert _inbox_rows(memory_supabase) == []
    assert set(memory_supabase.objects) == {
        f"user-a/{adopted['id']}/aa.jpg",
        f"user-a/{adopted['id']}/bb.jpg",
    }


def test_adopt_rolls_back_new_trip_objects_when_row_update_fails(use_sub, memory_supabase, monkeypatch):
    use_sub("user-a")
    image = b"\xff\xd8update-fail"
    snapshot = _collection_snapshot([_item("aa", image=image)])
    trip_store.save_collection_inbox(snapshot)
    inbox_id = trip_store.collection_inbox_id()
    real = trip_store._update_owned

    def flaky(owner, trip_id, payload, expected_version):
        if payload.get("kind") != "inbox":
            raise trip_store.TripStoreError(trip_store.MSG_CONTENT)
        return real(owner, trip_id, payload, expected_version)

    monkeypatch.setattr(trip_store, "_update_owned", flaky)
    with pytest.raises(trip_store.TripStoreError) as failed:
        trip_store.adopt_collection_inbox(snapshot)
    assert failed.value.args[0] == trip_store.MSG_CONTENT
    assert [row["id"] for row in memory_supabase.rows] == [inbox_id]
    assert memory_supabase.objects == {f"user-a/{inbox_id}/aa.jpg": image}
    monkeypatch.setattr(trip_store, "_update_owned", real)
    adopted = trip_store.adopt_collection_inbox(snapshot)
    assert len(memory_supabase.rows) == 1
    assert memory_supabase.rows[0]["id"] == adopted["id"]
    assert memory_supabase.objects == {f"user-a/{adopted['id']}/aa.jpg": image}


def test_cleanup_failure_keeps_the_new_trip_and_does_not_overwrite_it(use_sub, memory_supabase, monkeypatch, caplog):
    use_sub("user-a")
    image = b"\xff\xd8keep-inbox"
    snapshot = _collection_snapshot(
        [_item("aa", name="嚴島", image=image)],
        favourites=[{"id": "fav", "name": "嚴島"}],
    )
    trip_store.save_collection_inbox(snapshot)
    inbox_id = trip_store.collection_inbox_id()
    real_remove = trip_store._remove_owned

    def flaky(owner, paths):
        if any(inbox_id in path for path in paths):
            raise trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE)
        return real_remove(owner, paths)

    monkeypatch.setattr(trip_store, "_remove_owned", flaky)
    caplog.set_level("WARNING", logger="lazy_trip_planner.store")
    adopted = trip_store.adopt_collection_inbox(snapshot)
    assert adopted["inbox_cleanup_pending"] is True
    assert adopted["id"] != inbox_id
    trip_id = adopted["id"]
    assert memory_supabase.objects[f"user-a/{inbox_id}/aa.jpg"] == image
    assert memory_supabase.objects[f"user-a/{trip_id}/aa.jpg"] == image
    loaded = trip_store.load_trip(trip_id)
    assert loaded["inbox"][0]["image"] == image
    assert loaded["favourites"][0]["name"] == "嚴島"
    pending_logs = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("Collection inbox cleanup pending")
    ]
    assert pending_logs == [
        f"Collection inbox cleanup pending inbox={inbox_id} trip={trip_id} (TripStoreError)"
    ]
    assert "user-a" not in pending_logs[0]
    assert "supabase" not in pending_logs[0].lower()

    edited = trip_store.load_trip(trip_id)
    edited["favourites"] = [{"id": "fav", "name": "已修改"}]
    edited["trip_id"] = trip_id
    trip_store.save_trip(edited)
    stamped = trip_store.load_trip(trip_id)["updated_at"]
    edited_name = trip_store.load_trip(trip_id)["favourites"][0]["name"]
    again = trip_store.adopt_collection_inbox(_collection_snapshot(
        [_item("aa", name="覆蓋", image=b"\xff\xd8other")],
        favourites=[{"id": "fav", "name": "覆蓋"}],
    ))
    assert again["id"] == trip_id
    assert again["inbox_cleanup_pending"] is True
    assert len(trip_store.list_trips()) == 1
    kept = trip_store.load_trip(trip_id)
    assert kept["favourites"][0]["name"] == edited_name
    assert kept["inbox"][0]["image"] == image
    assert kept["updated_at"] == stamped
    assert trip_store.load_collection_inbox()["inbox"][0]["image"] == image

    monkeypatch.setattr(trip_store, "_remove_owned", real_remove)
    trip_store.release_adopted_collection_inbox()
    assert trip_store.load_collection_inbox() is None
    assert trip_store.has_pending_collection_inbox() is False
    assert len(trip_store.list_trips()) == 1
    final = trip_store.load_trip(trip_id)
    assert final["favourites"][0]["name"] == "已修改"
    assert final["inbox"][0]["image"] == image
    assert memory_supabase.objects == {f"user-a/{trip_id}/aa.jpg": image}


def test_inbox_cleanup_only_removes_its_own_image_paths(use_sub, memory_supabase, monkeypatch):
    use_sub("user-a")
    first = b"\xff\xd8image-one"
    second = b"\xff\xd8image-two"
    snapshot = _collection_snapshot([
        _item("aa", name="一", image=first),
        _item("bb", name="二", image=second),
    ])
    trip_store.save_collection_inbox(snapshot)
    inbox_id = trip_store.collection_inbox_id()
    inbox_prefix = f"user-a/{inbox_id}/"
    real_remove = trip_store._remove_owned
    real_upload = trip_store._upload_owned
    seen_removes: list[str] = []

    def flaky(owner, paths):
        seen_removes.extend(paths)
        raise trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE)

    monkeypatch.setattr(trip_store, "_remove_owned", flaky)
    adopted = trip_store.adopt_collection_inbox(snapshot)
    trip_id = adopted["id"]
    trip_prefix = f"user-a/{trip_id}/"
    expected = {
        f"{trip_prefix}aa.jpg": first,
        f"{trip_prefix}bb.jpg": second,
    }

    def trip_images() -> dict[str, bytes]:
        return {
            path: blob for path, blob in memory_supabase.objects.items()
            if path.startswith(trip_prefix)
        }

    assert trip_images() == expected
    assert seen_removes
    assert all(path.startswith(inbox_prefix) and path.count("/") == 2 for path in seen_removes)
    assert all(not path.startswith(trip_prefix) for path in seen_removes)
    assert memory_supabase.objects[f"{inbox_prefix}aa.jpg"] == first
    assert memory_supabase.objects[f"{inbox_prefix}bb.jpg"] == second

    cleanup_removes: list[str] = []
    cleanup_uploads: list[str] = []

    def watch_remove(owner, paths):
        cleanup_removes.extend(paths)
        return real_remove(owner, paths)

    def watch_upload(owner, path, blob):
        cleanup_uploads.append(path)
        return real_upload(owner, path, blob)

    monkeypatch.setattr(trip_store, "_remove_owned", watch_remove)
    monkeypatch.setattr(trip_store, "_upload_owned", watch_upload)
    trip_store.release_adopted_collection_inbox()
    assert trip_images() == expected
    assert cleanup_removes
    assert all(path.startswith(inbox_prefix) and not path.startswith(trip_prefix) for path in cleanup_removes)
    assert all(path.startswith(inbox_prefix) and not path.startswith(trip_prefix) for path in cleanup_uploads)
    assert f"{inbox_prefix}aa.jpg" not in memory_supabase.objects
    assert f"{inbox_prefix}bb.jpg" not in memory_supabase.objects
    assert trip_store.load_trip(trip_id)["inbox"][0]["image"] == first
    assert trip_store.load_trip(trip_id)["inbox"][1]["image"] == second


def test_other_member_cannot_read_or_change_collection_inbox(use_sub, memory_supabase):
    use_sub("user-a")
    image = b"\xff\xd8secret-inbox"
    trip_store.save_collection_inbox(_collection_snapshot(
        [_item("aa", name="秘密拉麵", image=image)],
        favourites=[{"id": "secret", "name": "秘密拉麵"}],
    ))
    inbox_id = trip_store.collection_inbox_id()
    path = f"user-a/{inbox_id}/aa.jpg"
    use_sub("user-b")
    assert trip_store.load_collection_inbox() is None
    assert trip_store.collection_inbox_id() == ""
    assert trip_store.list_trips() == []
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.load_trip(inbox_id)
    assert denied.value.args[0] == trip_store.MSG_DENIED
    assert "秘密" not in str(denied.value)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.delete_trip(inbox_id)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.delete_image_path(path)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.save_trip(_snap(inbox_id, [_item("aa", name="被改寫", image=b"\xff\xd8hijack")]))
    trip_store.save_collection_inbox(_collection_snapshot([_item("bb", name="乙的店", image=b"\xff\xd8b-only")]))
    assert trip_store.collection_inbox_id() != inbox_id
    assert memory_supabase.objects[path] == image
    use_sub("user-a")
    loaded = trip_store.load_collection_inbox()
    assert loaded["inbox"][0]["image"] == image
    assert loaded["favourites"][0]["name"] == "秘密拉麵"
    assert loaded["trip_id"] == inbox_id


def test_rows_without_kind_remain_normal_trips(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    assert "kind" not in memory_supabase.rows[0]["payload"]
    memory_supabase.rows.append({
        "id": "33333333-3333-4333-8333-333333333333",
        "owner_sub": "user-a",
        "payload": {"title": "舊行程", "inbox": [], "favourites": []},
        "created_at": "t0",
        "updated_at": "t0",
        "version": 1,
    })
    summaries = trip_store.list_trips()
    assert created["id"] in [row["id"] for row in summaries]
    assert "33333333-3333-4333-8333-333333333333" in [row["id"] for row in summaries]
    assert all(row["title"] != "收藏" for row in summaries)
    before = dict(memory_supabase.rows[0]["payload"])
    updated = memory_supabase.rows[0]["updated_at"]
    trip_store.load_collection_inbox()
    trip_store.release_adopted_collection_inbox()
    assert memory_supabase.rows[0]["payload"] == before
    assert memory_supabase.rows[0]["updated_at"] == updated


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


def test_second_session_save_is_rejected_and_keeps_the_first(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    trip_id = created["id"]
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa", image=b"\xff\xd8aa")],
        version=created["version"],
        title="原本",
    ))
    loaded = trip_store.load_trip(trip_id)
    assert loaded["version"] == 2
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa", image=b"\xff\xd8aa"), _item("bb", image=b"\xff\xd8bb")],
        version=loaded["version"],
        title="先寫入",
    ))
    uploads_after_winner = len(memory_supabase.uploads)
    with pytest.raises(trip_store.TripConflictError) as conflict:
        trip_store.save_trip(_snap(
            trip_id,
            [_item("aa", image=b"\xff\xd8aa"), _item("cc", image=b"\xff\xd8cc")],
            version=loaded["version"],
            title="後寫入",
        ))
    assert conflict.value.args[0] == trip_store.MSG_CONFLICT
    assert "user-a" not in str(conflict.value)
    kept = trip_store.load_trip(trip_id)
    assert kept["title"] == "先寫入"
    assert kept["version"] == 3
    assert [item["id"] for item in kept["inbox"]] == ["aa", "bb"]
    assert [item["image"] for item in kept["inbox"]] == [b"\xff\xd8aa", b"\xff\xd8bb"]
    assert memory_supabase.objects[f"user-a/{trip_id}/aa.jpg"] == b"\xff\xd8aa"
    assert memory_supabase.objects[f"user-a/{trip_id}/bb.jpg"] == b"\xff\xd8bb"
    assert f"user-a/{trip_id}/cc.jpg" not in memory_supabase.objects
    assert len(memory_supabase.uploads) == uploads_after_winner
    assert memory_supabase.update_filters[-1]["version"] == 2


def test_unchanged_image_is_not_reuploaded_and_explicit_delete_follows_success(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    trip_id = created["id"]
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa", image=b"\xff\xd8aa"), _item("bb", image=b"\xff\xd8bb")],
        version=created["version"],
    ))
    uploads_after_create = len(memory_supabase.uploads)
    loaded = trip_store.load_trip(trip_id)
    loaded["title"] = "改標題"
    loaded["inbox"] = [item for item in loaded["inbox"] if item["id"] == "aa"]
    trip_store.save_trip(loaded)
    assert len(memory_supabase.uploads) == uploads_after_create
    assert memory_supabase.objects[f"user-a/{trip_id}/aa.jpg"] == b"\xff\xd8aa"
    assert f"user-a/{trip_id}/bb.jpg" not in memory_supabase.objects
    renamed = trip_store.load_trip(trip_id)
    assert renamed["title"] == "改標題"
    assert [item["id"] for item in renamed["inbox"]] == ["aa"]

    replaced = trip_store.load_trip(trip_id)
    replaced["inbox"][0]["image"] = b"\xff\xd8zz"
    trip_store.save_trip(replaced)
    assert memory_supabase.uploads[-1][0] == f"user-a/{trip_id}/aa.jpg"
    assert memory_supabase.uploads[-1][1] == b"\xff\xd8zz"
    assert memory_supabase.objects[f"user-a/{trip_id}/aa.jpg"] == b"\xff\xd8zz"
    assert len(memory_supabase.uploads) == uploads_after_create + 1


def test_missing_image_bytes_do_not_delete_a_stored_image(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    trip_id = created["id"]
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa", image=b"\xff\xd8aa")],
        version=created["version"],
    ))
    loaded = trip_store.load_trip(trip_id)
    loaded["inbox"][0]["image"] = b""
    loaded["title"] = "仍然有圖"
    trip_store.save_trip(loaded)
    assert memory_supabase.objects[f"user-a/{trip_id}/aa.jpg"] == b"\xff\xd8aa"
    kept = trip_store.load_trip(trip_id)
    assert kept["inbox"][0]["image"] == b"\xff\xd8aa"
    assert kept["title"] == "仍然有圖"


def test_failed_upload_update_and_conflict_leave_no_orphan(use_sub, memory_supabase, monkeypatch):
    use_sub("user-a")
    created = trip_store.create_trip()
    trip_id = created["id"]
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa", image=b"\xff\xd8aa")],
        version=created["version"],
    ))
    base = trip_store.load_trip(trip_id)
    aa_path = f"user-a/{trip_id}/aa.jpg"
    bb_path = f"user-a/{trip_id}/bb.jpg"
    real_upload = trip_store._upload_owned
    real_update = trip_store._update_owned

    def fail_new(owner, path, blob):
        if path == bb_path:
            raise trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE)
        return real_upload(owner, path, blob)

    monkeypatch.setattr(trip_store, "_upload_owned", fail_new)
    attempt = dict(base)
    attempt["inbox"] = list(base["inbox"]) + [_item("bb", image=b"\xff\xd8bb")]
    with pytest.raises(trip_store.TripStoreError) as upload_failed:
        trip_store.save_trip(attempt)
    assert upload_failed.value.args[0] == trip_store.MSG_UNAVAILABLE
    assert bb_path not in memory_supabase.objects
    assert memory_supabase.objects[aa_path] == b"\xff\xd8aa"
    assert trip_store.load_trip(trip_id)["version"] == base["version"]

    monkeypatch.setattr(trip_store, "_upload_owned", real_upload)

    def fail_update(owner, row_id, payload, expected_version):
        raise trip_store.TripStoreError(trip_store.MSG_UNAVAILABLE)

    monkeypatch.setattr(trip_store, "_update_owned", fail_update)
    dropping = dict(base)
    dropping["inbox"] = []
    dropping["version"] = base["version"]
    with pytest.raises(trip_store.TripStoreError) as update_failed:
        trip_store.save_trip(dropping)
    assert update_failed.value.args[0] == trip_store.MSG_UNAVAILABLE
    assert not isinstance(update_failed.value, trip_store.TripConflictError)
    assert memory_supabase.objects[aa_path] == b"\xff\xd8aa"
    assert trip_store.load_trip(trip_id)["version"] == base["version"]
    assert [item["id"] for item in trip_store.load_trip(trip_id)["inbox"]] == ["aa"]

    def lose_the_race(owner, row_id, payload, expected_version):
        for row in memory_supabase.rows:
            if row["id"] == row_id:
                row["version"] = expected_version + 5
        return real_update(owner, row_id, payload, expected_version)

    monkeypatch.setattr(trip_store, "_update_owned", lose_the_race)
    racing = dict(base)
    racing["inbox"] = list(base["inbox"]) + [_item("bb", image=b"\xff\xd8bb")]
    racing["version"] = base["version"]
    with pytest.raises(trip_store.TripConflictError) as raced:
        trip_store.save_trip(racing)
    assert raced.value.args[0] == trip_store.MSG_CONFLICT
    assert bb_path not in memory_supabase.objects
    assert memory_supabase.objects[aa_path] == b"\xff\xd8aa"
    kept = trip_store.load_trip(trip_id)
    assert [item["id"] for item in kept["inbox"]] == ["aa"]
    assert kept["inbox"][0]["image"] == b"\xff\xd8aa"


def test_missing_version_refuses_without_writing(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    memory_supabase.rows[0].pop("version")
    before = dict(memory_supabase.rows[0]["payload"])
    updates = len(memory_supabase.update_filters)
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.save_trip(_snap(created["id"], title="被改寫", version=1))
    assert denied.value.args[0] == trip_store.MSG_UNAVAILABLE
    assert not isinstance(denied.value, trip_store.TripConflictError)
    assert memory_supabase.rows[0]["payload"] == before
    assert len(memory_supabase.update_filters) == updates
    with pytest.raises(trip_store.TripStoreError) as listed:
        trip_store.list_trips()
    assert listed.value.args[0] == trip_store.MSG_UNAVAILABLE


def test_explicit_image_clear_removes_only_after_update(use_sub, memory_supabase):
    use_sub("user-a")
    created = trip_store.create_trip()
    trip_id = created["id"]
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa", image=b"\xff\xd8aa")],
        version=created["version"],
    ))
    loaded = trip_store.load_trip(trip_id)
    loaded["inbox"][0]["image"] = b""
    loaded["inbox"][0]["has_image"] = False
    trip_store.save_trip(loaded)
    assert f"user-a/{trip_id}/aa.jpg" not in memory_supabase.objects
    cleared = trip_store.load_trip(trip_id)
    assert cleared["inbox"][0]["image"] == b""
    assert cleared["inbox"][0]["has_image"] is False

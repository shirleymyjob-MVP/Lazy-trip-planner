"""共同行程：權限、邀請、上限。只用記憶體替身，不連接正式 Supabase。"""
from datetime import timedelta
from pathlib import Path

import pytest

import auth_gate
import trip_store


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
        "remark": "",
        "status": "未排程",
        "analysis": "done",
        "places": [{"name": name, "name_zh": name}],
        "added_by": "Ada",
        "image": blob,
    }


def _snap(trip_id: str, inbox: list[dict] | None = None, **extra) -> dict:
    payload = {
        "trip_id": trip_id,
        "title": "福岡旅行",
        "inbox": inbox or [],
        "itinerary": None,
        "favourites": [],
        "members": ["Ada"],
        "version": extra.pop("version", None),
    }
    payload.update(extra)
    if payload.get("version") is None:
        payload.pop("version")
    return payload


def _clear_limits(monkeypatch):
    for name in (
        "MAX_TRIPS_PER_USER",
        "MAX_IMAGES_PER_USER",
        "MAX_JOINED_TRIPS_PER_USER",
        "MAX_MEMBERS_PER_TRIP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(trip_store, "_lookup_secret", lambda _path: None)


def test_owner_and_member_coedit_and_stale_version_is_rejected(use_sub, memory_supabase):
    use_sub("owner-sub")
    created = trip_store.create_trip()
    trip_id = created["id"]
    invite = trip_store.create_invite(trip_id)
    assert invite["url"].startswith("https://lazy-trip-planner.streamlit.app/?invite=")
    assert "/join/" not in invite["url"]
    assert memory_supabase.rows[0]["is_shared"] is True
    assert memory_supabase.rows[0]["version"] == 1
    assert all("token" not in row for row in memory_supabase.tables["trip_invites"])
    assert memory_supabase.tables["trip_invites"][0]["token_hash"] != invite["token"]

    use_sub("member-sub")
    joined = trip_store.accept_invite(invite["token"], "朋友 A")
    assert joined["role"] == "member"
    assert joined["already"] is False
    loaded = trip_store.load_trip(trip_id)
    assert loaded["role"] == "member"
    assert loaded["is_shared"] is True
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa" * 8, name="會員新店", image=b"\xff\xd8member")],
        version=loaded["version"],
        title="福岡旅行",
        owner_display_name="唔該改走",
    ))
    use_sub("owner-sub")
    after_member = trip_store.load_trip(trip_id)
    assert after_member["inbox"][0]["places"][0]["name"] == "會員新店"
    assert after_member["owner_display_name"] != "唔該改走"
    assert memory_supabase.objects[f"owner-sub/{trip_id}/{'aa' * 8}.jpg"] == b"\xff\xd8member"
    assert f"member-sub/{trip_id}/" not in "".join(memory_supabase.objects)

    owner_version = after_member["version"]
    trip_store.save_trip(_snap(
        trip_id,
        [_item("aa" * 8, name="擁有者改名", image=b"\xff\xd8member")],
        version=owner_version,
        title="福岡旅行已改",
    ))
    use_sub("member-sub")
    with pytest.raises(trip_store.TripConflictError) as conflict:
        trip_store.save_trip(_snap(
            trip_id,
            [_item("aa" * 8, name="舊版本覆蓋", image=b"\xff\xd8stale")],
            version=owner_version,
            title="舊版本",
        ))
    assert conflict.value.args[0] == trip_store.MSG_CONFLICT
    use_sub("owner-sub")
    kept = trip_store.load_trip(trip_id)
    assert kept["title"] == "福岡旅行已改"
    assert kept["inbox"][0]["places"][0]["name"] == "擁有者改名"
    assert memory_supabase.objects[f"owner-sub/{trip_id}/{'aa' * 8}.jpg"] == b"\xff\xd8member"


def test_private_trip_and_leftover_member_are_denied(use_sub, memory_supabase):
    use_sub("owner-sub")
    created = trip_store.create_trip()
    trip_id = created["id"]
    item_id = "bb" * 8
    trip_store.save_trip(_snap(
        trip_id,
        [_item(item_id, name="秘密拉麵", image=b"\xff\xd8secret")],
        version=created["version"],
    ))
    memory_supabase.tables["trip_members"].append({
        "trip_id": trip_id,
        "member_sub": "member-sub",
        "display_name": "舊成員",
        "joined_at": "2026-01-01T00:00:00+00:00",
    })
    downloads = list(memory_supabase.downloads)
    use_sub("member-sub")
    for action in (
        lambda: trip_store.load_trip(trip_id),
        lambda: trip_store.save_trip(_snap(trip_id, version=1, title="被改")),
        lambda: trip_store.delete_trip(trip_id),
        lambda: trip_store.delete_image(trip_id, item_id),
    ):
        with pytest.raises(trip_store.TripStoreError) as denied:
            action()
        assert denied.value.args[0] == trip_store.MSG_DENIED
        assert "秘密" not in str(denied.value)
        assert "owner-sub" not in str(denied.value)
    assert memory_supabase.downloads == downloads
    assert memory_supabase.objects[f"owner-sub/{trip_id}/{item_id}.jpg"] == b"\xff\xd8secret"
    assert trip_store.list_joined_trips() == []


def test_invite_expired_revoked_full_and_logged_out_are_denied(use_sub, memory_supabase, monkeypatch):
    _clear_limits(monkeypatch)
    monkeypatch.setenv("MAX_MEMBERS_PER_TRIP", "1")
    use_sub("owner-sub")
    created = trip_store.create_trip()
    invite = trip_store.create_invite(created["id"])
    expired = trip_store.create_invite(created["id"])
    memory_supabase.tables["trip_invites"][-1]["expires_at"] = "2000-01-01T00:00:00+00:00"
    use_sub(None)
    with pytest.raises(trip_store.TripStoreError) as logged_out:
        trip_store.accept_invite(invite["token"], "朋友")
    assert logged_out.value.args[0] == trip_store.MSG_LOGIN
    assert invite["token"] not in str(logged_out.value)

    use_sub("late-sub")
    with pytest.raises(trip_store.TripStoreError) as replaced:
        trip_store.accept_invite(invite["token"], "舊連結")
    assert replaced.value.args[0] == trip_store.MSG_INVITE
    with pytest.raises(trip_store.TripStoreError) as old:
        trip_store.accept_invite(expired["token"], "太遲")
    assert old.value.args[0] == trip_store.MSG_INVITE
    assert memory_supabase.tables["trip_members"] == []

    use_sub("owner-sub")
    fresh = trip_store.create_invite(created["id"])
    trip_store.revoke_invites(created["id"])
    use_sub("member-sub")
    with pytest.raises(trip_store.TripStoreError) as revoked:
        trip_store.accept_invite(fresh["token"], "朋友 A")
    assert revoked.value.args[0] == trip_store.MSG_INVITE

    use_sub("owner-sub")
    again = trip_store.create_invite(created["id"])
    use_sub("member-sub")
    trip_store.accept_invite(again["token"], "朋友 A")
    use_sub("other-sub")
    with pytest.raises(trip_store.TripStoreError) as full:
        trip_store.accept_invite(again["token"], "朋友 B")
    assert "5" not in str(full.value) or "1" in str(full.value)
    assert full.value.args[0] == trip_store.members_cap_message()
    assert [row["member_sub"] for row in memory_supabase.tables["trip_members"]] == ["member-sub"]


def test_remove_and_leave_drop_trip_and_image_access(use_sub, memory_supabase):
    use_sub("owner-sub")
    created = trip_store.create_trip()
    trip_id = created["id"]
    item_id = "cc" * 8
    invite = trip_store.create_invite(trip_id)
    use_sub("member-sub")
    trip_store.accept_invite(invite["token"], "朋友 A")
    loaded = trip_store.load_trip(trip_id)
    trip_store.save_trip(_snap(
        trip_id,
        [_item(item_id, image=b"\xff\xd8shared")],
        version=loaded["version"],
    ))
    use_sub("owner-sub")
    with pytest.raises(trip_store.TripStoreError):
        trip_store.remove_member(trip_id, "owner-sub")
    trip_store.remove_member(trip_id, "member-sub")
    downloads = len(memory_supabase.downloads)
    use_sub("member-sub")
    with pytest.raises(trip_store.TripStoreError) as denied:
        trip_store.load_trip(trip_id)
    assert denied.value.args[0] == trip_store.MSG_DENIED
    with pytest.raises(trip_store.TripStoreError):
        trip_store.save_trip(_snap(trip_id, version=99))
    assert len(memory_supabase.downloads) == downloads
    assert memory_supabase.objects[f"owner-sub/{trip_id}/{item_id}.jpg"] == b"\xff\xd8shared"

    use_sub("owner-sub")
    again = trip_store.create_invite(trip_id)
    use_sub("member-sub")
    trip_store.accept_invite(again["token"], "朋友 A")
    trip_store.leave_trip(trip_id)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.load_trip(trip_id)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.create_invite(trip_id)
    with pytest.raises(trip_store.TripStoreError):
        trip_store.delete_trip(trip_id)
    use_sub("owner-sub")
    assert trip_store.load_trip(trip_id)["inbox"][0]["image"] == b"\xff\xd8shared"


def test_owned_and_joined_caps_are_independent_and_images_bill_the_owner(
    use_sub, memory_supabase, monkeypatch,
):
    _clear_limits(monkeypatch)
    monkeypatch.setenv("MAX_TRIPS_PER_USER", "2")
    monkeypatch.setenv("MAX_JOINED_TRIPS_PER_USER", "1")
    monkeypatch.setenv("MAX_MEMBERS_PER_TRIP", "5")
    monkeypatch.setenv("MAX_IMAGES_PER_USER", "2")
    use_sub("owner-sub")
    assert trip_store.max_joined_trips() == 1
    assert trip_store.max_members_per_trip() == 5
    first = trip_store.create_trip()
    second = trip_store.create_trip()
    with pytest.raises(trip_store.TripStoreError) as owned_cap:
        trip_store.create_trip()
    assert "2" in str(owned_cap.value)
    trip_store.save_collection_inbox({
        "title": "收藏",
        "inbox": [_item("in" * 8, image=b"\xff\xd8inbox")],
        "favourites": [],
    })
    assert trip_store.list_trips()
    assert all(row["id"] != trip_store.collection_inbox_id() for row in trip_store.list_trips())
    assert len(trip_store.list_trips()) == 2
    invite = trip_store.create_invite(first["id"])
    other = trip_store.create_invite(second["id"])

    use_sub("member-sub")
    trip_store.create_trip()
    trip_store.create_trip()
    with pytest.raises(trip_store.TripStoreError):
        trip_store.create_trip()
    trip_store.accept_invite(invite["token"], "朋友 A")
    with pytest.raises(trip_store.TripStoreError) as joined_cap:
        trip_store.accept_invite(other["token"], "朋友 A")
    assert joined_cap.value.args[0] == trip_store.joined_cap_message()
    assert len(trip_store.list_trips()) == 2
    assert len(trip_store.list_joined_trips()) == 1

    loaded = trip_store.load_trip(first["id"])
    trip_store.save_trip(_snap(
        first["id"],
        [_item("mm" * 8, image=b"\xff\xd8from-member")],
        version=loaded["version"],
    ))
    with pytest.raises(trip_store.TripStoreError) as images:
        trip_store.save_trip(_snap(
            first["id"],
            [
                _item("mm" * 8, image=b"\xff\xd8from-member"),
                _item("nn" * 8, image=b"\xff\xd8overflow"),
            ],
            version=trip_store.load_trip(first["id"])["version"],
        ))
    assert "2" in str(images.value)
    use_sub("owner-sub")
    assert trip_store.count_images() == 2
    assert trip_store.collection_inbox_status()["items"] == 1
    use_sub("member-sub")
    assert trip_store.load_collection_inbox() is None
    use_sub("owner-sub")
    inbox_id = trip_store.collection_inbox_id()
    use_sub("member-sub")
    with pytest.raises(trip_store.TripStoreError) as inbox_denied:
        trip_store.load_trip(inbox_id)
    assert inbox_denied.value.args[0] == trip_store.MSG_DENIED
    assert trip_store.list_visible_trips()
    assert inbox_id not in [row["id"] for row in trip_store.list_visible_trips()]


def test_member_cannot_manage_invites_and_names_are_not_subs(use_sub, memory_supabase):
    use_sub("owner-sub")
    created = trip_store.create_trip()
    invite = trip_store.create_invite(created["id"])
    trip_store.refresh_actor_name(created["id"], "行程主人")
    use_sub("member-sub")
    trip_store.accept_invite(invite["token"], "member-sub")
    with pytest.raises(trip_store.TripStoreError):
        trip_store.create_invite(created["id"])
    with pytest.raises(trip_store.TripStoreError):
        trip_store.revoke_invites(created["id"])
    with pytest.raises(trip_store.TripStoreError):
        trip_store.remove_member(created["id"], "owner-sub")
    people = trip_store.list_collaborators(created["id"])
    names = [person["display_name"] for person in people]
    assert "行程主人" in names
    assert "旅人" in names
    assert "member-sub" not in names
    assert "owner-sub" not in names
    trip_store.refresh_actor_name(created["id"], "朋友 A")
    renamed = trip_store.list_collaborators(created["id"])
    assert "朋友 A" in [person["display_name"] for person in renamed]
    assert memory_supabase.rows[0]["version"] == 2


def test_travel_dates_stay_in_the_payload(use_sub, memory_supabase):
    use_sub("owner-sub")
    created = trip_store.create_trip()
    trip_store.save_trip(_snap(
        created["id"],
        version=created["version"],
        travel_start="2026-03-15",
        travel_end="2026-03-18",
    ))
    loaded = trip_store.load_trip(created["id"])
    assert loaded["travel_start"] == "2026-03-15"
    assert loaded["travel_end"] == "2026-03-18"
    assert "travel_dates" not in memory_supabase.tables


def test_limits_default_and_secret_override(use_sub, monkeypatch):
    _clear_limits(monkeypatch)
    assert trip_store.max_joined_trips() == 3
    assert trip_store.max_members_per_trip() == 5

    def lookup(path):
        values = {
            ("limits", "max_joined_trips_per_user"): 4,
            ("limits", "max_members_per_trip"): 2,
        }
        return values.get(tuple(path))

    monkeypatch.setattr(trip_store, "_lookup_secret", lookup)
    assert trip_store.max_joined_trips() == 4
    assert trip_store.max_members_per_trip() == 2


def test_migration_stops_on_schema_mismatch_and_rollback_keeps_version():
    sql = Path("docs/migrations/20261110_shared_trips.sql").read_text(encoding="utf-8")
    assert "Expected uuid" in sql
    assert "is_shared already exists" in sql
    assert "trip_members already exists" in sql
    assert "trip_invites already exists" in sql
    assert "FORCE RLS" in sql
    assert "token_hash" in sql
    assert "drop column if exists version" not in sql.lower()
    assert "drop column if exists is_shared" in sql
    assert "drop table if exists public.trip_members" in sql
    assert "drop table if exists public.trip_invites" in sql
    executable = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        executable.append(line)
    body = "\n".join(executable).lower()
    assert "drop column" not in body or "is_shared" in body
    assert "drop column if exists version" not in body


def test_invite_clock_can_be_moved(use_sub, memory_supabase, monkeypatch):
    use_sub("owner-sub")
    created = trip_store.create_trip()
    invite = trip_store.create_invite(created["id"])
    real = trip_store._utcnow
    monkeypatch.setattr(trip_store, "_utcnow", lambda: real() + timedelta(days=8))
    use_sub("member-sub")
    with pytest.raises(trip_store.TripStoreError) as expired:
        trip_store.accept_invite(invite["token"], "朋友 A")
    assert expired.value.args[0] == trip_store.MSG_INVITE

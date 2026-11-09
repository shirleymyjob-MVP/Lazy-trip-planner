"""登入閘：無網絡。未登入不得讀寫行程。"""
import os
import sys
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
APP = os.path.join(ROOT, "app.py")

import auth_gate  # noqa: E402
import trip_store  # noqa: E402

_SECRET = "SUPERSECRETVALUE-should-not-render"
_OTHER_SECRET = "OTHERSECRET-should-not-render"


class _Stop(Exception):
    pass


def _fake_st():
    fake = mock.Mock()
    fake.session_state = {}
    fake.stop.side_effect = _Stop
    fake.secrets.get.return_value = None
    return fake


def _shown(fake) -> str:
    chunks = []
    for method in (
        fake.title,
        fake.subheader,
        fake.error,
        fake.markdown,
        fake.caption,
        fake.button,
    ):
        for call in method.call_args_list:
            chunks.append(repr(call))
    return "\n".join(chunks)


def _ready(monkeypatch, user):
    monkeypatch.setattr(auth_gate, "_authlib_ready", lambda: True)
    monkeypatch.setattr(
        auth_gate,
        "_auth_section",
        lambda: {
            "redirect_uri": "https://app.example/oauth2callback",
            "cookie_secret": _SECRET,
            "client_id": "client",
            "client_secret": _OTHER_SECRET,
            "server_metadata_url": auth_gate.GOOGLE_SERVER_METADATA_URL,
        },
    )
    monkeypatch.setattr(auth_gate.st, "user", user)


def test_version_floor():
    assert auth_gate._version_ok("1.3.2")
    assert auth_gate._version_ok("1.9.2")
    assert auth_gate._version_ok("1.10.0")
    assert auth_gate._version_ok("1.3.2.post1")
    assert not auth_gate._version_ok("1.3.1")
    assert not auth_gate._version_ok("")
    assert not auth_gate._version_ok("not-a-version")


def test_installed_authlib_meets_streamlit_floor():
    assert auth_gate._authlib_ready()


def test_stop_noop_still_does_not_continue(monkeypatch):
    fake = _fake_st()
    fake.stop.side_effect = None
    monkeypatch.setattr(auth_gate, "st", fake)
    monkeypatch.setattr(auth_gate, "_authlib_ready", lambda: True)
    with pytest.raises(auth_gate.StopException):
        auth_gate.require_login()


def test_missing_secrets_stop_without_leaking_or_opening_the_app(monkeypatch):
    fake = _fake_st()
    fake.secrets.get.side_effect = RuntimeError("no secrets file")
    monkeypatch.setattr(auth_gate, "st", fake)
    monkeypatch.setattr(auth_gate, "_authlib_ready", lambda: True)
    with pytest.raises(_Stop):
        auth_gate.require_login()
    text = _shown(fake)
    assert "登入功能未設定好" in text
    assert "cookie_secret" in text
    assert "client_secret" in text
    assert _SECRET not in text
    fake.login.assert_not_called()
    fake.button.assert_not_called()


def test_blank_secret_names_are_listed_but_values_are_not(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    monkeypatch.setattr(auth_gate, "_authlib_ready", lambda: True)
    monkeypatch.setattr(
        auth_gate,
        "_auth_section",
        lambda: {
            "redirect_uri": "https://app.example/oauth2callback",
            "cookie_secret": _SECRET,
            "client_id": "   ",
            "client_secret": _OTHER_SECRET,
            "server_metadata_url": auth_gate.GOOGLE_SERVER_METADATA_URL,
        },
    )
    with pytest.raises(_Stop):
        auth_gate.require_login()
    text = _shown(fake)
    assert "client_id" in text
    assert _SECRET not in text
    assert _OTHER_SECRET not in text
    fake.login.assert_not_called()


def test_bad_redirect_uri_is_not_echoed(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    monkeypatch.setattr(auth_gate, "_authlib_ready", lambda: True)
    hidden = "https://internal.example/not-the-callback"
    monkeypatch.setattr(
        auth_gate,
        "_auth_section",
        lambda: {
            "redirect_uri": hidden,
            "cookie_secret": _SECRET,
            "client_id": "client",
            "client_secret": _OTHER_SECRET,
            "server_metadata_url": auth_gate.GOOGLE_SERVER_METADATA_URL,
        },
    )
    with pytest.raises(_Stop):
        auth_gate.require_login()
    text = _shown(fake)
    assert "oauth2callback" in text
    assert hidden not in text
    assert _SECRET not in text


def test_logged_out_shows_google_button_and_stops(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    _ready(monkeypatch, {"is_logged_in": False, "email": "person@example.com"})
    with pytest.raises(_Stop):
        auth_gate.require_login()
    text = _shown(fake)
    assert "請先用 Google 登入" in text
    assert "使用 Google 登入" in text
    fake.login.assert_not_called()
    assert auth_gate.get_member_id() is None


def test_missing_is_logged_in_attribute_is_logged_out(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    _ready(monkeypatch, {})
    with pytest.raises(_Stop):
        auth_gate.require_login()
    assert "使用 Google 登入" in _shown(fake)


def test_user_lookup_errors_are_logged_out(monkeypatch):
    class Boom:
        def get(self, _name, _default=None):
            raise AttributeError("no user")

    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    _ready(monkeypatch, Boom())
    with pytest.raises(_Stop):
        auth_gate.require_login()
    assert auth_gate.get_member_id() is None


def test_logged_in_sub_is_the_member_id_without_email_verified(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    user = {"is_logged_in": True, "sub": "  google-sub-1  ", "email": "a@b.c"}
    _ready(monkeypatch, user)
    assert auth_gate.require_login() == "google-sub-1"
    assert auth_gate.get_member_id() == "google-sub-1"
    fake.stop.assert_not_called()
    assert "a@b.c" not in _shown(fake)


def test_email_verified_false_does_not_block_sub(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    _ready(
        monkeypatch,
        {"is_logged_in": True, "sub": "sub-2", "email_verified": False},
    )
    assert auth_gate.get_member_id() == "sub-2"
    assert auth_gate.require_login() == "sub-2"


def test_logged_in_without_sub_stops(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    _ready(monkeypatch, {"is_logged_in": True, "email_verified": True, "sub": "  "})
    with pytest.raises(_Stop):
        auth_gate.require_login()
    assert "未能確認帳戶身份" in _shown(fake)
    assert auth_gate.get_member_id() is None


def test_login_failure_does_not_surface_exception_text(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    fake.login.side_effect = RuntimeError("Invalid redirect_uri: https://secret.internal/oauth2callback")
    auth_gate._do_login()
    assert fake.session_state[auth_gate._LOGIN_ERROR_KEY] is True
    _ready(monkeypatch, {"is_logged_in": False})
    with pytest.raises(_Stop):
        auth_gate.require_login()
    text = _shown(fake)
    assert "登入未能開始" in text
    assert "secret.internal" not in text


def test_render_logout_keeps_label_and_calls_st_logout(monkeypatch):
    """登出仍然係 st.logout，而且唔再塞入 1/7 窄欄。"""
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    auth_gate.render_logout()
    fake.columns.assert_not_called()
    fake.button.assert_called_once()
    args, kwargs = fake.button.call_args
    assert args[0] == "登出"
    assert kwargs["key"] == "auth_logout"
    assert kwargs["on_click"] is auth_gate._do_logout
    assert kwargs["width"] == "content"
    kwargs["on_click"]()
    fake.logout.assert_called_once()


def test_logout_failure_still_asks_to_retry_without_leaking(monkeypatch):
    fake = _fake_st()
    monkeypatch.setattr(auth_gate, "st", fake)
    fake.logout.side_effect = RuntimeError("secret end_session_endpoint")
    auth_gate._do_logout()
    assert fake.session_state[auth_gate._LOGOUT_ERROR_KEY] is True
    auth_gate.render_logout()
    text = _shown(fake)
    assert "登出未完成，請再試一次。" in text
    assert "end_session" not in text
    assert "登出" in text


def test_logout_layout_is_not_a_narrow_column():
    source = open(auth_gate.__file__, encoding="utf-8").read()
    body = source.split("def render_logout", 1)[1].split("\ndef ", 1)[0]
    code = body.split('"""', 2)[-1]
    assert "st.columns" not in code
    assert "[6, 1]" not in code
    assert 'key="auth_logout"' in code
    assert "登出" in code


def test_gate_call_is_before_trip_loading():
    source = open(APP, encoding="utf-8").read()
    gate = source.index("auth_gate.require_login()")
    assert gate < source.index("init_state()")
    assert gate < source.index("_ensure_share()")
    assert gate < source.index("_load_shared_trip()")
    assert gate < source.index("_persist_shared_trip()")
    assert gate < source.index('st.caption(f"build:')


def _block_store(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIP_STORE_DIR", str(tmp_path / "trips"))

    def fail(*_args, **_kwargs):
        raise AssertionError("trip store used before login")

    for name in (
        "list_trips",
        "create_trip",
        "load_trip",
        "save_trip",
        "delete_trip",
        "delete_image",
        "delete_image_path",
        "count_images",
    ):
        monkeypatch.setattr(trip_store, name, fail)


def test_logged_out_app_does_not_touch_trips(monkeypatch, tmp_path):
    _block_store(monkeypatch, tmp_path)
    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["share"] = "abc123def456"
    at.run()
    assert not at.exception
    page = "\n".join(block.value for block in list(at.title) + list(at.markdown) + list(at.caption) + list(at.error))
    assert "登入功能未設定好" in page
    assert "開始收集想去的地方" not in page
    assert not any("build:" in caption.value for caption in at.caption)
    assert not (tmp_path / "trips").exists()


def test_configured_but_logged_out_shows_login_and_skips_trips(monkeypatch, tmp_path):
    _block_store(monkeypatch, tmp_path)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["auth"] = {
        "redirect_uri": "http://localhost:8501/oauth2callback",
        "cookie_secret": _SECRET,
        "client_id": "client",
        "client_secret": _OTHER_SECRET,
        "server_metadata_url": auth_gate.GOOGLE_SERVER_METADATA_URL,
    }
    at.query_params["share"] = "abc123def456"
    at.run()
    assert not at.exception
    labels = [button.label for button in at.button]
    assert "使用 Google 登入" in labels
    assert "開始收集想去的地方" not in labels
    page = "\n".join(block.value for block in list(at.markdown) + list(at.caption) + list(at.error))
    assert _SECRET not in page
    assert _OTHER_SECRET not in page
    assert not (tmp_path / "trips").exists()

"""Phase 1：強制 Google 登入閘。

只判斷訪客可否進入應用程式，不讀寫行程，亦不把會員身份寫入 trip 資料。
會員身份只用已驗證 OIDC token 入面的 ``sub``（見 ``get_member_id``）。

Streamlit 1.66.1 行為（實作時對過官方文件同套件原始碼，唔好憑記憶改）：

- ``st.login`` / ``st.logout`` / ``st.user`` 係內建 OIDC。``st.login`` 需要
  ``Authlib>=1.3.2``（``lib/pyproject.toml`` 的 ``auth`` extra 同時需要 ``httpx``）。
- Google 登入成功後，ID token 的 claims（包括 ``sub``、``email_verified``）會複製到
  ``st.user``。``is_logged_in`` 只有設定咗 ``[auth]`` 或者 cookie 已經寫入先存在；
  未設定認證時讀 ``st.user.is_logged_in`` 會 ``AttributeError``。
- 身份 cookie（``_streamlit_user``）``max_age`` 係 30 日，不可設定，亦唔跟 ID token
  的 ``exp``。
- OAuth callback 成功、失敗、或者取消，最後都 ``302`` 去應用程式根路徑 ``/``，
  原本的 query string（包括 ``?share=``）唔會保留，而且會開新 session。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn
from urllib.parse import urlparse

import streamlit as st
from streamlit.runtime.scriptrunner import StopException

# Google 公開的 OIDC discovery 網址。唔係密鑰。
GOOGLE_SERVER_METADATA_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)
_AUTH_KEYS = (
    "redirect_uri",
    "cookie_secret",
    "client_id",
    "client_secret",
    "server_metadata_url",
)
_LOGIN_ERROR_KEY = "_auth_login_error"
_LOGOUT_ERROR_KEY = "_auth_logout_error"


def get_member_id() -> str | None:
    """回傳已登入會員的 OIDC ``sub``。未登入、或者 ``sub`` 缺失／空白就係 None。

    唔使用顯示名、電郵、或者分享 token。只放喺登入 session（``st.user``）度讀，
    呼叫方唔好寫入 ``st.session_state`` 或者行程檔。
    """
    if not _is_logged_in():
        return None
    sub = _claim("sub")
    if not isinstance(sub, str):
        return None
    sub = sub.strip()
    return sub or None


def require_login() -> str:
    """未登入或設定有問題時畫出對應畫面並 ``st.stop()``。通過就回傳 ``sub``。"""
    problems = _config_problems()
    if problems:
        _render_config(problems)
    if not _is_logged_in():
        _render_login()
    member_id = get_member_id()
    if member_id is None:
        _render_missing_sub()
    return member_id


def render_logout() -> None:
    """已登入之後先呼叫。細粒、常駐喺頁頂右側，唔展示電郵或 ``sub``。

    唔好再用 ``st.columns([6, 1])``。手機上第二欄大約 45px，「登出」會摺行，
    只睇到「登」。位置同可點範圍由 ``app.py`` 的 ``.st-key-auth_logout`` 負責：
    掣要避開 3.75rem 高的 Streamlit header，並且完整顯示兩個字。
    """
    st.button(
        "登出",
        key="auth_logout",
        on_click=_do_logout,
        width="content",
    )
    if st.session_state.get(_LOGOUT_ERROR_KEY):
        st.caption("登出未完成，請再試一次。")


def _do_login() -> None:
    try:
        st.login()
    except Exception:
        # StreamlitAuthError 有時會帶 redirect_uri 原文。唔好顯示例外內容。
        st.session_state[_LOGIN_ERROR_KEY] = True


def _do_logout() -> None:
    try:
        st.logout()
    except Exception:
        st.session_state[_LOGOUT_ERROR_KEY] = True


def _render_login() -> None:
    st.title("懶人出行")
    st.subheader("請先用 Google 登入")
    st.markdown("登入之後先可以收集地方同編輯行程。")
    if st.session_state.get(_LOGIN_ERROR_KEY):
        st.error("登入未能開始。請再試一次。如果一直失敗，請管理員檢查登入設定。")
    st.button(
        "使用 Google 登入",
        type="primary",
        key="auth_login",
        on_click=_do_login,
        use_container_width=True,
    )
    st.caption("如果登入被取消或失敗，會回到呢版，再試一次就得。")
    _halt()


def _render_config(problems: list[str]) -> None:
    st.title("懶人出行")
    st.error("登入功能未設定好，暫時未能開放。")
    for problem in problems:
        st.markdown(problem)
    st.caption("呢個畫面唔會顯示任何密鑰，亦唔會讀取或儲存行程。")
    _halt()


def _render_missing_sub() -> None:
    st.title("懶人出行")
    st.error("已登入，但未能確認帳戶身份。請登出後再用 Google 帳戶登入。")
    st.button("登出", key="auth_logout_missing_sub", on_click=_do_logout)
    _halt()


def _config_problems() -> list[str]:
    problems: list[str] = []
    if not _authlib_ready():
        problems.append(
            "伺服器未安裝可用的 Authlib>=1.3.2（Streamlit 的 st.login 需要；"
            "Starlette 整合亦需要 httpx）。請管理員更新依賴後重新啟動。"
        )
    section = _auth_section()
    if section is None:
        problems.append(
            "找不到 [auth] 設定。請管理員在 Streamlit Secrets 加入："
            + "、".join(_AUTH_KEYS)
            + "。"
        )
        return problems
    missing = [key for key in _AUTH_KEYS if _blank(section.get(key))]
    if missing:
        problems.append("[auth] 缺少或空白：" + "、".join(missing) + "。")
        return problems
    redirect_problem = _redirect_problem(section.get("redirect_uri"))
    if redirect_problem:
        problems.append(redirect_problem)
    if _blank(section.get("server_metadata_url")) or (
        str(section.get("server_metadata_url")).strip() != GOOGLE_SERVER_METADATA_URL
    ):
        problems.append(
            "server_metadata_url 必須係 Google 的 OpenID 設定網址："
            + GOOGLE_SERVER_METADATA_URL
            + "。"
        )
    return problems


def _auth_section() -> Mapping[str, object] | None:
    try:
        section = st.secrets.get("auth")
    except Exception:
        return None
    if isinstance(section, Mapping):
        return section
    return None


def _authlib_ready() -> bool:
    try:
        import authlib
        import httpx  # noqa: F401
        from authlib.integrations import starlette_client  # noqa: F401
    except Exception:
        return False
    return _version_ok(getattr(authlib, "__version__", ""))


def _version_ok(raw: object) -> bool:
    parts: list[int] = []
    for piece in str(raw).split(".")[:3]:
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            return False
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= (1, 3, 2)


def _redirect_problem(value: object) -> str | None:
    if not isinstance(value, str):
        return "redirect_uri 必須係文字，而且係絕對網址。"
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "redirect_uri 必須係以 http:// 或 https:// 開頭的絕對網址。"
    if not (parsed.path or "").endswith("/oauth2callback"):
        return "redirect_uri 路徑必須以 /oauth2callback 結尾。"
    return None


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _is_logged_in() -> bool:
    return _claim("is_logged_in") is True


def _halt() -> NoReturn:
    """停喺登入／設定畫面。

    Streamlit 1.66.1 的 ``st.stop()`` 會請 runner 喺下一個 yield 拋 ``StopException``。
    無 ScriptRunContext 時（例如被 import）它會直接返回，所以再拋一次，避免跌進行程讀寫。
    """
    st.stop()
    raise StopException()


def _claim(name: str) -> object:
    """讀 ``st.user``。缺少 claim、或者認證未設定，都回傳 None，唔好爆。"""
    try:
        user = st.user
        getter = getattr(user, "get", None)
        if callable(getter):
            return getter(name, None)
        return user[name]
    except (AttributeError, KeyError, TypeError):
        return None

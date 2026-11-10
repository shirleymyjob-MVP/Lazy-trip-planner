"""Streamlit Cloud 熱重載 app.py 之後，sys.modules 可能仍係舊本地模組。

測試 (a) 靜態掃 app.py 的屬性引用，對住真正模組，改名就會失敗。
測試 (b) 把缺少 list_visible_trips 的舊模組放進 sys.modules，跑 app.py 頂部的守衛。
未加守衛時，呢個測試會走出同正式環境一樣的 AttributeError。
"""
import ast
import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
# app.py 頂部 plain import 的倉庫本地模組。守衛的重載順序見 _LOCAL_MODULE_ORDER。
LOCAL_MODULES = ("auth_gate", "trip_core", "gemini_client", "trip_store")


def _source() -> str:
    return APP.read_text(encoding="utf-8")


def _literal_assign(source: str, name: str):
    tree = ast.parse(source)
    for node in tree.body:
        value = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(value)
    raise AssertionError(f"app.py 沒有 {name}")


def _is_repo_module(name: str) -> bool:
    top = name.split(".", 1)[0]
    return (ROOT / f"{top}.py").is_file()


def _imported_local_modules(source: str) -> list[str]:
    """頂層 import 的本地模組。from-import 會綁住舊物件，守衛重載都修補唔到。"""
    imported: list[str] = []
    from_imports: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_repo_module(alias.name):
                    imported.append(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if _is_repo_module(node.module):
                from_imports.append(node.module)
    assert not from_imports, from_imports
    return imported


def _referenced_local_attrs(source: str) -> dict[str, set[str]]:
    """app.py 需要的屬性：直接屬性、getattr 字面量、同 _call_trip_core 的字面量。"""
    found = {name: set() for name in LOCAL_MODULES}
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in found
        ):
            found[node.value.id].add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in found
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found[node.args[0].id].add(node.args[1].value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_call_trip_core"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found["trip_core"].add(node.args[0].value)
    return found


def _preamble(source: str) -> str:
    """執行到守衛呼叫為止。未加守衛時只執行到頁面設定之前，等舊模組保持舊。"""
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == "_ensure_local_modules()":
            return "".join(lines[: index + 1])
    for index, line in enumerate(lines):
        if line.startswith("st.set_page_config("):
            return "".join(lines[:index])
    raise AssertionError("app.py 沒有模組守衛，也沒有 st.set_page_config")


def _exec_preamble() -> None:
    source = _source()
    namespace = {"__name__": "app_preamble", "__file__": str(APP)}
    exec(compile(_preamble(source), str(APP), "exec"), namespace)


def _module_without(module, attr: str):
    """複製舊模組物件，保留 __spec__／__loader__／__file__，刪走一個新屬性。

    真正的模組物件留喺呼叫方，測試結束可以放返入 sys.modules。
    """
    stale = types.ModuleType(module.__name__)
    stale.__dict__.update(module.__dict__)
    delattr(stale, attr)
    return stale


def test_app_referenced_local_attributes_exist_and_are_guarded():
    """(a) app.py 用到的本地屬性必須真係存在，而且守衛名單要齊。"""
    source = _source()
    referenced = _referenced_local_attrs(source)
    required = _literal_assign(source, "_REQUIRED_LOCAL_ATTRS")
    order = _literal_assign(source, "_LOCAL_MODULE_ORDER")
    imported = _imported_local_modules(source)

    assert list(order) == ["auth_gate", "trip_core", "gemini_client", "trip_store"]
    assert order.index("auth_gate") < order.index("trip_store")
    assert order.index("trip_core") < order.index("gemini_client")
    assert set(order) == set(imported)
    assert set(required) == set(LOCAL_MODULES)

    for name in LOCAL_MODULES:
        module = importlib.import_module(name)
        names = required[name]
        assert list(names) == sorted(set(names))
        assert set(names) == referenced[name]
        missing = [attr for attr in names if not hasattr(module, attr)]
        assert missing == [], (name, missing)
        for attr in referenced[name]:
            assert hasattr(module, attr), (name, attr)

    start = source.index("def _ensure_local_modules")
    body = source[start:source.index("\n_ensure_local_modules()", start)]
    assert "for name in _LOCAL_MODULE_ORDER" in body
    assert "importlib.reload(module)" in body
    assert "RuntimeError" in body


def test_fresh_local_modules_are_not_reloaded(monkeypatch):
    """屬性齊就唔好重載。重載會清走行程鎖，亦會拆走測試替身。"""

    def forbid(module):
        raise AssertionError(f"should not reload {module.__name__}")

    monkeypatch.setattr(importlib, "reload", forbid)
    _exec_preamble()


def test_stale_dependencies_reload_before_dependents(monkeypatch):
    """trip_store 依賴 auth_gate；gemini_client 依賴 trip_core。缺屬性時先重載依賴。"""
    order: list[str] = []
    reals: dict[str, types.ModuleType] = {}

    def tracking_reload(module):
        order.append(module.__name__)
        module.__dict__.update(reals[module.__name__].__dict__)
        return module

    monkeypatch.setattr(importlib, "reload", tracking_reload)
    drops = {
        "auth_gate": "get_member_id",
        "trip_core": "DESTINATION_DEFAULT",
        "gemini_client": "make_client",
        "trip_store": "list_visible_trips",
    }
    for name, attr in drops.items():
        real = sys.modules[name]
        reals[name] = real
        monkeypatch.setitem(sys.modules, name, _module_without(real, attr))

    _exec_preamble()
    assert order == ["auth_gate", "trip_core", "gemini_client", "trip_store"]


def test_stale_trip_store_reload_restores_list_visible_trips(monkeypatch):
    """(b) 舊 trip_store 冇 list_visible_trips。守衛要重載真正模組，之後屬性要在。"""
    real = sys.modules["trip_store"]
    patched_connect = real._connect
    stale = _module_without(real, "list_visible_trips")
    assert stale.__file__ == real.__file__
    assert stale.__loader__ is real.__loader__
    monkeypatch.setitem(sys.modules, "trip_store", stale)

    _exec_preamble()

    # 同 _load_shared_trip 一樣直接讀屬性。未重載就係正式環境的 AttributeError。
    restored = sys.modules["trip_store"].list_visible_trips
    assert sys.modules["trip_store"] is stale
    assert callable(restored)
    assert restored.__module__ == "trip_store"
    assert Path(restored.__code__.co_filename).name == "trip_store.py"
    # 重載的是 sys.modules 入面嗰個舊物件，唔好動到測試進程原本的模組。
    assert real._connect is patched_connect
    assert hasattr(real, "list_visible_trips")


def test_stale_trip_store_still_missing_after_reload_raises(monkeypatch):
    """(b) 重載之後仍然冇個屬性，就要 RuntimeError 講出名字，唔好靜默退回。"""
    real = sys.modules["trip_store"]
    stale = _module_without(real, "list_visible_trips")
    monkeypatch.setitem(sys.modules, "trip_store", stale)

    def reload_leaves_module_unchanged(module):
        return module

    monkeypatch.setattr(importlib, "reload", reload_leaves_module_unchanged)
    with pytest.raises(RuntimeError, match="list_visible_trips") as caught:
        _exec_preamble()
    message = str(caught.value)
    assert "trip_store" in message
    assert "list_visible_trips" in message
    assert "本機" in message
    assert "分享" in message
    assert not hasattr(stale, "list_visible_trips")

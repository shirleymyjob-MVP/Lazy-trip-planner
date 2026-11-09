"""AppTest 預設同步認店名。行程儲存用記憶體替身，唔連接真 Supabase。"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for path in (str(ROOT), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("LAZY_TRIP_SYNC_RECOGNIZE", "1")


@pytest.fixture(autouse=True)
def memory_supabase(monkeypatch):
    import trip_store
    from supabase_memory import MemorySupabase

    client = MemorySupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
    monkeypatch.setattr(trip_store, "_connect", lambda: client)
    return client

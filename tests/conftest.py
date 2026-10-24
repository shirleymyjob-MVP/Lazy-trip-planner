"""AppTest 預設同步認店名，分享行程寫入暫存目錄。"""
import os
import tempfile

os.environ.setdefault("LAZY_TRIP_SYNC_RECOGNIZE", "1")
os.environ.setdefault("TRIP_STORE_DIR", tempfile.mkdtemp(prefix="lazy-trips-"))

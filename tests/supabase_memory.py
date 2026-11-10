"""測試用 Supabase 替身。只存在測試，應用程式唔會因為環境變數改用佢。

``eq`` 條件一齊先算命中，所以 ``version`` 比較交換會喺 0 行時拒絕更新。
``trips``、``trip_members``、``trip_invites`` 分開存放。``rows`` 仍然係行程列，
方便既有測試直接讀寫。
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace


class MemorySupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {
            "trips": [],
            "trip_members": [],
            "trip_invites": [],
        }
        # 既有測試把行程列當成 rows。呢個係同一個 list，唔好換成副本。
        self.rows = self.tables["trips"]
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, bytes, dict]] = []
        self.removes: list[list[str]] = []
        self.downloads: list[str] = []
        self.select_filters: list[dict] = []
        self.update_filters: list[dict] = []
        self.delete_filters: list[dict] = []
        self.inserts: list[dict] = []
        self.buckets: list[str] = []
        self.storage = _Storage(self)

    def table(self, name: str) -> "_Query":
        if name not in self.tables:
            raise AssertionError(name)
        return _Query(self, name)

    def _execute(self, query: "_Query") -> SimpleNamespace:
        filters = dict(query.filters)
        bucket = self.tables[query.table]
        if query.op == "select":
            self.select_filters.append(filters)
            matched = self._match(bucket, filters)
            if query.row_limit is not None:
                matched = matched[: query.row_limit]
            return SimpleNamespace(data=deepcopy(matched))
        if query.op == "insert":
            record = deepcopy(query.payload)
            self.inserts.append(deepcopy(record))
            bucket.append(record)
            return SimpleNamespace(data=[deepcopy(record)])
        if query.op == "update":
            self.update_filters.append(filters)
            updated = []
            for row in bucket:
                if self._is_match(row, filters):
                    row.update(deepcopy(query.payload))
                    updated.append(deepcopy(row))
            return SimpleNamespace(data=updated)
        if query.op == "delete":
            self.delete_filters.append(filters)
            kept = []
            deleted = []
            for row in bucket:
                if self._is_match(row, filters):
                    deleted.append(deepcopy(row))
                else:
                    kept.append(row)
            bucket[:] = kept
            return SimpleNamespace(data=deleted)
        raise AssertionError(query.op)

    def _match(self, rows: list[dict], filters: dict) -> list[dict]:
        return [row for row in rows if self._is_match(row, filters)]

    @staticmethod
    def _is_match(row: dict, filters: dict) -> bool:
        for key, value in filters.items():
            if str(row.get(key)) != str(value):
                return False
        return True


class _Storage:
    def __init__(self, store: MemorySupabase) -> None:
        self.store = store

    def from_(self, bucket: str) -> "_Bucket":
        self.store.buckets.append(bucket)
        if bucket != "trip-images":
            raise AssertionError(bucket)
        return _Bucket(self.store)


class _Bucket:
    def __init__(self, store: MemorySupabase) -> None:
        self.store = store

    def upload(self, path: str, file: bytes, file_options: dict | None = None) -> dict:
        blob = bytes(file)
        options = dict(file_options or {})
        self.store.uploads.append((path, blob, options))
        self.store.objects[path] = blob
        return {"path": path}

    def download(self, path: str) -> bytes:
        self.store.downloads.append(path)
        if path not in self.store.objects:
            raise FileNotFoundError(path)
        return self.store.objects[path]

    def remove(self, paths: list[str]) -> list:
        copied = list(paths)
        self.store.removes.append(copied)
        for path in copied:
            self.store.objects.pop(path, None)
        return []


class _Query:
    def __init__(self, store: MemorySupabase, table: str) -> None:
        self.store = store
        self.table = table
        self.op = ""
        self.payload = None
        self.filters: dict = {}
        self.row_limit = None

    def select(self, *_columns: str) -> "_Query":
        self.op = "select"
        return self

    def insert(self, row: dict) -> "_Query":
        self.op = "insert"
        self.payload = row
        return self

    def update(self, patch: dict) -> "_Query":
        self.op = "update"
        self.payload = patch
        return self

    def delete(self) -> "_Query":
        self.op = "delete"
        return self

    def eq(self, field: str, value: object) -> "_Query":
        self.filters[field] = value
        return self

    def limit(self, count: int) -> "_Query":
        self.row_limit = count
        return self

    def execute(self) -> SimpleNamespace:
        return self.store._execute(self)

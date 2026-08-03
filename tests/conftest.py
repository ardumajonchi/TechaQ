# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Test bootstrap: puts python/ on sys.path, and installs a minimal in-memory-SQLite stand-in for
`arduino.app_bricks.dbstorage_sqlstore` so `engine/db.py`'s top-level
`from arduino.app_bricks.dbstorage_sqlstore import SQLStore` succeeds in a plain CI/dev
environment with no on-device Arduino SDK installed and no board attached (engine/ai_search.py and
engine/ocr.py don't need this -- they only import `arduino.app_bricks.llm` inside their own
try/except, and requests/PIL directly).

This stub implements exactly the documented SQLStore surface (per the brick's API.md:
start/stop/create_table/store/read/update/delete/execute_sql) on top of the real sqlite3 module,
so tests exercise the REAL `BookDB` end-to-end (per the testing brief) rather than a fake DB layer
-- only the brick boundary itself is stubbed, everything above it (BookDB, Library) is real code.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))


def _install_arduino_dbstorage_stub() -> None:
    if "arduino.app_bricks.dbstorage_sqlstore" in sys.modules:
        return  # real SDK (or an earlier stub) already present -- don't shadow it

    class DBStorageSQLStoreError(Exception):
        pass

    class SQLStore:
        """Sqlite3-backed stand-in matching dbstorage_sqlstore's documented API.md surface."""

        def __init__(self, database_name: str = "arduino.db"):
            self.database_name = database_name
            self._conn: sqlite3.Connection | None = None
            self._lock = threading.Lock()

        def start(self) -> None:
            self._conn = sqlite3.connect(self.database_name, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

        def stop(self) -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        def create_table(self, table: str, columns: dict) -> None:
            cols_sql = ", ".join(f"{name} {sqltype}" for name, sqltype in columns.items())
            with self._lock:
                self._conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols_sql})")
                self._conn.commit()

        def drop_table(self, table: str) -> None:
            with self._lock:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
                self._conn.commit()

        def store(self, table: str, data: dict, create_table: bool = True) -> None:
            keys = list(data.keys())
            placeholders = ", ".join("?" for _ in keys)
            cols_sql = ", ".join(keys)
            with self._lock:
                self._conn.execute(
                    f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})",
                    [data[k] for k in keys],
                )
                self._conn.commit()

        def read(self, table: str, columns=None, condition=None, order_by=None, limit: int = -1) -> list:
            cols_sql = ", ".join(columns) if columns else "*"
            sql = f"SELECT {cols_sql} FROM {table}"
            if condition:
                sql += f" WHERE {condition}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            if limit and limit != -1:
                sql += f" LIMIT {int(limit)}"
            with self._lock:
                try:
                    rows = self._conn.execute(sql).fetchall()
                except sqlite3.OperationalError:
                    return []  # matches documented "empty list if table doesn't exist"
            return [dict(r) for r in rows]

        def update(self, table: str, data: dict, condition: str = "") -> None:
            keys = list(data.keys())
            set_sql = ", ".join(f"{k} = ?" for k in keys)
            sql = f"UPDATE {table} SET {set_sql}"
            if condition:
                sql += f" WHERE {condition}"
            with self._lock:
                self._conn.execute(sql, [data[k] for k in keys])
                self._conn.commit()

        def delete(self, table: str, condition: str = "") -> None:
            sql = f"DELETE FROM {table}"
            if condition:
                sql += f" WHERE {condition}"
            with self._lock:
                self._conn.execute(sql)
                self._conn.commit()

        def execute_sql(self, sql: str, args=None):
            with self._lock:
                cur = self._conn.execute(sql, args or ())
                if cur.description is None:
                    self._conn.commit()
                    return None
                rows = cur.fetchall()
            return [dict(r) for r in rows]

    sqlstore_mod = types.ModuleType("arduino.app_bricks.dbstorage_sqlstore")
    sqlstore_mod.SQLStore = SQLStore
    sqlstore_mod.DBStorageSQLStoreError = DBStorageSQLStoreError

    app_bricks_pkg = types.ModuleType("arduino.app_bricks")
    app_bricks_pkg.dbstorage_sqlstore = sqlstore_mod

    arduino_pkg = types.ModuleType("arduino")
    arduino_pkg.app_bricks = app_bricks_pkg

    sys.modules["arduino"] = arduino_pkg
    sys.modules["arduino.app_bricks"] = app_bricks_pkg
    sys.modules["arduino.app_bricks.dbstorage_sqlstore"] = sqlstore_mod


_install_arduino_dbstorage_stub()

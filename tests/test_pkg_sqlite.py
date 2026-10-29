"""pyxpo-sqlite: the expo-sqlite API on the stdlib sqlite3 module."""
import asyncio
import os
import sqlite3
import sys

import pytest

from pyxpo import App, LocalChannel, Text, View, component
from pyxpo.renderers.mock import MockRenderer

import pyxpo_sqlite as SQLite
from pyxpo_sqlite import SQLiteOpenOptions


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield
    for db in list(SQLite._open.values()):
        db._refs = 1
        db.close_sync()


SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE todo (id INTEGER PRIMARY KEY, title TEXT NOT NULL, done INTEGER DEFAULT 0, data BLOB);
"""


def test_default_directory_and_crud(tmp_path):
    async def main():
        db = await SQLite.open_database_async("app.db")
        assert db.database_path == os.path.join(str(tmp_path), ".pyxpo-data", "documents", "SQLite", "app.db")
        assert SQLite.default_database_directory == os.path.dirname(db.database_path)
        await db.exec_async(SCHEMA)
        r = await db.run_async("INSERT INTO todo (title, data) VALUES (?, ?)", "milk", b"\x00\x01")
        assert (r.last_insert_row_id, r.changes) == (1, 1) and r["lastInsertRowId"] == 1
        await db.run_async("INSERT INTO todo (title) VALUES (?)", ["eggs"])
        await db.run_async("INSERT INTO todo (title, done) VALUES ($t, :d)", {"$t": "tea", ":d": 1})
        await db.run_async("INSERT INTO todo (title) VALUES (@t)", {"@t": "jam"})
        r = await db.run_async("UPDATE todo SET done = 1 WHERE done = 0")
        assert r.changes == 3
        first = await db.get_first_async("SELECT * FROM todo WHERE id = ?", 1)
        assert first == {"id": 1, "title": "milk", "done": 1, "data": b"\x00\x01"}
        assert await db.get_first_async("SELECT * FROM todo WHERE id = 99") is None
        rows = await db.get_all_async("SELECT title FROM todo ORDER BY id")
        assert [x["title"] for x in rows] == ["milk", "eggs", "tea", "jam"]
        each = [x["id"] async for x in db.get_each_async("SELECT id FROM todo ORDER BY id")]
        assert each == [1, 2, 3, 4]
        assert await db.getAllAsync("SELECT count(*) AS n FROM todo") == [{"n": 4}]
        await db.close_async()
        with pytest.raises(sqlite3.ProgrammingError):
            await db.get_all_async("SELECT 1")
    asyncio.run(main())


def test_persist_reopen_shared_connection_and_delete(tmp_path):
    d = str(tmp_path / "dbs")
    db = SQLite.open_database_sync("p.db", directory=d)
    db.exec_sync("CREATE TABLE t (v); INSERT INTO t VALUES (7)")
    same = SQLite.open_database_sync("p.db", directory=d)
    assert same is db
    other = SQLite.open_database_sync("p.db", SQLiteOpenOptions(use_new_connection=True), directory=d)
    assert other is not db and other.get_first_sync("SELECT v FROM t") == {"v": 7}
    other.close_sync()
    db.close_sync()
    with pytest.raises(sqlite3.OperationalError):  # still open through `same`
        SQLite.delete_database_sync("p.db", directory=d)
    same.close_sync()
    again = SQLite.open_database_sync("p.db", directory=d)
    assert again is not db and again.get_all_sync("SELECT v FROM t") == [{"v": 7}]
    again.close_sync()
    SQLite.delete_database_sync("p.db", directory=d)
    assert not any(n.startswith("p.db") for n in os.listdir(d))
    with pytest.raises(FileNotFoundError):
        SQLite.delete_database_sync("p.db", directory=d)


def test_memory_databases_are_private():
    a = SQLite.open_database_sync(":memory:")
    b = SQLite.open_database_sync(":memory:")
    a.exec_sync("CREATE TABLE t (v)")
    assert a is not b and b.get_all_sync("SELECT name FROM sqlite_master") == []
    a.close_sync()
    b.close_sync()


def test_transactions():
    async def main():
        db = await SQLite.open_database_async("tx.db")
        await db.exec_async("CREATE TABLE t (v INTEGER)")

        async def ok():
            assert await db.is_in_transaction_async()
            await db.run_async("INSERT INTO t VALUES (1)")
        await db.with_transaction_async(ok)
        assert not await db.is_in_transaction_async()

        async def bad():
            await db.run_async("INSERT INTO t VALUES (2)")
            raise ValueError("stop")
        with pytest.raises(ValueError):
            await db.with_transaction_async(bad)
        assert await db.get_all_async("SELECT v FROM t") == [{"v": 1}]

        async def excl(txn):
            await txn.run_async("INSERT INTO t VALUES (3)")
            assert await txn.get_first_async("SELECT count(*) AS n FROM t") == {"n": 2}
        await db.with_exclusive_transaction_async(excl)
        assert await db.get_all_async("SELECT v FROM t ORDER BY v") == [{"v": 1}, {"v": 3}]

        def sync_bad():
            db.run_sync("INSERT INTO t VALUES (4)")
            raise KeyError("x")
        with pytest.raises(KeyError):
            db.with_transaction_sync(sync_bad)
        assert db.get_first_sync("SELECT count(*) AS n FROM t") == {"n": 2}
        await db.close_async()
    asyncio.run(main())


def test_prepared_statement():
    async def main():
        db = await SQLite.open_database_async(":memory:")
        await db.exec_async("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        ins = await db.prepare_async("INSERT INTO t (name) VALUES ($name)")
        for n in ("a", "b", "c"):
            r = await ins.execute_async({"$name": n})
        assert r.last_insert_row_id == 3 and r.changes == 1
        assert await ins.get_column_names_async() == []
        await ins.finalize_async()
        with pytest.raises(sqlite3.ProgrammingError):
            await ins.execute_async({"$name": "d"})
        sel = await db.prepare_async("SELECT id, name FROM t WHERE id >= ? ORDER BY id")
        assert await sel.get_column_names_async() == ["id", "name"]
        res = await sel.execute_async(2)
        assert await res.get_first_async() == {"id": 2, "name": "b"}
        assert [x["name"] async for x in res] == ["b", "c"]
        assert await res.get_all_async() == [{"id": 2, "name": "b"}, {"id": 3, "name": "c"}]
        named = db.prepare_sync("SELECT name AS n FROM t WHERE name = :n")
        assert named.get_column_names_sync() == ["n"]
        assert named.execute_sync({":n": "c"}).get_all_sync() == [{"n": "c"}]
        await db.close_async()
    asyncio.run(main())


def test_errors_raise():
    db = SQLite.open_database_sync(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        db.run_sync("INSERT INTO nope VALUES (1)")
    db.exec_sync("CREATE TABLE u (v UNIQUE)")
    db.run_sync("INSERT INTO u VALUES (1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.run_sync("INSERT INTO u VALUES (1)")
    db.close_sync()


@pytest.mark.skipif(sys.version_info < (3, 11), reason="serialize needs Python 3.11")
def test_serialize_round_trip():
    db = SQLite.open_database_sync(":memory:")
    db.exec_sync("CREATE TABLE t (v); INSERT INTO t VALUES ('x')")
    data = db.serialize_sync()
    copy = SQLite.deserialize_database_sync(data)
    assert copy.get_all_sync("SELECT v FROM t") == [{"v": "x"}]
    db.close_sync()
    copy.close_sync()


def test_provider_runs_on_init_and_gives_context():
    seen = []

    async def migrate(db):
        await db.exec_async("CREATE TABLE IF NOT EXISTS t (v); INSERT INTO t VALUES ('hi')")

    @component
    def Inner():
        db = SQLite.use_sqlite_context()
        seen.append(db)
        return Text(db.get_first_sync("SELECT v FROM t")["v"])

    @component
    def Root():
        return View(SQLite.SQLiteProvider(Inner(), database_name="prov.db", on_init=migrate,
                                          fallback=Text("loading")))

    async def main():
        ch = LocalChannel()
        r = MockRenderer(ch)
        App(Root, ch, asyncio.get_running_loop()).start()
        await asyncio.sleep(0.3)
        return r

    r = asyncio.run(main())
    assert r.texts() == ["hi"] and seen[0].database_name == "prov.db"


def test_context_without_provider_raises():
    errors = []

    @component
    def Bad():
        try:
            SQLite.use_sqlite_context()
        except RuntimeError as e:
            errors.append(str(e))
        return Text("x")

    ch = LocalChannel()
    MockRenderer(ch)
    App(Bad, ch, None).start()
    assert errors and "SQLiteProvider" in errors[0]


def test_documents_on_ios_and_mac_catalyst(monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    monkeypatch.setattr(SQLite.sys, "platform", "ios")
    assert SQLite._documents() == "/h/Documents"
    # The Mac Catalyst app: Python reports "darwin" and cwd is "/" (read-only); the iOS runtime sets PYXPO_TARGET.
    monkeypatch.setattr(SQLite.sys, "platform", "darwin")
    monkeypatch.setenv("PYXPO_TARGET", "ios")
    assert SQLite._documents() == "/h/Documents"

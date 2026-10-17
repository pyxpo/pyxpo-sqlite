"""SQLite databases that persist across app restarts: the expo-sqlite API.

    import pyxpo_sqlite as SQLite

    db = await SQLite.open_database_async("app.db")
    await db.exec_async('''
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS todo (id INTEGER PRIMARY KEY, title TEXT NOT NULL, done INTEGER DEFAULT 0);
    ''')
    res = await db.run_async("INSERT INTO todo (title) VALUES (?)", "Buy milk")
    res.last_insert_row_id, res.changes
    row = await db.get_first_async("SELECT * FROM todo WHERE id = ?", res.last_insert_row_id)   # dict or None
    rows = await db.get_all_async("SELECT * FROM todo WHERE done = $done", {"$done": 0})
    async for row in db.get_each_async("SELECT * FROM todo"):
        ...
    async def move():
        await db.run_async("UPDATE todo SET done = 1 WHERE id = ?", 1)
    await db.with_transaction_async(move)                        # rolls back if move raises

Databases live in `<documents>/SQLite/` (`default_database_directory`). The phone's Python ships `sqlite3`, so
this package needs no native code; async methods run on one worker thread per connection.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

__version__ = "0.1.0a1"

Row = Dict[str, Any]
_BATCH = 64


def _ios_container() -> bool:
    """iOS, or the iOS shell built for Mac (Catalyst Python reports sys.platform "darwin"): HOME is laid out
    like an iOS app container."""
    return sys.platform == "ios" or os.environ.get("PYXPO_TARGET") == "ios"


def _documents() -> str:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if _ios_container():
        return os.path.join(home, "Documents")
    if sys.platform == "android":
        return home  # the app's filesDir
    return os.path.join(os.getcwd(), ".pyxpo-data", "documents")


def _default_dir() -> str:
    return os.path.join(_documents(), "SQLite")


def __getattr__(name: str) -> Any:
    if name in ("default_database_directory", "defaultDatabaseDirectory"):
        return _default_dir()
    raise AttributeError(name)


@dataclass(frozen=True)
class SQLiteRunResult:
    last_insert_row_id: int
    changes: int

    @property
    def lastInsertRowId(self) -> int:  # noqa: N802
        return self.last_insert_row_id

    def __getitem__(self, key: str) -> int:
        return {"lastInsertRowId": self.last_insert_row_id, "changes": self.changes}[key]


@dataclass(frozen=True)
class SQLiteOpenOptions:
    """`use_new_connection`: open a separate connection instead of sharing the open one for this file."""
    use_new_connection: bool = False


def _params(args: Sequence[Any]) -> Any:
    """Positional values, one list/tuple, or one dict of named values (`$name`, `:name`, `@name` or `name`)."""
    if len(args) == 1 and isinstance(args[0], dict):
        return {(k[1:] if k[:1] in "$:@" else k): v for k, v in args[0].items()}
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        return tuple(args[0])
    return tuple(args)


def _script(c: sqlite3.Connection, source: str) -> None:
    """Runs `;`-separated statements one by one. `executescript` would COMMIT an open transaction first."""
    buf = ""
    for part in source.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(" \t\r\n;"):
                c.execute(buf).fetchall()
            buf = ""
    if buf.strip(" \t\r\n;"):
        c.execute(buf.rstrip(";")).fetchall()


def _need_311(what: str) -> None:
    if not hasattr(sqlite3.Connection, "serialize"):
        raise sqlite3.NotSupportedError("%s needs Python 3.11 or later" % what)


def _rows(cur: sqlite3.Cursor, raw: List[Tuple[Any, ...]]) -> List[Row]:
    names = [d[0] for d in cur.description or ()]
    return [dict(zip(names, r)) for r in raw]


class SQLiteDatabase:
    """An open database. Every method has an `_async` form (worker thread) and a `_sync` form (blocks)."""

    def __init__(self, database_name: str, path: str, options: SQLiteOpenOptions) -> None:
        self.database_name = database_name
        self.database_path = path
        self.options = options
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyxpo-sqlite")
        self._refs = 1

    # plumbing
    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.ProgrammingError("the database %r is closed" % self.database_name)
        return self._conn

    def _do(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            return fn(self._c())

    async def _async(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        self._c()
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._do, fn)

    # statements
    def exec_sync(self, source: str) -> None:
        """Runs one or more `;`-separated statements without parameters or results."""
        self._do(lambda c: _script(c, source))

    async def exec_async(self, source: str) -> None:
        await self._async(lambda c: _script(c, source))

    @staticmethod
    def _run(c: sqlite3.Connection, sql: str, p: Any) -> SQLiteRunResult:
        cur = c.execute(sql, p)
        cur.fetchall()
        return SQLiteRunResult(cur.lastrowid or 0, c.execute("SELECT changes()").fetchone()[0])

    def run_sync(self, source: str, *params: Any) -> SQLiteRunResult:
        p = _params(params)
        return self._do(lambda c: self._run(c, source, p))

    async def run_async(self, source: str, *params: Any) -> SQLiteRunResult:
        p = _params(params)
        return await self._async(lambda c: self._run(c, source, p))

    @staticmethod
    def _first(c: sqlite3.Connection, sql: str, p: Any) -> Optional[Row]:
        cur = c.execute(sql, p)
        rows = _rows(cur, cur.fetchmany(1))
        cur.close()
        return rows[0] if rows else None

    def get_first_sync(self, source: str, *params: Any) -> Optional[Row]:
        p = _params(params)
        return self._do(lambda c: self._first(c, source, p))

    async def get_first_async(self, source: str, *params: Any) -> Optional[Row]:
        p = _params(params)
        return await self._async(lambda c: self._first(c, source, p))

    @staticmethod
    def _all(c: sqlite3.Connection, sql: str, p: Any) -> List[Row]:
        cur = c.execute(sql, p)
        return _rows(cur, cur.fetchall())

    def get_all_sync(self, source: str, *params: Any) -> List[Row]:
        p = _params(params)
        return self._do(lambda c: self._all(c, source, p))

    async def get_all_async(self, source: str, *params: Any) -> List[Row]:
        p = _params(params)
        return await self._async(lambda c: self._all(c, source, p))

    def get_each_sync(self, source: str, *params: Any) -> Iterator[Row]:
        p = _params(params)
        cur = self._do(lambda c: c.execute(source, p))
        while True:
            batch = self._do(lambda c: _rows(cur, cur.fetchmany(_BATCH)))
            if not batch:
                return
            yield from batch

    async def get_each_async(self, source: str, *params: Any) -> AsyncIterator[Row]:
        """Yields rows one by one, reading them in small batches."""
        p = _params(params)
        cur = await self._async(lambda c: c.execute(source, p))
        try:
            while True:
                batch = await self._async(lambda c: _rows(cur, cur.fetchmany(_BATCH)))
                if not batch:
                    return
                for r in batch:
                    yield r
        finally:
            cur.close()

    def prepare_sync(self, source: str) -> "SQLiteStatement":
        self._c()
        return SQLiteStatement(self, source)

    async def prepare_async(self, source: str) -> "SQLiteStatement":
        return self.prepare_sync(source)

    # transactions
    def is_in_transaction_sync(self) -> bool:
        return self._do(lambda c: c.in_transaction)

    async def is_in_transaction_async(self) -> bool:
        return await self._async(lambda c: c.in_transaction)

    async def with_transaction_async(self, task: Callable[[], Awaitable[Any]]) -> None:
        """Runs `task` inside BEGIN/COMMIT and rolls back if it raises. Other queries on this connection made
        meanwhile (from other tasks) join the transaction: use `with_exclusive_transaction_async` to avoid that."""
        await self.exec_async("BEGIN")
        try:
            await task()
        except BaseException:
            await self.exec_async("ROLLBACK")
            raise
        await self.exec_async("COMMIT")

    async def with_exclusive_transaction_async(self, task: Callable[["SQLiteDatabase"], Awaitable[Any]]) -> None:
        """Runs `task(txn)` on a separate connection inside BEGIN EXCLUSIVE/COMMIT; use only `txn` inside it."""
        if self.database_path == ":memory:":
            raise sqlite3.NotSupportedError("an in-memory database has no second connection")
        txn = SQLiteDatabase(self.database_name, self.database_path, SQLiteOpenOptions(use_new_connection=True))
        try:
            await txn.exec_async("BEGIN EXCLUSIVE")
            try:
                await task(txn)
            except BaseException:
                await txn.exec_async("ROLLBACK")
                raise
            await txn.exec_async("COMMIT")
        finally:
            await txn.close_async()

    def with_transaction_sync(self, task: Callable[[], Any]) -> None:
        with self._lock:
            self.exec_sync("BEGIN")
            try:
                task()
            except BaseException:
                self.exec_sync("ROLLBACK")
                raise
            self.exec_sync("COMMIT")

    # whole database
    def serialize_sync(self, database_name: str = "main") -> bytes:
        """The database file as bytes (Python 3.11+)."""
        _need_311("serialize")
        return self._do(lambda c: c.serialize(name=database_name))

    async def serialize_async(self, database_name: str = "main") -> bytes:
        _need_311("serialize")
        return await self._async(lambda c: c.serialize(name=database_name))

    def close_sync(self) -> None:
        """Closes this connection. A shared connection closes when every opener closed it."""
        with _open_lock:
            self._refs -= 1
            if self._refs > 0:
                return
            if _open.get(self.database_path) is self:
                del _open[self.database_path]
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        self._pool.shutdown(wait=False)

    async def close_async(self) -> None:
        if self._conn is None:
            return
        # Wait for queued work, then close on the caller's side.
        await asyncio.get_running_loop().run_in_executor(self._pool, lambda: None)
        self.close_sync()

    def __repr__(self) -> str:
        return "<SQLiteDatabase %r>" % self.database_path

    # Expo spellings
    execAsync, execSync = exec_async, exec_sync
    runAsync, runSync = run_async, run_sync
    getFirstAsync, getFirstSync = get_first_async, get_first_sync
    getAllAsync, getAllSync = get_all_async, get_all_sync
    getEachAsync, getEachSync = get_each_async, get_each_sync
    prepareAsync, prepareSync = prepare_async, prepare_sync
    isInTransactionAsync, isInTransactionSync = is_in_transaction_async, is_in_transaction_sync
    withTransactionAsync, withTransactionSync = with_transaction_async, with_transaction_sync
    withExclusiveTransactionAsync = with_exclusive_transaction_async
    serializeAsync, serializeSync = serialize_async, serialize_sync
    closeAsync, closeSync = close_async, close_sync
    databasePath = property(lambda self: self.database_path)
    databaseName = property(lambda self: self.database_name)


class SQLiteExecuteResult:
    """What `SQLiteStatement.execute_*` returns: the run result plus the rows."""

    def __init__(self, run: SQLiteRunResult, rows: List[Row]) -> None:
        self.last_insert_row_id = run.last_insert_row_id
        self.changes = run.changes
        self._rows = rows

    def get_first_sync(self) -> Optional[Row]:
        return self._rows[0] if self._rows else None

    def get_all_sync(self) -> List[Row]:
        return list(self._rows)

    async def get_first_async(self) -> Optional[Row]:
        return self.get_first_sync()

    async def get_all_async(self) -> List[Row]:
        return self.get_all_sync()

    def __iter__(self) -> Iterator[Row]:
        return iter(list(self._rows))

    async def __aiter__(self) -> AsyncIterator[Row]:
        for r in list(self._rows):
            yield r

    lastInsertRowId = property(lambda self: self.last_insert_row_id)
    getFirstAsync, getAllAsync = get_first_async, get_all_async
    getFirstSync, getAllSync = get_first_sync, get_all_sync


class SQLiteStatement:
    """A statement to run many times with different values. `sqlite3` caches the compiled statement."""

    def __init__(self, db: SQLiteDatabase, source: str) -> None:
        self._db = db
        self.source = source
        self._final = False
        self._names: Optional[List[str]] = None

    def _exec(self, c: sqlite3.Connection, p: Any) -> SQLiteExecuteResult:
        if self._final:
            raise sqlite3.ProgrammingError("the statement was finalized")
        cur = c.execute(self.source, p)
        rows = _rows(cur, cur.fetchall())
        if cur.description:
            self._names = [d[0] for d in cur.description]
        changes = c.execute("SELECT changes()").fetchone()[0]
        return SQLiteExecuteResult(SQLiteRunResult(cur.lastrowid or 0, changes), rows)

    def execute_sync(self, *params: Any) -> SQLiteExecuteResult:
        p = _params(params)
        return self._db._do(lambda c: self._exec(c, p))

    async def execute_async(self, *params: Any) -> SQLiteExecuteResult:
        p = _params(params)
        return await self._db._async(lambda c: self._exec(c, p))

    def get_column_names_sync(self) -> List[str]:
        """Result column names. A query statement is compiled without running; other statements have none."""
        if self._names is not None:
            return list(self._names)
        src = self.source.strip().rstrip(";")
        if src[:6].upper() not in ("SELECT", "VALUES") and src[:4].upper() != "WITH":
            return []
        named = re.findall(r"[$:@]([A-Za-z_][A-Za-z0-9_]*)", src)
        p: Any = {n: None for n in named} if named else [None] * src.count("?")

        def names(c: sqlite3.Connection) -> List[str]:
            cur = c.execute("SELECT * FROM (%s) LIMIT 0" % src, p)
            return [d[0] for d in cur.description or ()]
        return self._db._do(names)

    async def get_column_names_async(self) -> List[str]:
        return self.get_column_names_sync()

    def finalize_sync(self) -> None:
        self._final = True

    async def finalize_async(self) -> None:
        self._final = True

    executeAsync, executeSync = execute_async, execute_sync
    getColumnNamesAsync, getColumnNamesSync = get_column_names_async, get_column_names_sync
    finalizeAsync, finalizeSync = finalize_async, finalize_sync


_open: Dict[str, SQLiteDatabase] = {}
_open_lock = threading.Lock()


def _path(database_name: str, directory: Optional[str]) -> str:
    if database_name == ":memory:":
        return ":memory:"
    if os.path.isabs(database_name):
        return database_name
    d = directory or _default_dir()
    if d.startswith("file://"):
        d = d[len("file://"):]
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, database_name)


def open_database_sync(database_name: str, options: Optional[SQLiteOpenOptions] = None,
                       directory: Optional[str] = None) -> SQLiteDatabase:
    """Opens (and creates) `database_name` in `directory` (default `<documents>/SQLite`). `":memory:"` opens a
    private in-memory database. Opening the same file again shares the connection unless
    `SQLiteOpenOptions(use_new_connection=True)`."""
    opts = options or SQLiteOpenOptions()
    path = _path(database_name, directory)
    with _open_lock:
        cur = _open.get(path)
        if cur is not None and not opts.use_new_connection and path != ":memory:":
            cur._refs += 1
            return cur
        db = SQLiteDatabase(database_name, path, opts)
        if not opts.use_new_connection and path != ":memory:":
            _open[path] = db
        return db


async def open_database_async(database_name: str, options: Optional[SQLiteOpenOptions] = None,
                              directory: Optional[str] = None) -> SQLiteDatabase:
    return await asyncio.to_thread(open_database_sync, database_name, options, directory)


def delete_database_sync(database_name: str, directory: Optional[str] = None) -> None:
    """Deletes the database file and its -wal/-shm/-journal files. Fails while the database is open."""
    path = _path(database_name, directory)
    if path in _open:
        raise sqlite3.OperationalError("close the database %r before deleting it" % database_name)
    if not os.path.exists(path):
        raise FileNotFoundError("no database %r" % path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


async def delete_database_async(database_name: str, directory: Optional[str] = None) -> None:
    await asyncio.to_thread(delete_database_sync, database_name, directory)


def deserialize_database_sync(data: bytes) -> SQLiteDatabase:
    """Opens an in-memory database with the contents of `data` (from `serialize_*`; Python 3.11+)."""
    _need_311("deserialize")
    db = SQLiteDatabase(":memory:", ":memory:", SQLiteOpenOptions(use_new_connection=True))
    db._do(lambda c: c.deserialize(data))
    return db


async def deserialize_database_async(data: bytes) -> SQLiteDatabase:
    return deserialize_database_sync(data)


# Provider and hook, as expo-sqlite's <SQLiteProvider> and useSQLiteContext().
def _make_provider() -> Tuple[Any, Callable[[], SQLiteDatabase]]:
    from pyxpo import component, create_context, use_async, use_context, use_effect

    ctx = create_context(None)

    @component
    def SQLiteProvider(*children: Any, database_name: str, options: Optional[SQLiteOpenOptions] = None,
                       directory: Optional[str] = None, on_init: Optional[Callable[[SQLiteDatabase], Awaitable[Any]]] = None,
                       on_error: Optional[Callable[[BaseException], Any]] = None, fallback: Any = None) -> Any:
        async def open_() -> SQLiteDatabase:
            db = await open_database_async(database_name, options, directory)
            if on_init is not None:
                await on_init(db)
            return db

        res = use_async(open_, [database_name, directory])

        def close() -> Callable[[], None]:
            db = res.value
            return lambda: db.close_sync() if db is not None else None
        use_effect(close, [res.value])
        if res.error is not None:
            if on_error is None:
                raise res.error
            on_error(res.error)
            return fallback
        if res.loading:
            return fallback
        return ctx.Provider(*children, value=res.value)

    def use_sqlite_context() -> SQLiteDatabase:
        db = use_context(ctx)
        if db is None:
            raise RuntimeError("use_sqlite_context() needs an SQLiteProvider above it")
        return db

    return SQLiteProvider, use_sqlite_context


_provider: Optional[Tuple[Any, Callable[[], SQLiteDatabase]]] = None


def _get_provider() -> Tuple[Any, Callable[[], SQLiteDatabase]]:
    global _provider
    if _provider is None:
        _provider = _make_provider()
    return _provider


def SQLiteProvider(*children: Any, **props: Any) -> Any:  # noqa: N802
    """Opens `database_name`, runs `on_init(db)` (migrations), then renders `children` with the database
    available to `use_sqlite_context()`. Shows `fallback` (default nothing) while opening."""
    return _get_provider()[0](*children, **props)


def use_sqlite_context() -> SQLiteDatabase:
    return _get_provider()[1]()


openDatabaseAsync, openDatabaseSync = open_database_async, open_database_sync
deleteDatabaseAsync, deleteDatabaseSync = delete_database_async, delete_database_sync
deserializeDatabaseAsync, deserializeDatabaseSync = deserialize_database_async, deserialize_database_sync
useSQLiteContext = use_sqlite_context

__all__ = ["open_database_async", "open_database_sync", "delete_database_async", "delete_database_sync",
           "deserialize_database_async", "deserialize_database_sync", "SQLiteDatabase", "SQLiteStatement",
           "SQLiteRunResult", "SQLiteExecuteResult", "SQLiteOpenOptions", "SQLiteProvider", "use_sqlite_context"]

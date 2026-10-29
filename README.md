# pyxpo-sqlite

SQLite databases that persist across app restarts, for [pyxpo](https://github.com/pyxpo/pyxpo) apps written in Python.
The API follows [expo-sqlite](https://docs.expo.dev/versions/latest/sdk/sqlite/): open a database, run SQL with
parameters, read rows as dicts, use transactions and prepared statements. It needs no native code, because the
Python that ships inside the app already has `sqlite3`. Async methods run on one worker thread per connection, so the
UI stays responsive.

<p align="center">
  <img src="https://raw.githubusercontent.com/pyxpo/pyxpo-sqlite/main/docs/images/hero.png" width="820" alt="A todo table read from a SQLite file on Android and iOS, with the last insert id and change count">
</p>

```python
import pyxpo_sqlite as SQLite

db = await SQLite.open_database_async("app.db")
await db.run_async("INSERT INTO todo (title) VALUES (?)", "Buy milk")
rows = await db.get_all_async("SELECT * FROM todo")      # [{"id": 1, "title": "Buy milk", ...}]
```

## Features

- **Real SQLite files** that survive restarts: in the app's Documents folder on iOS and `filesDir` on Android.
- **Rows are dicts:** `get_all_async`, `get_first_async`, and `get_each_async` for big results.
- **Parameters:** positional, a list, or named (`$id`, `:id`, `@id`).
- **Transactions:** `with_transaction_async` and an exclusive variant on its own connection.
- **Prepared statements** to run many times with different values.
- **`SQLiteProvider` and `use_sqlite_context()`** open the database for a whole screen tree and run migrations.
- **Sync forms too:** every method has a blocking `_sync` form.
- **Backup:** `serialize_async` and `deserialize_database_async` (Python 3.11+ on the desktop).
- **Typed:** ships `py.typed`. camelCase names (`getAllAsync`, `lastInsertRowId`) also work.

## Contents

| If you are... | Start here |
| --- | --- |
| Saving rows | [Quick start](#quick-start) |
| Reading data | [Queries](#queries) |
| Opening the database for a screen | [Provider and hook](#provider-and-hook) |
| Making several changes together | [Transactions](#transactions) |
| Running one statement many times | [Prepared statements](#prepared-statements) |
| Exact signatures | [API reference](#api-reference) · [Errors](#errors) |
| Seeing it in a real app | [Recipe: todo list](#recipe-todo-list) |
| Stuck | [Testing](#testing) · [Troubleshooting](#troubleshooting) · [FAQ](#faq) |

## Platform support

| Target | What works |
| --- | --- |
| Android, API 24+ | Everything. Databases in the app's `filesDir`. |
| iOS 15+ | Everything. Databases in the app's `Documents`. |
| Desktop window and tests | Everything. Databases in `.pyxpo-data/documents/SQLite` in the working folder. `serialize` and `deserialize` need Python 3.11+. |
| Web page | The build loads Python's `sqlite3` for the page. |

The bundled Python on iOS and Android is 3.13. Requires Python 3.9+ and `pyxpo>=0.1.0a1`.

## Install

```sh
pip install pyxpo-sqlite
```

```toml
[tool.pyxpo]
modules = ["sqlite"]
```

Rebuild the app. There are no plugin options and no permissions.

## Quick start

```python
import pyxpo_sqlite as SQLite
from pyxpo import Button, Text, View, component, use_async, use_state


@component
def Todos():
    taps, set_taps = use_state(0)
    rows, set_rows = use_state([])

    async def run():
        db = await SQLite.open_database_async("app.db")
        await db.exec_async(
            "CREATE TABLE IF NOT EXISTS todo (id INTEGER PRIMARY KEY, title TEXT NOT NULL, done INTEGER DEFAULT 0)")
        if taps > 0:
            await db.run_async("INSERT INTO todo (title) VALUES (?)", "Todo #%d" % taps)
        set_rows(await db.get_all_async("SELECT * FROM todo ORDER BY id"))

    use_async(run, [taps])

    return View(
        *[Text("%s  %s" % (r["id"], r["title"])) for r in rows],
        Button("Add a row", lambda: set_taps(taps + 1)),
    )
```

> **Tip:** `use_async(run, [taps])` runs `run` once on mount and again each time the counter changes. A button handler
> that is a `lambda` returning a coroutine would never be awaited, so bump a counter instead.
> Opening the same file again shares the connection, so calling `open_database_async` repeatedly is fine.

<p align="center">
  <img src="https://raw.githubusercontent.com/pyxpo/pyxpo-sqlite/main/docs/images/rows.png" width="720" alt="Rows read from the todo table with the run result of an insert, on Android and iOS">
</p>

## Opening

```python
db = await SQLite.open_database_async("app.db")                    # in the default folder
mem = await SQLite.open_database_async(":memory:")                 # private, in-memory
other = await SQLite.open_database_async("x.db", directory="/some/folder")
fresh = await SQLite.open_database_async(
    "app.db", SQLite.SQLiteOpenOptions(use_new_connection=True))   # a separate connection
```

Opening the same file again shares one connection (counted, so each `close_async` pairs with an open). An absolute
`database_name` is used as is. `default_database_directory` is the default folder. Remove a database with
`await SQLite.delete_database_async("app.db")`, after closing it.

## Queries

```python
res = await db.run_async("INSERT INTO todo (title) VALUES (?)", "Buy milk")
res.last_insert_row_id, res.changes

row = await db.get_first_async("SELECT * FROM todo WHERE id = ?", 1)            # dict or None
rows = await db.get_all_async("SELECT * FROM todo WHERE done = $done", {"$done": 0})
async for row in db.get_each_async("SELECT * FROM todo"):                      # read in batches of 64
    ...
await db.exec_async("PRAGMA journal_mode = WAL; CREATE TABLE IF NOT EXISTS t (x)")   # many statements, no result
```

| Parameter style | Example |
| --- | --- |
| Positional | `run_async(sql, 1, "a")` |
| One list or tuple | `run_async(sql, [1, "a"])` |
| Named | `run_async(sql, {"$id": 1})` for `$id`, `:id` or `@id`. The prefix in the dict is optional. |

Values map to `int`, `float`, `str`, `bytes` and `None`.

## Provider and hook

Open the database once for a screen tree and run migrations before the children render:

```python
from pyxpo import Text, View, component
from pyxpo_sqlite import SQLiteProvider, use_sqlite_context


async def migrate(db):
    await db.exec_async("CREATE TABLE IF NOT EXISTS todo (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")


@component
def List():
    db = use_sqlite_context()
    return View(Text(db.database_name))


def App():
    return SQLiteProvider(List(), database_name="app.db", on_init=migrate, fallback=Text("Loading..."))
```

| `SQLiteProvider` prop | Notes |
| --- | --- |
| `database_name` | Required. |
| `options`, `directory` | As for `open_database_async`. |
| `on_init(db)` | Awaited before the children render: migrations. |
| `on_error(error)` | Without it the error is raised to the nearest `ErrorBoundary`. |
| `fallback` | Shown while opening. |

The provider closes its connection on unmount. `use_sqlite_context()` raises `RuntimeError` outside a provider.

## Transactions

```python
async def move():
    await db.run_async("UPDATE account SET cents = cents - 500 WHERE id = 1")
    await db.run_async("UPDATE account SET cents = cents + 500 WHERE id = 2")

await db.with_transaction_async(move)          # rolls back if move raises
```

`with_transaction_async` joins other queries made meanwhile on the same connection. For isolation use
`with_exclusive_transaction_async(task)`, which runs `task(txn)` on its own connection inside `BEGIN EXCLUSIVE`: use only
`txn` inside. It is not available for `":memory:"`. `is_in_transaction_async()` reports the state.

## Prepared statements

```python
stmt = await db.prepare_async("INSERT INTO todo (title) VALUES ($title)")
for t in ("a", "b", "c"):
    await stmt.execute_async({"$title": t})
await stmt.finalize_async()
```

`execute_async` returns an object with `last_insert_row_id`, `changes`, and the rows (`get_all_async()`,
`get_first_async()`, or iterate it). `get_column_names_async()` lists result columns.

## Backup

```python
data = await db.serialize_async()                     # bytes of the database file (Python 3.11+)
copy = await SQLite.deserialize_database_async(data)  # an in-memory database with that content
```

## Errors

| Error | When | What to do |
| --- | --- | --- |
| `sqlite3.OperationalError` | Bad SQL, a missing table, a locked database, deleting an open database. | Fix the SQL. Close before deleting. |
| `sqlite3.IntegrityError` | A constraint failed (`NOT NULL`, `UNIQUE`, ...). | Validate before inserting, or catch it. |
| `sqlite3.ProgrammingError` | Using a closed database or a finalized statement, or wrong parameter count. | Keep the database open while in use. |
| `sqlite3.NotSupportedError` | `serialize` or `deserialize` before Python 3.11, or an exclusive transaction on `:memory:`. | Use a file database or newer Python. |
| `FileNotFoundError` | `delete_database_async` of a missing file. | Check `database_name`. |
| `RuntimeError` | `use_sqlite_context()` without a provider. | Wrap the tree in `SQLiteProvider`. |

## Config

None. The default folder is shown by `SQLite.default_database_directory`.

## Web and desktop

The desktop window and tests keep databases in `.pyxpo-data/documents/SQLite` under the working folder. The web build loads
`sqlite3` into the page. Not supported anywhere: change listeners (`addDatabaseChangeListener`, `enableChangeListener`),
SQLCipher encryption, loading extensions, and expo-sqlite's key-value `Storage` (use `pyxpo-storage`).

## Recipe: todo list

A list with a provider, a migration that keeps a schema version, and add/complete actions.

```python
from pyxpo import Button, Text, View, component, use_async, use_state
from pyxpo_sqlite import SQLiteProvider, use_sqlite_context


async def migrate(db):
    row = await db.get_first_async("PRAGMA user_version")
    version = row["user_version"]
    if version < 1:
        await db.exec_async("CREATE TABLE todo (id INTEGER PRIMARY KEY, title TEXT NOT NULL, done INTEGER DEFAULT 0)")
        await db.exec_async("PRAGMA user_version = 1")


@component
def TodoList():
    db = use_sqlite_context()
    n, set_n = use_state(0)
    rows, set_rows = use_state([])

    async def refresh():
        if n > 0:
            await db.run_async("INSERT INTO todo (title) VALUES (?)", "Item %d" % n)
        set_rows(await db.get_all_async("SELECT * FROM todo ORDER BY id"))

    use_async(refresh, [n])
    return View(
        *[Text(("[x] " if r["done"] else "[ ] ") + r["title"]) for r in rows],
        Button("Add", lambda: set_n(n + 1)),
    )


def App():
    return SQLiteProvider(TodoList(), database_name="todos.db", on_init=migrate, fallback=Text("Opening..."))
```

## Production checklist

- [ ] Migrations run in `on_init`, guarded by `PRAGMA user_version`.
- [ ] Values are passed as parameters, never formatted into the SQL string.
- [ ] Related writes are in `with_transaction_async`.
- [ ] You use `PRAGMA journal_mode = WAL` for write-heavy apps.
- [ ] You catch `sqlite3.IntegrityError` where users can cause duplicates.
- [ ] Big results use `get_each_async`.
- [ ] You close databases you opened by hand.
- [ ] Secrets are not in the database (use `pyxpo-secure-store`). It is not encrypted.

## Testing

`:memory:` databases run on the desktop, so you can test real SQL without a phone:

```python
import asyncio
import sqlite3

import pytest

import pyxpo_sqlite as SQLite


def test_insert_and_query():
    async def go():
        db = await SQLite.open_database_async(":memory:")
        await db.exec_async("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        res = await db.run_async("INSERT INTO t (name) VALUES (?)", "a")
        rows = await db.get_all_async("SELECT * FROM t WHERE name = $n", {"$n": "a"})
        return res, rows

    res, rows = asyncio.run(go())
    assert (res.last_insert_row_id, res.changes) == (1, 1)
    assert rows == [{"id": 1, "name": "a"}]


def test_rollback():
    async def go():
        db = await SQLite.open_database_async(":memory:")
        await db.exec_async("CREATE TABLE t (x)")

        async def boom():
            await db.run_async("INSERT INTO t VALUES (1)")
            raise RuntimeError("stop")

        with pytest.raises(RuntimeError):
            await db.with_transaction_async(boom)
        return await db.get_all_async("SELECT * FROM t")

    assert asyncio.run(go()) == []


def test_constraint():
    db = SQLite.open_database_sync(":memory:")
    db.exec_sync("CREATE TABLE t (x TEXT NOT NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        db.run_sync("INSERT INTO t VALUES (NULL)")
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `OperationalError: no such table` | The migration did not run. Create tables in `on_init` or before the first query. |
| `OperationalError: database is locked` | Two connections wrote at once. Use one shared connection, WAL mode, or an exclusive transaction. |
| `ProgrammingError: closed` | The database was closed. Keep the provider mounted. |
| `ProgrammingError` about bindings | The number of parameters does not match the `?` marks. |
| Cannot delete the database | Close it first. |
| `serialize` fails on the desktop | Needs Python 3.11 or newer. |
| `RuntimeError: needs an SQLiteProvider` | Wrap the component in `SQLiteProvider`. |

## FAQ

**Is it async?** Yes, and every method has a `_sync` form too. Async methods use one worker thread per connection.

**Where is the file?** See `SQLite.default_database_directory`. It is private to the app and kept across updates.

**Can I use an ORM?** Python's `sqlite3` is what runs underneath, so anything built on it can work against the same file.
This package only gives you the expo-sqlite API.

**Is it encrypted?** No.

**Change listeners?** Not supported. Re-query after your own writes.

## API reference

### Module functions

| Function | Notes |
| --- | --- |
| `open_database_async` / `_sync(database_name, options=None, directory=None)` | Returns an `SQLiteDatabase`. |
| `delete_database_async` / `_sync(database_name, directory=None)` | Deletes the file and its `-wal`, `-shm`, `-journal` files. |
| `deserialize_database_async` / `_sync(data)` | In-memory database from bytes (Python 3.11+). |
| `SQLiteProvider(*children, database_name, options, directory, on_init, on_error, fallback)` | Opens the database for the tree. |
| `use_sqlite_context()` | The provider's `SQLiteDatabase`. |

### `SQLiteDatabase`

Each has `_async` and `_sync` forms unless noted.

| Method | Returns |
| --- | --- |
| `exec(source)` | Nothing. Several statements. |
| `run(source, *params)` | `SQLiteRunResult(last_insert_row_id, changes)` |
| `get_first(source, *params)` | A dict or `None`. |
| `get_all(source, *params)` | A list of dicts. |
| `get_each(source, *params)` | Iterates rows (async iterator or iterator). |
| `prepare(source)` | `SQLiteStatement` |
| `with_transaction(task)` | Runs `task()` in a transaction. |
| `with_exclusive_transaction_async(task)` | Runs `task(txn)` on a separate connection. |
| `is_in_transaction()` | `bool` |
| `serialize(database_name="main")` | `bytes` (Python 3.11+) |
| `close()` | Closes when every opener closed it. |

Attributes: `database_name`, `database_path`, `options`.

### `SQLiteStatement`

`execute_async` / `execute_sync(*params)` returns a result with `last_insert_row_id`, `changes`, `get_all_async()`,
`get_first_async()`. Also `get_column_names_async()` and `finalize_async()`.

## License

MIT

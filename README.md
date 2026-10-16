# pyxpo-sqlite

SQLite databases that persist across app restarts, for [pyxpo](https://github.com/pyxpo/pyxpo) apps. The API
follows [expo-sqlite](https://docs.expo.dev/versions/latest/sdk/sqlite/). The phone's Python ships the `sqlite3`
module, so this package is pure Python: no native code and no rebuild of the native shell.

```sh
pip install pyxpo-sqlite
```

```toml
[tool.pyxpo]
modules = ["sqlite"]
```

```python
import pyxpo_sqlite as SQLite

db = await SQLite.open_database_async("app.db")
await db.exec_async("""
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS todo (id INTEGER PRIMARY KEY, title TEXT NOT NULL, done INTEGER DEFAULT 0);
""")
res = await db.run_async("INSERT INTO todo (title) VALUES (?)", "Buy milk")
print(res.last_insert_row_id, res.changes)

row = await db.get_first_async("SELECT * FROM todo WHERE id = ?", res.last_insert_row_id)  # dict or None
rows = await db.get_all_async("SELECT * FROM todo WHERE done = $done", {"$done": 0})         # list of dicts
async for row in db.get_each_async("SELECT * FROM todo"):                                   # one at a time
    ...

async def mark_all():
    await db.run_async("UPDATE todo SET done = 1")
await db.with_transaction_async(mark_all)     # COMMIT, or ROLLBACK if mark_all raises
```

## Functions

| Function | Notes |
| --- | --- |
| `open_database_async(name, options=None, directory=None)` | Opens or creates `name` in `directory` (default `default_database_directory`). `":memory:"` opens a private in-memory database. Opening the same file again returns the same connection unless `SQLiteOpenOptions(use_new_connection=True)`. |
| `delete_database_async(name, directory=None)` | Deletes the file and its `-wal`, `-shm` and `-journal` files. Raises while the database is open. |
| `deserialize_database_async(data)` | An in-memory database holding `data` from `serialize_async()`. |
| `default_database_directory` | `<documents>/SQLite`: the app's Documents folder on iOS, `filesDir` on Android. |

## `SQLiteDatabase`

| Method | Notes |
| --- | --- |
| `exec_async(sql)` | Runs `;`-separated statements without values or results (schema, PRAGMA). |
| `run_async(sql, *values)` | Returns `SQLiteRunResult(last_insert_row_id, changes)`. |
| `get_first_async(sql, *values)` | First row as a dict, or `None`. |
| `get_all_async(sql, *values)` | All rows as dicts. |
| `get_each_async(sql, *values)` | Async iterator of rows, read in small batches. |
| `prepare_async(sql)` | `SQLiteStatement`: `execute_async(*values)` returns a result with `last_insert_row_id`, `changes`, `get_first_async()`, `get_all_async()` and async iteration; also `get_column_names_async()` and `finalize_async()`. |
| `with_transaction_async(task)` | Runs `await task()` inside `BEGIN`/`COMMIT`, rolling back if it raises. Queries from other tasks on the same connection meanwhile join the transaction. |
| `with_exclusive_transaction_async(task)` | Runs `await task(txn)` on a separate connection inside `BEGIN EXCLUSIVE`; run queries on `txn` only. Not for `":memory:"`. |
| `is_in_transaction_async()` | |
| `serialize_async(name="main")` | The database as bytes. |
| `close_async()` | A shared connection closes when every opener closed it. |
| `database_path`, `database_name` | |

Values: positional (`run_async(sql, 1, "a")`), one list or tuple, or one dict for named parameters. Dict keys may
keep the prefix used in the SQL (`{"$id": 1}` for `$id`, `:id`, `@id`) or drop it. Rows map column names to
Python values: `int`, `float`, `str`, `bytes` or `None`.

Every method also has a `_sync` form that blocks (for example `get_all_sync`), and camelCase names work
(`getAllAsync`, `runAsync`, `lastInsertRowId`). Async methods run on one worker thread per connection, so the UI
stays responsive. SQL errors raise the `sqlite3` exceptions (`sqlite3.OperationalError`,
`sqlite3.IntegrityError`, ...).

## Provider and hook

```python
from pyxpo_sqlite import SQLiteProvider, use_sqlite_context

async def migrate(db):
    await db.exec_async("CREATE TABLE IF NOT EXISTS todo (id INTEGER PRIMARY KEY, title TEXT)")

def App():
    return SQLiteProvider(TodoList(), database_name="app.db", on_init=migrate, fallback=Text("Loading..."))

@component
def TodoList():
    db = use_sqlite_context()
    ...
```

`SQLiteProvider` props: `database_name`, `options`, `directory`, `on_init(db)` (awaited before the children render),
`on_error(error)` (without it the error is raised to the nearest `ErrorBoundary`) and `fallback`.

## Not supported

Change listeners (`addDatabaseChangeListener`), `enableChangeListener`, SQLCipher encryption, loading extensions
and the key-value `Storage` export of expo-sqlite (use `pyxpo-storage`).

## Platforms

iOS, Android and desktop. The bundled Python on iOS and Android is 3.13. On desktop, `serialize` and
`deserialize` need Python 3.11 or later and raise `sqlite3.NotSupportedError` before that. On desktop, databases
live in `.pyxpo-data/documents/SQLite` under the working directory.

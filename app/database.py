"""SQLite 持久化层。

- cells：导入的调查格值（导入后不可更换，无更新/删除接口）。
- publications：已获准公开的汇总记录（不可撤销，只插入，无更新/删除接口）。
- citation_reports：生成的引用页快照（发布编号 + 算式 + 生成时总数），
  按不可变快照保存，新增发布不影响已有引用页。

所有写操作均在单事务中完成：失败即整体回滚，不会留下半次发布。
连接按操作创建，启用 WAL，重启后数据保留在 DB_PATH 指定的库文件中。
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "app.db"
DB_PATH = os.environ.get("DB_PATH", str(_DEFAULT_DB))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    district   TEXT NOT NULL,
    age_group  TEXT NOT NULL,
    population INTEGER NOT NULL CHECK (population >= 5),
    detected   INTEGER NOT NULL CHECK (detected >= 0 AND detected <= population),
    PRIMARY KEY (district, age_group)
);
CREATE TABLE IF NOT EXISTS publications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    districts  TEXT NOT NULL,          -- JSON 数组
    age_groups TEXT NOT NULL,          -- JSON 数组
    total      INTEGER NOT NULL CHECK (total >= 0),
    created_at TEXT NOT NULL           -- ISO8601 UTC
);
CREATE TABLE IF NOT EXISTS citation_reports (
    token      TEXT PRIMARY KEY,       -- 公开引用页标识
    snapshot   TEXT NOT NULL,          -- JSON：发布编号、系数、生成时总数
    created_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    path = DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.executescript(_SCHEMA)


def get_cells() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT district, age_group, population, detected FROM cells "
            "ORDER BY district, age_group"
        ).fetchall()
    return [dict(r) for r in rows]


def insert_cells(rows: list[dict]) -> None:
    """单事务写入全部格值；任一失败则整体回滚。组合重复抛 IntegrityError。"""
    with closing(connect()) as conn, conn:
        conn.executemany(
            "INSERT INTO cells (district, age_group, population, detected) "
            "VALUES (:district, :age_group, :population, :detected)",
            rows,
        )


def get_publications() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, districts, age_groups, total, created_at "
            "FROM publications ORDER BY id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["districts"] = json.loads(d["districts"])
        d["age_groups"] = json.loads(d["age_groups"])
        out.append(d)
    return out


def insert_publication(districts: list[str], age_groups: list[str], total: int) -> dict:
    """单事务插入一条公开记录；失败回滚，不会留下半次发布。"""
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO publications (districts, age_groups, total, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                json.dumps(list(districts), ensure_ascii=False),
                json.dumps(list(age_groups), ensure_ascii=False),
                int(total),
                created,
            ),
        )
        pid = cur.lastrowid
    return {
        "id": pid,
        "districts": list(districts),
        "age_groups": list(age_groups),
        "total": int(total),
        "created_at": created,
    }


def insert_citation_report(token: str, snapshot: dict) -> dict:
    """单事务保存引用页快照；失败回滚，不留下半份报告。"""
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO citation_reports (token, snapshot, created_at) VALUES (?, ?, ?)",
            (token, payload, created),
        )
    return {"token": token, "snapshot": snapshot, "created_at": created}


def get_citation_report(token: str) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT token, snapshot, created_at FROM citation_reports WHERE token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["snapshot"] = json.loads(out["snapshot"])
    return out

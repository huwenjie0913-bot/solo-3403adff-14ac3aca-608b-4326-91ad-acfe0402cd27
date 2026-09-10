# -*- coding: utf-8 -*-
"""SQLite 存储：设计版本与参数历史。所有数据保存在本地 data/sundial.db。"""
import json
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "sundial.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS designs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    params_json TEXT NOT NULL,
    note TEXT DEFAULT ''
);
"""

FIELDS = ["lat", "lng", "tz", "dst", "az", "inc", "width", "height",
          "style_len", "min_spacing", "hour_step", "year"]


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def list_designs():
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, created_at, updated_at, note FROM designs "
            "ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_design(did):
    with _conn() as c:
        row = c.execute("SELECT * FROM designs WHERE id=?", (did,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json"))
    return d


def save_design(name, params, note="", did=None):
    now = time.time()
    pj = json.dumps(params, ensure_ascii=False)
    with _conn() as c:
        if did is None:
            cur = c.execute(
                "INSERT INTO designs (name, created_at, updated_at, params_json, note) "
                "VALUES (?,?,?,?,?)", (name, now, now, pj, note))
            return cur.lastrowid
        c.execute(
            "UPDATE designs SET name=?, updated_at=?, params_json=?, note=? WHERE id=?",
            (name, now, pj, note, did))
        return did


def delete_design(did):
    with _conn() as c:
        c.execute("DELETE FROM designs WHERE id=?", (did,))


def diff_designs(id_a, id_b):
    a, b = get_design(id_a), get_design(id_b)
    if a is None or b is None:
        return None
    rows = []
    pa, pb = a["params"], b["params"]
    labels = {
        "lat": "纬度 (°)", "lng": "经度 (°)", "tz": "时区 (h)",
        "dst": "夏令时偏移 (h)", "az": "盘面方位 (°，南0西正)",
        "inc": "盘面倾角 (°)", "width": "盘面宽 (mm)", "height": "盘面高 (mm)",
        "style_len": "晷针长度 (mm)", "min_spacing": "最小加工间距 (mm)",
        "hour_step": "刻度步进 (min)",
    }
    for k in FIELDS:
        if k in ("year",):
            continue
        va, vb = pa.get(k), pb.get(k)
        rows.append({"key": k, "label": labels.get(k, k),
                     "a": va, "b": vb, "changed": va != vb})
    return {"a": {"id": a["id"], "name": a["name"]},
            "b": {"id": b["id"], "name": b["name"]},
            "rows": rows}

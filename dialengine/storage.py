# -*- coding: utf-8 -*-
"""SQLite 存储：设计版本、参数历史与现场校准批次。所有数据保存在本地 data/sundial.db。"""
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
CREATE TABLE IF NOT EXISTS cal_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    fit_config_json TEXT DEFAULT '',
    result_json TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    applied_design_id INTEGER
);
CREATE TABLE IF NOT EXISTS cal_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    dt_local TEXT NOT NULL,
    u REAL NOT NULL,
    v REAL NOT NULL,
    valid INTEGER DEFAULT 1,
    note TEXT DEFAULT '',
    created_at REAL NOT NULL
);
"""

FIELDS = ["lat", "lng", "tz", "dst", "az", "inc", "width", "height",
          "style_len", "min_spacing", "hour_step", "year",
          "root_du", "root_dv"]


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    _migrate(c)
    return c


def _migrate(c):
    """老库增补列：设计版本的血统关联（校准生成的新版本指向原版本）。"""
    cols = [r[1] for r in c.execute("PRAGMA table_info(designs)")]
    if "parent_id" not in cols:
        c.execute("ALTER TABLE designs ADD COLUMN parent_id INTEGER")
    if "origin" not in cols:
        c.execute("ALTER TABLE designs ADD COLUMN origin TEXT DEFAULT 'manual'")


def list_designs():
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, created_at, updated_at, note, parent_id, origin "
            "FROM designs ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_design(did):
    with _conn() as c:
        row = c.execute("SELECT * FROM designs WHERE id=?", (did,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json"))
    return d


def save_design(name, params, note="", did=None, parent_id=None, origin="manual"):
    now = time.time()
    pj = json.dumps(params, ensure_ascii=False)
    with _conn() as c:
        if did is None:
            cur = c.execute(
                "INSERT INTO designs (name, created_at, updated_at, params_json, note, "
                "parent_id, origin) VALUES (?,?,?,?,?,?,?)",
                (name, now, now, pj, note, parent_id, origin))
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
        "root_du": "根点偏移 u (mm)", "root_dv": "根点偏移 v (mm)",
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


# ---------------------------------------------------------------- 校准批次

def create_batch(design_id, name):
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO cal_batches (design_id, name, created_at, updated_at) "
            "VALUES (?,?,?,?)", (design_id, name, now, now))
        return cur.lastrowid


def list_batches(design_id=None):
    sql = ("SELECT b.id, b.design_id, b.name, b.created_at, b.updated_at, "
           "b.status, b.applied_design_id, "
           "(SELECT COUNT(*) FROM cal_observations o WHERE o.batch_id = b.id) AS n_obs "
           "FROM cal_batches b")
    args = ()
    if design_id is not None:
        sql += " WHERE b.design_id = ?"
        args = (design_id,)
    sql += " ORDER BY b.updated_at DESC"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def get_batch(bid):
    with _conn() as c:
        row = c.execute("SELECT * FROM cal_batches WHERE id=?", (bid,)).fetchone()
        if row is None:
            return None
        b = dict(row)
        b["fit_config"] = (json.loads(b["fit_config_json"])
                           if b.get("fit_config_json") else None)
        b["result"] = (json.loads(b["result_json"])
                       if b.get("result_json") else None)
        b.pop("fit_config_json", None)
        b.pop("result_json", None)
        rows = c.execute(
            "SELECT id, batch_id, dt_local, u, v, valid, note, created_at "
            "FROM cal_observations WHERE batch_id=? ORDER BY dt_local, id",
            (bid,)).fetchall()
        b["observations"] = [dict(r) for r in rows]
    return b


def delete_batch(bid):
    with _conn() as c:
        c.execute("DELETE FROM cal_observations WHERE batch_id=?", (bid,))
        c.execute("DELETE FROM cal_batches WHERE id=?", (bid,))


def save_batch_fit(bid, config, result):
    """保存反算配置与结果；result 为 None 时清除旧结果（拟合失败）。"""
    now = time.time()
    cj = json.dumps(config, ensure_ascii=False) if config else ""
    rj = json.dumps(result, ensure_ascii=False) if result else ""
    with _conn() as c:
        c.execute(
            "UPDATE cal_batches SET fit_config_json=?, result_json=?, updated_at=? "
            "WHERE id=?", (cj, rj, now, bid))


def mark_batch_applied(bid, new_design_id):
    with _conn() as c:
        c.execute(
            "UPDATE cal_batches SET status='applied', applied_design_id=?, "
            "updated_at=? WHERE id=?", (new_design_id, time.time(), bid))


def add_observation(batch_id, dt_local, u, v, note=""):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO cal_observations (batch_id, dt_local, u, v, valid, note, "
            "created_at) VALUES (?,?,?,?,1,?,?)",
            (batch_id, dt_local, u, v, note, time.time()))
        return cur.lastrowid


def get_observation(oid):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM cal_observations WHERE id=?", (oid,)).fetchone()
    return dict(row) if row else None


def update_observation(oid, fields):
    sets, args = [], []
    for k in ("dt_local", "u", "v", "valid", "note"):
        if k in fields:
            sets.append("%s=?" % k)
            args.append(fields[k])
    if not sets:
        return
    args.append(oid)
    with _conn() as c:
        c.execute("UPDATE cal_observations SET %s WHERE id=?" % ", ".join(sets), args)


def delete_observation(oid):
    with _conn() as c:
        c.execute("DELETE FROM cal_observations WHERE id=?", (oid,))

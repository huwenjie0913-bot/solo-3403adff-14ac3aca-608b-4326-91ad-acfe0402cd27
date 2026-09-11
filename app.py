# -*- coding: utf-8 -*-
"""日晷刻度盘设计与校核 — Flask 应用。全部数据与计算均在本机完成，不访问外部服务。"""
import json

from flask import Flask, request, jsonify, Response, send_from_directory

from dialengine import engine, storage, svg_export, calibrate

app = Flask(__name__, static_folder="static", static_url_path="")

NUM_FIELDS = {
    "lat": (-90.0, 90.0),
    "lng": (-180.0, 180.0),
    "tz": (-12.0, 14.0),
    "dst": (0.0, 3.0),
    "az": (-180.0, 180.0),
    "inc": (0.0, 180.0),
    "width": (20.0, 5000.0),
    "height": (20.0, 5000.0),
    "style_len": (1.0, 5000.0),
    "min_spacing": (0.1, 100.0),
    "root_du": (-2000.0, 2000.0),
    "root_dv": (-2000.0, 2000.0),
}
INT_FIELDS = {"hour_step": (5, 180), "year": (1900, 2100)}
DEFAULTS = {
    "lat": 39.9, "lng": 116.4, "tz": 8.0, "dst": 0.0,
    "az": 180.0, "inc": 0.0, "width": 300.0, "height": 300.0,
    "style_len": 80.0, "min_spacing": 2.0, "hour_step": 60, "year": 2026,
    "root_du": 0.0, "root_dv": 0.0,
}


def parse_params(src):
    """从 dict（表单/JSON）解析参数，返回 (params, errors)。"""
    p = {}
    errors = []
    for k, (lo, hi) in NUM_FIELDS.items():
        try:
            v = float(src.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            errors.append("参数 %s 必须为数字" % k)
            continue
        if not (lo <= v <= hi):
            errors.append("参数 %s=%g 超出允许范围 [%g, %g]" % (k, v, lo, hi))
        p[k] = v
    for k, (lo, hi) in INT_FIELDS.items():
        try:
            v = int(float(src.get(k, DEFAULTS[k])))
        except (TypeError, ValueError):
            errors.append("参数 %s 必须为整数" % k)
            continue
        if not (lo <= v <= hi):
            errors.append("参数 %s=%d 超出允许范围 [%d, %d]" % (k, v, lo, hi))
        p[k] = v
    if p.get("hour_step") and 60 % p["hour_step"] != 0 and p["hour_step"] not in ():
        # 允许 5,10,12,15,20,30,60
        if p["hour_step"] not in (5, 10, 12, 15, 20, 30, 60):
            errors.append("刻度步进建议取 5/10/15/20/30/60 分钟")
    return p, errors


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    src = request.get_json(force=True, silent=True) or {}
    p, errors = parse_params(src)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    try:
        data = engine.compute_dial(p, p["year"])
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "errors": ["计算失败: %s" % ex]}), 500
    return jsonify({"ok": True, "data": data, "params": p})


@app.route("/api/preview", methods=["POST"])
def api_preview():
    src = request.get_json(force=True, silent=True) or {}
    p, errors = parse_params(src.get("params", {}))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    mode = src.get("mode", "solar")
    if mode not in ("solar", "civil"):
        return jsonify({"ok": False, "errors": ["mode 必须为 solar/civil"]}), 400
    try:
        data = engine.preview_shadow(
            p, int(src["year"]), int(src["month"]), int(src["day"]),
            float(src["hour"]), mode)
    except (KeyError, TypeError, ValueError) as ex:
        return jsonify({"ok": False, "errors": ["预览参数无效: %s" % ex]}), 400
    return jsonify({"ok": True, "data": data})


@app.route("/api/designs", methods=["GET", "POST", "DELETE"])
def api_designs():
    if request.method == "GET":
        return jsonify({"ok": True, "designs": storage.list_designs()})
    if request.method == "DELETE":
        did = int(request.args.get("id"))
        storage.delete_design(did)
        return jsonify({"ok": True})
    body = request.get_json(force=True, silent=True) or {}
    p, errors = parse_params(body.get("params", {}))
    name = (body.get("name") or "").strip()
    if not name:
        errors.append("请填写版本名称")
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    did = storage.save_design(name, p, body.get("note", ""),
                              body.get("id"))
    return jsonify({"ok": True, "id": did})


@app.route("/api/designs/<int:did>")
def api_design_detail(did):
    d = storage.get_design(did)
    if d is None:
        return jsonify({"ok": False, "errors": ["版本不存在"]}), 404
    return jsonify({"ok": True, "design": d})


@app.route("/api/diff")
def api_diff():
    a = request.args.get("a", type=int)
    b = request.args.get("b", type=int)
    d = storage.diff_designs(a, b)
    if d is None:
        return jsonify({"ok": False, "errors": ["版本不存在"]}), 404
    # 对两版参数实际计算盘面，比较刻线几何
    da_obj = storage.get_design(a)
    db_obj = storage.get_design(b)
    try:
        pa = _with_defaults(da_obj["params"])
        pb = _with_defaults(db_obj["params"])
        calc_a = engine.compute_dial(pa, pa["year"])
        calc_b = engine.compute_dial(pb, pb["year"])
        d["geometry"] = engine.geometry_diff(calc_a, calc_b)
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "errors": ["几何比较失败: %s" % ex]}), 500
    return jsonify({"ok": True, "diff": d})


def _with_defaults(p):
    out = dict(DEFAULTS)
    out.update(p or {})
    return out


@app.route("/api/export.svg", methods=["POST"])
def api_export_svg():
    src = request.get_json(force=True, silent=True) or {}
    p, errors = parse_params(src)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    data = engine.compute_dial(p, p["year"])
    svg = svg_export.generate_print_svg(p, data)
    return Response(svg, mimetype="image/svg+xml")


# ---------------------------------------------------------------- 现场校准

@app.route("/api/cal/batches", methods=["GET", "POST"])
def api_cal_batches():
    if request.method == "GET":
        design_id = request.args.get("design_id", type=int)
        return jsonify({"ok": True, "batches": storage.list_batches(design_id)})
    body = request.get_json(force=True, silent=True) or {}
    try:
        design_id = int(body.get("design_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "errors": ["缺少所属设计版本"]}), 400
    if storage.get_design(design_id) is None:
        return jsonify({"ok": False, "errors": ["所属设计版本不存在"]}), 404
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "errors": ["请填写批次名称"]}), 400
    bid = storage.create_batch(design_id, name)
    return jsonify({"ok": True, "id": bid})


@app.route("/api/cal/batches/<int:bid>", methods=["GET", "DELETE"])
def api_cal_batch(bid):
    if request.method == "DELETE":
        storage.delete_batch(bid)
        return jsonify({"ok": True})
    batch = storage.get_batch(bid)
    if batch is None:
        return jsonify({"ok": False, "errors": ["校准批次不存在"]}), 404
    design = storage.get_design(batch["design_id"])
    batch["design"] = {"id": design["id"], "name": design["name"],
                       "params": design["params"]} if design else None
    return jsonify({"ok": True, "batch": batch})


@app.route("/api/cal/batches/<int:bid>/obs", methods=["POST"])
def api_cal_obs_add(bid):
    if storage.get_batch(bid) is None:
        return jsonify({"ok": False, "errors": ["校准批次不存在"]}), 404
    body = request.get_json(force=True, silent=True) or {}
    errors = []
    dt_local = (body.get("dt_local") or "").strip()
    if calibrate.parse_dt_local(dt_local) is None:
        errors.append("观测时间格式无效（应为 YYYY-MM-DD HH:MM）")
    uv = {}
    for kc in ("u", "v"):
        try:
            uv[kc] = float(body.get(kc))
            if not (-5000.0 <= uv[kc] <= 5000.0):
                errors.append("坐标 %s 超出允许范围 ±5000 mm" % kc)
        except (TypeError, ValueError):
            errors.append("坐标 %s 必须为数字" % kc)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    oid = storage.add_observation(bid, dt_local, uv["u"], uv["v"],
                                  (body.get("note") or "").strip())
    return jsonify({"ok": True, "id": oid})


@app.route("/api/cal/obs/<int:oid>", methods=["PUT", "DELETE"])
def api_cal_obs(oid):
    if request.method == "DELETE":
        storage.delete_observation(oid)
        return jsonify({"ok": True})
    if storage.get_observation(oid) is None:
        return jsonify({"ok": False, "errors": ["观测记录不存在"]}), 404
    body = request.get_json(force=True, silent=True) or {}
    fields = {}
    errors = []
    if "valid" in body:
        fields["valid"] = 1 if body["valid"] else 0
    if "note" in body:
        fields["note"] = (body.get("note") or "").strip()
    if "dt_local" in body:
        dt = (body.get("dt_local") or "").strip()
        if calibrate.parse_dt_local(dt) is None:
            errors.append("观测时间格式无效（应为 YYYY-MM-DD HH:MM）")
        else:
            fields["dt_local"] = dt
    for kc in ("u", "v"):
        if kc in body:
            try:
                val = float(body[kc])
                if not (-5000.0 <= val <= 5000.0):
                    errors.append("坐标 %s 超出允许范围 ±5000 mm" % kc)
                else:
                    fields[kc] = val
            except (TypeError, ValueError):
                errors.append("坐标 %s 必须为数字" % kc)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    storage.update_observation(oid, fields)
    return jsonify({"ok": True})


@app.route("/api/cal/batches/<int:bid>/fit", methods=["POST"])
def api_cal_fit(bid):
    batch = storage.get_batch(bid)
    if batch is None:
        return jsonify({"ok": False, "errors": ["校准批次不存在"]}), 404
    design = storage.get_design(batch["design_id"])
    if design is None:
        return jsonify({"ok": False, "errors": ["所属设计版本已被删除"]}), 404
    body = request.get_json(force=True, silent=True) or {}
    base = _with_defaults(design["params"])
    result = calibrate.fit(base, batch["observations"], body.get("config"))
    # 配置始终保留；结果仅在成功时保存，失败则清除过期结果
    storage.save_batch_fit(bid, result.get("config") or body.get("config"),
                           result if result.get("ok") else None)
    if not result.get("ok"):
        return jsonify({"ok": False, "errors": result.get("errors", ["反算失败"]),
                        "result": result}), 400
    return jsonify({"ok": True, "result": result})


@app.route("/api/cal/batches/<int:bid>/apply", methods=["POST"])
def api_cal_apply(bid):
    batch = storage.get_batch(bid)
    if batch is None:
        return jsonify({"ok": False, "errors": ["校准批次不存在"]}), 404
    result = batch.get("result")
    if not result or not result.get("ok"):
        return jsonify({"ok": False, "errors": ["尚无成功的反算结果，请先执行反算"]}), 400
    design = storage.get_design(batch["design_id"])
    if design is None:
        return jsonify({"ok": False, "errors": ["所属设计版本已被删除"]}), 404
    params = _with_defaults(design["params"])
    for key, _label, _unit in calibrate.FIT_FIELDS:
        params[key] = result["best"][key]
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip() or ("%s·校准#%d" % (design["name"], bid))
    c = result["corrections"]
    note = ("由校准批次 #%d「%s」反算生成：RMS %.2f mm（%d 条有效样本）；安装修正量 "
            "方位 %+.2f°，倾角 %+.2f°，晷针长度 %+.1f mm，根点 u %+.1f mm、v %+.1f mm"
            % (bid, batch["name"], result["rms"], result["n_fit"],
               c["az"], c["inc"], c["style_len"], c["root_du"], c["root_dv"]))
    new_id = storage.save_design(name, params, note,
                                 parent_id=design["id"], origin="calibration")
    storage.mark_batch_applied(bid, new_id)
    return jsonify({"ok": True, "id": new_id, "name": name, "note": note})


@app.route("/api/cal/batches/<int:bid>/report")
def api_cal_report(bid):
    batch = storage.get_batch(bid)
    if batch is None:
        return jsonify({"ok": False, "errors": ["校准批次不存在"]}), 404
    result = batch.get("result")
    if not result or not result.get("ok"):
        return jsonify({"ok": False, "errors": ["尚无反算结果，无法生成报告"]}), 400
    design = storage.get_design(batch["design_id"])
    if design is None:
        return jsonify({"ok": False, "errors": ["所属设计版本已被删除"]}), 404
    report = calibrate.build_report(batch, design, result)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    return Response(
        payload, mimetype="application/json",
        headers={"Content-Disposition":
                 "attachment; filename=calibration_report_b%d.json" % bid})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

# -*- coding: utf-8 -*-
"""日晷刻度盘设计与校核 — Flask 应用。全部数据与计算均在本机完成，不访问外部服务。"""
import json

from flask import Flask, request, jsonify, Response, send_from_directory

from dialengine import engine, storage, svg_export, calibrate, shade

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


# ---------------------------------------------------------------- 安装点与遮光分析

def _load_design_params(did):
    design = storage.get_design(did)
    if design is None:
        return None, None
    return design, _with_defaults(design["params"])


def _get_mount_or_err(mid):
    mount = storage.get_mount(mid)
    if mount is None:
        return None, (jsonify({"ok": False, "errors": ["安装点不存在"]}), 404)
    design, p = _load_design_params(mount["design_id"])
    if p is None:
        return None, (jsonify({"ok": False, "errors": ["所属设计版本不存在"]}), 404)
    mount["_design"] = design
    mount["_params"] = p
    return mount, None


@app.route("/api/designs/<int:did>/mounts", methods=["GET", "POST"])
def api_mounts(did):
    design, p = _load_design_params(did)
    if p is None:
        return jsonify({"ok": False, "errors": ["设计版本不存在"]}), 404
    if request.method == "GET":
        return jsonify({"ok": True, "mounts": storage.list_mounts(did)})
    body = request.get_json(force=True, silent=True) or {}
    mount, errors = shade.normalize_mount_payload(body)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    mid = storage.save_mount(did, mount)
    return jsonify({"ok": True, "id": mid})


@app.route("/api/mounts/<int:mid>", methods=["GET", "PUT", "DELETE"])
def api_mount(mid):
    mount = storage.get_mount(mid)
    if mount is None:
        return jsonify({"ok": False, "errors": ["安装点不存在"]}), 404
    if request.method == "GET":
        return jsonify({"ok": True, "mount": mount})
    if request.method == "DELETE":
        storage.delete_mount(mid)
        return jsonify({"ok": True})
    body = request.get_json(force=True, silent=True) or {}
    new_mount, errors = shade.normalize_mount_payload(body, existing=mount)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    rid = storage.save_mount(mount["design_id"], new_mount, mid)
    if rid is None:
        return jsonify({"ok": False, "errors": ["更新失败"]}), 400
    return jsonify({"ok": True, "id": rid})


@app.route("/api/mounts/<int:mid>/duplicate", methods=["POST"])
def api_mount_duplicate(mid):
    body = request.get_json(force=True, silent=True) or {}
    new_id = storage.duplicate_mount(mid, (body.get("name") or "").strip() or None)
    if new_id is None:
        return jsonify({"ok": False, "errors": ["安装点不存在"]}), 404
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/mounts/import", methods=["POST"])
def api_mount_import():
    """导入一个或多个安装点 JSON：始终写入新行，不覆盖既有数据。"""
    body = request.get_json(force=True, silent=True) or {}
    try:
        design_id = int(body.get("design_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "errors": ["缺少所属设计版本"]}), 400
    _design, p = _load_design_params(design_id)
    if p is None:
        return jsonify({"ok": False, "errors": ["设计版本不存在"]}), 404
    items = body.get("mounts")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "errors": ["导入内容为空"]}), 400
    ids, errors = [], []
    for it in items:
        mount, errs = shade.normalize_mount_payload(it)
        errors.extend(errs)
        if not errs:
            ids.append(storage.save_mount(design_id, mount))
    if not ids:
        return jsonify({"ok": False, "errors": errors or ["无可导入的安装点"]}), 400
    return jsonify({"ok": True, "ids": ids, "errors": errors})


@app.route("/api/shade/instant", methods=["POST"])
def api_shade_instant():
    body = request.get_json(force=True, silent=True) or {}
    mount, err = _mount_from_body(body)
    if err:
        return err
    try:
        data = shade.instant(mount["_params"], mount, int(body["year"]),
                             int(body["month"]), int(body["day"]),
                             float(body["hour"]))
    except (KeyError, TypeError, ValueError) as ex:
        return jsonify({"ok": False, "errors": ["时刻参数无效: %s" % ex]}), 400
    return jsonify({"ok": True, "data": data})


@app.route("/api/shade/day", methods=["POST"])
def api_shade_day():
    body = request.get_json(force=True, silent=True) or {}
    mount, err = _mount_from_body(body)
    if err:
        return err
    try:
        data = shade.day_curve(mount["_params"], mount, int(body["year"]),
                               int(body["month"]), int(body["day"]))
    except (KeyError, TypeError, ValueError) as ex:
        return jsonify({"ok": False, "errors": ["日期参数无效: %s" % ex]}), 400
    return jsonify({"ok": True, "data": data})


@app.route("/api/shade/arcs", methods=["POST"])
def api_shade_arcs():
    body = request.get_json(force=True, silent=True) or {}
    p, errors = parse_params(body.get("params", {}))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    try:
        data = shade.month_arcs(p, int(body.get("year") or p["year"]))
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "errors": ["弧线计算失败: %s" % ex]}), 500
    return jsonify({"ok": True, "data": data, "az_ref": body.get("az_ref", "south")})


@app.route("/api/shade/analyze", methods=["POST"])
def api_shade_analyze():
    body = request.get_json(force=True, silent=True) or {}
    mount, err = _mount_from_body(body)
    if err:
        return err
    try:
        year = int(body.get("year") or mount["_params"]["year"])
        data = shade.analyze_year(mount["_params"], mount, year)
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "errors": ["全年分析失败: %s" % ex]}), 500
    return jsonify({"ok": True, "data": data})


@app.route("/api/shade/compare", methods=["POST"])
def api_shade_compare():
    body = request.get_json(force=True, silent=True) or {}
    ma, ea = _get_mount_or_err(int(body.get("a"))) if body.get("a") else (None, None)
    mb, eb = _get_mount_or_err(int(body.get("b"))) if body.get("b") else (None, None)
    if ea or eb:
        return ea or eb
    if ma["design_id"] != mb["design_id"]:
        return jsonify({"ok": False,
                        "errors": ["请选择同一设计版本下的两个安装点进行比较"]}), 400
    try:
        year = int(body.get("year") or ma["_params"]["year"])
        data = shade.compare_sites(ma["_params"], ma, mb, year)
        data["a"] = {"id": ma["id"], "name": ma["name"]}
        data["b"] = {"id": mb["id"], "name": mb["name"]}
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "errors": ["比较失败: %s" % ex]}), 500
    return jsonify({"ok": True, "data": data})


@app.route("/api/mounts/<int:mid>/report")
def api_mount_report(mid):
    year = request.args.get("year", type=int)
    mount, err = _get_mount_or_err(mid)
    if err:
        return err
    try:
        analysis = shade.analyze_year(mount["_params"], mount,
                                      year or mount["_params"]["year"])
        report = shade.build_report(mount["_params"], mount, mount["_design"],
                                    analysis, year or mount["_params"]["year"])
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "errors": ["报告生成失败: %s" % ex]}), 500
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    return Response(
        payload, mimetype="application/json",
        headers={"Content-Disposition":
                 "attachment; filename=mount_report_m%d.json" % mid})


def _mount_from_body(body):
    """已保存安装点：body['mount_id']；或携带内联轮廓 body['mount'] + params/design_id。"""
    if body.get("mount_id"):
        mount, err = _get_mount_or_err(int(body["mount_id"]))
        return (mount, None) if mount is not None else (None, err)
    if body.get("mount") and (body.get("design_id") or body.get("params")):
        mount_in, errors = shade.normalize_mount_payload(body["mount"])
        if errors:
            return None, (jsonify({"ok": False, "errors": errors}), 400)
        # 请求携带 params 时以当前界面参数为准；否则回退到已保存版本的参数
        if body.get("params"):
            p, errs = parse_params(body.get("params", {}))
            if errs:
                return None, (jsonify({"ok": False, "errors": errs}), 400)
        else:
            _design, p = _load_design_params(int(body["design_id"]))
            if p is None:
                return None, (jsonify({"ok": False,
                                       "errors": ["设计版本不存在"]}), 404)
        mount_in["_params"] = p
        mount_in["_design"] = None
        return mount_in, None
    return None, (jsonify({"ok": False,
                           "errors": ["缺少安装点（mount_id 或 mount 数据）"]}), 400)


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

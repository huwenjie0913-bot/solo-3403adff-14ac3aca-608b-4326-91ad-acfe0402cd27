# -*- coding: utf-8 -*-
"""日晷刻度盘设计与校核 — Flask 应用。全部数据与计算均在本机完成，不访问外部服务。"""
from flask import Flask, request, jsonify, Response, send_from_directory

from dialengine import engine, storage, svg_export

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
}
INT_FIELDS = {"hour_step": (5, 180), "year": (1900, 2100)}
DEFAULTS = {
    "lat": 39.9, "lng": 116.4, "tz": 8.0, "dst": 0.0,
    "az": 180.0, "inc": 0.0, "width": 300.0, "height": 300.0,
    "style_len": 80.0, "min_spacing": 2.0, "hour_step": 60, "year": 2026,
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

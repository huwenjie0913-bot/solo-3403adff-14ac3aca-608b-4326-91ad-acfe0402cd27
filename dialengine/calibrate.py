# -*- coding: utf-8 -*-
"""现场观测反算与安装校准。

复用 engine 的盘面投影模型与 solar 的太阳位置算法：
给定设计版本参数与一组现场观测（民用日期时间 + 实测影端 (u, v)），
对盘面方位、盘面倾角、晷针长度与根点偏移做最小二乘反算，
返回最佳参数、残差统计、逐条预测/残差向量与参数可辨识性评估。

可辨识性基于数值雅可比：列范数给出灵敏度，列间余弦给出参数对相关性
（|相关| 接近 1 即“近似等价解”），(JᵀJ)⁻¹ 给出近似标准差。
全部计算在本机完成，纯 Python，无第三方依赖。
"""
import math
import time
from datetime import datetime, timedelta

from . import engine, solar

# 可估算参数：(key, 中文名, 单位)
FIT_FIELDS = [
    ("az", "盘面方位", "°"),
    ("inc", "盘面倾角", "°"),
    ("style_len", "晷针长度", "mm"),
    ("root_du", "根点偏移 u", "mm"),
    ("root_dv", "根点偏移 v", "mm"),
]
FIT_KEYS = [k for k, _, _ in FIT_FIELDS]
LABELS = {k: lb for k, lb, _ in FIT_FIELDS}
UNITS = {k: un for k, _, un in FIT_FIELDS}

# 参数物理边界（用户搜索范围再取其交集）
HARD_LIMITS = {
    "az": (-180.0, 180.0),
    "inc": (0.0, 180.0),
    "style_len": (1.0, 5000.0),
    "root_du": (-2000.0, 2000.0),
    "root_dv": (-2000.0, 2000.0),
}

# 相关绝对值超过该阈值即判定“近似等价解”（良态多日样本典型值 < 0.9）
EQUIV_CORR = 0.95


def base_value(base, key):
    """原设计参数值（根点偏移缺省为 0）。"""
    v = base.get(key)
    if v is None:
        return 0.0
    return float(v)


def parse_dt_local(s):
    """'YYYY-MM-DDTHH:MM'（接受空格/斜杠与可选秒）-> 朴素 datetime（民用本地）。"""
    s = (s or "").strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- 配置整理

def normalize_config(base, raw):
    """整理前端提交的反算配置：每项 {on, value, min, max}。返回 (cfg, errors)。"""
    raw = raw or {}
    cfg = {}
    errors = []
    for key, label, unit in FIT_FIELDS:
        c = raw.get(key) or {}
        bv = base_value(base, key)
        on = bool(c.get("on", False))
        try:
            value = float(c.get("value", bv))
        except (TypeError, ValueError):
            errors.append("%s 的锁定值必须为数字" % label)
            continue
        try:
            lo = float(c.get("min", bv - 5.0))
            hi = float(c.get("max", bv + 5.0))
        except (TypeError, ValueError):
            errors.append("%s 的搜索范围必须为数字" % label)
            continue
        lo_h, hi_h = HARD_LIMITS[key]
        value = min(max(value, lo_h), hi_h)
        lo = min(max(lo, lo_h), hi_h)
        hi = min(max(hi, lo_h), hi_h)
        if hi < lo:
            lo, hi = hi, lo
        if hi - lo < 1e-9:
            # 范围退化为一点：视为锁定
            on = False
            value = lo
        cfg[key] = {"on": on, "value": value, "min": lo, "max": hi}
    return cfg, errors


# ---------------------------------------------------------------- 投影预测

def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _frame(base, theta):
    """按当前参数向量构造盘面坐标架。"""
    n, u, v, pole, p_used, sigma = engine.face_frame(
        float(base["lat"]), theta["az"], theta["inc"])
    return n, u, v, p_used


def _predict_uv(frame, theta, s):
    """太阳单位向量 s 下的预测影端 (u, v)（含根点偏移）；不受光返回 None。"""
    n, u, v, p_used = frame
    if s[2] <= 1e-9:
        return None
    sdn = _dot3(s, n)
    if sdn <= 1e-9:
        return None
    L = theta["style_len"]
    k = L * _dot3(p_used, n) / sdn
    x = (L * p_used[0] - k * s[0],
         L * p_used[1] - k * s[1],
         L * p_used[2] - k * s[2])
    pu = _dot3(x, u) + theta["root_du"]
    pv = _dot3(x, v) + theta["root_dv"]
    return (pu, pv)


def _obs_sun(base, dt_local):
    """观测民用时刻对应的太阳单位向量（ENU）与 UTC 时刻。"""
    tz = float(base["tz"])
    dst = float(base.get("dst", 0.0))
    utc = dt_local - timedelta(hours=tz + dst)
    e, n, u, alt, az_sun = solar.sun_vector_enu(
        utc, float(base["lat"]), float(base["lng"]))
    return (e, n, u), utc


# ---------------------------------------------------------------- 优化器

def _clamp(x, lo, hi):
    for i in range(len(x)):
        x[i] = min(max(x[i], lo[i]), hi[i])
    return x


def _coordinate_search(cost, x0, lo, hi, grid=21, sweeps=4):
    """逐坐标轴网格线搜索：在各自搜索范围内粗扫，反复扫描直到无改进。"""
    x = list(x0)
    fx = cost(x)
    for _ in range(sweeps):
        improved = False
        for i in range(len(x)):
            best_v, best_f = x[i], fx
            for g in range(grid):
                v = lo[i] + (hi[i] - lo[i]) * g / (grid - 1)
                if v == x[i]:
                    continue
                old = x[i]
                x[i] = v
                f = cost(x)
                if f < best_f:
                    best_f, best_v = f, v
                x[i] = old
            x[i] = best_v
            if best_f < fx - 1e-12:
                improved = True
            fx = best_f
        if not improved:
            break
    return x, fx


def _nelder_mead(cost, x0, lo, hi, step, max_iter=800, tol=1e-10):
    """盒约束 Nelder-Mead 单纯形精化。"""
    n = len(x0)
    simplex = [list(x0)]
    for i in range(n):
        x = list(x0)
        x[i] = x[i] + step[i] if x[i] + step[i] <= hi[i] else x[i] - step[i]
        simplex.append(_clamp(x, lo, hi))
    vals = [cost(x) for x in simplex]
    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        simplex = [simplex[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) <= tol * max(1.0, abs(vals[0])):
            break
        centroid = [sum(simplex[i][d] for i in range(n)) / n for d in range(n)]
        worst = simplex[-1]
        # 反射
        xr = _clamp([centroid[d] + (centroid[d] - worst[d]) for d in range(n)], lo, hi)
        fr = cost(xr)
        if fr < vals[0]:
            # 扩张
            xe = _clamp([centroid[d] + 2.0 * (xr[d] - centroid[d]) for d in range(n)], lo, hi)
            fe = cost(xe)
            simplex[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            simplex[-1], vals[-1] = xr, fr
        else:
            # 收缩
            if fr < vals[-1]:
                xc = _clamp([centroid[d] + 0.5 * (xr[d] - centroid[d]) for d in range(n)], lo, hi)
                fc = cost(xc)
                if fc <= fr:
                    simplex[-1], vals[-1] = xc, fc
                else:
                    _shrink(simplex, vals, cost)
            else:
                xc = _clamp([centroid[d] + 0.5 * (worst[d] - centroid[d]) for d in range(n)], lo, hi)
                fc = cost(xc)
                if fc < vals[-1]:
                    simplex[-1], vals[-1] = xc, fc
                else:
                    _shrink(simplex, vals, cost)
    best_i = min(range(n + 1), key=lambda i: vals[i])
    return simplex[best_i], vals[best_i]


def _shrink(simplex, vals, cost):
    best = simplex[0]
    for i in range(1, len(simplex)):
        simplex[i] = [best[d] + 0.5 * (simplex[i][d] - best[d]) for d in range(len(best))]
        vals[i] = cost(simplex[i])


# ---------------------------------------------------------------- 矩阵工具

def _invert(mat):
    """高斯-约当求逆（部分主元）；奇异时返回 None。"""
    n = len(mat)
    aug = [list(mat[i]) + [1.0 if i == j else 0.0 for j in range(n)]
           for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[piv][col]) < 1e-14:
            return None
        aug[col], aug[piv] = aug[piv], aug[col]
        d = aug[col][col]
        aug[col] = [a / d for a in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0.0:
                f = aug[r][col]
                aug[r] = [a - f * b for a, b in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


# ---------------------------------------------------------------- 反算主流程

def fit(base, observations, raw_cfg):
    """反算主入口。

    base: 原设计参数 dict；observations: [{id, dt_local, u, v, valid}]；
    raw_cfg: 各参数 {on, value, min, max}。
    返回可 JSON 序列化的结果 dict（ok=False 时含 errors 与逐条状态）。
    """
    cfg, errors = normalize_config(base, raw_cfg)
    if errors:
        return {"ok": False, "errors": errors}

    # ---- 观测预处理：解析时间、太阳向量、基于原几何的受光状态
    theta_base = {k: base_value(base, k) for k in FIT_KEYS}
    frame0 = _frame(base, theta_base)
    n0 = frame0[0]
    obs = []
    for o in observations:
        rec = {
            "id": o.get("id"),
            "dt_local": o.get("dt_local", ""),
            "u": _fnum(o.get("u")),
            "v": _fnum(o.get("v")),
            "valid": bool(o.get("valid", 1)),
            "note": o.get("note", ""),
            "state": "ok",
            "sun": None,
            "utc": None,
        }
        dt = parse_dt_local(rec["dt_local"])
        if dt is None or rec["u"] is None or rec["v"] is None:
            rec["state"] = "bad_input"
        else:
            try:
                s, utc = _obs_sun(base, dt)
                rec["sun"] = s
                rec["utc"] = utc.strftime("%Y-%m-%d %H:%M")
                if s[2] <= 1e-4:
                    rec["state"] = "below_horizon"
                elif _dot3(s, n0) <= 1e-4:
                    rec["state"] = "backside"
            except Exception:  # noqa: BLE001
                rec["state"] = "bad_input"
        obs.append(rec)

    fit_obs = [r for r in obs if r["valid"] and r["state"] == "ok"]
    n_back = sum(1 for r in obs if r["valid"] and r["state"] == "backside")
    n_hor = sum(1 for r in obs if r["valid"] and r["state"] == "below_horizon")
    n_bad = sum(1 for r in obs if r["valid"] and r["state"] == "bad_input")

    warnings = []
    if n_back:
        warnings.append({
            "code": "backside",
            "msg": "%d 条观测时刻太阳位于盘面背侧（该面不受光），未参与反算；"
                   "请在盘面受光时段观测。" % n_back})
    if n_hor:
        warnings.append({
            "code": "horizon",
            "msg": "%d 条观测时刻太阳位于地平线下，未参与反算。" % n_hor})
    if n_bad:
        warnings.append({
            "code": "bad_input",
            "msg": "%d 条观测的时间或坐标无法解析，未参与反算。" % n_bad})

    free = [k for k in FIT_KEYS if cfg[k]["on"]]
    k = len(free)
    if k == 0:
        return {"ok": False,
                "errors": ["未选择任何待估算参数：请至少勾选一个参数并给出搜索范围。"],
                "obs": _public_obs(obs), "warnings": warnings}

    need = max(2, k // 2 + 1)
    if len(fit_obs) < need:
        errs = ["样本不足：%d 个待估参数至少需要 %d 条有效且盘面受光的观测"
                "（当前 %d 条）。请补充不同时刻、不同日期的影端记录。"
                % (k, need, len(fit_obs))]
        if not fit_obs and (n_back or n_hor):
            errs.append("全部有效观测的太阳均位于盘面背侧或地平线下，无法反算。")
        return {"ok": False, "errors": errs,
                "obs": _public_obs(obs), "warnings": warnings}

    # ---- 目标函数（太阳向量与参数无关，预处理后可快速反复求值）
    def make_theta(x):
        t = {}
        i = 0
        for key in FIT_KEYS:
            if cfg[key]["on"]:
                t[key] = x[i]
                i += 1
            else:
                t[key] = cfg[key]["value"]
        return t

    def cost(x):
        theta = make_theta(x)
        frame = _frame(base, theta)
        total = 0.0
        for r in fit_obs:
            pred = _predict_uv(frame, theta, r["sun"])
            if pred is None:
                total += 1e8  # 拟合几何下该观测不受光：重罚，引导离开此区域
                continue
            du_ = r["u"] - pred[0]
            dv_ = r["v"] - pred[1]
            total += du_ * du_ + dv_ * dv_
        return total

    lo = [cfg[key]["min"] for key in free]
    hi = [cfg[key]["max"] for key in free]
    x0 = [min(max(cfg[key]["value"], l), h) for key, l, h in zip(free, lo, hi)]
    x, _f = _coordinate_search(cost, x0, lo, hi)
    step = [(h - l) * 0.05 + 1e-9 for l, h in zip(lo, hi)]
    x, _f = _nelder_mead(cost, x, lo, hi, step)
    best = make_theta(x)

    # ---- 残差统计（全部观测在最佳参数下的预测；仅有效受光样本计入统计）
    sum2 = 0.0
    max_res = 0.0
    best_frame = _frame(base, best)
    for r in obs:
        pred = _predict_uv(best_frame, best, r["sun"]) if r["sun"] else None
        r["pred"] = pred
        r["res"] = None
        r["res_len"] = None
        if pred is not None and r["u"] is not None and r["v"] is not None:
            ru = r["u"] - pred[0]
            rv = r["v"] - pred[1]
            r["res"] = (ru, rv)
            r["res_len"] = math.hypot(ru, rv)
    for r in fit_obs:
        if r["res_len"] is not None:
            sum2 += r["res_len"] ** 2
            max_res = max(max_res, r["res_len"])
    m = len(fit_obs)
    rms = math.sqrt(sum2 / m) if m else 0.0

    # ---- 可辨识性：数值雅可比（对拟合样本的残差向量）
    def resid_vec(theta):
        frame = _frame(base, theta)
        out = []
        for r in fit_obs:
            pred = _predict_uv(frame, theta, r["sun"])
            if pred is None:
                out.extend((0.0, 0.0))
            else:
                out.extend((r["u"] - pred[0], r["v"] - pred[1]))
        return out

    n_rows = 2 * m
    J = []
    for i, key in enumerate(free):
        h = max((hi[i] - lo[i]) * 1e-3, 1e-5)
        bp = min(best[key] + h, hi[i])
        bm = max(best[key] - h, lo[i])
        if bp - bm < 1e-12:
            J.append([0.0] * n_rows)
            continue
        tp = dict(best); tp[key] = bp
        tm = dict(best); tm[key] = bm
        rp = resid_vec(tp)
        rm = resid_vec(tm)
        J.append([(a - b) / (bp - bm) for a, b in zip(rp, rm)])

    sens = []
    for col in J:
        sens.append(math.sqrt(sum(c * c for c in col) / max(1, n_rows)))

    pairs = []
    for i in range(k):
        for j in range(i + 1, k):
            ni = math.sqrt(sum(c * c for c in J[i]))
            nj = math.sqrt(sum(c * c for c in J[j]))
            if ni * nj > 1e-12:
                c = sum(a * b for a, b in zip(J[i], J[j])) / (ni * nj)
                pairs.append({"a": free[i], "b": free[j],
                              "label": "%s ~ %s" % (LABELS[free[i]], LABELS[free[j]]),
                              "corr": round(c, 4)})
    pairs.sort(key=lambda p: -abs(p["corr"]))

    # 协方差 ≈ σ²(JᵀJ)⁻¹，σ² 用残差均方估计
    dof = n_rows - k
    sigma2 = sum2 / max(1, dof)
    A = [[sum(J[i][r] * J[j][r] for r in range(n_rows))
          for j in range(k)] for i in range(k)]
    inv = _invert(A) if k else None
    std = []
    for i in range(k):
        if inv is not None and inv[i][i] > 0:
            std.append(math.sqrt(sigma2 * inv[i][i]))
        else:
            std.append(None)

    ident_params = []
    for i, key in enumerate(free):
        resolve = (rms / sens[i]) if sens[i] > 1e-12 else None
        half_span = 0.5 * (hi[i] - lo[i])
        weak = (resolve is None) or (resolve > half_span)
        ident_params.append({
            "key": key, "label": LABELS[key], "unit": UNITS[key],
            "estimated": True,
            "sensitivity": round(sens[i], 5),
            "resolve": round(resolve, 4) if resolve is not None else None,
            "std": round(std[i], 4) if std[i] is not None else None,
            "weak": weak,
        })
    for key in FIT_KEYS:
        if not cfg[key]["on"]:
            ident_params.append({
                "key": key, "label": LABELS[key], "unit": UNITS[key],
                "estimated": False, "locked_value": cfg[key]["value"],
                "sensitivity": None, "resolve": None, "std": None, "weak": False,
            })

    for pr in pairs:
        if abs(pr["corr"]) >= EQUIV_CORR:
            warnings.append({
                "code": "equivalent",
                "msg": "近似等价解：%s 与 %s 对影端位置的影响近似共线（相关 %.3f），"
                       "存在多组参数组合给出几乎相同的拟合结果；建议锁定其中之一，"
                       "或补充不同时刻、不同日期（赤纬）的观测后再重算。"
                       % (LABELS[pr["a"]], LABELS[pr["b"]], pr["corr"])})
    for ip in ident_params:
        if ip["estimated"] and ip["weak"]:
            warnings.append({
                "code": "weak",
                "msg": "参数「%s」在当前搜索范围内弱辨识：全程引起的影端变化不足 "
                       "RMS 残差量级，结果不可信；建议扩大观测的时间/日期跨度，"
                       "或将其锁定。" % ip["label"]})
    if inv is None and k > 0:
        warnings.append({
            "code": "singular",
            "msg": "雅可比矩阵接近奇异，参数标准差无法估计；通常意味着样本分布"
                   "过于集中或存在等价参数，请增加观测多样性或锁定部分参数。"})
    if m < k + 2:
        warnings.append({
            "code": "few_samples",
            "msg": "有效样本数（%d）相对待估参数数（%d）偏少，拟合结果可能不稳定，"
                   "建议至少 %d 条。" % (m, k, k + 2)})
    if rms > 20.0:
        warnings.append({
            "code": "large_rms",
            "msg": "RMS 残差 %.1f mm 偏大：请检查观测记录、原版本地点参数，"
                   "或适当扩大搜索范围。" % rms})

    corrections = {key: round(best[key] - base_value(base, key), 4)
                   for key in FIT_KEYS}
    return {
        "ok": True,
        "config": cfg,
        "base": {key: base_value(base, key) for key in FIT_KEYS},
        "best": {key: round(best[key], 4) for key in FIT_KEYS},
        "corrections": corrections,
        "rms": round(rms, 3),
        "max_res": round(max_res, 3),
        "n_obs": len(obs),
        "n_fit": m,
        "n_params": k,
        "dof": dof,
        "obs": _public_obs(obs),
        "ident": {"params": ident_params, "pairs": pairs},
        "warnings": warnings,
        "fitted_at": time.time(),
    }


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _public_obs(obs):
    """剔除内部字段（太阳向量），输出可 JSON 序列化的逐条结果。"""
    out = []
    for r in obs:
        out.append({
            "id": r["id"],
            "dt_local": r["dt_local"],
            "utc": r["utc"],
            "u": r["u"],
            "v": r["v"],
            "valid": r["valid"],
            "state": r["state"],
            "pred": ([round(r["pred"][0], 3), round(r["pred"][1], 3)]
                     if r.get("pred") else None),
            "res": ([round(r["res"][0], 3), round(r["res"][1], 3)]
                    if r.get("res") else None),
            "res_len": (round(r["res_len"], 3)
                        if r.get("res_len") is not None else None),
        })
    return out


# ---------------------------------------------------------------- 校准报告

def build_report(batch, design, result):
    """汇总 JSON 校准报告：样本、搜索范围、残差与安装修正量。"""
    return {
        "report": "sundial-calibration-report",
        "version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "batch": {
            "id": batch["id"],
            "name": batch["name"],
            "created_at": _fmt_ts(batch["created_at"]),
            "status": batch["status"],
            "applied_design_id": batch.get("applied_design_id"),
        },
        "source_design": {
            "id": design["id"],
            "name": design["name"],
            "params": design["params"],
        },
        "fit_config": result["config"],
        "samples": result["obs"],
        "statistics": {
            "n_obs": result["n_obs"],
            "n_fit": result["n_fit"],
            "n_params": result["n_params"],
            "dof": result["dof"],
            "rms_mm": result["rms"],
            "max_res_mm": result["max_res"],
        },
        "base_params": result["base"],
        "best_params": result["best"],
        "corrections": result["corrections"],
        "identifiability": result["ident"],
        "warnings": result["warnings"],
    }


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""

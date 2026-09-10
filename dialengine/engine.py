# -*- coding: utf-8 -*-
"""日晷盘面几何引擎。

约定
----
* 全局坐标 ENU：x=东，y=北，z=天顶，单位向量。
* 盘面方位角 az：晷面法向水平投影的方向，自正南向西为正（东 -90、南 0、西 +90、北 ±180，度）。
* 盘面倾角 inc：法向与天顶的夹角（水平 0、垂直 90、仰置 90..180，度）。
* 盘面二维坐标 (u, v)：u 指向“盘面右方”，v 指向“盘面上方”，均为毫米，原点为盘面中心
  （也是晷针极轴边与盘面的交点）。SVG 中 x=u, y=-v。
* 时角 H：正午为 0，下午为正（度）。真太阳时 = 12 + H/15。
* 晷针为沿天轴方向、长度 L 的直边（极轴式晷针），自动选择向阳一侧。
"""
import math
from datetime import datetime, timedelta

from . import solar

DEG = math.pi / 180.0
EPS = 1e-9
OBLIQ = 23.4397 * DEG

# 同 |赤纬| 的节气配对（黄经, 名称）；顺序为自春分起
JIEQI_PAIRS = [
    ([0, 180], ("春分", "秋分")),
    ([15, 165], ("清明", "白露")),
    ([30, 150], ("谷雨", "处暑")),
    ([45, 135], ("立夏", "立秋")),
    ([60, 120], ("大暑", "小满")),
    ([75, 105], ("芒种", "小暑")),
    ([90], ("夏至",)),
    ([270], ("冬至",)),
    ([285, 255], ("小寒", "大雪")),
    ([300, 240], ("大寒", "小雪")),
    ([315, 225], ("立春", "立冬")),
    ([330, 210], ("雨水", "霜降")),
    ([345, 195], ("惊蛰", "寒露")),
]


# ---------------------------------------------------------------- 基础向量

def _norm(v):
    x, y, z = v
    m = math.sqrt(x * x + y * y + z * z)
    return (x / m, y / m, z / m)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def face_frame(lat, az_deg, inc_deg):
    """返回 (n, u, v, p, p_used, sigma)。"""
    az, inc = az_deg * DEG, inc_deg * DEG
    n = (-math.sin(inc) * math.sin(az),
         -math.sin(inc) * math.cos(az),
         math.cos(inc))
    up0 = (0.0, 0.0, 1.0)
    if abs(n[2]) > 0.999:
        ref = (0.0, 1.0, 0.0)  # 水平盘以真北为盘面上方
    else:
        ref = up0
    d = _dot(ref, n)
    v = _norm((ref[0] - d * n[0], ref[1] - d * n[1], ref[2] - d * n[2]))
    u = _cross(v, n)
    latr = lat * DEG
    p = (0.0, math.cos(latr), math.sin(latr))  # 北天极
    # 晷针直边伸向盘面背光侧（尖端在板面上方），阴影才能投在向阳面上
    sigma = 1.0 if _dot(p, n) >= 0 else -1.0
    p_used = (sigma * p[0], sigma * p[1], sigma * p[2])
    return n, u, v, p, p_used, sigma


def _sun_enu(H, decl, latr):
    sh, ch = math.sin(H), math.cos(H)
    sd, cd = math.sin(decl), math.cos(decl)
    sl, cl = math.sin(latr), math.cos(latr)
    e = -cd * sh
    n_ = cl * sd - sl * cd * ch
    up = sl * sd + cl * cd * ch
    return _norm((e, n_, up))


def shadow_tip(L, p_used, n, s):
    """晷针尖端 B=L*p_used 被太阳方向 s 投射到盘面上的影点（相对盘心，三维）。"""
    sdn = _dot(s, n)
    if sdn <= EPS:
        return None
    k = L * _dot(p_used, n) / sdn
    return (L * p_used[0] - k * s[0],
            L * p_used[1] - k * s[1],
            L * p_used[2] - k * s[2])


def to_uv(point, u, v):
    return (_dot(point, u), _dot(point, v))


# ---------------------------------------------------------------- 矩形工具

def rect_bounds(W, H):
    return (-W / 2.0, W / 2.0, -H / 2.0, H / 2.0)


def in_rect(pu, pv, b, margin=0.0):
    return (b[0] - margin <= pu <= b[1] + margin and
            b[2] - margin <= pv <= b[3] + margin)


def ray_rect_edge(theta, b):
    """从原点出发、方向角 theta（自 +v 向 +u）的射线与矩形边界的交点。"""
    du, dv = math.sin(theta), math.cos(theta)
    t = float("inf")
    if abs(du) > EPS:
        t = min(t, (b[1] if du > 0 else b[0]) / du)
    if abs(dv) > EPS:
        t = min(t, (b[3] if dv > 0 else b[2]) / dv)
    return (du * t, dv * t)


def _point_seg_dist(px, py, a, c):
    ax, ay = a
    cx, cy = c
    dx, dy = cx - ax, cy - ay
    L2 = dx * dx + dy * dy
    if L2 < EPS:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def seg_seg_min_dist(a, c, d, e, step_hint):
    """线段 a-c 与 d-e 的最小距离：沿 a-c 密采样求点段距离。"""
    length = math.hypot(c[0] - a[0], c[1] - a[1])
    n = max(2, int(length / max(step_hint, 1e-6)) + 1)
    best = float("inf")
    best_t = 0.0
    for i in range(n + 1):
        t = i / n
        px = a[0] + (c[0] - a[0]) * t
        py = a[1] + (c[1] - a[1]) * t
        dd = _point_seg_dist(px, py, d, e)
        if dd < best:
            best, best_t = dd, t
    mid = (a[0] + (c[0] - a[0]) * best_t,
           a[1] + (c[1] - a[1]) * best_t)
    return best, mid


# ---------------------------------------------------------------- 太阳时

def longitude_correction_min(lng, tz):
    """经度修正（分钟）：真太阳时 - 区时中的经度项 = 4*(经度 - 时区中央经线)。"""
    return 4.0 * (lng - 15.0 * tz)


def hour_angle_for_clock(date_utc, clock_hours, lng, tz, dst):
    eot = solar.equation_of_time(date_utc)
    # 真太阳时 = 区钟 + 经度修正 - 夏令时 + EOT
    tst = clock_hours + longitude_correction_min(lng, tz) / 60.0 - dst + eot / 60.0
    return (tst - 12.0) * 15.0, eot, tst


# ---------------------------------------------------------------- 主计算

def _fast_declination(day_of_year):
    return -OBLIQ * math.cos(2 * math.pi * (day_of_year + 10) / 365.0)


def compute_dial(p, year=datetime.now().year):
    """完整计算盘面几何。p 为参数字典，返回可 JSON 序列化的数据结构。"""
    lat = float(p["lat"])
    lng = float(p["lng"])
    tz = float(p["tz"])
    dst = float(p.get("dst", 0.0))
    az = float(p["az"])
    inc = float(p["inc"])
    W = float(p["width"])
    H = float(p["height"])
    L = float(p["style_len"])
    min_gap = float(p.get("min_spacing", 2.0))
    step_min = int(p.get("hour_step", 60))
    latr = lat * DEG

    n, u, v, pole, p_used, sigma = face_frame(lat, az, inc)
    b = rect_bounds(W, H)

    warnings_out = []

    # 晷针几何
    pn = _dot(pole, n)
    style_angle = math.degrees(math.asin(min(1.0, abs(pn))))  # 晷针与盘面夹角
    dw = _dot(p_used, n)
    wvec = _norm((p_used[0] - dw * n[0],
                  p_used[1] - dw * n[1],
                  p_used[2] - dw * n[2]))  # 盘面投影 = 副法线方向
    substyle_ang = math.degrees(math.atan2(_dot(wvec, u), _dot(wvec, v)))
    polar_elev = math.degrees(math.asin(max(-1, min(1, p_used[2]))))
    # 方位角同样为南起西正：南 0、西 +90、北 ±180、东 -90。
    # 西向分量 = -E，南向分量 = -N，故 atan2(-E, -N)。
    polar_az_s = math.degrees(math.atan2(-p_used[0], -p_used[1]))
    tip3 = (L * p_used[0], L * p_used[1], L * p_used[2])
    tip_uv = to_uv(tip3, u, v)

    polar_degenerate = style_angle < 2.0
    if polar_degenerate:
        warnings_out.append({
            "code": "polar_degenerate", "sev": "error",
            "msg": "盘面与天轴几乎平行（极式/赤道式临界状态），时线将趋于平行且影长发散，"
                   "无法按极轴晷针正常刻制，请改变倾角约 2° 以上。",
        })

    # ------------------------------------------------------------ 时线
    test_decl = (-OBLIQ, 0.0, OBLIQ)

    def ray_direction(H):
        """返回该时角在盘面上的单位 (u,v) 方向；任何赤纬下都不受光则 None。"""
        theta = None
        for d in test_decl:
            s = _sun_enu(H, d, latr)
            if _dot(s, n) <= 1e-4 or s[2] <= 1e-4:
                continue
            x = shadow_tip(1.0, p_used, n, s)
            if x is None:
                continue
            pu, pv = to_uv(x, u, v)
            t = math.atan2(pu, pv)
            if theta is None:
                theta = t
            else:
                # 时线与赤纬无关；数值偏差超过 0.2° 说明公式有误
                d_ang = abs((t - theta + math.pi) % (2 * math.pi) - math.pi)
                if d_ang > 0.2 * DEG:
                    theta = t
        return theta

    rays = []
    step_h = step_min / 60.0
    t = 4.0
    while t <= 20.0 + 1e-9:
        H = (t - 12.0) * 15.0 * DEG
        minutes = round((t - int(t)) * 60 + 1e-6)
        major = minutes == 0
        theta = ray_direction(H)
        if theta is None:
            t += step_h
            continue
        edge = ray_rect_edge(theta, b)
        rays.append({
            "label": ("%d" % int(round(t))) if major else "",
            "time_h": round(t, 3),
            "major": major,
            "angle_deg": round(math.degrees(theta), 3),
            "inner_uv": None,
            "edge_uv": [round(edge[0], 2), round(edge[1], 2)],
        })
        t += step_h

    # ------------------------------------------------------- 全年采样
    # 内半径（每根时线一年中最小影长，即分日线处）与包络多边形
    inner_r = [float("inf")] * len(rays)
    inner_p = [None] * len(rays)
    NB = 360
    env_r = [0.0] * NB
    day = 0
    while day < 365:
        d = _fast_declination(day + 1)
        H = -120 * DEG
        while H <= 120 * DEG + 1e-9:
            s = _sun_enu(H, d, latr)
            if s[2] > 1e-4 and _dot(s, n) > 1e-4:
                x = shadow_tip(L, p_used, n, s)
                pu, pv = to_uv(x, u, v)
                r = math.hypot(pu, pv)
                ang = (math.atan2(pu, pv) % (2 * math.pi))
                k = int((ang / (2 * math.pi) * NB)) % NB
                if r > env_r[k]:
                    env_r[k] = r
                # 归入最近时线
                tst_h = 12 + H / DEG / 15.0
                idx = int(round((tst_h - 4.0) / step_h))
                if 0 <= idx < len(rays) and r < inner_r[idx]:
                    # 必须确实接近该时角（采样网格）
                    Htarget = (4.0 + idx * step_h - 12.0) * 15.0 * DEG
                    if abs(H - Htarget) <= (step_h * 15.0 * DEG) / 2 + 1e-6:
                        inner_r[idx] = r
                        inner_p[idx] = (pu, pv)
            H += 1.25 * DEG  # 5 分钟
        day += 2

    # 包络多边形
    envelope = []
    for k in range(NB):
        if env_r[k] > 0:
            ang = (k + 0.5) / NB * 2 * math.pi
            envelope.append([round(env_r[k] * math.sin(ang), 2),
                             round(env_r[k] * math.cos(ang), 2)])

    for i, ray in enumerate(rays):
        if inner_p[i] is not None:
            ray["inner_uv"] = [round(inner_p[i][0], 2), round(inner_p[i][1], 2)]
            if not in_rect(inner_p[i][0], inner_p[i][1], b):
                warnings_out.append({
                    "code": "hour_out", "sev": "warn",
                    "msg": "%s 时线在最近盘面处（分日影长 %.0f mm）已超出盘面边界，"
                           "该时刻刻线在当前尺寸内无法完整落针。"
                           % (ray["label"] or ("%02d:%02d" % (int(ray["time_h"]),
                              int(round((ray["time_h"] % 1) * 60)))), inner_r[i]),
                    "x": round(inner_p[i][0], 1), "y": round(inner_p[i][1], 1),
                })
        else:
            # 全年无采样：理论受光但太阳高度总不足（极少见），退化用小内半径
            theta = ray["angle_deg"] * DEG
            ray["inner_uv"] = [round(2.0 * math.sin(theta), 2),
                               round(2.0 * math.cos(theta), 2)]

    # ------------------------------------------------------- 相邻刻线间距
    if len(rays) >= 2:
        for i in range(len(rays) - 1):
            a, c = rays[i], rays[i + 1]
            if not a["major"] or not c["major"]:
                continue  # 仅校核整时刻度
            theta1, theta2 = a["angle_deg"] * DEG, c["angle_deg"] * DEG
            gap_ang = abs((theta2 - theta1 + math.pi) % (2 * math.pi) - math.pi)
            if gap_ang > 60 * DEG:
                continue
            p1 = a["inner_uv"]
            if p1 is None:
                continue
            # 自内半径起刻到盘边
            d1 = ray_rect_edge(theta1, b)
            d2 = ray_rect_edge(theta2, b)
            dist, mid = seg_seg_min_dist(tuple(p1), tuple(d1),
                                         tuple(c["inner_uv"]), tuple(d2),
                                         max(min_gap * 0.5, 0.5))
            if dist < min_gap:
                t1 = "%g" % a["time_h"]
                t2 = "%g" % c["time_h"]
                warnings_out.append({
                    "code": "spacing", "sev": "warn",
                    "msg": "%s 与 %s 时线在有效刻段内最小间距约 %.2f mm，"
                           "小于设定加工间距 %.1f mm，建议加大盘面或改用半日刻度。"
                           % (_fmt_hour(a["time_h"]), _fmt_hour(c["time_h"]),
                              dist, min_gap),
                    "x": round(mid[0], 1), "y": round(mid[1], 1),
                })

    # ------------------------------------------------------- 节气日期线
    terms = solar.jieqi_dates(year)
    date_by_lon = {}
    for name, dtutc in terms:
        local = dtutc + timedelta(hours=tz)
        date_by_lon[name] = "%d/%d" % (local.month, local.day)
    name_by_lon = {lon: nm for lon, nm in solar.JIEQI}

    date_curves = []
    for lons, pair_names in JIEQI_PAIRS:
        lam = lons[0] * DEG
        d = math.asin(math.sin(OBLIQ) * math.sin(lam))
        pts = []
        H = -120 * DEG
        while H <= 120 * DEG + 1e-9:
            s = _sun_enu(H, d, latr)
            if s[2] > 1e-4 and _dot(s, n) > 1e-4:
                x = shadow_tip(L, p_used, n, s)
                pu, pv = to_uv(x, u, v)
                pts.append([round(pu, 2), round(pv, 2),
                            round(math.degrees(math.asin(max(-1, min(1, s[2])))), 2)])
            H += 1.25 * DEG
        if len(pts) < 2:
            continue
        # 只在太阳具备实用高度（>=8°）后判断越界，避免日出影长发散造成的误报
        usable = [q for q in pts if q[2] >= 8.0]
        if usable:
            ep0, ep1 = usable[0], usable[-1]
            out_bounds = (not in_rect(ep0[0], ep0[1], b, 0.5)
                          or not in_rect(ep1[0], ep1[1], b, 0.5))
            bad_ep = ep0 if not in_rect(ep0[0], ep0[1], b, 0.5) else ep1
        else:
            out_bounds, bad_ep = False, pts[0]
        labels = []
        for j, nm in enumerate(pair_names):
            lon_val = lons[j]
            labels.append({
                "name": nm,
                "date": date_by_lon.get(nm, ""),
                "uv": pts[0][:2] if j == 0 else pts[-1][:2],
            })
        title = " · ".join("%s %s" % (lb["name"], lb["date"]) for lb in labels)
        date_curves.append({
            "name": title,
            "decl_deg": round(math.degrees(d), 2),
            "solstice": len(lons) == 1,
            "points": [[q[0], q[1]] for q in pts],
            "labels": labels,
            "out_bounds": out_bounds,
        })
        if out_bounds:
            ep = bad_ep
            warnings_out.append({
                "code": "date_out", "sev": "warn",
                "msg": "节气线「%s」在太阳高度 8° 时影端仍越出盘面（%.0f, %.0f mm），"
                       "该日期的有效时刻刻线超出裁切边界。"
                       % (title, ep[0], ep[1]),
                "x": round(ep[0], 1), "y": round(ep[1], 1),
            })

    # ------------------------------------------------------- 均时差年表
    eot_pts = []
    eot_min_v, eot_max_v = float("inf"), -float("inf")
    eot_min_d = eot_max_d = ""
    for doy in range(1, 366, 10):
        dt0 = datetime(year, 1, 1) + timedelta(days=doy - 1)
        e = solar.equation_of_time(dt0)
        eot_pts.append({"date": "%d/%d" % (dt0.month, dt0.day), "min": round(e, 2)})
        if e < eot_min_v:
            eot_min_v, eot_min_d = e, "%d/%d" % (dt0.month, dt0.day)
        if e > eot_max_v:
            eot_max_v, eot_max_d = e, "%d/%d" % (dt0.month, dt0.day)

    return {
        "meta": {
            "lat": lat, "lng": lng, "tz": tz, "dst": dst,
            "az": az, "inc": inc, "width": W, "height": H,
            "style_len": L, "min_spacing": min_gap, "hour_step": step_min,
            "year": year,
        },
        "plate": {"width": W, "height": H,
                  "bounds": {"umin": b[0], "umax": b[1], "vmin": b[2], "vmax": b[3]}},
        "gnomon": {
            "length": L,
            "style_angle_deg": round(style_angle, 3),
            "substyle_angle_deg": round(substyle_ang, 3),
            "polar_elev_deg": round(polar_elev, 3),
            "polar_az_south_deg": round(polar_az_s, 3),
            "tip_uv": [round(tip_uv[0], 2), round(tip_uv[1], 2)],
            "sigma": sigma,
        },
        "rays": rays,
        "envelope": envelope,
        "date_curves": date_curves,
        "warnings": warnings_out,
        "eot": {
            "curve": eot_pts,
            "min": {"date": eot_min_d, "value": round(eot_min_v, 2)},
            "max": {"date": eot_max_d, "value": round(eot_max_v, 2)},
            "longitude_correction_min": round(longitude_correction_min(lng, tz), 2),
        },
        "terms": [{"name": nm,
                   "date": date_by_lon.get(nm, ""),
                   "lon": lon} for lon, nm in solar.JIEQI],
    }


def _fmt_hour(t):
    hh = int(t)
    mm = int(round((t - hh) * 60))
    return "%02d:%02d" % (hh, mm)


def _ang_diff_deg(a, b):
    """两方向角之差（度），归一化到 [-180, 180]。"""
    d = (a - b + 180.0) % 360.0 - 180.0
    return d


def geometry_diff(data_a, data_b):
    """比较两次 compute_dial 结果的实际刻线几何。

    返回：
      gnomon  — 晷针安装几何对照（极边仰角/方位/与盘夹角/副法线角）
      rays    — 按真太阳时配对的时线：盘面方向角、盘边端点、分日内端点
      dates   — 按赤纬配对的节气日期线：端点与最大形偏
    """
    ga, gb = data_a["gnomon"], data_b["gnomon"]
    gnomon = []
    g_fields = [
        ("style_angle_deg", "晷针与盘面夹角 (°)", 0.01),
        ("substyle_angle_deg", "副法线角 (°)", 0.01),
        ("polar_elev_deg", "极边仰角 (°)", 0.01),
        ("polar_az_south_deg", "极边方位·南起西正 (°)", 0.01),
        ("length", "晷针长度 (mm)", 0.01),
    ]
    for key, label, tol in g_fields:
        va, vb = ga.get(key), gb.get(key)
        gnomon.append({
            "label": label, "a": round(va, 3) if va is not None else None,
            "b": round(vb, 3) if vb is not None else None,
            "delta": round(vb - va, 3),
            "changed": abs(vb - va) > tol,
        })

    # 时线按 time_h 配对
    ra = {round(r["time_h"], 3): r for r in data_a["rays"]}
    rb = {round(r["time_h"], 3): r for r in data_b["rays"]}
    times = sorted(set(ra) | set(rb))
    rays = []
    max_ang = 0.0
    max_end = 0.0
    for t in times:
        x, y = ra.get(t), rb.get(t)
        row = {
            "label": _fmt_hour(t), "major": bool(x["major"] if x else y["major"]),
            "a": None, "b": None, "d_angle": None, "d_edge": None,
            "d_inner": None, "present_a": x is not None, "present_b": y is not None,
            "changed": False,
        }
        if x:
            row["a"] = {"angle": x["angle_deg"],
                        "edge": x["edge_uv"],
                        "inner": x.get("inner_uv")}
        if y:
            row["b"] = {"angle": y["angle_deg"],
                        "edge": y["edge_uv"],
                        "inner": y.get("inner_uv")}
        if x and y:
            da = _ang_diff_deg(y["angle_deg"], x["angle_deg"])
            de = math.hypot(y["edge_uv"][0] - x["edge_uv"][0],
                            y["edge_uv"][1] - x["edge_uv"][1])
            di = None
            if x.get("inner_uv") and y.get("inner_uv"):
                di = math.hypot(y["inner_uv"][0] - x["inner_uv"][0],
                                y["inner_uv"][1] - x["inner_uv"][1])
            row.update({"d_angle": round(da, 3), "d_edge": round(de, 2),
                        "d_inner": round(di, 2) if di is not None else None,
                        "changed": abs(da) > 0.01 or de > 0.5})
            max_ang = max(max_ang, abs(da))
            max_end = max(max_end, de)
        rays.append(row)

    # 节气线按赤纬配对（同名配对）。
    # 原始曲线包含日出/日落附近影长发散的点，因此只在两盘“共同边界”内重采样比较，
    # 使差异反映刻线本身（方位/倾角/晷针长），而非盘面裁切大小。
    def curve_index(data):
        out = {}
        for c in data["date_curves"]:
            name = c["name"].split(" ")[0]
            out[(round(c["decl_deg"], 2), name)] = c
        return out

    def common_bounds(d1, d2):
        b1, b2 = d1["plate"]["bounds"], d2["plate"]["bounds"]
        return (max(b1["umin"], b2["umin"]),
                min(b1["umax"], b2["umax"]),
                max(b1["vmin"], b2["vmin"]),
                min(b1["vmax"], b2["vmax"]))

    cb = common_bounds(data_a, data_b)

    def clip_curve(points, b):
        """折线对矩形 b 的裁剪（逐点保留在矩形内的连续段），返回最长段。"""
        segs, cur = [], []
        for p in points:
            if b[0] <= p[0] <= b[1] and b[2] <= p[1] <= b[3]:
                cur.append(p)
            else:
                if len(cur) > 1:
                    segs.append(cur)
                cur = []
        if len(cur) > 1:
            segs.append(cur)
        if not segs:
            return []
        return max(segs, key=len)

    ca, cb_map = curve_index(data_a), curve_index(data_b)
    dates = []
    max_curve_dev = 0.0
    for key in sorted(set(ca) | set(cb_map)):
        x, y = ca.get(key), cb_map.get(key)
        pa = clip_curve(x["points"], cb) if x else []
        pb = clip_curve(y["points"], cb) if y else []
        row = {"label": (x or y)["name"], "a": None, "b": None,
               "d_start": None, "d_end": None, "d_max": None,
               "present_a": x is not None, "present_b": y is not None,
               "changed": False}
        if x:
            row["a"] = {"start": x["points"][0], "end": x["points"][-1],
                        "n": len(x["points"])}
        if y:
            row["b"] = {"start": y["points"][0], "end": y["points"][-1],
                        "n": len(y["points"])}
        if pa and pb:
            ds = math.hypot(pb[0][0] - pa[0][0], pb[0][1] - pa[0][1])
            de = math.hypot(pb[-1][0] - pa[-1][0], pb[-1][1] - pa[-1][1])
            dmax = 0.0
            n = min(len(pa), len(pb))
            for i in range(n):
                dmax = max(dmax, math.hypot(
                    pb[i][0] - pa[i][0], pb[i][1] - pa[i][1]))
            row.update({"d_start": round(ds, 2), "d_end": round(de, 2),
                        "d_max": round(dmax, 2),
                        "changed": dmax > 0.5})
            max_curve_dev = max(max_curve_dev, dmax)
        dates.append(row)

    return {
        "gnomon": gnomon,
        "rays": rays,
        "dates": dates,
        "common_bounds": {"umin": round(cb[0], 1), "umax": round(cb[1], 1),
                          "vmin": round(cb[2], 1), "vmax": round(cb[3], 1)},
        "summary": {
            "max_angle_diff_deg": round(max_ang, 3),
            "max_edge_diff_mm": round(max_end, 2),
            "max_curve_diff_mm": round(max_curve_dev, 2),
            "ray_count_a": len(data_a["rays"]),
            "ray_count_b": len(data_b["rays"]),
        },
    }


# ---------------------------------------------------------------- 瞬时预览

def preview_shadow(p, date_y, date_m, date_d, time_hours, mode):
    """计算指定日期与时间的影端位置。

    mode='solar'：time_hours 为真太阳时；mode='civil'：为民用钟面时（含夏令时）。
    返回 dict（含 civil rays 所需的当日赤纬与 EOT）。
    """
    lat = float(p["lat"]); lng = float(p["lng"])
    tz = float(p["tz"]); dst = float(p.get("dst", 0.0))
    az = float(p["az"]); inc = float(p["inc"])
    L = float(p["style_len"])
    latr = lat * DEG
    n, u, v, pole, p_used, sigma = face_frame(lat, az, inc)
    offset = tz + dst

    date0 = datetime(date_y, date_m, date_d)
    if mode == "civil":
        utc = date0 + timedelta(hours=time_hours - offset)
        eot = solar.equation_of_time(utc)
        tst = time_hours + longitude_correction_min(lng, tz) / 60.0 - dst + eot / 60.0
    else:
        # 迭代求 UTC（EOT 随时刻缓慢变化）
        utc = date0 + timedelta(hours=time_hours - lng / 15.0)
        for _ in range(3):
            eot = solar.equation_of_time(utc)
            utc = date0 + timedelta(hours=time_hours - lng / 15.0 - eot / 60.0)
        eot = solar.equation_of_time(utc)
        tst = time_hours
    civil_clock = (utc + timedelta(hours=offset))
    civil_h = civil_clock.hour + civil_clock.minute / 60.0 + civil_clock.second / 3600.0

    e, nn, uu, alt, az_sun = solar.sun_vector_enu(utc, lat, lng)
    s = (e, nn, uu)
    H = (tst - 12.0) * 15.0 * DEG
    decl = solar.declination(utc)

    result = {
        "eot_min": round(eot, 2),
        "longitude_correction_min": round(longitude_correction_min(lng, tz), 2),
        "dst_hours": dst,
        "tst": _fmt_hour(tst % 24),
        "civil": civil_clock.strftime("%H:%M"),
        "civil_date": civil_clock.strftime("%Y-%m-%d"),
        "alt_deg": round(math.degrees(alt), 2),
        "az_south_deg": round(math.degrees(az_sun), 2),
        "hour_angle_deg": round(math.degrees(H), 2),
        "decl_deg": round(math.degrees(decl), 2),
        "state": "ok",
        "shadow_uv": None,
        "in_bounds": False,
        "civil_rays": [],
    }

    W = float(p["width"]); Hgt = float(p["height"])
    b = rect_bounds(W, Hgt)

    if s[2] <= 1e-4:
        result["state"] = "below_horizon"
    elif _dot(s, n) <= 1e-4:
        result["state"] = "backside"

    if result["state"] == "ok":
        x = shadow_tip(L, p_used, n, s)
        pu, pv = to_uv(x, u, v)
        result["shadow_uv"] = [round(pu, 2), round(pv, 2)]
        result["in_bounds"] = in_rect(pu, pv, b)

    # 当日民用时刻线（与当前所选日期一致）
    step_min = int(p.get("hour_step", 60))
    step_h = step_min / 60.0
    clock = 0.0
    while clock < 24.0 - 1e-9:
        # 以当日 UTC 正午估 EOT，整日误差 < 0.1 分钟
        utc_noon = date0 + timedelta(hours=12 - offset)
        eot_d = solar.equation_of_time(utc_noon)
        tst_c = clock + longitude_correction_min(lng, tz) / 60.0 - dst + eot_d / 60.0
        Hc = (tst_c - 12) * 15 * DEG
        theta = None
        for dtest in (decl,):
            sc = _sun_enu(Hc, decl, latr)
            if sc[2] > 1e-4 and _dot(sc, n) > 1e-4:
                xc = shadow_tip(1.0, p_used, n, sc)
                cu, cv = to_uv(xc, u, v)
                theta = math.atan2(cu, cv)
        if theta is not None:
            edge = ray_rect_edge(theta, b)
            result["civil_rays"].append({
                "label": "%02d:%02d" % (int(clock), int(round((clock - int(clock)) * 60))),
                "major": int(round((clock - int(clock)) * 60)) == 0,
                "angle_deg": round(math.degrees(theta), 3),
                "edge_uv": [round(edge[0], 2), round(edge[1], 2)],
            })
        clock += step_h

    return result

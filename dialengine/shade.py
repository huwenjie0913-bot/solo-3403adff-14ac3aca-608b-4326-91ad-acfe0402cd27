# -*- coding: utf-8 -*-
"""安装点遮光与可读时段分析。

复用 solar 的太阳位置算法与 engine 的盘面投影模型，对某个设计版本下的
安装点（挂载若干方位角—高度角遮挡轮廓）做全年可读窗口分析。

判定状态（民用钟面时采样）按优先级：
  below   太阳在地平线下（alt <= 0）
  back    太阳位于盘面背侧（s·n <= 0）
  block   太阳被某条遮挡轮廓挡住
  out     影端越出盘面矩形
  read    可读

全部计算在本机完成，纯 Python，无第三方依赖。
"""
import math
from datetime import datetime, timedelta

from . import engine, solar

DEG = math.pi / 180.0

STEP_MIN = 5        # 全年扫描步长（分钟）
TRACE_MIN = 10      # 影端轨迹/当日弧线输出步长（分钟）
ALT_EPS = 1e-4

STATE_BELOW = "below"
STATE_BACK = "back"
STATE_BLOCK = "block"
STATE_OUT = "out"
STATE_READ = "read"

STATE_LABELS = {
    STATE_BELOW: "地平线下",
    STATE_BACK: "盘面背侧",
    STATE_BLOCK: "轮廓遮挡",
    STATE_OUT: "影端越界",
    STATE_READ: "可读",
}

# 关键时段（民用钟面时，小时）
KEY_PERIODS = [
    ("morning", "上午 08–10", 8.0, 10.0),
    ("noon", "正午 11–13", 11.0, 13.0),
    ("afternoon", "下午 14–16", 14.0, 16.0),
]

VALID_CLOSED = ("ground", "closed", "open")
VALID_AZREF = ("south", "north")


# ---------------------------------------------------------------- 轮廓几何

def _unwrap_rel(points):
    """把折点方位角相对首点逐点累积展开，保证相邻边 |Δaz| <= 180。"""
    out = []
    prev = None
    for az, alt in points:
        if prev is None:
            a = az
        else:
            d = (az - prev + 180.0) % 360.0 - 180.0
            a = prev + d
        out.append((a, alt))
        prev = a
    return out


def _polygon_contains(poly_unwrapped, qaz, qalt):
    """已展开的多边形顶点对查询点 (方位, 高度) 做经典射线法。"""
    inside = False
    m = len(poly_unwrapped)
    for i in range(m):
        a1, y1 = poly_unwrapped[i]
        a2, y2 = poly_unwrapped[(i + 1) % m]
        if min(a1, a2) - 1e-9 <= qaz < max(a1, a2) - 1e-9 and (a1 != a2):
            ycross = y1 + (y2 - y1) * (qaz - a1) / (a2 - a1)
            if ycross > qalt:
                inside = not inside
    return inside


def _point_edge_state(points, closed, qaz, qalt):
    """查询太阳点 (南基准方位°, 高度°) 相对一条轮廓的关系。

    返回 (hit, nearest_index, nearest_dist_deg)。
      ground — 顶点按方位排序，折线下方（朝向地平线）为遮挡；跨越 ±180 的
               折边用三份拷贝处理。
      closed — 顶点构成闭合多边形，内部遮挡。
      open   — 开放折线段，以到最近边的角距离判定，阈值按该边水平跨度自适应。
    """
    if len(points) < 2:
        return False, None, None
    rel = _unwrap_rel(points)
    nearest_i, nearest_d = None, float("inf")

    if closed == "open":
        # 最近边 + 自适应容差（开放折线表示一道有厚度的遮挡带）
        m = len(rel)
        for i in range(m - 1):
            a1, y1 = rel[i]
            a2, y2 = rel[i + 1]
            span = abs(a2 - a1)
            dd = _point_seg_dist_deg(qaz, qalt, a1, y1, a2, y2)
            tol = max(1.0, min(6.0, span * 0.35))
            if dd < tol:
                return True, i, dd
            if dd < nearest_d:
                nearest_d, nearest_i = dd, i
        return False, nearest_i, nearest_d

    # ground / closed：三份方位拷贝，兼容跨越 ±180 的轮廓
    any_hit = False
    for k in (-1, 0, 1):
        shift = 360.0 * k
        verts = [(a + shift, y) for a, y in rel]
        if closed == "ground":
            azs = [a for a, _ in verts]
            amin, amax = min(azs), max(azs)
            if amin - 1e-9 <= qaz <= amax + 1e-9:
                poly = verts + [(amax, -90.0), (amin, -90.0)]
                if _polygon_contains(poly, qaz, qalt):
                    any_hit = True
            for i in range(len(verts) - 1):
                dd = _point_seg_dist_deg(qaz, qalt, verts[i][0], verts[i][1],
                                         verts[i + 1][0], verts[i + 1][1])
                if dd < nearest_d:
                    nearest_d, nearest_i = dd, i
        else:
            if _polygon_contains(verts, qaz, qalt):
                any_hit = True
            m = len(verts)
            for i in range(m):
                dd = _point_seg_dist_deg(qaz, qalt, verts[i][0], verts[i][1],
                                         verts[(i + 1) % m][0],
                                         verts[(i + 1) % m][1])
                if dd < nearest_d:
                    nearest_d, nearest_i = dd, i
    n = len(rel)
    nseg = n if closed == "closed" else n - 1
    if nearest_i is not None and nseg > 0:
        nearest_i %= nseg
    return any_hit, nearest_i, nearest_d


def _point_seg_dist_deg(px, py, ax, ay, cx, cy):
    """方位—高度平面内点到线段的欧氏角距离（度）。"""
    dx, dy = cx - ax, cy - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def normalize_profile(prof):
    """校验并规范化一条轮廓，返回 (clean, errors)。"""
    errors = []
    name = str(prof.get("name") or "轮廓")[:40]
    closed = prof.get("closed", "ground")
    if closed not in VALID_CLOSED:
        errors.append("轮廓「%s」的闭合方式无效" % name)
        closed = "ground"
    raw = prof.get("points") or []
    pts = []
    for q in raw:
        try:
            az = float(q[0]) % 360.0
            alt = max(0.0, min(90.0, float(q[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        pts.append([round(az, 3), round(alt, 3)])
    need = 3 if closed == "closed" else 2
    if len(pts) < need:
        errors.append("轮廓「%s」需要至少 %d 个样点（当前 %d）"
                      % (name, need, len(pts)))
    notes_in = prof.get("notes") or {}
    notes = {}
    if isinstance(notes_in, dict):
        for k, v in notes_in.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            v = str(v)[:120].strip()
            if v and 0 <= idx < len(pts):
                notes[str(idx)] = v
    clean = {"name": name, "closed": closed, "points": pts, "notes": notes}
    return clean, errors


# ---------------------------------------------------------------- 安装点分析

class Site:
    """一个安装点：设计参数 + 规范化后的轮廓集合（方位统一换算为南基准）。"""

    def __init__(self, params, mount):
        self.p = dict(params)
        self.lat = float(params["lat"])
        self.lng = float(params["lng"])
        self.tz = float(params["tz"])
        self.dst = float(params.get("dst", 0.0))
        self.az = float(params["az"])
        self.inc = float(params["inc"])
        self.L = float(params["style_len"])
        self.root_du = float(params.get("root_du", 0.0))
        self.root_dv = float(params.get("root_dv", 0.0))
        self.W = float(params["width"])
        self.H = float(params["height"])
        self.bounds = engine.rect_bounds(self.W, self.H)
        self.n, self.u, self.v, _pole, self.p_used, _sig = engine.face_frame(
            self.lat, self.az, self.inc)
        self.mount = mount or {}
        self.az_ref = (mount or {}).get("az_ref", "south")
        if self.az_ref not in VALID_AZREF:
            self.az_ref = "south"
        self.profiles = mount.get("profiles", []) if mount else []
        self._prof_geo = [self._precompute(prof) for prof in self.profiles]

    def _precompute(self, prof):
        """展开方位并缓存折边（含三份拷贝），热循环中零分配判定。"""
        south_pts = [(self._to_south_az(a), y) for a, y in prof["points"]]
        rel = _unwrap_rel(south_pts)
        closed = prof["closed"]
        copies = (-1, 0, 1) if closed != "open" else (0,)
        edge_groups = []   # 每份方位拷贝一组 (a1,y1,a2,y2, 原始段索引, 容差)
        polys = []
        flat_groups = []   # 与 polys 对应的预展开射线法边
        amin = min(a for a, _ in rel)
        amax = max(a for a, _ in rel)
        for k in copies:
            shift = 360.0 * k
            verts = [(a + shift, y) for a, y in rel]
            m = len(verts)
            nseg = m if closed == "closed" else m - 1
            grp = []
            for i in range(nseg):
                a1, y1 = verts[i]
                a2, y2 = verts[(i + 1) % m]
                idx = i % max(1, nseg)
                tol = -1.0
                if closed == "open":
                    tol = max(1.0, min(6.0, abs(a2 - a1) * 0.35))
                grp.append((a1, y1, a2, y2, idx, tol))
            edge_groups.append(grp)
            if closed == "ground":
                poly = verts + [(amax + shift, -90.0), (amin + shift, -90.0)]
            elif closed == "closed":
                poly = verts
            else:
                continue
            polys.append(poly)
            fe = []
            for i in range(len(poly)):
                a1, y1 = poly[i]
                a2, y2 = poly[(i + 1) % len(poly)]
                if abs(a2 - a1) < 1e-12:
                    continue
                lo, hi = (a1, a2) if a1 < a2 else (a2, a1)
                slope = (y2 - y1) / (a2 - a1)
                fe.append((lo, hi, slope, y1 - slope * a1))
            flat_groups.append(fe)
        return {"closed": closed, "edge_groups": edge_groups,
                "flat_groups": flat_groups, "copies": copies,
                "amin": amin, "amax": amax,
                "nseg": max(1, len(rel) if closed == "closed" else len(rel) - 1)}

    def _to_south_az(self, az):
        # 录入基准为北起顺时（北0/东90/南180/西270）时换算到南起西正
        return az - 180.0 if self.az_ref == "north" else az

    def sun_at_civil(self, date_local, clock_hours):
        """本地民用钟面时 -> (太阳向量, 高度角°, 南基准方位°, UTC)。

        热循环版本：按日缓存赤纬/GMST 正午基准，时刻只补时角旋转。
        """
        offset = self.tz + self.dst
        utc = date_local + timedelta(hours=clock_hours - offset)
        e, n_, up, alt, az_s, _d, _g = solar.sun_position_fast(
            utc, self.lat, self.lng)
        return (e, n_, up), math.degrees(alt), math.degrees(az_s), utc

    def classify(self, date_local, clock_hours):
        """返回 (state, detail)；detail 为 block 时给出轮廓与样点信息。"""
        s, alt, az_south, _utc = self.sun_at_civil(date_local, clock_hours)
        if s[2] <= ALT_EPS:
            return STATE_BELOW, None
        if engine._dot(s, self.n) <= ALT_EPS:
            return STATE_BACK, None
        for pi, geo in enumerate(self._prof_geo):
            hit, edge_i, dist = self._hit_geo(geo, az_south, alt)
            if hit:
                prof = self.profiles[pi]
                note = prof["notes"].get(str(edge_i), "") if edge_i is not None else ""
                detail = {
                    "profile_index": pi,
                    "profile_name": prof["name"],
                    "edge_index": edge_i,
                    "segment_label": "%s：段 %d–%d"
                        % (prof["name"], (edge_i or 0) + 1,
                           ((edge_i or 0) + 1) % geo["nseg"] + 1),
                    "note": note,
                    "dist_deg": round(dist, 2) if dist is not None else None,
                    "sun_az_south": round(az_south, 2),
                    "sun_alt": round(alt, 2),
                }
                return STATE_BLOCK, detail
        # 影端是否落在盘面内
        uv = self._shadow_uv_from_sun(s)
        if uv is None:
            return STATE_BACK, None
        if not engine.in_rect(uv[0], uv[1], self.bounds):
            return STATE_OUT, {"uv": [round(uv[0], 1), round(uv[1], 1)]}
        return STATE_READ, {"uv": [round(uv[0], 1), round(uv[1], 1)]}

    @staticmethod
    def _hit_geo(geo, qaz, qalt):
        """对预处理后的轮廓几何做命中判定（热路径，零额外分配）。"""
        closed = geo["closed"]
        if closed == "open":
            nearest_i, nearest_d = None, float("inf")
            for a1, y1, a2, y2, idx, tol in geo["edge_groups"][0]:
                dx, dy = a2 - a1, y2 - y1
                t = ((qaz - a1) * dx + (qalt - y1) * dy) / (dx * dx + dy * dy)
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                dd = math.hypot(qaz - (a1 + t * dx), qalt - (y1 + t * dy))
                if dd < nearest_d:
                    nearest_d, nearest_i = dd, idx
                if dd < tol:
                    return True, idx, dd
            return False, nearest_i, nearest_d

        # ground / closed：仅查询方位落入的那份拷贝需要做射线法
        hit = False
        nearest_i, nearest_d = None, float("inf")
        for gi, shift in enumerate(geo["copies"]):
            lo = geo["amin"] + 360.0 * shift
            hi = geo["amax"] + 360.0 * shift
            in_range = lo - 1e-9 <= qaz <= hi + 1e-9
            if in_range:
                inside = False
                for elo, ehi, slope, icept in geo["flat_groups"][gi]:
                    if elo - 1e-9 <= qaz < ehi - 1e-9 and slope * qaz + icept > qalt:
                        inside = not inside
                if inside:
                    hit = True
            if in_range:
                for a1, y1, a2, y2, idx, _tol in geo["edge_groups"][gi]:
                    dx, dy = a2 - a1, y2 - y1
                    t = ((qaz - a1) * dx + (qalt - y1) * dy) / (dx * dx + dy * dy)
                    if t < 0.0:
                        t = 0.0
                    elif t > 1.0:
                        t = 1.0
                    dd = math.hypot(qaz - (a1 + t * dx), qalt - (y1 + t * dy))
                    if dd < nearest_d:
                        nearest_d, nearest_i = dd, idx
        if nearest_i is not None:
            nearest_i %= geo["nseg"]
        return hit, nearest_i, nearest_d

    def _shadow_uv_from_sun(self, s):
        x = engine.shadow_tip(self.L, self.p_used, self.n, s)
        if x is None:
            return None
        pu, pv = engine.to_uv(x, self.u, self.v)
        return [pu + self.root_du, pv + self.root_dv]

    # -- 按日缓存的快速太阳位置（全年扫描热路径） -------------------
    def sun_ctx(self, date_local):
        """以 UTC 正午为基准缓存该日赤纬/RA/GMST，时刻只做自转换算。"""
        offset = self.tz + self.dst
        noon_utc = date_local + timedelta(hours=12.0 - offset)
        app_long, decl0, _ml = solar.solar_longitude(noon_utc)
        _l2, decl1, _m2 = solar.solar_longitude(noon_utc + timedelta(days=1))
        gst0 = solar.gmst_rad(noon_utc)
        # 黄经 -> 赤经（含黄赤交角）
        lam = app_long * DEG
        ra = math.atan2(math.cos(23.4397 * DEG) * math.sin(lam), math.cos(lam))
        return {"offset": offset, "decl0": decl0, "decl1": decl1,
                "gst0": gst0, "ra": ra, "lon_r": self.lng * DEG,
                "latr": self.lat * DEG}

    def sun_enu_ctx(self, ctx, clock_hours):
        # 本地民用钟面时相对本地正午的日分数（ctx 基准取 UTC 正午，
        # 即本地 12:00，故 delta 直接为 (clock-12)/24）
        delta = (clock_hours - 12.0) / 24.0
        gst = ctx["gst0"] + 360.98564736629 * DEG * delta
        decl = ctx["decl0"] + (ctx["decl1"] - ctx["decl0"]) * (delta + 0.5)
        h = gst + ctx["lon_r"] - ctx["ra"]
        return engine._sun_enu(h, decl, ctx["latr"])

    @staticmethod
    def _alt_az(s):
        alt = math.asin(max(-1.0, min(1.0, s[2])))
        az = math.atan2(-s[0], -s[1])  # 南起西正
        return alt, az

    def classify_ctx(self, ctx, clock_hours):
        """与 classify 等价的快速版本，供全年扫描/二分复用同一日上下文。"""
        s = self.sun_enu_ctx(ctx, clock_hours)
        alt, az_south = self._alt_az(s)
        if s[2] <= ALT_EPS:
            return STATE_BELOW, None
        if engine._dot(s, self.n) <= ALT_EPS:
            return STATE_BACK, None
        for pi, geo in enumerate(self._prof_geo):
            hit, edge_i, dist = self._hit_geo(geo, math.degrees(az_south),
                                              math.degrees(alt))
            if hit:
                prof = self.profiles[pi]
                note = prof["notes"].get(str(edge_i), "") if edge_i is not None else ""
                detail = {
                    "profile_index": pi,
                    "profile_name": prof["name"],
                    "edge_index": edge_i,
                    "segment_label": "%s：段 %d–%d"
                        % (prof["name"], (edge_i or 0) + 1,
                           ((edge_i or 0) + 1) % geo["nseg"] + 1),
                    "note": note,
                    "dist_deg": round(dist, 2) if dist is not None else None,
                    "sun_az_south": round(math.degrees(az_south), 2),
                    "sun_alt": round(math.degrees(alt), 2),
                }
                return STATE_BLOCK, detail
        uv = self._shadow_uv_from_sun(s)
        if uv is None:
            return STATE_BACK, None
        if not engine.in_rect(uv[0], uv[1], self.bounds):
            return STATE_OUT, {"uv": [round(uv[0], 1), round(uv[1], 1)]}
        return STATE_READ, {"uv": [round(uv[0], 1), round(uv[1], 1)]}

    def shadow_uv_ctx(self, ctx, clock_hours):
        s = self.sun_enu_ctx(ctx, clock_hours)
        if s[2] <= ALT_EPS:
            return None, STATE_BELOW
        if engine._dot(s, self.n) <= ALT_EPS:
            return None, STATE_BACK
        uv = self._shadow_uv_from_sun(s)
        return [round(uv[0], 2), round(uv[1], 2)], (
            STATE_READ if engine.in_rect(uv[0], uv[1], self.bounds) else STATE_OUT)

    def shadow_uv(self, date_local, clock_hours):
        """可读/越界判定共用：返回 (uv 或 None, state)。"""
        s, alt, _az, _utc = self.sun_at_civil(date_local, clock_hours)
        if s[2] <= ALT_EPS:
            return None, STATE_BELOW
        if engine._dot(s, self.n) <= ALT_EPS:
            return None, STATE_BACK
        uv = self._shadow_uv_from_sun(s)
        return [round(uv[0], 2), round(uv[1], 2)], (
            STATE_READ if engine.in_rect(uv[0], uv[1], self.bounds) else STATE_OUT)

    def daylight_window(self, date_local):
        """估算该日可能日照的民用钟面时窗口（外扩边距），用于缩小扫描范围。"""
        latr = self.lat * DEG
        d0 = datetime(date_local.year, date_local.month, date_local.day)
        decl = solar.declination(d0 + timedelta(hours=12 - self.tz))
        cos_w0 = -math.tan(latr) * math.tan(decl)
        if cos_w0 >= 1.0:
            return None  # 极夜
        if cos_w0 <= -1.0:
            return (0.0, 24.0)  # 极昼
        daylen_h = 2.0 * math.degrees(math.acos(cos_w0)) / 15.0
        # 正午民用钟面时 ≈ 12 - 经度修正 + dst - EOT
        eot = solar.equation_of_time(d0 + timedelta(hours=12 - self.tz))
        lon_corr = engine.longitude_correction_min(self.lng, self.tz)
        noon_civil = 12.0 - lon_corr / 60.0 + self.dst - eot / 60.0
        lo = max(0.0, noon_civil - daylen_h / 2.0 - 1.0)
        hi = min(24.0, noon_civil + daylen_h / 2.0 + 1.0)
        return lo, hi


# ---------------------------------------------------------------- 时刻工具

def _clock_label(hours):
    total = int(round(hours * 60)) % (24 * 60)
    return "%02d:%02d" % (total // 60, total % 60)


def _bisect_edge(site, ctx, t1, t2, state1, depth=6):
    """在状态变化的 [t1,t2] 间二分，返回 (边界小时, 边界处状态)。"""
    state2 = site.classify_ctx(ctx, t2)[0]
    for _ in range(depth):
        tm = (t1 + t2) / 2.0
        sm, _d = site.classify_ctx(ctx, tm)
        if sm == state1:
            t1 = tm
        else:
            t2 = tm
            state2 = sm
    return (t1 + t2) / 2.0, state2


def _runs_from_cells(cells):
    """把逐格 [(state, t0, t1, detail)] 压缩为连续区间；block 段保留样点信息。"""
    def same_block(a, b):
        return (a.get("detail") and b.get("detail") and
                a["detail"].get("profile_index") == b["detail"].get("profile_index") and
                a["detail"].get("edge_index") == b["detail"].get("edge_index"))

    runs = []
    for state, t0, t1, detail in cells:
        cell = {"state": state, "detail": detail}
        if runs and runs[-1]["state"] == state and \
                (state != STATE_BLOCK or same_block(runs[-1], cell)):
            runs[-1]["end"] = t1
        else:
            runs.append({"state": state, "start": t0, "end": t1,
                         "detail": detail})
    for r in runs:
        r["start_hm"] = _clock_label(r["start"])
        r["end_hm"] = _clock_label(r["end"])
        r["hours"] = round(r["end"] - r["start"], 3)
    return runs


# ---------------------------------------------------------------- 全年分析

def analyze_year(params, mount, year=None, step_min=STEP_MIN):
    """全年逐日扫描，返回可读窗口、月度统计与影端遮挡轨迹。"""
    year = int(year or params.get("year") or datetime.now().year)
    site = Site(params, mount)
    step_h = step_min / 60.0
    months = [{"month": m, "read_hours": 0.0, "sun_hours": 0.0,
               "key": {k: {"target": round(t1 - t0, 4), "deficit": 0.0,
                           "read": 0.0} for k, _lb, t1, t0 in []}}
              for m in range(1, 13)]
    for m in months:
        m["key"] = {key: {"label": lb, "target_hours": round(th1 - th0, 4),
                          "read_hours": 0.0, "deficit_hours": 0.0}
                    for key, lb, th0, th1 in KEY_PERIODS}
    totals = {s: 0.0 for s in (STATE_BELOW, STATE_BACK, STATE_BLOCK,
                               STATE_OUT, STATE_READ)}
    days = []
    blocked_trace = []  # 被遮挡时刻的影端（落在盘面上才有物理意义，供叠加）
    di = 0
    date0 = datetime(year, 1, 1)
    while di < 365:
        dl = date0 + timedelta(days=di)
        ctx = site.sun_ctx(dl)
        win = site.daylight_window(dl)
        cells = []
        # 午夜到窗口起点：地平线下
        if win is None:
            cells.append((STATE_BELOW, 0.0, 24.0, None))
        else:
            wlo, whi = win
            t = 0.0
            if wlo > 0.0:
                cells.append((STATE_BELOW, 0.0, wlo, None))
                t = wlo
            while t < whi - 1e-9:
                t1 = min(t + step_h, whi)
                tm = min(t + step_h / 2.0, whi)
                st, detail = site.classify_ctx(ctx, tm)
                cells.append((st, t, t1, detail))
                if st == STATE_BLOCK:
                    uv, _ = site.shadow_uv_ctx(ctx, tm)
                    if uv is not None:
                        blocked_trace.append({
                            "month": dl.month, "day": dl.day,
                            "clock": _clock_label(tm),
                            "uv": uv, "profile": detail["profile_name"],
                            "edge": detail["edge_index"],
                        })
                t = t1
            if whi < 24.0:
                cells.append((STATE_BELOW, whi, 24.0, None))

        # 边界二分细化
        refined = [cells[0]]
        for c in cells[1:]:
            prev = refined[-1]
            if c[0] != prev[0]:
                tb, _sb = _bisect_edge(site, ctx, prev[2], c[1], prev[0])
                refined[-1] = (prev[0], prev[1], tb, prev[3])
                refined.append((c[0], tb, c[2], c[3]))
            else:
                refined.append(c)
        runs = _runs_from_cells(refined)

        # 统计
        sun_hours = 0.0
        read_hours = 0.0
        for st, t0, t1, _d in refined:
            h = t1 - t0
            totals[st] += h
            if st != STATE_BELOW:
                sun_hours += h
            if st == STATE_READ:
                read_hours += h
        months[dl.month - 1]["read_hours"] += read_hours
        months[dl.month - 1]["sun_hours"] += sun_hours

        # 关键时段缺口（与扫描同步长，计入各月）
        for key, _lb, th0, th1 in KEY_PERIODS:
            t = th0
            while t < th1 - 1e-9:
                tc = min(t + step_h, th1)
                st, _d = site.classify_ctx(ctx, t + step_h / 2.0)
                slot = min(tc, th1) - t
                if st == STATE_READ:
                    months[dl.month - 1]["key"][key]["read_hours"] += slot
                elif st != STATE_BELOW:
                    months[dl.month - 1]["key"][key]["deficit_hours"] += slot
                t = tc

        readable = [{"start": r["start_hm"], "end": r["end_hm"],
                     "hours": r["hours"]}
                    for r in runs if r["state"] == STATE_READ]
        days.append({
            "date": "%02d-%02d" % (dl.month, dl.day),
            "month": dl.month, "day": dl.day,
            "read_hours": round(read_hours, 3),
            "sun_hours": round(sun_hours, 3),
            "readable": readable,
            "runs": runs,
        })
        di += 1

    month_rows = []
    for m in months:
        ndays = sum(1 for x in days if x["month"] == m["month"])
        row = {
            "month": m["month"], "days": ndays,
            "read_hours": round(m["read_hours"], 2),
            "sun_hours": round(m["sun_hours"], 2),
            "avg_read_hours": round(m["read_hours"] / max(ndays, 1), 2),
            "avg_sun_hours": round(m["sun_hours"] / max(ndays, 1), 2),
            "key": {},
        }
        for key, _lb, _t0, _t1 in KEY_PERIODS:
            kk = m["key"][key]
            row["key"][key] = {
                "label": kk["label"],
                "read_hours": round(kk["read_hours"], 2),
                "deficit_hours": round(kk["deficit_hours"], 2),
                "target_hours": round(kk["target_hours"] * ndays, 2),
            }
        month_rows.append(row)

    total_read = totals[STATE_READ]
    total_sun = sum(v for k, v in totals.items() if k != STATE_BELOW)
    summary = {
        "year": year,
        "total_read_hours": round(total_read, 1),
        "total_sun_hours": round(total_sun, 1),
        "read_ratio": round(total_read / total_sun, 3) if total_sun else 0.0,
        "hours_by_state": {k: round(v, 1) for k, v in totals.items()},
        "best_month": max(month_rows, key=lambda r: r["read_hours"])["month"],
        "worst_month": min(month_rows, key=lambda r: r["read_hours"])["month"],
    }

    # 被遮挡影端轨迹按月抽稀（每月保留等间隔最多约 90 点，用于盘面叠加）
    trace_by_month = {}
    for q in blocked_trace:
        trace_by_month.setdefault(q["month"], []).append(q)
    blocked_trace_out = []
    for mm, arr in trace_by_month.items():
        keep = max(1, len(arr) // 90 + 1)
        blocked_trace_out.extend(arr[::keep])

    return {
        "summary": summary,
        "months": month_rows,
        "days": days,
        "blocked_trace": blocked_trace_out,
        "state_labels": STATE_LABELS,
    }


# ---------------------------------------------------------------- 当日预览

def day_curve(params, mount, year, month, day, mode="civil",
              trace_min=TRACE_MIN):
    """给定日期的太阳弧线、全天状态轨迹与当前时刻判定，供前端联动。"""
    site = Site(params, mount)
    dl = datetime(year, month, day)
    ctx = site.sun_ctx(dl)
    win = site.daylight_window(dl)
    sun_arc = []
    trace = []
    step_h = trace_min / 60.0
    t = 0.0
    while t < 24.0 - 1e-9:
        tm = t + step_h / 2.0
        s = site.sun_enu_ctx(ctx, tm)
        alt = math.degrees(math.asin(max(-1.0, min(1.0, s[2]))))
        az_s = math.degrees(math.atan2(-s[0], -s[1]))
        if alt > -0.5:
            az_plot = az_s + 180.0 if site.az_ref == "north" else az_s
            sun_arc.append({
                "clock": _clock_label(tm),
                "t": round(tm, 4),
                "alt": round(alt, 2),
                "az": round(((az_plot + 180.0) % 360.0) - 180.0, 2),
            })
        if win is None or not (win[0] <= tm <= win[1]):
            st, detail, uv = STATE_BELOW, None, None
        else:
            st, detail = site.classify_ctx(ctx, tm)
            uv, _ = site.shadow_uv_ctx(ctx, tm)
        trace.append({
            "t0": round(t, 4), "t1": round(t + step_h, 4),
            "state": st, "uv": uv, "detail": detail,
        })
        t += step_h
    return {"date": "%04d-%02d-%02d" % (year, month, day),
            "sun_arc": sun_arc, "trace": trace,
            "window": ([round(win[0], 3), round(win[1], 3)]
                       if win else None)}


def month_arcs(params, year=None, trace_min=30):
    """每月一条代表性太阳弧线（取每月 15 日），作为方位高度图的年变化底图。"""
    year = int(year or params.get("year") or datetime.now().year)
    # 轮廓不参与，只需 Site 的太阳换算
    site = Site(params, {"az_ref": "south", "profiles": []})
    arcs = []
    for m in range(1, 13):
        dl = datetime(year, m, 15)
        pts = []
        t = 4.0
        while t < 20.5:
            s, alt, az_s, _u = site.sun_at_civil(dl, t)
            if alt > -0.2:
                pts.append({"t": round(t, 3), "alt": round(alt, 2),
                            "az": round(az_s, 2)})
            t += trace_min / 60.0
        arcs.append({"month": m, "points": pts})
    return arcs


def instant(params, mount, year, month, day, hour):
    """当前滑块时刻：状态、原因、太阳位置与影端。"""
    site = Site(params, mount)
    dl = datetime(year, month, day)
    st, detail = site.classify(dl, hour)
    s, alt, az_s, utc = site.sun_at_civil(dl, hour)
    uv, _ = site.shadow_uv(dl, hour)
    offset = site.tz + site.dst
    civil = utc + timedelta(hours=offset)
    eot = solar.equation_of_time(
        dl + timedelta(hours=12 - offset))
    lon_corr = engine.longitude_correction_min(site.lng, site.tz)
    tst = hour + lon_corr / 60.0 - site.dst + eot / 60.0
    az_plot = az_s + 180.0 if site.az_ref == "north" else az_s
    return {
        "state": st,
        "state_label": STATE_LABELS[st],
        "detail": detail,
        "uv": uv,
        "alt_deg": round(alt, 2),
        "az_south_deg": round(az_s, 2),
        "az_ref_deg": round(((az_plot + 180.0) % 360.0) - 180.0, 2),
        "az_ref": site.az_ref,
        "civil": civil.strftime("%H:%M"),
        "tst": _clock_label(tst % 24),
    }


# ---------------------------------------------------------------- 双安装点比较

def compare_sites(params, mount_a, mount_b, year=None, step_min=STEP_MIN):
    ra = analyze_year(params, mount_a, year, step_min)
    rb = analyze_year(params, mount_b, year, step_min)
    months = []
    for ma, mb in zip(ra["months"], rb["months"]):
        row = {"month": ma["month"],
               "read_a": ma["avg_read_hours"], "read_b": mb["avg_read_hours"],
               "sun": ma["avg_sun_hours"],
               "delta": round(mb["avg_read_hours"] - ma["avg_read_hours"], 2),
               "key": {}}
        for key, _lb, _t0, _t1 in KEY_PERIODS:
            ka, kb = ma["key"][key], mb["key"][key]
            row["key"][key] = {
                "label": ka["label"],
                "deficit_a": round(ka["deficit_hours"] / max(ma["days"], 1), 2),
                "deficit_b": round(kb["deficit_hours"] / max(mb["days"], 1), 2),
            }
        months.append(row)
    return {
        "months": months,
        "summary_a": ra["summary"], "summary_b": rb["summary"],
        "total_delta_hours": round(
            rb["summary"]["total_read_hours"] - ra["summary"]["total_read_hours"], 1),
    }


# ---------------------------------------------------------------- 报告

def build_report(params, mount, design, analysis=None, year=None):
    """生成含样点、判定原因与可读窗口的完整 JSON 报告结构。"""
    year = int(year or params.get("year") or datetime.now().year)
    if analysis is None:
        analysis = analyze_year(params, mount, year)
    # 轮廓样点使用录入基准导出，避免混淆
    profiles_out = []
    for prof in mount.get("profiles", []):
        profiles_out.append({
            "name": prof["name"],
            "closed_mode": prof["closed"],
            "points": [{"index": i, "az_deg": pt[0], "alt_deg": pt[1],
                        "note": prof["notes"].get(str(i), "")}
                       for i, pt in enumerate(_prof_points(prof))],
        })
    # 关键时段缺口全年汇总（月均）
    key_summary = []
    for key, label, _t0, _t1 in KEY_PERIODS:
        da = sum(m["key"][key]["deficit_hours"] for m in analysis["months"])
        key_summary.append({"key": key, "period": label,
                            "total_deficit_hours": round(da, 1)})
    block_reasons = {}
    for d in analysis["days"]:
        for r in d["runs"]:
            if r["state"] == STATE_BLOCK and r.get("detail"):
                key = "%s｜段%d" % (r["detail"]["profile_name"],
                                    (r["detail"]["edge_index"] or 0) + 1)
                rec = block_reasons.setdefault(key, {"hours": 0.0, "days": set()})
                rec["hours"] += r["hours"]
                rec["days"].add(d["date"])
    reasons = [{"segment": k, "hours": round(v["hours"], 1),
                "days": len(v["days"])}
               for k, v in sorted(block_reasons.items(),
                                  key=lambda kv: -kv[1]["hours"])]
    return {
        "report": "sundial_mount_readability",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "year": year,
        "design": {"id": (design or {}).get("id"),
                   "name": (design or {}).get("name", "")},
        "mount": {
            "id": mount.get("id"),
            "name": mount.get("name", ""),
            "note": mount.get("note", ""),
            "azimuth_datum": "north-clockwise" if mount.get("az_ref") == "north"
                             else "south-west-positive",
            "profiles": profiles_out,
        },
        "params": params,
        "summary": analysis["summary"],
        "monthly": analysis["months"],
        "key_period_deficit": key_summary,
        "block_reasons_ranked": reasons,
        "daily_windows": [
            {"date": d["date"], "sun_hours": d["sun_hours"],
             "read_hours": d["read_hours"], "readable": d["readable"],
             "segments": [
                 {"from": r["start_hm"], "to": r["end_hm"],
                  "state": r["state"],
                  "reason": STATE_LABELS[r["state"]],
                  "profile": (r["detail"] or {}).get("profile_name"),
                  "segment_index": (r["detail"] or {}).get("edge_index"),
                  "note": (r["detail"] or {}).get("note", "")}
                 for r in d["runs"]]}
            for d in analysis["days"]
        ],
        "blocked_shadow_trace": analysis["blocked_trace"],
    }


def _prof_points(prof):
    return prof.get("points", [])


def normalize_mount_payload(payload, existing=None):
    """校验前端提交的安装点字段，返回 (mount_dict, errors)。"""
    errors = []
    name = str(payload.get("name") or "").strip()[:60]
    if not name:
        errors.append("请填写安装点名称")
    az_ref = payload.get("az_ref", "south")
    if az_ref not in VALID_AZREF:
        errors.append("方位基准无效")
        az_ref = "south"
    note = str(payload.get("note") or "").strip()[:500]
    profiles = []
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        errors.append("请至少绘制一条遮挡轮廓")
    else:
        for prof in raw_profiles:
            clean, errs = normalize_profile(prof)
            errors.extend(errs)
            profiles.append(clean)
    mount = {
        "name": name, "note": note, "az_ref": az_ref,
        "profiles": profiles,
    }
    if existing:
        mount["id"] = existing.get("id")
        mount["design_id"] = existing.get("design_id")
    return mount, errors

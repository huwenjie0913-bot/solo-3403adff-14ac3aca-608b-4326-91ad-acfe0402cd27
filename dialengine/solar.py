# -*- coding: utf-8 -*-
"""太阳位置与二十四节气计算（NOAA Solar Position Algorithm 派生公式）。

所有时间以公历 datetime（朴素，视为 UTC）表示；角度函数一律使用弧度。
均时差约定：EOT = 真太阳时 - 平太阳时（分钟），2 月约为 -14，11 月约为 +16。
"""
import math
from datetime import datetime, timedelta

DEG = math.pi / 180.0
HOURS_TO_DEG = 15.0

# 黄经（度）-> 节气名（以春分黄经 0 度为始）
JIEQI = [
    (0, "春分"), (15, "清明"), (30, "谷雨"), (45, "立夏"),
    (60, "小满"), (75, "芒种"), (90, "夏至"), (105, "小暑"),
    (120, "大暑"), (135, "立秋"), (150, "处暑"), (165, "白露"),
    (180, "秋分"), (195, "寒露"), (210, "霜降"), (225, "立冬"),
    (240, "小雪"), (255, "大雪"), (270, "冬至"), (285, "小寒"),
    (300, "大寒"), (315, "立春"), (330, "雨水"), (345, "惊蛰"),
]


def julian_day(dt):
    """datetime(UTC) -> 儒略日。"""
    year = dt.year
    month = dt.month
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    day_frac = (dt.hour + (dt.minute + dt.second / 60.0) / 60.0) / 24.0
    return (math.floor(365.25 * (year + 4716))
            + math.floor(30.6001 * (month + 1))
            + dt.day + day_frac + b - 1524.5)


def solar_longitude(dt):
    """返回太阳视黄经（度，0..360）、赤纬（弧度）、平黄经（度）。"""
    jd = julian_day(dt)
    t = (jd - 2451545.0) / 36525.0
    geom_mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    geom_mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccent = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    gma = geom_mean_anom * DEG
    sun_eq = (math.sin(gma) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * gma) * (0.019993 - 0.000101 * t)
              + math.sin(3 * gma) * 0.000289)
    sun_true_long = geom_mean_long + sun_eq
    omega = 125.04 - 1934.136 * t
    app_long = sun_true_long - 0.00569 - 0.00478 * math.sin(omega * DEG)
    mean_obliq = (23.0 + (26.0 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))) / 60.0) / 60.0)
    obliq_corr = mean_obliq + 0.00256 * math.cos(omega * DEG)
    decl = math.asin(math.sin(obliq_corr * DEG) * math.sin(app_long * DEG))
    return app_long % 360.0, decl, geom_mean_long


def equation_of_time(dt):
    """均时差（分钟，EOT = 真太阳时 - 平太阳时）。"""
    jd = julian_day(dt)
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = m * DEG
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    obliq = (23.0 + (26.0 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))) / 60.0) / 60.0)
    obliq_corr = obliq + 0.00256 * math.cos(omega * DEG)
    y = math.tan(obliq_corr * DEG / 2.0) ** 2
    eot_min = 4.0 * (y * math.sin(2 * l0 * DEG)
                     - 2 * e * math.sin(mr)
                     + 4 * e * y * math.sin(mr) * math.cos(2 * l0 * DEG)
                     - 0.5 * y * y * math.sin(4 * l0 * DEG)
                     - 1.25 * e * e * math.sin(2 * mr)) / DEG
    return eot_min


def declination(dt):
    """太阳赤纬（弧度）。"""
    return solar_longitude(dt)[1]


def jieqi_dates(year):
    """返回该年 24 节气的 [(名称, datetime(UTC)), ...]，按公历日期排序。

    采用先估算日期再用黄经二分精确化的方式。
    """
    approx = {
        "春分": (3, 20), "清明": (4, 5), "谷雨": (4, 20), "立夏": (5, 5),
        "小满": (5, 21), "芒种": (6, 6), "夏至": (6, 21), "小暑": (7, 7),
        "大暑": (7, 23), "立秋": (8, 8), "处暑": (8, 23), "白露": (9, 8),
        "秋分": (9, 23), "寒露": (10, 8), "霜降": (10, 23), "立冬": (11, 7),
        "小雪": (11, 22), "大雪": (12, 7), "冬至": (12, 22),
    }
    # 小寒/大寒/立春/雨水/惊蛰 在年初
    approx_early = {
        "小寒": (1, 6), "大寒": (1, 20), "立春": (2, 4),
        "雨水": (2, 19), "惊蛰": (3, 5),
    }
    result = []
    for lon, name in JIEQI:
        if name in approx:
            m, d = approx[name]
            guess = datetime(year, m, d, 12)
        else:
            m, d = approx_early[name]
            guess = datetime(year, m, d, 12)
        # 直接在估算点 ±1 天用连续角度二分（黄经在该窗口单调，日变化约 1 度）
        lo = guess - timedelta(hours=30)
        hi = guess + timedelta(hours=30)

        def fval(t):
            v = (solar_longitude(t)[0] - lon) % 360.0
            return v if v <= 180 else v - 360.0

        # 确保根落在区间内
        flo, fhi = fval(lo), fval(hi)
        if flo * fhi > 0:
            # 微调窗口
            lo -= timedelta(hours=12)
            hi += timedelta(hours=12)
            flo, fhi = fval(lo), fval(hi)
        for _ in range(48):
            mid = lo + (hi - lo) / 2
            fm = fval(mid)
            if flo * fm <= 0:
                hi = mid
                fhi = fm
            else:
                lo = mid
                flo = fm
        root = lo + (hi - lo) / 2
        result.append((name, root))
    result.sort(key=lambda x: x[1])
    return result


def gmst_rad(dt):
    """格林尼治平恒星时（弧度）。"""
    jd = julian_day(dt)
    t = (jd - 2451545.0) / 36525.0
    gmst_deg = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
                + 0.000387933 * t * t - t ** 3 / 38710000.0)
    return gmst_deg % 360.0 * DEG


def sun_vector_enu(dt, lat, lon):
    """给定 UTC 时刻与经纬度（度），返回太阳单位向量 (e, n, u) 与高度角、方位角。

    方位角从正南向西为正（日晷盘面方位约定一致）；高度角为地平纬度。
    """
    decl = declination(dt)
    gst = gmst_rad(dt)
    lst = gst + lon * DEG
    ra = math.atan2(
        math.cos(23.4397 * DEG) * math.sin(solar_longitude(dt)[0] * DEG),
        math.cos(solar_longitude(dt)[0] * DEG))
    h = lst - ra  # 时角，正南过中天为 0，下午为正
    latr = lat * DEG
    sh, ch = math.sin(h), math.cos(h)
    sd, cd = math.sin(decl), math.cos(decl)
    sl, cl = math.sin(latr), math.cos(latr)
    e = -cd * sh
    n_ = cl * sd - sl * cd * ch
    up = sl * sd + cl * cd * ch
    norm = math.sqrt(e * e + n_ * n_ + up * up)
    e, n_, up = e / norm, n_ / norm, up / norm
    alt = math.asin(max(-1.0, min(1.0, up)))
    # 方位角：自正南向西为正 = atan2(E, N)（北点 N、东点 E）
    az = math.atan2(e, n_)
    return e, n_, up, alt, az

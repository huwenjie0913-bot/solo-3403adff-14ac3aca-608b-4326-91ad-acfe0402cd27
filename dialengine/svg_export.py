# -*- coding: utf-8 -*-
"""生成可打印 SVG 模板：1:1 刻度盘 + 比例尺 + 安装角/方位基准 + 参数信息。"""
import math
import xml.sax.saxutils as sx

from . import engine

MM_PER_IN = 25.4
DPI = 96
PX_PER_MM = DPI / MM_PER_IN  # SVG 用户单位 = px；按 96dpi 输出，1mm=3.7795px

STROK = "#111111"
FAINT = "#8a8f98"
ACCENT = "#b3541e"


def _esc(t):
    return sx.escape(str(t))


def _line(x1, y1, x2, y2, **kw):
    a = " ".join('%s="%s"' % (k, v) for k, v in kw.items())
    return '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" %s/>' % (
        x1, y1, x2, y2, a)


def _text(x, y, s, size=3.2, anchor="middle", weight="normal", fill=STROK, rotate=None):
    tr = ' transform="rotate(%s %.2f %.2f)"' % (rotate, x, y) if rotate is not None else ""
    return ('<text x="%.2f" y="%.2f" font-size="%.2f" text-anchor="%s" '
            'font-weight="%s" fill="%s" font-family="Noto Sans CJK SC, sans-serif"%s>%s</text>'
            % (x, y, size, anchor, weight, fill, tr, _esc(s)))


def _poly(points, fill="none", stroke=STROK, width=0.35, dash=None, opacity=1.0):
    pts = " ".join("%.2f,%.2f" % (p[0] * PX_PER_MM, -p[1] * PX_PER_MM) for p in points)
    d = 'stroke-dasharray="%s"' % dash if dash else ""
    return ('<polygon points="%s" fill="%s" stroke="%s" stroke-width="%s" '
            'stroke-linejoin="round" opacity="%.2f" %s/>'
            % (pts, fill, stroke, width, opacity, d))


def _path_from_points(points, closed=False):
    d = "M%.2f,%.2f" % (points[0][0] * PX_PER_MM, -points[0][1] * PX_PER_MM)
    for p in points[1:]:
        d += " L%.2f,%.2f" % (p[0] * PX_PER_MM, -p[1] * PX_PER_MM)
    if closed:
        d += " Z"
    return d


def _uv(p):
    return p[0] * PX_PER_MM, -p[1] * PX_PER_MM


def generate_print_svg(params, data):
    """返回完整 SVG 文档字符串（打印单位 1mm=3.7795px，即 96dpi）。"""
    W = params["width"]
    H = params["height"]
    m = data["meta"]
    g = data["gnomon"]
    b = data["plate"]["bounds"]

    pad = 30.0 * PX_PER_MM      # 盘面四周留白
    right = 70.0 * PX_PER_MM    # 右侧信息栏
    pw = W * PX_PER_MM + 2 * pad + right
    ph = max(H * PX_PER_MM + 2 * pad, 760.0)
    cx = pad + W * PX_PER_MM / 2
    cy = ph / 2

    parts = []
    parts.append(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="%.1fmm" height="%.1fmm" '
        'viewBox="0 0 %.1f %.1f">' % (pw / PX_PER_MM, ph / PX_PER_MM, pw, ph))
    parts.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    parts.append(
        '<g transform="translate(%.2f %.2f)">' % (cx, cy))

    def gxy(u, v):
        return (u * PX_PER_MM, -v * PX_PER_MM)

    # 裁切边界（盘面外框，粗实线）
    x0, y0 = gxy(b["umin"], b["vmax"])
    x1, y1 = gxy(b["umax"], b["vmin"])

    # 所有刻度内容裁剪到盘面内，避免低太阳高度影长发散污染图面
    clip_id = "plateclip"
    parts.append('<defs><clipPath id="%s"><rect x="%.2f" y="%.2f" width="%.2f" height="%.2f"/>'
                 '</clipPath></defs>'
                 % (clip_id, x0, y0, x1 - x0, y1 - y0))
    parts.append('<g clip-path="url(#%s)">' % clip_id)

    # 全年影端轨迹包络（淡灰填充）
    if data["envelope"]:
        parts.append('<path d="%s" fill="#e8e2d8" stroke="%s" '
                     'stroke-width="0.25" opacity="0.75"/>'
                     % (_path_from_points(data["envelope"], True), FAINT))

    # 节气日期线
    for i, c in enumerate(data["date_curves"]):
        dash = None if c["solstice"] else "2.2,1.4"
        col = ACCENT if c["solstice"] else "#9c6b3a"
        parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" %s/>'
                     % (_path_from_points(c["points"]), col,
                        0.5 if c["solstice"] else 0.3,
                        ('stroke-dasharray="%s"' % dash) if dash else ""))
        # 仅在盘内的端点标名称
        for lb in c["labels"]:
            u, v = lb["uv"]
            if b["umin"] - 2 <= u <= b["umax"] + 2 and b["vmin"] - 2 <= v <= b["vmax"] + 2:
                tx, ty = gxy(u, v)
                parts.append(_text(tx, ty - 1.2, "%s %s" % (lb["name"], lb["date"]),
                                   size=2.6, fill="#7a4a22"))

    # 时线
    for ray in data["rays"]:
        th = ray["angle_deg"] * math.pi / 180
        iuv = ray.get("inner_uv")
        r0 = iuv if iuv else (0, 0)
        r1 = ray["edge_uv"]
        a = gxy(*r0)
        e = gxy(*r1)
        w = 0.55 if ray["major"] else 0.22
        parts.append(_line(a[0], a[1], e[0], e[1], stroke=STROK,
                           **{"stroke-width": w}))
        # 时刻数字：放在内外半径 65% 处（盘内）
        if ray["major"] and ray["label"]:
            ru = r0[0] + (r1[0] - r0[0]) * 0.65
            rv = r0[1] + (r1[1] - r0[1]) * 0.65
            if b["umin"] <= ru <= b["umax"] and b["vmin"] <= rv <= b["vmax"]:
                tx, ty = gxy(ru, rv)
                parts.append(_text(tx, ty + 1.1, ray["label"], size=3.6, weight="bold"))
    parts.append('</g>')

    # 盘面边框（置于裁剪层之上，保证为完整粗实线）
    parts.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" '
                 'fill="none" stroke="%s" stroke-width="0.8"/>'
                 % (x0, y0, x1 - x0, y1 - y0, STROK))

    # 晷针根点（盘心）与极边投影
    ox, oy = gxy(0, 0)
    parts.append('<circle cx="%.2f" cy="%.2f" r="1.6" fill="%s"/>' % (ox, oy, STROK))
    sub = g["substyle_angle_deg"]
    lx, ly = gxy(10 * math.sin(math.radians(sub)), 10 * math.cos(math.radians(sub)))
    parts.append(_line(ox, oy, lx, ly, stroke="#1f6f4a", **{"stroke-width": 0.9}))

    parts.append('</g>')

    # ------------------------------------------------ 比例尺（100mm）
    sbx, sby = pad, ph - 16 * PX_PER_MM
    Lmm = 100
    seg = 10
    for i in range(seg):
        fill = STROK if i % 2 == 0 else "#ffffff"
        parts.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" '
                     'fill="%s" stroke="%s" stroke-width="0.25"/>'
                     % (sbx + i * Lmm / seg * PX_PER_MM, sby,
                        Lmm / seg * PX_PER_MM, 3.2 * PX_PER_MM, fill, STROK))
    for i in range(seg + 1):
        xx = sbx + i * Lmm / seg * PX_PER_MM
        parts.append(_text(xx, sby - 1.2, str(i * 10), size=2.6))
    parts.append(_text(sbx + Lmm * PX_PER_MM / 2, sby + 7.6, "比例尺 0–100 mm（1:1 打印）",
                       size=3.0))

    # ------------------------------------------------ 右侧信息栏
    ix = pad + W * PX_PER_MM + 18 * PX_PER_MM
    iy = pad
    lines = [
        ("日晷刻度盘模板", 5.2, "bold"),
        ("", 2.0, "normal"),
        ("【方位基准】", 3.6, "bold"),
        ("盘面朝向：方位 %g°（正南 0°，向西为正）" % m["az"], 3.0, "normal"),
        ("盘面倾角：%g°（水平 0 / 垂直 90）" % m["inc"], 3.0, "normal"),
        ("", 1.5, "normal"),
        ("【安装角】", 3.6, "bold"),
        ("晷针（极边）与盘面夹角：%.2f°" % g["style_angle_deg"], 3.0, "normal"),
        ("晷针仰角（相对水平面）：%.2f°" % g["polar_elev_deg"], 3.0, "normal"),
        ("晷针方位（南起西正）：%.2f°" % g["polar_az_south_deg"], 3.0, "normal"),
        ("副法线方向（自盘面上方向右）：%.2f°" % g["substyle_angle_deg"], 3.0, "normal"),
        ("晷针长度 L = %g mm" % m["style_len"], 3.0, "normal"),
        ("", 1.5, "normal"),
        ("【地点参数】", 3.6, "bold"),
        ("纬度 %g°  经度 %g°" % (m["lat"], m["lng"]), 3.0, "normal"),
        ("时区 UTC%+g   夏令时 %+g h" % (m["tz"], m["dst"]), 3.0, "normal"),
        ("经度修正 %+.2f 分钟" % data["eot"]["longitude_correction_min"], 3.0, "normal"),
        ("均时差全年 %+.1f（%s）至 %+.1f 分（%s）"
         % (data["eot"]["min"]["value"], data["eot"]["min"]["date"],
            data["eot"]["max"]["value"], data["eot"]["max"]["date"]), 3.0, "normal"),
        ("", 1.5, "normal"),
        ("【盘面】", 3.6, "bold"),
        ("尺寸 %g × %g mm（粗框为裁切边界）" % (m["width"], m["height"]), 3.0, "normal"),
        ("最小加工间距设定：%g mm" % m["min_spacing"], 3.0, "normal"),
        ("", 1.5, "normal"),
        ("【刻线说明】", 3.6, "bold"),
        ("粗实线：整时（真太阳时）", 3.0, "normal"),
        ("细实线：细分刻度", 3.0, "normal"),
        ("橙色实线：冬至/夏至影线", 3.0, "normal"),
        ("棕色虚线：其余节气影线", 3.0, "normal"),
        ("灰色填充：全年影端可达范围", 3.0, "normal"),
        ("绿线：晷针在盘面投影方向", 3.0, "normal"),
        ("黑点：晷针根点（时线极点）", 3.0, "normal"),
    ]
    yy = iy
    for s, size, weight in lines:
        parts.append(_text(ix, yy, s, size=size, anchor="start", weight=weight))
        yy += (size + 2.6)

    # 盘面四角裁切标记（超出盘面的对位十字）
    for (u, v, sx, sy) in [
        (b["umin"], b["vmax"], -1, 1),
        (b["umax"], b["vmax"], 1, 1),
        (b["umin"], b["vmin"], -1, -1),
        (b["umax"], b["vmin"], 1, -1),
    ]:
        px, py = gxy(u, v)
        px += sx * 0.0
        L = 5 * PX_PER_MM
        parts.append(_line(px, py - sy * 0.0, px + sx * L, py,
                           stroke=STROK, **{"stroke-width": 0.4}))
        parts.append(_line(px, py, px, py - sy * L,
                           stroke=STROK, **{"stroke-width": 0.4}))

    # 盘面上的指北标记（v 轴顶边内侧）
    nx, ny = gxy(0, b["vmax"] - 6)
    parts.append(_text(nx, ny - 2.0, "N（盘面上方基准）", size=3.0, fill="#1f6f4a"))
    parts.append(_line(nx, ny + 2.0, nx, ny - 4.0, stroke="#1f6f4a",
                       **{"stroke-width": 0.6}))
    parts.append('<polygon points="%.2f,%.2f %.2f,%.2f %.2f,%.2f" fill="#1f6f4a"/>'
                 % (nx - 1.4, ny - 2.2, nx + 1.4, ny - 2.2, nx, ny - 5.2))

    parts.append("</svg>")
    return "\n".join(parts)

/* 日晷刻度盘设计与校核 — 前端逻辑（原生 JS + SVG） */
(function () {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);

  const state = {
    params: defaults(),
    data: null,
    preview: null,
    mode: "solar",
    doy: dayOfYear(new Date(2026, new Date().getMonth(), new Date().getDate())),
    minute: 12 * 60,
    year: 2026,
    showEnvelope: true,
    showDates: true,
    showCivil: false,
    view: { x: 0, y: 0, k: 1 }, // 世界 mm -> 屏幕 px 变换
    pending: null,
    previewTimer: null,
    marker: null,
  };

  // 现场校准状态：批次、观测、反算配置与结果、叠加显示开关
  const cal = {
    designId: null,      // 当前载入/保存的设计版本 id
    designName: "",
    batch: null,         // 当前批次详情（含 observations、fit_config、result、design）
    cfg: null,           // 反算配置（各参数 on/min/max/value）
    result: null,        // 最新反算结果
    corrected: null,     // 校正后盘面的 compute_dial 数据
    pick: false,         // 盘面取点模式
    show: false,         // 叠加实测/预测点
    showCorrected: false, // 叠加校正后盘面
  };

  const FIT_PARAMS = [
    { key: "az", label: "方位 °" },
    { key: "inc", label: "倾角 °" },
    { key: "style_len", label: "针长 mm" },
    { key: "root_du", label: "根点 u" },
    { key: "root_dv", label: "根点 v" },
  ];

  function defaults() {
    return { lat: 39.9, lng: 116.4, tz: 8, dst: 0, az: 180, inc: 0,
      width: 300, height: 300, style_len: 80, min_spacing: 2,
      root_du: 0, root_dv: 0, hour_step: 60, year: 2026 };
  }

  const PRESETS = {
    horizontal: { az: 180, inc: 0 },
    vsouth: { az: 0, inc: 90 },
    vnorth: { az: 180, inc: 90 },
    veast: { az: -90, inc: 90 },
    vwest: { az: 90, inc: 90 },
    equatorial: { az: 180, inc: 90 - 39.9 },
    custom: {},
  };

  // ------------------------------------------------------------ 工具
  function el(tag, attrs, parent) {
    const e = document.createElementNS(SVGNS, tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function dayOfYear(d) {
    const start = new Date(d.getFullYear(), 0, 0);
    return Math.floor((d - start) / 86400000) - 1;
  }
  function doyToDate(year, doy) {
    const d = new Date(year, 0, 1);
    d.setDate(d.getDate() + doy);
    return d;
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtDate(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
  function fmtClock(min) { return `${pad(Math.floor(min / 60))}:${pad(min % 60)}`; }
  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json();
  }

  // ------------------------------------------------------------ 读取输入
  function readParams() {
    const p = {};
    for (const k of ["lat", "lng", "tz", "dst", "az", "inc", "width", "height",
      "style_len", "min_spacing", "root_du", "root_dv"]) {
      p[k] = parseFloat($(k).value);
    }
    p.hour_step = parseInt($("hour_step").value, 10);
    p.year = parseInt($("year").value, 10);
    return p;
  }
  function writeInputs(p) {
    for (const k of ["lat", "lng", "tz", "dst", "az", "inc", "width", "height",
      "style_len", "min_spacing", "root_du", "root_dv", "year"]) {
      if ($(k) && p[k] !== undefined) $(k).value = p[k];
    }
    $("hour_step").value = p.hour_step || 60;
  }

  // ------------------------------------------------------------ 计算调度
  function scheduleCompute() {
    clearTimeout(state.pending);
    state.pending = setTimeout(doCompute, 250);
  }
  async function doCompute() {
    const p = readParams();
    if (Object.values(p).some((v) => Number.isNaN(v))) {
      setStatus("参数不完整");
      return;
    }
    state.params = p;
    state.year = p.year;
    setStatus("计算中…");
    const res = await postJSON("/api/calculate", p);
    if (!res.ok) { setStatus("计算失败：" + (res.errors || []).join("；")); return; }
    state.data = res.data;
    setStatus("");
    fitView(false);
    render();
    renderWarnings();
    renderInfo();
    schedulePreview(true);
  }

  function setStatus(t) { $("status").textContent = t; }

  // ------------------------------------------------------------ 视图变换
  const canvas = $("canvas");
  function viewTransform() {
    const r = canvas.getBoundingClientRect();
    return `matrix(${state.view.k} 0 0 ${state.view.k} ${r.width / 2 + state.view.x} ${r.height / 2 + state.view.y})`;
  }
  function fitView(force) {
    const p = state.params;
    const r = canvas.getBoundingClientRect();
    const span = Math.max(p.width, p.height) * 1.35;
    state.view.k = Math.min(r.width, r.height) / span;
    state.view.x = 0; state.view.y = 0;
    const wg = $("worldGroup");
    if (wg) wg.setAttribute("transform", viewTransform());
    updateAxis();
  }
  function zoomAt(factor, cx, cy) {
    const r = canvas.getBoundingClientRect();
    const oldK = state.view.k;
    const newK = Math.min(60, Math.max(0.4, oldK * factor));
    // 以 (cx,cy) 为锚点缩放
    const wx = (cx - r.width / 2 - state.view.x) / oldK;
    const wy = (cy - r.height / 2 - state.view.y) / oldK;
    state.view.x = cx - r.width / 2 - wx * newK;
    state.view.y = cy - r.height / 2 - wy * newK;
    state.view.k = newK;
    $("worldGroup").setAttribute("transform", viewTransform());
    updateAxis();
  }
  function updateAxis() {
    const ax = $("axisLayer");
    if (!ax) return;
    const r = canvas.getBoundingClientRect();
    const stepMM = niceStep(80 / state.view.k);
    ax.innerHTML = "";
    const halfW = r.width / 2 / state.view.k;
    const halfH = r.height / 2 / state.view.k;
    for (let gx = -halfW; gx <= halfW; gx += stepMM) {
      const x = gx * state.view.k + r.width / 2 + state.view.x;
      el("line", { x1: x, y1: 0, x2: x, y2: r.height, class: "gridline" }, ax);
    }
    for (let gy = -halfH; gy <= halfH; gy += stepMM) {
      const y = gy * state.view.k + r.height / 2 + state.view.y;
      el("line", { x1: 0, y1: y, x2: r.width, y2: y, class: "gridline" }, ax);
    }
  }
  function niceStep(target) {
    const raw = target;
    const p10 = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) {
      if (m * p10 >= raw) return m * p10;
    }
    return 10 * p10;
  }
  function screenToWorld(sx, sy) {
    const r = canvas.getBoundingClientRect();
    return {
      x: (sx - r.width / 2 - state.view.x) / state.view.k,
      y: -(sy - r.height / 2 - state.view.y) / state.view.k, // 屏幕 y -> 世界 v
    };
  }
  function worldToScreen(u, v) {
    const r = canvas.getBoundingClientRect();
    return { x: u * state.view.k + r.width / 2 + state.view.x,
             y: -v * state.view.k + r.height / 2 + state.view.y };
  }

  // ------------------------------------------------------------ 渲染
  function render() {
    const d = state.data;
    canvas.innerHTML = "";
    const ax = el("g", { id: "axisLayer" }, canvas);
    const wg = el("g", { id: "worldGroup" }, canvas);
    wg.setAttribute("transform", viewTransform());

    const b = d.plate.bounds;
    // 裁剪定义：所有刻度内容限制在盘面内
    const defs = el("defs", {}, wg);
    const cp = el("clipPath", { id: "plateClip" }, defs);
    el("rect", { x: b.umin, y: -b.vmax, width: b.umax - b.umin, height: b.vmax - b.vmin }, cp);

    // 盘面底色
    el("rect", {
      x: b.umin, y: -b.vmax, width: b.umax - b.umin, height: b.vmax - b.vmin,
      fill: "#fffdf8", stroke: "#222", "stroke-width": .5,
    }, wg);

    const clipped = el("g", { "clip-path": "url(#plateClip)" }, wg);

    // 全年包络
    if (state.showEnvelope && d.envelope.length > 2) {
      const pts = d.envelope.map((q) => `${q[0]},${-q[1]}`).join(" ");
      el("polygon", { points: pts, class: "envelope" }, clipped);
    }

    // 节气日期线
    if (state.showDates) {
      for (const c of d.date_curves) {
        if (c.points.length < 2) continue;
        let path = `M${c.points[0][0]},${-c.points[0][1]}`;
        for (const q of c.points.slice(1)) path += `L${q[0]},${-q[1]}`;
        el("path", { d: path, class: "date-line" + (c.solstice ? " solstice" : "") }, clipped);
        for (const lb of c.labels) {
          const inside = lb.uv[0] >= b.umin - 3 && lb.uv[0] <= b.umax + 3 &&
                         lb.uv[1] >= b.vmin - 3 && lb.uv[1] <= b.vmax + 3;
          if (!inside) continue;
          const t = el("text", {
            x: lb.uv[0], y: -lb.uv[1] - 1.2, class: "date-label",
            "text-anchor": "middle",
          }, clipped);
          t.textContent = `${lb.name} ${lb.date}`;
        }
      }
    }

    // 时线
    const linesG = el("g", {}, clipped);
    for (const ray of d.rays) {
      const iuv = ray.inner_uv || [0, 0];
      el("line", {
        x1: iuv[0], y1: -iuv[1], x2: ray.edge_uv[0], y2: -ray.edge_uv[1],
        class: "hour-line" + (ray.major ? " major" : ""),
      }, linesG);
      if (ray.major && ray.label) {
        const ru = iuv[0] + (ray.edge_uv[0] - iuv[0]) * 0.62;
        const rv = iuv[1] + (ray.edge_uv[1] - iuv[1]) * 0.62;
        if (ru >= b.umin && ru <= b.umax && rv >= b.vmin && rv <= b.vmax) {
          const t = el("text", { x: ru, y: -rv + 1.2, class: "hour-label" }, linesG);
          t.textContent = ray.label;
        }
      }
    }

    // 副法线方向（晷针投影）+ 根点（根点可能相对盘心偏移）
    const g = d.gnomon;
    const root = g.root_uv || [0, 0];
    const sa = g.substyle_angle_deg * Math.PI / 180;
    el("line", { x1: root[0], y1: -root[1], x2: root[0] + 12 * Math.sin(sa),
      y2: -(root[1] + 12 * Math.cos(sa)), class: "substyle" }, wg);
    el("circle", { cx: root[0], cy: -root[1], r: 1.3, class: "gnomon-root" }, wg);

    // 民用时刻线（预览日期对应的钟面时）
    if (state.showCivil && state.preview) {
      for (const cr of state.preview.civil_rays) {
        const th = cr.angle_deg * Math.PI / 180;
        // 从盘心外一点开始（避免与真太阳时线重叠无法分辨）
        el("line", {
          x1: 3 * Math.sin(th), y1: -3 * Math.cos(th),
          x2: cr.edge_uv[0], y2: -cr.edge_uv[1], class: "civil-line",
        }, clipped);
        if (cr.major) {
          const t = el("text", {
            x: cr.edge_uv[0] * 0.88, y: -cr.edge_uv[1] * 0.88 + 1,
            class: "civil-label",
          }, clipped);
          t.textContent = cr.label;
        }
      }
    }

    // 影端预览（越界时仍要显示：放在裁剪层外）
    renderPreviewShadow(wg, clipped);

    // 校正后盘面预览（虚线时线 + 校正根点）
    if (cal.showCorrected && cal.corrected) {
      const g2 = el("g", {}, clipped);
      for (const ray of cal.corrected.rays) {
        const iuv = ray.inner_uv || [0, 0];
        el("line", {
          x1: iuv[0], y1: -iuv[1], x2: ray.edge_uv[0], y2: -ray.edge_uv[1],
          class: "cal-line" + (ray.major ? " major" : ""),
        }, g2);
      }
      const rt = cal.corrected.gnomon.root_uv || [0, 0];
      el("circle", { cx: rt[0], cy: -rt[1], r: 1.8, class: "cal-root" }, wg);
    }

    // 校准观测叠加：实测点、预测点、偏差箭头
    if (cal.show && cal.result && cal.result.obs) {
      const g3 = el("g", {}, wg);
      for (const o of cal.result.obs) {
        if (o.u === null || o.u === undefined) continue;
        el("circle", { cx: o.u, cy: -o.v, r: 1.8,
          class: "obs-meas" + (o.valid ? "" : " invalid") }, g3);
        if (o.pred) {
          el("circle", { cx: o.pred[0], cy: -o.pred[1], r: 2.6, class: "obs-pred" }, g3);
          drawArrow(g3, o.pred[0], o.pred[1], o.u, o.v);
        } else {
          // 背侧/地平线下等无预测：画 ×
          el("line", { x1: o.u - 2, y1: -(o.v - 2), x2: o.u + 2, y2: -(o.v + 2),
            class: "obs-cross" }, g3);
          el("line", { x1: o.u - 2, y1: -(o.v + 2), x2: o.u + 2, y2: -(o.v - 2),
            class: "obs-cross" }, g3);
        }
      }
    }

    // 警告定位标记
    if (state.marker) drawMarker(wg, state.marker);

    // 尺寸标注（四角坐标小字）
    const dim = el("text", { x: b.umax, y: -b.vmin + 6, class: "axis-label",
      "text-anchor": "end" }, wg);
    dim.textContent = `${d.meta.width} × ${d.meta.height} mm`;

    updateAxis();
  }

  function renderPreviewShadow(wg, clipped) {
    const pv = state.preview;
    if (!pv) return;
    if (pv.state === "ok" && pv.shadow_uv) {
      const [u, v] = pv.shadow_uv;
      const root = (state.data && state.data.gnomon.root_uv) || [0, 0];
      el("line", { x1: root[0], y1: -root[1], x2: u, y2: -v, class: "shadow-line" }, clipped || wg);
      el("circle", { cx: u, cy: -v, r: 2.4, class: "shadow-dot" }, wg);
    }
  }

  // 偏差箭头：预测点 -> 实测点（uv 坐标，y 轴取负）
  function drawArrow(g, u1, v1, u2, v2) {
    const dx = u2 - u1, dy = v2 - v1;
    const len = Math.hypot(dx, dy);
    if (len < 0.3) return;
    el("line", { x1: u1, y1: -v1, x2: u2, y2: -v2, class: "obs-arrow" }, g);
    const ux = dx / len, uy = dy / len, s = 1.8;
    const bx = u2 - ux * s, by = v2 - uy * s; // 箭头底点
    const px = -uy, py = ux;                  // 垂直方向
    const pts = `${u2},${-v2} ${bx + px * s * 0.45},${-(by + py * s * 0.45)} ` +
                `${bx - px * s * 0.45},${-(by - py * s * 0.45)}`;
    el("polygon", { points: pts, class: "obs-arrow-head" }, g);
  }

  function drawMarker(wg, m) {
    el("circle", { cx: m.x, cy: -m.y, r: 9, class: "warn-marker-pulse" }, wg);
    el("circle", { cx: m.x, cy: -m.y, r: 5, class: "warn-marker" }, wg);
    el("line", { x1: m.x - 7, y1: -m.y, x2: m.x + 7, y2: -m.y,
      stroke: "#c0392b", "stroke-width": .6 }, wg);
    el("line", { x1: m.x, y1: -m.y - 7, x2: m.x, y2: -m.y + 7,
      stroke: "#c0392b", "stroke-width": .6 }, wg);
  }

  // ------------------------------------------------------------ 警告面板
  function renderWarnings() {
    const ul = $("warnings");
    ul.innerHTML = "";
    const ws = state.data.warnings;
    const n = ws.length;
    const badge = $("warnCount");
    badge.textContent = n;
    badge.className = "badge" + (n ? "" : " zero");
    for (const w of ws) {
      const li = document.createElement("li");
      li.className = w.sev;
      li.textContent = w.msg;
      if (w.x !== undefined) {
        const loc = document.createElement("span");
        loc.className = "loc";
        loc.textContent = `点击定位：盘面坐标 (${Math.round(w.x)}, ${Math.round(w.y)}) mm`;
        li.appendChild(loc);
        li.addEventListener("click", () => locatePoint(w.x, w.y));
      }
      ul.appendChild(li);
    }
  }

  function locatePoint(x, y) {
    state.marker = { x, y };
    const r = canvas.getBoundingClientRect();
    state.view.k = 2.2;
    state.view.x = r.width / 2 - x * state.view.k;
    state.view.y = r.height / 2 + y * state.view.k;
    render();
    setTimeout(() => { state.marker = null; render(); }, 4000);
  }

  // ------------------------------------------------------------ 信息栏
  function renderInfo() {
    const d = state.data; const g = d.gnomon;
    $("installInfo").innerHTML = `
      晷针与盘面夹角：<b>${g.style_angle_deg.toFixed(2)}°</b><br>
      晷针仰角（相对水平面）：${g.polar_elev_deg.toFixed(2)}°<br>
      晷针方位（南起西正）：${g.polar_az_south_deg.toFixed(2)}°<br>
      副法线角（自盘面上方向右）：${g.substyle_angle_deg.toFixed(2)}°<br>
      晷针长度 L：${d.meta.style_len} mm`;
    // EOT 小图
    drawEotChart(d.eot);
    $("eotInfo").innerHTML =
      `经度修正：<b>${d.eot.longitude_correction_min >= 0 ? "+" : ""}${d.eot.longitude_correction_min.toFixed(1)}</b> 分<br>` +
      `均时差：${d.eot.min.value.toFixed(1)}（${d.eot.min.date}）～ ${d.eot.max.value.toFixed(1)} 分（${d.eot.max.date}）<br>` +
      `夏令时偏移：${d.meta.dst} 小时`;
  }

  function drawEotChart(eot) {
    const svg = $("eotChart");
    svg.innerHTML = "";
    const W = 260, H = 90, padL = 26, padR = 6, padT = 8, padB = 16;
    const vals = eot.curve.map((q) => q.min);
    const vmin = Math.floor(Math.min(-1, ...vals) / 5) * 5;
    const vmax = Math.ceil(Math.max(1, ...vals) / 5) * 5;
    const X = (i) => padL + i / (eot.curve.length - 1) * (W - padL - padR);
    const Y = (v) => padT + (1 - (v - vmin) / (vmax - vmin)) * (H - padT - padB);
    el("line", { x1: padL, y1: Y(0), x2: W - padR, y2: Y(0),
      stroke: "#bbb", "stroke-width": .6 }, svg);
    let path = "";
    eot.curve.forEach((q, i) => { path += (i ? "L" : "M") + X(i) + "," + Y(q.min); });
    el("path", { d: path, fill: "none", stroke: "#9a5a1c", "stroke-width": 1.4 }, svg);
    for (const v of [vmin, 0, vmax]) {
      const t = el("text", { x: 4, y: Y(v) + 3, "font-size": 8, fill: "#888" }, svg);
      t.textContent = v;
    }
    for (const [i, lab] of [[0, "1月"], [82 / 10, ""], [Math.floor(eot.curve.length / 2), "7月"],
                            [eot.curve.length - 1, "12月"]]) {
      if (!lab) continue;
      const t = el("text", { x: X(i), y: H - 4, "font-size": 8, fill: "#888",
        "text-anchor": "middle" }, svg);
      t.textContent = lab;
    }
    const ti = el("text", { x: W / 2, y: 8, "font-size": 8, fill: "#999",
      "text-anchor": "middle" }, svg);
    ti.textContent = "均时差（真太阳时 − 平太阳时，分钟）";
  }

  // ------------------------------------------------------------ 预览
  function schedulePreview(immediate) {
    clearTimeout(state.previewTimer);
    const wait = immediate ? 0 : 120;
    state.previewTimer = setTimeout(doPreview, wait);
  }

  async function doPreview() {
    if (!state.data) return;
    const d = doyToDate(state.year, state.doy);
    const body = {
      params: state.params, mode: state.mode,
      year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate(),
      hour: state.minute / 60,
    };
    const res = await postJSON("/api/preview", body);
    if (!res.ok) return;
    state.preview = res.data;
    updatePreviewUI();
    render();
  }

  function updatePreviewUI() {
    const d = doyToDate(state.year, state.doy);
    $("dateLabel").textContent = `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
    $("timeLabel").textContent = fmtClock(state.minute);
    const pv = state.preview;
    if (!pv) return;
    let extra = "";
    if (pv.state === "below_horizon")
      extra = `<span style="color:#c0392b">太阳位于地平线下，盘面无日影。</span>`;
    else if (pv.state === "backside")
      extra = `<span style="color:#c0392b">太阳位于盘面背侧，该面不受光。</span>`;
    else {
      const inb = pv.in_bounds
        ? '<span style="color:#3b7d52">影端在盘面内</span>'
        : '<span style="color:#c0392b">影线越出盘面边界</span>';
      extra = `影端 (${pv.shadow_uv[0]}, ${pv.shadow_uv[1]}) mm — ${inb}`;
    }
    $("previewInfo").innerHTML = `
      真太阳时 <b>${pv.tst}</b> ／ 民用时 ${pv.civil}（${pv.civil_date.slice(5)}）<br>
      太阳高度 ${pv.alt_deg.toFixed(1)}°，方位 ${pv.az_south_deg.toFixed(1)}°（南起西正）<br>
      时角 ${pv.hour_angle_deg.toFixed(1)}°，赤纬 ${pv.decl_deg.toFixed(2)}°<br>
      ${extra}`;
  }

  // ------------------------------------------------------------ 交互绑定
  for (const k of ["lat", "lng", "tz", "dst", "az", "inc", "width", "height",
    "style_len", "min_spacing", "root_du", "root_dv", "hour_step", "year"]) {
    $(k).addEventListener("change", scheduleCompute);
    if ($(k).type === "number") $(k).addEventListener("input", scheduleCompute);
  }
  $("hour_step").addEventListener("change", scheduleCompute);
  $("year").addEventListener("change", () => {
    state.year = parseInt($("year").value, 10);
    scheduleCompute();
    schedulePreview(true);
  });

  document.querySelectorAll(".presets button").forEach((b) =>
    b.addEventListener("click", () => {
      const set = PRESETS[b.dataset.preset];
      if (b.dataset.preset === "equatorial") set.inc = 90 - parseFloat($("lat").value || 0);
      writeInputs(Object.assign(readParams(), set));
      scheduleCompute();
    }));

  $("modeSolar").addEventListener("click", () => setMode("solar"));
  $("modeCivil").addEventListener("click", () => setMode("civil"));
  function setMode(m) {
    state.mode = m;
    $("modeSolar").classList.toggle("active", m === "solar");
    $("modeCivil").classList.toggle("active", m === "civil");
    schedulePreview(true);
  }

  $("dateSlider").addEventListener("input", (e) => {
    state.doy = parseInt(e.target.value, 10);
    schedulePreview();
  });
  $("timeSlider").addEventListener("input", (e) => {
    state.minute = parseInt(e.target.value, 10);
    schedulePreview();
  });
  // 点击画布拖时间（水平拖时间、竖直拖日期）；取点模式下点击填入影端坐标
  let dragTime = null;
  canvas.addEventListener("pointerdown", (e) => {
    if (cal.pick) {
      const r = canvas.getBoundingClientRect();
      const w = screenToWorld(e.clientX - r.left, e.clientY - r.top);
      $("obsU").value = w.x.toFixed(1);
      $("obsV").value = w.y.toFixed(1);
      setPick(false);
      return;
    }
    if (e.target === canvas || e.target.tagName === "line" ||
        e.target.classList.contains("gridline")) {
      dragTime = { x: e.clientX, y: e.clientY, doy: state.doy, min: state.minute, pan: true };
      canvas.setPointerCapture(e.pointerId);
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dragTime || !dragTime.pan) return;
    const dx = e.clientX - dragTime.x;
    if (Math.abs(e.clientY - dragTime.y) > Math.abs(dx)) {
      // 竖直拖动 = 日期
      state.doy = Math.max(0, Math.min(364, dragTime.doy - Math.round((e.clientY - dragTime.y) / 1.5)));
      $("dateSlider").value = state.doy;
    } else {
      // 水平拖动 = 时间
      state.minute = Math.max(0, Math.min(1439, dragTime.min + Math.round(dx / 2)));
      $("timeSlider").value = state.minute;
    }
    schedulePreview();
  });
  canvas.addEventListener("pointerup", () => { dragTime = null; });
  canvas.addEventListener("pointercancel", () => { dragTime = null; });

  // 滚轮缩放 + 中键/右键平移（左键拖时预览）
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });

  // 空格拖动画布
  let panning = null;
  window.addEventListener("keydown", (e) => { if (e.code === "Space") canvas.style.cursor = "move"; });
  window.addEventListener("keyup", () => { canvas.style.cursor = "grab"; });
  canvas.addEventListener("mousedown", (e) => {
    if (e.shiftKey) {
      panning = { x: e.clientX, y: e.clientY, vx: state.view.x, vy: state.view.y };
      e.preventDefault();
    }
  });
  window.addEventListener("mousemove", (e) => {
    if (!panning) return;
    state.view.x = panning.vx + (e.clientX - panning.x);
    state.view.y = panning.vy + (e.clientY - panning.y);
    $("worldGroup").setAttribute("transform", viewTransform());
    updateAxis();
  });
  window.addEventListener("mouseup", () => { panning = null; });

  $("zoomIn").addEventListener("click", () => {
    const r = canvas.getBoundingClientRect(); zoomAt(1.25, r.width / 2, r.height / 2);
  });
  $("zoomOut").addEventListener("click", () => {
    const r = canvas.getBoundingClientRect(); zoomAt(1 / 1.25, r.width / 2, r.height / 2);
  });
  $("zoomFit").addEventListener("click", () => fitView(true));
  $("toggleEnvelope").addEventListener("click", (e) => {
    state.showEnvelope = !state.showEnvelope;
    e.target.classList.toggle("active", state.showEnvelope);
    render();
  });
  $("toggleDates").addEventListener("click", (e) => {
    state.showDates = !state.showDates;
    e.target.classList.toggle("active", state.showDates);
    render();
  });
  $("toggleCivil").addEventListener("click", (e) => {
    state.showCivil = !state.showCivil;
    e.target.classList.toggle("active", state.showCivil);
    schedulePreview(true);
  });

  // ------------------------------------------------------------ 版本管理
  async function refreshDesigns(selectedId) {
    const res = await fetch("/api/designs").then((r) => r.json());
    const sel = $("designSelect");
    sel.innerHTML = "";
    for (const d of res.designs) {
      const o = document.createElement("option");
      o.value = d.id;
      const tag = d.origin === "calibration" ? "·校准" : "";
      o.textContent = `${d.name}${tag}（${new Date(d.updated_at * 1000).toLocaleDateString()}）`;
      sel.appendChild(o);
    }
    if (selectedId) sel.value = selectedId;
  }

  $("saveBtn").addEventListener("click", async () => {
    const name = $("designName").value.trim();
    if (!name) { alert("请先填写版本名称"); return; }
    const res = await postJSON("/api/designs", { name, params: state.params });
    if (!res.ok) { alert(res.errors.join("；")); return; }
    $("designName").value = "";
    refreshDesigns(res.id);
    calSetDesign(res.id, name);
  });
  $("loadBtn").addEventListener("click", async () => {
    const id = $("designSelect").value;
    if (!id) return;
    const res = await fetch("/api/designs/" + id).then((r) => r.json());
    if (!res.ok) return;
    writeInputs(res.design.params);
    $("designName").value = res.design.name;
    calSetDesign(res.design.id, res.design.name);
    scheduleCompute();
  });
  $("delBtn").addEventListener("click", async () => {
    const id = $("designSelect").value;
    if (!id || !confirm("确认删除该版本？")) return;
    await fetch("/api/designs?id=" + id, { method: "DELETE" });
    if (parseInt(id, 10) === cal.designId) calSetDesign(null);
    refreshDesigns();
  });

  $("diffBtn").addEventListener("click", async () => {
    const opts = [...$("designSelect").options];
    if (opts.length < 2) { alert("请先保存至少两个版本（按住 Ctrl 依次点击选中两个版本进行比较）"); return; }
    const picked = [...$("designSelect").selectedOptions].map((o) => o.value);
    let a, b;
    if (picked.length >= 2) { [a, b] = picked; }
    else {
      const idA = opts[0].value;
      const idB = opts[1].value;
      const inp = prompt(
        `输入要比较的两个版本 ID（用逗号分隔）\n${opts.map((o) => o.value + ": " + o.textContent).join("\n")}`,
        `${idA},${idB}`);
      if (!inp) return;
      [a, b] = inp.split(",").map((s) => s.trim());
    }
    const res = await fetch(`/api/diff?a=${a}&b=${b}`).then((r) => r.json());
    if (!res.ok) { alert(res.errors.join("；")); return; }
    showDiff(res.diff);
  });
  function showDiff(d) {
    $("diffBox").style.display = "";
    let html = `<table class="diff"><tr><th>参数</th><th>${d.a.name}</th><th>${d.b.name}</th></tr>`;
    for (const r of d.rows) {
      html += `<tr class="${r.changed ? "changed" : ""}"><td>${r.label}</td><td>${r.a ?? "—"}</td><td>${r.b ?? "—"}</td></tr>`;
    }
    html += "</table>";

    const g = d.geometry;
    if (g) {
      html += `<h3 style="font-size:12px;color:#6b4a1e;margin:10px 0 4px">晷针安装几何</h3>`;
      html += `<table class="diff"><tr><th>项目</th><th>${d.a.name}</th><th>${d.b.name}</th><th>Δ</th></tr>`;
      for (const r of g.gnomon) {
        html += `<tr class="${r.changed ? "changed" : ""}"><td>${r.label}</td>` +
          `<td>${r.a ?? "—"}</td><td>${r.b ?? "—"}</td><td>${r.delta > 0 ? "+" : ""}${r.delta}</td></tr>`;
      }
      html += "</table>";

      const s = g.summary;
      html += `<p class="info small" style="margin:6px 0">时线 ${s.ray_count_a} → ${s.ray_count_b} 条；` +
        `最大方向角差 <b>${s.max_angle_diff_deg.toFixed(2)}°</b>，` +
        `盘边端点最大位移 <b>${s.max_edge_diff_mm.toFixed(1)} mm</b>，` +
        `节气线最大形偏 <b>${s.max_curve_diff_mm.toFixed(1)} mm</b></p>`;

      html += `<h3 style="font-size:12px;color:#6b4a1e;margin:8px 0 4px">时线刻线差异（真太阳时）</h3>`;
      html += `<table class="diff"><tr><th>时刻</th><th>角A°</th><th>角B°</th><th>Δ角°</th><th>盘边Δ mm</th><th>内端Δ mm</th></tr>`;
      for (const r of g.rays) {
        if (!r.major && !r.changed) continue;
        const mark = (r.present_a && !r.present_b) ? "（B 无此线）"
          : (!r.present_a && r.present_b) ? "（A 无此线）" : "";
        html += `<tr class="${r.changed ? "changed" : ""}"><td>${r.label}${mark}</td>` +
          `<td>${r.a ? r.a.angle.toFixed(2) : "—"}</td>` +
          `<td>${r.b ? r.b.angle.toFixed(2) : "—"}</td>` +
          `<td>${r.d_angle === null ? "—" : (r.d_angle > 0 ? "+" : "") + r.d_angle.toFixed(2)}</td>` +
          `<td>${r.d_edge === null ? "—" : r.d_edge.toFixed(1)}</td>` +
          `<td>${r.d_inner === null ? "—" : r.d_inner.toFixed(1)}</td></tr>`;
      }
      html += "</table>";

      html += `<h3 style="font-size:12px;color:#6b4a1e;margin:8px 0 4px">节气日期线差异</h3>`;
      html += `<table class="diff"><tr><th>节气线</th><th>起点Δ mm</th><th>终点Δ mm</th><th>最大形偏 mm</th></tr>`;
      for (const r of g.dates) {
        if (!r.changed) continue;
        const mark = (r.present_a && !r.present_b) ? "（B 无此线）"
          : (!r.present_a && r.present_b) ? "（A 无此线）" : "";
        html += `<tr class="changed"><td>${r.label}${mark}</td>` +
          `<td>${r.d_start === null ? "—" : r.d_start.toFixed(1)}</td>` +
          `<td>${r.d_end === null ? "—" : r.d_end.toFixed(1)}</td>` +
          `<td>${r.d_max === null ? "—" : r.d_max.toFixed(1)}</td></tr>`;
      }
      const changedDates = g.dates.filter((r) => r.changed).length;
      if (!changedDates) html += `<tr><td colspan="4" style="text-align:left;color:#3b7d52">全部节气线位置一致（&lt;0.5 mm）</td></tr>`;
      html += "</table>";
    }
    $("diffContent").innerHTML = html;
  }
  $("diffClose").addEventListener("click", () => { $("diffBox").style.display = "none"; });

  // ------------------------------------------------------------ 导出
  $("exportBtn").addEventListener("click", async () => {
    const resp = await fetch("/api/export.svg", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.params),
    });
    if (!resp.ok) {
      const j = await resp.json().catch(() => ({}));
      alert((j.errors || ["导出失败"]).join("；"));
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `sundial_${state.params.lat}_${state.params.inc}inc.svg`;
    a.click();
    URL.revokeObjectURL(url);
  });

  // ------------------------------------------------------------ 现场校准
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }
  function fmtN(v) {
    if (v === null || v === undefined) return "—";
    return String(Math.round(v * 1000) / 1000);
  }
  function localNow() {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // 载入/保存/删除设计版本后更新校准上下文
  function calSetDesign(id, name) {
    cal.designId = id;
    cal.designName = name || "";
    cal.batch = null;
    cal.result = null;
    cal.corrected = null;
    cal.showCorrected = false;
    setPick(false);
    $("toggleCorrected").classList.remove("active");
    $("calNoDesign").style.display = id ? "none" : "";
    $("calMain").style.display = id ? "" : "none";
    $("calObsBlock").style.display = "none";
    $("calBox").style.display = "none";
    $("calBatchInfo").textContent = "";
    if (id) refreshBatches();
    if (state.data) render();
  }

  async function refreshBatches(selectId) {
    if (!cal.designId) return;
    const res = await fetch("/api/cal/batches?design_id=" + cal.designId).then((r) => r.json());
    const sel = $("calBatchSelect");
    sel.innerHTML = "";
    for (const b of res.batches || []) {
      const o = document.createElement("option");
      o.value = b.id;
      o.textContent = `#${b.id} ${b.name}（${b.n_obs}条${b.status === "applied" ? "·已应用" : ""}）`;
      sel.appendChild(o);
    }
    if (selectId) sel.value = selectId;
    if (sel.value) loadBatch(parseInt(sel.value, 10));
    else {
      cal.batch = null;
      $("calObsBlock").style.display = "none";
      renderBatchInfo();
    }
  }

  async function loadBatch(bid) {
    const res = await fetch("/api/cal/batches/" + bid).then((r) => r.json());
    if (!res.ok) { alert((res.errors || ["载入批次失败"]).join("；")); return; }
    cal.batch = res.batch;
    cal.result = res.batch.result || null;
    cal.corrected = null;
    cal.showCorrected = false;
    $("toggleCorrected").classList.remove("active");
    cal.cfg = res.batch.fit_config ||
      defaultFitCfg(res.batch.design ? res.batch.design.params : state.params);
    $("calObsBlock").style.display = "";
    buildFitCfgUI();
    renderObsList();
    renderBatchInfo();
    if (!$("obsDt").value) $("obsDt").value = localNow();
    if (cal.result) {
      await fetchCorrected();
      showCalResult();
    } else {
      $("calBox").style.display = "none";
    }
    cal.show = true;
    $("toggleCal").classList.add("active");
    render();
  }

  // 仅刷新批次数据（观测增删/有效标记后），保留当前结果展示
  async function reloadBatch() {
    if (!cal.batch) return;
    const res = await fetch("/api/cal/batches/" + cal.batch.id).then((r) => r.json());
    if (!res.ok) return;
    cal.batch = res.batch;
    renderObsList();
    renderBatchInfo();
  }

  function renderBatchInfo() {
    const b = cal.batch;
    if (!b) { $("calBatchInfo").textContent = ""; return; }
    const dname = b.design ? b.design.name : "（版本已删除）";
    let t = `基于版本 #${b.design_id}「${dname}」 · ${b.observations.length} 条观测`;
    if (b.status === "applied") t += ` · 已生成修正版本 #${b.applied_design_id}`;
    $("calBatchInfo").textContent = t;
  }

  function defaultFitCfg(p) {
    const du = p.root_du || 0, dv = p.root_dv || 0;
    return {
      az: { on: true, value: p.az, min: p.az - 5, max: p.az + 5 },
      inc: { on: true, value: p.inc, min: Math.max(0, p.inc - 5), max: Math.min(180, p.inc + 5) },
      style_len: { on: false, value: p.style_len, min: p.style_len * 0.8, max: p.style_len * 1.2 },
      root_du: { on: true, value: du, min: du - 30, max: du + 30 },
      root_dv: { on: true, value: dv, min: dv - 30, max: dv + 30 },
    };
  }

  // ------------------------------------------------------------ 观测记录
  function renderObsList() {
    const ul = $("obsList");
    ul.innerHTML = "";
    const obs = (cal.batch && cal.batch.observations) || [];
    for (const o of obs) {
      const li = document.createElement("li");
      li.className = "obs-item" + (o.valid ? "" : " invalid");
      const txt = document.createElement("span");
      txt.className = "obs-txt";
      txt.textContent = `${String(o.dt_local).replace("T", " ").slice(5)}  (${o.u}, ${o.v})`;
      li.appendChild(txt);
      const lab = document.createElement("label");
      lab.className = "obs-valid";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!o.valid;
      cb.title = "取消勾选即标为无效（异常记录）";
      cb.addEventListener("change", () => toggleObsValid(o.id, cb.checked));
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode("有效"));
      li.appendChild(lab);
      const del = document.createElement("button");
      del.className = "obs-del";
      del.textContent = "✕";
      del.title = "删除该记录";
      del.addEventListener("click", () => deleteObs(o.id));
      li.appendChild(del);
      ul.appendChild(li);
    }
    if (!obs.length) {
      const li = document.createElement("li");
      li.className = "obs-empty";
      li.textContent = "尚无观测记录：填写民用时间与影端 u、v，或点“盘面取点”后在盘面上点击取点。";
      ul.appendChild(li);
    }
  }

  async function toggleObsValid(oid, valid) {
    const res = await fetch("/api/cal/obs/" + oid, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ valid: valid ? 1 : 0 }),
    }).then((r) => r.json());
    if (!res.ok) { alert((res.errors || ["更新失败"]).join("；")); return; }
    await reloadBatch();
    if (cal.result) await runFit();  // 排除/恢复离群点后自动重算
    else if (state.data) render();
  }

  async function deleteObs(oid) {
    if (!confirm("删除该观测记录？")) return;
    await fetch("/api/cal/obs/" + oid, { method: "DELETE" });
    await reloadBatch();
    if (cal.result) await runFit();
  }

  $("obsAdd").addEventListener("click", async () => {
    if (!cal.batch) return;
    const dt = $("obsDt").value;
    const u = parseFloat($("obsU").value);
    const v = parseFloat($("obsV").value);
    if (!dt || Number.isNaN(u) || Number.isNaN(v)) {
      alert("请填写民用日期时间与 u、v 坐标（或用“盘面取点”在盘面上点击）");
      return;
    }
    const res = await postJSON(`/api/cal/batches/${cal.batch.id}/obs`,
      { dt_local: dt, u, v });
    if (!res.ok) { alert((res.errors || ["添加失败"]).join("；")); return; }
    await reloadBatch();
    if (cal.result) await runFit();
  });

  function setPick(on) {
    cal.pick = on;
    $("obsPick").classList.toggle("active", on);
    canvas.style.cursor = on ? "crosshair" : "";
    setStatus(on ? "取点模式：在盘面上点击影端位置，自动填入 u、v" : "");
  }
  $("obsPick").addEventListener("click", () => setPick(!cal.pick));

  // ------------------------------------------------------------ 批次管理
  $("calNewBatch").addEventListener("click", async () => {
    if (!cal.designId) return;
    const name = $("calBatchName").value.trim() ||
      ("批次 " + new Date().toLocaleString("zh-CN",
        { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }));
    const res = await postJSON("/api/cal/batches", { design_id: cal.designId, name });
    if (!res.ok) { alert((res.errors || ["创建失败"]).join("；")); return; }
    $("calBatchName").value = "";
    await refreshBatches(res.id);
  });
  $("calDelBatch").addEventListener("click", async () => {
    if (!cal.batch) return;
    if (!confirm(`删除批次「${cal.batch.name}」及其全部观测记录？`)) return;
    await fetch("/api/cal/batches/" + cal.batch.id, { method: "DELETE" });
    cal.batch = null;
    cal.result = null;
    cal.corrected = null;
    $("calObsBlock").style.display = "none";
    $("calBox").style.display = "none";
    await refreshBatches();
    if (state.data) render();
  });
  $("calBatchSelect").addEventListener("change", () => {
    const id = $("calBatchSelect").value;
    if (id) loadBatch(parseInt(id, 10));
  });

  // ------------------------------------------------------------ 反算配置
  function buildFitCfgUI() {
    const box = $("fitCfg");
    box.innerHTML = "";
    for (const def of FIT_PARAMS) {
      const c = cal.cfg[def.key];
      const row = document.createElement("div");
      row.className = "fitrow";
      const lab = document.createElement("label");
      lab.className = "fitlab";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!c.on;
      cb.id = "fc_on_" + def.key;
      cb.title = "勾选=估算该参数；取消=按锁定值固定";
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(" " + def.label));
      row.appendChild(lab);
      const inputs = {};
      for (const [suffix, prop, ph] of [["min", "min", "搜索下限"], ["max", "max", "搜索上限"], ["val", "value", "锁定值"]]) {
        const inp = document.createElement("input");
        inp.type = "number";
        inp.step = "any";
        inp.id = `fc_${suffix}_${def.key}`;
        inp.value = Math.round(c[prop] * 100) / 100;
        inp.title = `${def.label} ${ph}`;
        inputs[suffix] = inp;
        row.appendChild(inp);
      }
      const sync = () => {
        inputs.min.disabled = !cb.checked;
        inputs.max.disabled = !cb.checked;
        inputs.val.disabled = cb.checked;
      };
      cb.addEventListener("change", sync);
      sync();
      box.appendChild(row);
    }
  }

  function collectFitCfg() {
    for (const def of FIT_PARAMS) {
      const c = cal.cfg[def.key];
      c.on = $("fc_on_" + def.key).checked;
      for (const [suffix, prop] of [["min", "min"], ["max", "max"], ["val", "value"]]) {
        const v = parseFloat($(`fc_${suffix}_${def.key}`).value);
        if (!Number.isNaN(v)) c[prop] = v;
      }
    }
  }

  // ------------------------------------------------------------ 反算与结果
  $("fitRun").addEventListener("click", runFit);

  async function runFit() {
    if (!cal.batch) return;
    collectFitCfg();
    setStatus("反算中…");
    const res = await postJSON(`/api/cal/batches/${cal.batch.id}/fit`, { config: cal.cfg });
    setStatus("");
    if (!res.ok) {
      cal.result = (res.result && res.result.obs) ? res.result : null;
      cal.corrected = null;
      cal.showCorrected = false;
      $("toggleCorrected").classList.remove("active");
      showCalErrors(res);
      if (state.data) render();
      return;
    }
    cal.result = res.result;
    await fetchCorrected();
    showCalResult();
    if (state.data) render();
  }

  // 用校正参数重算盘面，作为“校正后”预览叠加
  async function fetchCorrected() {
    if (!cal.batch || !cal.batch.design || !cal.result) return;
    const p = Object.assign({}, cal.batch.design.params, cal.result.best);
    const res = await postJSON("/api/calculate", p);
    if (res.ok) {
      cal.corrected = res.data;
      cal.showCorrected = true;
      $("toggleCorrected").classList.add("active");
    }
  }

  function showCalErrors(res) {
    $("calBox").style.display = "";
    let html = "";
    for (const e of res.errors || []) html += `<div class="calwarn err">${esc(e)}</div>`;
    const r = res.result;
    if (r && r.warnings) {
      for (const w of r.warnings) html += `<div class="calwarn">${esc(w.msg)}</div>`;
    }
    if (r && r.obs && r.obs.length) {
      html += `<h3 class="sub">逐条观测状态</h3>` + obsTable(r.obs);
    }
    $("calContent").innerHTML = html;
    bindObsRowButtons();
  }

  function showCalResult() {
    const r = cal.result;
    if (!r) return;
    $("calBox").style.display = "";
    let html = `<p class="info small">RMS 残差 <b>${r.rms.toFixed(2)} mm</b> · ` +
      `最大残差 <b>${r.max_res.toFixed(2)} mm</b> · 有效样本 ${r.n_fit}/${r.n_obs} · ` +
      `待估参数 ${r.n_params} · 自由度 ${r.dof}</p>`;
    for (const w of r.warnings || []) html += `<div class="calwarn">${esc(w.msg)}</div>`;

    html += `<h3 class="sub">参数反算结果</h3>` +
      `<table class="diff"><tr><th>参数</th><th>原值</th><th>校正值</th><th>修正量</th><th>来源</th></tr>`;
    for (const def of FIT_PARAMS) {
      const k = def.key;
      const est = r.config[k].on;
      const corr = r.corrections[k];
      html += `<tr class="${est && Math.abs(corr) > 1e-9 ? "changed" : ""}">` +
        `<td>${def.label}</td><td>${fmtN(r.base[k])}</td><td>${fmtN(r.best[k])}</td>` +
        `<td>${corr > 0 ? "+" : ""}${fmtN(corr)}</td><td>${est ? "估算" : "锁定"}</td></tr>`;
    }
    html += "</table>";

    html += `<h3 class="sub">参数可辨识性</h3>` +
      `<table class="diff"><tr><th>参数</th><th>灵敏度</th><th>辨识半径</th><th>1σ</th><th>判定</th></tr>`;
    for (const p of r.ident.params) {
      if (!p.estimated) {
        html += `<tr><td>${p.label}</td><td colspan="3">锁定于 ${fmtN(p.locked_value)}</td><td>—</td></tr>`;
      } else {
        html += `<tr class="${p.weak ? "changed" : ""}"><td>${p.label}</td>` +
          `<td>${p.sensitivity === null ? "—" : p.sensitivity.toFixed(3)}</td>` +
          `<td>${p.resolve === null ? "—" : fmtN(p.resolve) + p.unit}</td>` +
          `<td>${p.std === null ? "—" : "±" + fmtN(p.std)}</td>` +
          `<td>${p.weak ? "弱" : "可辨识"}</td></tr>`;
      }
    }
    html += "</table>";
    if (r.ident.pairs && r.ident.pairs.length) {
      const top = r.ident.pairs[0];
      html += `<p class="info small">最大参数相关：${esc(top.label)} ` +
        `${top.corr >= 0 ? "+" : ""}${top.corr.toFixed(3)}（|相关| ≥ 0.95 判为近似等价解）</p>`;
    }

    html += `<h3 class="sub">逐条观测与残差</h3>` + obsTable(r.obs);
    html += `<div class="row2" style="margin-top:6px">` +
      `<button id="calAutoPrune">剔除 &gt;3σ 离群点重算</button>` +
      `<button id="calRefit">按当前设置重算</button></div>`;
    $("calContent").innerHTML = html;
    bindObsRowButtons();
    $("calAutoPrune").addEventListener("click", autoPrune);
    $("calRefit").addEventListener("click", runFit);
  }

  function stateLabel(st) {
    return { backside: "盘面背侧", below_horizon: "地平线下", bad_input: "输入无效" }[st] || st;
  }

  function obsTable(obs) {
    let html = `<table class="diff obs"><tr><th>时间</th><th>实测 u,v</th>` +
      `<th>预测 u,v</th><th>Δu,Δv</th><th>|r|</th><th>状态</th><th></th></tr>`;
    for (const o of obs) {
      const st = o.valid ? (o.state === "ok" ? "" : stateLabel(o.state)) : "已排除";
      const meas = (o.u === null || o.u === undefined) ? "—" : `${o.u.toFixed(1)}, ${o.v.toFixed(1)}`;
      const pred = o.pred ? `${o.pred[0].toFixed(1)}, ${o.pred[1].toFixed(1)}` : "—";
      const resv = o.res ? `${o.res[0].toFixed(1)}, ${o.res[1].toFixed(1)}` : "—";
      const rl = o.res_len === null || o.res_len === undefined ? "—" : o.res_len.toFixed(1);
      const btn = o.valid
        ? `<button data-oid="${o.id}" data-valid="0">剔除</button>`
        : `<button data-oid="${o.id}" data-valid="1">恢复</button>`;
      html += `<tr class="${o.valid ? "" : "invalid"}">` +
        `<td>${esc(String(o.dt_local).replace("T", " ").slice(5))}</td>` +
        `<td>${meas}</td><td>${pred}</td>` +
        `<td>${resv}</td><td>${rl}</td><td>${st}</td><td>${btn}</td></tr>`;
    }
    return html + "</table>";
  }

  function bindObsRowButtons() {
    $("calContent").querySelectorAll("button[data-oid]").forEach((b) =>
      b.addEventListener("click", () =>
        toggleObsValid(parseInt(b.dataset.oid, 10), b.dataset.valid === "1")));
  }

  // 自动剔除 |残差| > 3σ 的离群点并重算
  async function autoPrune() {
    const r = cal.result;
    if (!r) return;
    const thresh = 3 * r.rms;
    const targets = r.obs.filter((o) =>
      o.valid && o.state === "ok" && o.res_len !== null && o.res_len > thresh);
    if (!targets.length) {
      alert(`没有 |残差| > 3σ（${thresh.toFixed(1)} mm）的观测`);
      return;
    }
    if (!confirm(`将 ${targets.length} 条 |残差| > 3σ 的观测标为无效并重算？`)) return;
    for (const o of targets) {
      await fetch("/api/cal/obs/" + o.id, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ valid: 0 }),
      });
    }
    await reloadBatch();
    await runFit();
  }

  // ------------------------------------------------------------ 应用与报告
  $("calApply").addEventListener("click", async () => {
    if (!cal.batch || !cal.result) { alert("请先成功执行一次反算"); return; }
    const dname = cal.batch.design ? cal.batch.design.name : "";
    const name = prompt("修正版本名称（保存为关联原版本的新设计，原版本与观测均保留）：",
      dname + "·校准");
    if (name === null) return;
    const res = await postJSON(`/api/cal/batches/${cal.batch.id}/apply`,
      { name: name.trim() });
    if (!res.ok) { alert((res.errors || ["保存失败"]).join("；")); return; }
    alert(`已保存修正版本 #${res.id}「${res.name}」。\n${res.note || ""}`);
    await refreshDesigns(res.id);
    await reloadBatch();
  });

  $("calReport").addEventListener("click", async () => {
    if (!cal.batch) return;
    const r = await fetch(`/api/cal/batches/${cal.batch.id}/report`);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      alert((j.errors || ["导出失败"]).join("；"));
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `calibration_report_b${cal.batch.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  });

  $("calClose").addEventListener("click", () => { $("calBox").style.display = "none"; });

  $("toggleCal").addEventListener("click", (e) => {
    cal.show = !cal.show;
    e.target.classList.toggle("active", cal.show);
    if (state.data) render();
  });
  $("toggleCorrected").addEventListener("click", (e) => {
    if (!cal.corrected && !cal.showCorrected) {
      alert("尚无校正结果：请先执行反算");
      return;
    }
    cal.showCorrected = !cal.showCorrected;
    e.target.classList.toggle("active", cal.showCorrected);
    if (state.data) render();
  });

  // ------------------------------------------------------------ 初始化
  writeInputs(state.params);
  $("dateSlider").value = state.doy;
  $("timeSlider").value = state.minute;
  window.addEventListener("resize", () => {
    const wg = $("worldGroup");
    if (wg) wg.setAttribute("transform", viewTransform());
    updateAxis();
  });
  refreshDesigns();
  doCompute();
})();

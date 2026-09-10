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

  function defaults() {
    return { lat: 39.9, lng: 116.4, tz: 8, dst: 0, az: 180, inc: 0,
      width: 300, height: 300, style_len: 80, min_spacing: 2,
      hour_step: 60, year: 2026 };
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
      "style_len", "min_spacing"]) {
      p[k] = parseFloat($(k).value);
    }
    p.hour_step = parseInt($("hour_step").value, 10);
    p.year = parseInt($("year").value, 10);
    return p;
  }
  function writeInputs(p) {
    for (const k of ["lat", "lng", "tz", "dst", "az", "inc", "width", "height",
      "style_len", "min_spacing", "year"]) {
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

    // 副法线方向（晷针投影）+ 根点
    const g = d.gnomon;
    const sa = g.substyle_angle_deg * Math.PI / 180;
    el("line", { x1: 0, y1: 0, x2: 12 * Math.sin(sa), y2: -12 * Math.cos(sa),
      class: "substyle" }, wg);
    el("circle", { cx: 0, cy: 0, r: 1.3, class: "gnomon-root" }, wg);

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
      el("line", { x1: 0, y1: 0, x2: u, y2: -v, class: "shadow-line" }, clipped || wg);
      el("circle", { cx: u, cy: -v, r: 2.4, class: "shadow-dot" }, wg);
    }
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
    "style_len", "min_spacing", "hour_step", "year"]) {
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
  // 点击画布拖时间（水平拖时间、竖直拖日期）
  let dragTime = null;
  canvas.addEventListener("pointerdown", (e) => {
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
      o.textContent = `${d.name}（${new Date(d.updated_at * 1000).toLocaleDateString()}）`;
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
  });
  $("loadBtn").addEventListener("click", async () => {
    const id = $("designSelect").value;
    if (!id) return;
    const res = await fetch("/api/designs/" + id).then((r) => r.json());
    if (!res.ok) return;
    writeInputs(res.design.params);
    $("designName").value = res.design.name;
    scheduleCompute();
  });
  $("delBtn").addEventListener("click", async () => {
    const id = $("designSelect").value;
    if (!id || !confirm("确认删除该版本？")) return;
    await fetch("/api/designs?id=" + id, { method: "DELETE" });
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

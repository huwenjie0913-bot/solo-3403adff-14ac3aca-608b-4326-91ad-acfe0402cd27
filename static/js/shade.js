/* 安装点遮光与可读时段分析 — 前端模块（原生 JS + SVG）。
 * 依赖 app.js 暴露的 window.Sundial（参数读写/计算数据/网络工具）。
 */
(function () {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);

  // 状态色
  const STATE_COLORS = {
    read: "#3b7d52", block: "#c0392b", out: "#d07a1e",
    back: "#7d5fb8", below: "#9aa0a8",
  };
  const STATE_LABELS = {
    read: "可读", block: "轮廓遮挡", out: "影端越界",
    back: "盘面背侧", below: "地平线下",
  };
  const PROF_COLORS = ["#b3541e", "#2c5d8c", "#6b4a8a", "#1f6f4a",
                       "#a07a1c", "#8a4a6b", "#466a8a"];

  const sky = {
    designId: null,
    mounts: [],
    current: null,        // 正在编辑的安装点（本地草稿，含 id 表示已保存）
    profIndex: 0,
    arcs: null,           // 每月太阳弧线底图
    dayData: null,
    instant: null,
    analysis: null,       // 当前安装点全年分析
    analysisMountId: null,
    cmp: null,
    doy: 171,
    minute: 12 * 60,
    dirty: false,
    drawMode: true,       // 点击空白添加样点
  };

  function el(tag, attrs, parent) {
    const e = document.createElementNS(SVGNS, tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function doyToDate(year, doy) {
    const d = new Date(year, 0, 1);
    d.setDate(d.getDate() + doy);
    return d;
  }
  function fmtHM(hours) {
    const t = Math.round(hours * 60) % 1440;
    return pad(Math.floor(t / 60)) + ":" + pad(t % 60);
  }
  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json();
  }

  // ------------------------------------------------------------ 与主模块的接口
  // app.js 提供：getParams()、getYear()、designId 变化时调用 mountSetDesign
  const App = window.Sundial || {};

  // ------------------------------------------------------------ 安装点管理
  function setDesign(id) {
    sky.designId = id;
    sky.current = null;
    sky.analysis = null;
    $("mountNoDesign").style.display = id ? "none" : "";
    $("mountMain").style.display = id ? "" : "none";
    $("viewSkyBtn").disabled = !id;
    if (id) refreshMounts();
    else {
      $("mountSelect").innerHTML = "";
      sky.mounts = [];
    }
  }

  async function refreshMounts(selectId) {
    if (!sky.designId) return;
    const res = await fetch(`/api/designs/${sky.designId}/mounts`).then((r) => r.json());
    sky.mounts = res.mounts || [];
    const sel = $("mountSelect");
    sel.innerHTML = "";
    for (const m of sky.mounts) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = `${m.name}（${m.profiles.length}条轮廓）`;
      sel.appendChild(o);
    }
    fillCmpSelects();
    if (selectId) sel.value = selectId;
  }

  function draftMount() {
    return {
      name: $("mountName").value.trim(),
      note: sky.current ? sky.current.note || "" : "",
      az_ref: $("azRef").value,
      profiles: sky.current ? sky.current.profiles : [],
    };
  }

  $("mountNew").addEventListener("click", () => {
    sky.current = {
      name: "", note: "", az_ref: "south",
      profiles: [newProfile("轮廓1")],
    };
    sky.profIndex = 0;
    sky.analysis = null;
    $("mountName").value = "";
    syncProfEditor();
    markDirty(false);
    openSkyView();
  });

  $("mountSelect").addEventListener("change", () => {
    const id = parseInt($("mountSelect").value, 10);
    const m = sky.mounts.find((x) => x.id === id);
    if (m) loadMount(m);
  });

  async function loadMount(m) {
    const res = await fetch("/api/mounts/" + m.id).then((r) => r.json());
    if (!res.ok) { alert((res.errors || ["载入失败"]).join("；")); return; }
    const mm = res.mount;
    sky.current = {
      id: mm.id, name: mm.name, note: mm.note || "",
      az_ref: mm.az_ref || "south",
      profiles: mm.profiles.length ? mm.profiles : [newProfile("轮廓1")],
    };
    sky.profIndex = 0;
    sky.analysis = null;
    $("mountName").value = mm.name;
    syncProfEditor();
    markDirty(false);
    renderMountInfo();
    if (isSkyOpen()) renderSky();
  }

  $("mountDup").addEventListener("click", async () => {
    const id = parseInt($("mountSelect").value, 10);
    if (!id) { alert("请先选择一个安装点"); return; }
    const res = await postJSON(`/api/mounts/${id}/duplicate`, {});
    if (!res.ok) { alert((res.errors || ["复制失败"]).join("；")); return; }
    await refreshMounts(res.id);
    loadMount(sky.mounts.find((m) => m.id === res.id));
  });

  $("mountDel").addEventListener("click", async () => {
    const id = parseInt($("mountSelect").value, 10);
    if (!id || !confirm("删除该安装点及其全部轮廓？")) return;
    await fetch("/api/mounts/" + id, { method: "DELETE" });
    if (sky.current && sky.current.id === id) sky.current = null;
    await refreshMounts();
  });

  $("mountSave").addEventListener("click", saveCurrent);

  async function saveCurrent() {
    if (!sky.designId || !sky.current) { alert("请先新建或选择一个安装点"); return null; }
    const payload = draftMount();
    if (!payload.name) { alert("请填写安装点名称"); return null; }
    let url, method;
    if (sky.current.id) {
      url = "/api/mounts/" + sky.current.id; method = "PUT";
    } else {
      url = `//api/designs/${sky.designId}/mounts`; method = "POST";
    }
    const r = await fetch(url, {
      method, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((x) => x.json());
    if (!r.ok) { alert((r.errors || ["保存失败"]).join("；")); return null; }
    sky.current.id = r.id;
    sky.current.name = payload.name;
    sky.current.az_ref = payload.az_ref;
    markDirty(false);
    await refreshMounts(r.id);
    renderMountInfo();
    return r.id;
  }

  // ---- 导入 / 导出
  $("mountExport").addEventListener("click", async () => {
    let data;
    if (sky.current && sky.dirty) {
      if (!confirm("当前编辑尚未保存，导出未保存的草稿？")) return;
      data = { format: "sundial-mounts", version: 1,
               design_name: App.getDesignName ? App.getDesignName() : "",
               mounts: [stripLocal(draftMount())] };
    } else {
      const id = parseInt($("mountSelect").value, 10);
      if (!id) { alert("请先选择一个安装点"); return; }
      const res = await fetch("/api/mounts/" + id).then((r) => r.json());
      if (!res.ok) return;
      data = { format: "sundial-mounts", version: 1, mounts: [stripLocal(res.mount)] };
    }
    downloadJSON(data, `mounts_${(data.mounts[0].name || "mount")}.json`);
  });

  function stripLocal(m) {
    return { name: m.name, note: m.note || "", az_ref: m.az_ref || "south",
             profiles: (m.profiles || []).map((p) => ({
               name: p.name, closed: p.closed, points: p.points, notes: p.notes || {} })) };
  }

  $("mountImport").addEventListener("click", () => $("mountFile").click());
  $("mountFile").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    let parsed;
    try {
      parsed = JSON.parse(await f.text());
    } catch (err) { alert("JSON 解析失败：" + err.message); return; }
    let items = parsed.mounts || (parsed.name ? [parsed] : null);
    if (!Array.isArray(items)) { alert("文件格式无效：缺少 mounts 数组"); return; }
    const res = await postJSON("/api/mounts/import",
      { design_id: sky.designId, mounts: items });
    if (!res.ok) { alert((res.errors || ["导入失败"]).join("；")); return; }
    await refreshMounts(res.ids[0]);
    alert(`已导入 ${res.ids.length} 个安装点（始终新建，不覆盖原数据）`);
    e.target.value = "";
  });

  function downloadJSON(obj, filename) {
    const blob = new Blob([JSON.stringify(obj, null, 2)],
      { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }

  // ------------------------------------------------------------ 轮廓编辑
  function newProfile(name) {
    return { name, closed: "ground", points: [], notes: {} };
  }

  function curProfile() {
    return sky.current ? sky.current.profiles[sky.profIndex] : null;
  }

  function syncProfEditor() {
    const selP = $("profSelect");
    selP.innerHTML = "";
    if (!sky.current) return;
    sky.current.profiles.forEach((p, i) => {
      const o = document.createElement("option");
      o.value = i; o.textContent = `${p.name}（${p.points.length}点·${closedLabel(p.closed)}）`;
      selP.appendChild(o);
    });
    selP.value = sky.profIndex;
    $("profClosed").value = curProfile() ? curProfile().closed : "ground";
    $("azRef").value = sky.current.az_ref || "south";
    $("mountName").value = sky.current.name || "";
  }

  function closedLabel(c) {
    return { ground: "贴地", closed: "闭合", open: "开放" }[c] || c;
  }

  $("skyAddProf").addEventListener("click", () => {
    if (!sky.current) return;
    const name = prompt("新轮廓名称", `轮廓${sky.current.profiles.length + 1}`);
    if (name === null) return;
    sky.current.profiles.push(newProfile(name.trim() || `轮廓${sky.current.profiles.length + 1}`));
    sky.profIndex = sky.current.profiles.length - 1;
    syncProfEditor(); markDirty(true); renderSky();
  });

  $("profSelect").addEventListener("change", (e) => {
    sky.profIndex = parseInt(e.target.value, 10);
    syncProfEditor(); renderSky();
  });

  $("profClosed").addEventListener("change", (e) => {
    const p = curProfile();
    if (p) { p.closed = e.target.value; markDirty(true); renderSky(); refreshProfLabels(); }
  });
  $("azRef").addEventListener("change", (e) => {
    if (!sky.current) return;
    if (sky.current.profiles.some((p) => p.points.length) &&
        !confirm("切换方位基准会改变所有轮廓点的方位含义，确定？")) {
      e.target.value = sky.current.az_ref;
      return;
    }
    sky.current.az_ref = e.target.value;
    markDirty(true);
    Promise.all([loadArcs(), refreshDay(true)]).then(renderSky);
  });

  $("profDel").addEventListener("click", () => {
    if (!sky.current || sky.current.profiles.length <= 1) { alert("至少保留一条轮廓"); return; }
    const p = curProfile();
    if (!confirm(`删除轮廓「${p.name}」及其 ${p.points.length} 个样点？`)) return;
    sky.current.profiles.splice(sky.profIndex, 1);
    sky.profIndex = Math.max(0, sky.profIndex - 1);
    syncProfEditor(); markDirty(true); renderSky();
  });

  function refreshProfLabels() {
    const selP = $("profSelect");
    [...selP.options].forEach((o, i) => {
      const p = sky.current.profiles[i];
      o.textContent = `${p.name}（${p.points.length}点·${closedLabel(p.closed)}）`;
    });
  }

  function markDirty(d) {
    sky.dirty = d;
    $("mountSave").textContent = d ? "保存安装点 ●" : "保存安装点";
  }

  function renderMountInfo() {
    if (!sky.current) { $("mountInfo").textContent = ""; return; }
    const m = sky.current;
    $("mountInfo").innerHTML =
      `当前：<b>${esc(m.name)}</b>${m.id ? "（已保存 #" + m.id + "）" : "（未保存）"}<br>` +
      `轮廓 ${m.profiles.length} 条，样点 ${m.profiles.reduce((n, p) => n + p.points.length, 0)} 个；` +
      `方位基准：${m.az_ref === "north" ? "北起顺时针" : "南起西正"}`;
  }

  // ------------------------------------------------------------ 视图切换
  $("viewSkyBtn").addEventListener("click", openSkyView);
  $("skyBackDial").addEventListener("click", closeSkyView);

  function isSkyOpen() { return $("skyStage").style.display !== "none"; }

  async function openSkyView() {
    if (!sky.designId) return;
    if (!sky.current) {
      // 自动选第一个安装点或建草稿
      if (sky.mounts.length) await loadMount(sky.mounts[0]);
      else {
        sky.current = { name: "", note: "", az_ref: "south",
          profiles: [newProfile("轮廓1")] };
        sky.profIndex = 0;
        syncProfEditor();
      }
    }
    $("canvas").style.display = "none";
    $("hint").style.display = "none";
    $("skyStage").style.display = "flex";
    $("skyDate").value = sky.doy;
    $("skyTime").value = sky.minute;
    await Promise.all([loadArcs(), refreshDay(true), refreshInstant(true)]);
    renderSky();
    renderMountInfo();
    fillCmpSelects();
    if (sky.analysis) renderAnnual(sky.analysis);
  }

  function closeSkyView() {
    $("skyStage").style.display = "none";
    $("canvas").style.display = "";
    $("hint").style.display = "";
    window.dispatchEvent(new Event("resize"));
  }

  // ------------------------------------------------------------ 方位—高度坐标
  const plot = { padL: 46, padR: 16, padT: 30, padB: 34 };

  function plotBox() {
    const svg = $("skyCanvas");
    const r = svg.getBoundingClientRect();
    return {
      x0: plot.padL, x1: r.width - plot.padR,
      y0: plot.padT, y1: r.height - plot.padB,
      w: r.width - plot.padL - plot.padR,
      h: r.height - plot.padT - plot.padB,
    };
  }
  // 图形横坐标统一为北起顺时针 0..360（北左、东中左、南中、西中右）
  function azToX(az, box) {
    if (sky.current.az_ref === "south") az += 180;
    return box.x0 + ((((az % 360) + 360) % 360) / 360) * box.w;
  }
  function xToAz(x, box) {
    const f = Math.max(0, Math.min(1, (x - box.x0) / box.w));
    const northLike = f * 360;
    return sky.current.az_ref === "north" ? northLike : northLike - 180;
  }
  function altToY(alt, box) { return box.y1 - (alt / 90) * box.h; }
  function yToAlt(y, box) { return Math.max(0, Math.min(90, (box.y1 - y) / box.h * 90)); }

  // 南基准 -> 当前录入基准
  function southToRef(az) {
    return sky.current.az_ref === "north" ? (((az + 180) % 360) + 360) % 360 : az;
  }

  // ------------------------------------------------------------ 渲染方位高度图
  function renderSky() {
    const svg = $("skyCanvas");
    svg.innerHTML = "";
    const box = plotBox();

    // 高度网格
    for (let alt = 0; alt <= 90; alt += 10) {
      const y = altToY(alt, box);
      el("line", { x1: box.x0, y1: y, x2: box.x1, y2: y,
        class: alt % 30 === 0 ? "sky-grid-major" : "sky-grid" }, svg);
      const t = el("text", { x: box.x0 - 6, y: y + 4, class: "sky-axis-label",
        "text-anchor": "end" }, svg);
      t.textContent = alt + "°";
    }
    // 方位网格（横轴统一北起顺时针；南基准录入时用括号标注其南起值）
    const azTicks = [[0, "北"], [90, "东"], [180, "南"], [270, "西"]];
    for (const [az, lab] of azTicks) {
      const x = azToX(az, box);
      el("line", { x1: x, y1: box.y0, x2: x, y2: box.y1, class: "sky-grid-major" }, svg);
      const southVal = az === 0 ? "±180" : az - 180;
      const t = el("text", { x, y: box.y1 + 16, class: "sky-axis-label" }, svg);
      t.textContent = sky.current.az_ref === "south" ? `${lab}(${southVal}°)` : `${lab}${az}°`;
    }

    // 月度太阳弧线
    if (sky.arcs) {
      sky.arcs.forEach((arc, i) => {
        const hue = 200 - i * 14;
        drawSunArc(arc.points, box, `hsl(${Math.max(0, hue)},45%,45%)`, svg);
      });
    }

    // 各轮廓
    sky.current.profiles.forEach((prof, pi) => {
      drawProfile(prof, pi, pi === sky.profIndex, box, svg);
    });

    // 当日太阳弧线（加粗）与状态色轨迹
    if (sky.dayData) {
      drawSunArc(sky.dayData.sun_arc.map((q) =>
        ({ az: q.az, alt: q.alt, _ref: true })),
        box, "#b3541e", svg, 2.0, 0.9);
      // 状态点
      for (const c of sky.dayData.trace) {
        if (!c.uv && c.state !== "block") continue;
        const mid = (c.t0 + c.t1) / 2;
        const pt = arcPointAt(mid);
        if (!pt) continue;
        el("circle", {
          cx: azToX(pt.az, box), cy: altToY(pt.alt, box), r: c.state === "block" ? 4.2 : 2.8,
          class: "sky-state-" + c.state, stroke: "#fff", "stroke-width": .8,
          "data-t": mid, style: "cursor:pointer",
        }, svg).addEventListener("click", () => {
          sky.minute = Math.round(mid * 60);
          $("skyTime").value = sky.minute;
          refreshInstant(true);
        });
      }
    }

    // 当前太阳
    if (sky.instant) drawSun(box, svg);

    $("skyTitle").textContent = sky.current
      ? `方位角—高度角图 · ${esc(sky.current.name)} · ` +
        (sky.current.az_ref === "north" ? "方位北起顺时针" : "方位南起西正") +
        (sky.dirty ? " · 有未保存修改" : "")
      : "";
    syncProfEditorKeep();
  }

  function syncProfEditorKeep() {
    const selP = $("profSelect");
    if (selP.options.length !== sky.current.profiles.length) { syncProfEditor(); return; }
    sky.current.profiles.forEach((p, i) => {
      const o = selP.options[i];
      const txt = `${p.name}（${p.points.length}点·${closedLabel(p.closed)}）`;
      if (o.textContent !== txt) o.textContent = txt;
    });
    selP.value = sky.profIndex;
  }

  function drawSunArc(points, box, color, svg, width, opacity) {
    // 点方位可能是南基准（month_arcs）或已在 _ref 标记的当前基准
    let path = "";
    let pen = null;
    for (const q of points) {
      const az = q._ref ? q.az : southToRef(q.az);
      const x = azToX(az, box), y = altToY(q.alt, box);
      // 跨越北点（左右边界）时抬笔
      if (pen && Math.abs(x - pen.x) > box.w * 0.45) path += "M" + x + "," + y;
      else path += (path ? "L" : "M") + x + "," + y;
      pen = { x, y };
    }
    if (path) el("path", { d: path, class: "sky-sunarc", stroke: color,
      "stroke-width": width || 1.4, opacity: opacity == null ? .6 : opacity }, svg);
  }

  function profColor(i) { return PROF_COLORS[i % PROF_COLORS.length]; }

  function drawProfile(prof, pi, selected, box, svg) {
    const color = profColor(pi);
    const toXY = (pt) => ({ x: azToX(pt[0], box), y: altToY(pt[1], box) });
    if (prof.points.length === 0) return;
    const g = el("g", { "data-prof": pi }, svg);
    let path = "";
    prof.points.forEach((pt, i) => {
      const q = toXY(pt);
      path += (i ? "L" : "M") + q.x + "," + q.y;
    });
    if (prof.closed === "ground" && prof.points.length >= 2) {
      const first = toXY(prof.points[0]), last = toXY(prof.points[lastIdx(prof)]);
      const fillPath = path + `L${last.x},${box.y1}L${first.x},${box.y1}Z`;
      el("path", { d: fillPath, fill: color, class: "sky-profile ground " +
        (selected ? "selected" : ""), stroke: "none", opacity: .16 }, g);
    } else if (prof.closed === "closed" && prof.points.length >= 3) {
      el("path", { d: path + "Z", fill: color, opacity: .14, stroke: "none" }, g);
    }
    el("path", { d: path + (prof.closed === "closed" ? "Z" : ""),
      class: "sky-profile" + (selected ? " selected" : ""),
      stroke: color, fill: "none", opacity: selected ? 1 : .75 }, g);

    prof.points.forEach((pt, i) => {
      const q = toXY(pt);
      const c = el("circle", { cx: q.x, cy: q.y, r: selected ? 5 : 4,
        fill: color, class: "sky-vertex" + (selected ? " selected" : ""),
        "data-i": i }, g);
      if (selected) attachVertexDrag(c, i, box);
      c.addEventListener("dblclick", (ev) => { ev.stopPropagation(); openNotePop(i, q, color); });
      const note = prof.notes[String(i)];
      if (note) {
        const t = el("text", { x: q.x + 6, y: q.y - 6, class: "sky-note" }, g);
        t.textContent = "备注";
      }
    });

    // 选中整个轮廓（点击线条切过去）
    g.addEventListener("click", (ev) => {
      if (ev.target.tagName === "circle") return;
      sky.profIndex = pi;
      syncProfEditor(); renderSky();
    });
  }

  function lastIdx(prof) { return prof.points.length - 1; }

  // ------------------------------------------------------------ 样点拖拽 / 添加
  function svgPoint(evt) {
    const r = $("skyCanvas").getBoundingClientRect();
    return { x: evt.clientX - r.left, y: evt.clientY - r.top };
  }

  function attachVertexDrag(circle, idx, box) {
    circle.addEventListener("pointerdown", (ev) => {
      ev.stopPropagation();
      circle.setPointerCapture(ev.pointerId);
      const move = (mv) => {
        const q = svgPoint(mv);
        const prof = curProfile();
        prof.points[idx] = [
          round1(xToAz(q.x, box)), round1(yToAlt(q.y, box))];
        markDirty(true);
        renderSky();
        scheduleRecompute();
      };
      const up = () => {
        circle.removeEventListener("pointermove", move);
        circle.removeEventListener("pointerup", up);
        scheduleRecomputeFlush();
      };
      circle.addEventListener("pointermove", move);
      circle.addEventListener("pointerup", up);
    });
  }

  // 在空白处点击/拖拽 = 添加样点
  let paintStroke = null;
  const skyCanvas = $("skyCanvas");
  skyCanvas.addEventListener("pointerdown", (ev) => {
    if (!sky.current) return;
    if (ev.target !== skyCanvas && ev.target.classList.contains("sky-sunarc") === false
        && ev.target.tagName === "circle") return; // 交给样点
    // 只在点击背景或弧线时加点
    if (ev.target !== skyCanvas && !ev.target.classList.contains("sky-grid")
        && !ev.target.classList.contains("sky-grid-major")
        && !ev.target.classList.contains("sky-sunarc")) return;
    const box = plotBox();
    const q = svgPoint(ev);
    if (q.x < box.x0 || q.x > box.x1 || q.y < box.y0 || q.y > box.y1) return;
    const prof = curProfile();
    if (!prof) return;
    const pt = [round1(xToAz(q.x, box)), round1(yToAlt(q.y, box))];
    insertPointOrdered(prof, pt);
    paintStroke = { lastX: q.x, lastY: q.y };
    skyCanvas.setPointerCapture(ev.pointerId);
    markDirty(true);
    renderSky();
    scheduleRecompute();
  });
  skyCanvas.addEventListener("pointermove", (ev) => {
    if (!paintStroke) return;
    const box = plotBox();
    const q = svgPoint(ev);
    if (Math.hypot(q.x - paintStroke.lastX, q.y - paintStroke.lastY) < 8) return;
    if (q.x < box.x0 || q.x > box.x1 || q.y < box.y0 || q.y > box.y1) return;
    const prof = curProfile();
    insertPointOrdered(prof, [round1(xToAz(q.x, box)), round1(yToAlt(q.y, box))]);
    paintStroke = { lastX: q.x, lastY: q.y };
    markDirty(true);
    renderSky();
    scheduleRecompute();
  });
  skyCanvas.addEventListener("pointerup", () => {
    paintStroke = null;
    scheduleRecomputeFlush();
  });

  function round1(v) { return Math.round(v * 10) / 10; }

  function insertPointOrdered(prof, pt) {
    // ground/closed 轮廓按方位插入；open 折线允许乱序，按方位也更直观
    const pts = prof.points;
    if (prof.closed === "open") { pts.push(pt); return; }
    let inserted = false;
    // 以首点为基准展开，避免跨越 ±180/0 时乱序
    if (pts.length) {
      const base = pts[0][0];
      const unwrap = (a) => {
        let d = (a - base + 180) % 360;
        if (d < 0) d += 360;
        return d - 180;
      };
      const target = unwrap(pt[0]);
      for (let i = 0; i < pts.length; i++) {
        if (unwrap(pts[i][0]) > target) { pts.splice(i, 0, pt); inserted = true; break; }
      }
    }
    if (!inserted) pts.push(pt);
  }

  // 备注气泡
  function openNotePop(idx, q, color) {
    document.querySelectorAll(".prof-note-pop").forEach((n) => n.remove());
    const prof = curProfile();
    const pop = document.createElement("div");
    pop.className = "prof-note-pop";
    pop.style.left = "60px"; pop.style.top = "60px";
    pop.innerHTML =
      `<div style="font-weight:600;color:${color}">${esc(prof.name)} · 样点 ${idx + 1}</div>` +
      `<div class="info small">方位 ${prof.points[idx][0]}°，高度 ${prof.points[idx][1]}°</div>` +
      `<input type="text" class="noteText" maxlength="120" placeholder="备注，如：楼顶水箱/树梢">` +
      `<div class="row2"><button class="noteDel">删除样点</button><button class="noteOk">完成</button></div>`;
    document.body.appendChild(pop);
    const inp = pop.querySelector(".noteText");
    inp.value = prof.notes[String(idx)] || "";
    inp.focus();
    const r = skyCanvas.getBoundingClientRect();
    pop.style.left = Math.min(r.left + q.x + 8, window.innerWidth - 250) + "px";
    pop.style.top = Math.max(8, r.top + q.y - 30) + "px";
    pop.querySelector(".noteOk").addEventListener("click", () => {
      const v = inp.value.trim();
      if (v) prof.notes[String(idx)] = v; else delete prof.notes[String(idx)];
      pop.remove(); markDirty(true); renderSky();
    });
    pop.querySelector(".noteDel").addEventListener("click", () => {
      if (!confirm("删除该样点？")) return;
      // 同步移动备注索引
      const notes = {};
      prof.points.forEach((p, i) => {
        const old = i >= idx ? i + 1 : i;
        if (prof.notes[String(old)]) notes[String(i)] = prof.notes[String(old)];
      });
      prof.notes = notes;
      prof.points.splice(idx, 1);
      pop.remove(); markDirty(true); renderSky(); scheduleRecomputeFlush();
    });
  }

  // ------------------------------------------------------------ 当前太阳
  function drawSun(box, svg) {
    const st = sky.instant;
    let az;
    if (sky.current.az_ref === "south") az = st.az_south_deg;
    else az = (((st.az_south_deg + 180) % 360) + 360) % 360;
    const x = azToX(az, box), y = altToY(Math.max(0, st.alt_deg), box);
    if (st.alt_deg <= 0) return;
    el("circle", { cx: x, cy: y, r: 7,
      class: "sky-sun sky-state-" + st.state }, svg);
  }

  function arcPointAt(t) {
    // 从 sun_arc 线性插值
    const arc = sky.dayData && sky.dayData.sun_arc;
    if (!arc || !arc.length) return null;
    for (let i = 0; i < arc.length - 1; i++) {
      if (arc[i].t <= t && t <= arc[i + 1].t) {
        const f = (t - arc[i].t) / (arc[i + 1].t - arc[i].t);
        return { az: arc[i].az + (arc[i + 1].az - arc[i].az) * f,
                 alt: arc[i].alt + (arc[i + 1].alt - arc[i].alt) * f };
      }
    }
    return null;
  }

  // ------------------------------------------------------------ 数据请求
  async function loadArcs() {
    const res = await postJSON("/api/shade/arcs", {
      params: App.getParams(), az_ref: sky.current.az_ref,
    });
    if (res.ok) sky.arcs = res.data;
  }

  function mountBody(extra) {
    return Object.assign({
      design_id: sky.designId,
      params: App.getParams(),
      mount: {
        name: sky.current.name || "草稿",
        az_ref: sky.current.az_ref,
        note: sky.current.note || "",
        profiles: sky.current.profiles,
      },
    }, extra || {});
  }

  let recomputeTimer = null;
  function scheduleRecompute() {
    clearTimeout(recomputeTimer);
    recomputeTimer = setTimeout(refreshDay, 250);
  }
  function scheduleRecomputeFlush() {
    clearTimeout(recomputeTimer);
    refreshDay();
  }

  async function refreshDay(immediate) {
    if (!sky.current) return;
    const d = doyToDate(App.getYear(), sky.doy);
    const res = await postJSON("/api/shade/day", mountBody({
      year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate(),
    }));
    if (!res.ok) return;
    sky.dayData = res.data;
    renderDayRuns();
    if (isSkyOpen()) renderSky();
    refreshInstant(immediate);
  }

  async function refreshInstant(immediate) {
    if (!sky.current) return;
    const d = doyToDate(App.getYear(), sky.doy);
    const res = await postJSON("/api/shade/instant", mountBody({
      year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate(),
      hour: sky.minute / 60,
    }));
    if (!res.ok) return;
    sky.instant = res.data;
    renderInstant();
    if (isSkyOpen()) renderSky();
  }

  function renderInstant() {
    const s = sky.instant;
    const d = doyToDate(App.getYear(), sky.doy);
    $("skyDateLab").textContent = `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
    $("skyTimeLab").textContent = fmtHM(sky.minute / 60);
    if (!s) return;
    let reason = "";
    if (s.state === "block" && s.detail) {
      reason = `被 <b style="color:#c0392b">「${esc(s.detail.profile_name)}」段 ${(s.detail.edge_index || 0) + 1}</b> 挡住` +
        (s.detail.note ? `（${esc(s.detail.note)}）` : "") +
        `，太阳方位 ${s.az_south_deg}°、高度 ${s.alt_deg}°`;
    } else if (s.state === "out" && s.detail) {
      reason = `影端越界：影端 (${s.detail.uv[0]}, ${s.detail.uv[1]}) mm 超出盘面`;
    } else if (s.uv) {
      reason = `影端 (${s.uv[0]}, ${s.uv[1]}) mm`;
    }
    $("skyInstant").innerHTML =
      `民用 <b>${s.civil}</b> ／ 真太阳时 ${s.tst}<br>` +
      `太阳高度 ${s.alt_deg}°，方位 ${s.az_south_deg}°（南起西正）<br>` +
      `判定：<b style="color:${STATE_COLORS[s.state]}">${STATE_LABELS[s.state]}</b><br>` +
      reason;
  }

  // ------------------------------------------------------------ 当日区间条
  function renderDayRuns() {
    const box = $("dayRuns");
    if (!sky.dayData) { box.innerHTML = ""; return; }
    const runs = mergeRuns(sky.dayData.trace);
    let bar = '<div class="runbar" title="点击区间定位到该时刻并在图上高亮">';
    for (const r of runs) {
      const w = (r.t1 - r.t0) / 24 * 100;
      bar += `<div data-t="${(r.t0 + r.t1) / 2}" style="width:${w}%;background:${STATE_COLORS[r.state]}"
        title="${fmtHM(r.t0)}–${fmtHM(r.t1)} ${STATE_LABELS[r.state]}"></div>`;
    }
    bar += "</div>";
    let lines = "";
    for (const r of runs) {
      const det = r.state === "block" && r.detail
        ? `— ${esc(r.detail.profile_name)}·段${(r.detail.edge_index || 0) + 1}` +
          (r.detail.note ? "（" + esc(r.detail.note) + "）" : "") : "";
      lines += `<div class="runline" data-t="${(r.t0 + r.t1) / 2}">
        <b>${fmtHM(r.t0)}–${fmtHM(r.t1)}</b>
        <span><span class="dot" style="background:${STATE_COLORS[r.state]}"></span>${STATE_LABELS[r.state]}${det}</span>
        <span>${(r.t1 - r.t0).toFixed(2)}h</span></div>`;
    }
    box.innerHTML = bar + lines +
      `<div class="info small" style="margin-top:4px">图例：` +
      Object.keys(STATE_COLORS).map((k) =>
        `<span class="dot" style="background:${STATE_COLORS[k]}"></span>${STATE_LABELS[k]}`).join("　") +
      `</div>`;
    box.querySelectorAll("[data-t]").forEach((node) =>
      node.addEventListener("click", () => {
        const t = parseFloat(node.getAttribute("data-t"));
        sky.minute = Math.round(t * 60);
        $("skyTime").value = sky.minute;
        refreshInstant(true);
        const run = runs.find((rr) => Math.abs((rr.t0 + rr.t1) / 2 - t) < (rr.t1 - rr.t0) / 2 + 1e-6);
        if (run && run.state === "block" && run.detail) {
          const pi = sky.current.profiles.findIndex(
            (p) => p.name === run.detail.profile_name);
          if (pi >= 0) { sky.profIndex = pi; syncProfEditor(); renderSky(); }
        }
      }));
  }

  function mergeRuns(trace) {
    const out = [];
    for (const c of trace) {
      const last = out[out.length - 1];
      const sameReason = last && last.state === c.state &&
        (c.state !== "block" ||
         (last.detail.profile_name === (c.detail || {}).profile_name &&
          last.detail.edge_index === (c.detail || {}).edge_index));
      if (sameReason) last.t1 = c.t1;
      else out.push({ state: c.state, t0: c.t0, t1: c.t1, detail: c.detail });
    }
    return out;
  }

  // ------------------------------------------------------------ 滑块联动
  $("skyDate").addEventListener("input", (e) => {
    sky.doy = parseInt(e.target.value, 10);
    refreshDay();
  });
  $("skyTime").addEventListener("input", (e) => {
    sky.minute = parseInt(e.target.value, 10);
    refreshInstant();
  });

  // ------------------------------------------------------------ 全年分析
  $("analyzeBtn").addEventListener("click", runAnalyze);

  async function runAnalyze() {
    if (!sky.current) return;
    $("analyzeBtn").textContent = "分析中…";
    let mid = sky.current.id;
    if (sky.dirty || !mid) mid = await saveCurrent();
    if (!mid) { $("analyzeBtn").textContent = "全年分析"; return; }
    const res = await postJSON("/api/shade/analyze",
      { mount_id: mid, year: App.getYear() });
    $("analyzeBtn").textContent = "全年分析";
    if (!res.ok) { alert((res.errors || ["分析失败"]).join("；")); return; }
    sky.analysis = res.data;
    sky.analysisMountId = mid;
    renderAnnual(res.data);
    App.setBlockedTrace && App.setBlockedTrace(sky.current.id, res.data.blocked_trace);
  }

  function renderAnnual(data) {
    const s = data.summary;
    const maxAvg = Math.max(1, ...data.months.map((m) => m.avg_read_hours));
    let html = `<div class="info small">全年可读 <b>${s.total_read_hours}</b> 小时 / 受光 ` +
      `${s.total_sun_hours} 小时（占比 ${(s.read_ratio * 100).toFixed(0)}%）<br>` +
      `最佳 ${s.best_month} 月，最差 ${s.worst_month} 月</div><div class="monthbars">`;
    for (const m of data.months) {
      html += `<div class="mbar-row"><span>${m.month}月</span>
        <div class="mbar-track" title="日均可读 ${m.avg_read_hours}h / 受光 ${m.avg_sun_hours}h">
          <div class="mbar-fill" style="width:${m.avg_read_hours / maxAvg * 100}%"></div></div>
        <span>${m.avg_read_hours}h</span></div>`;
    }
    html += "</div>";
    html += `<div class="info small" style="margin-top:6px">状态合计：` +
      Object.entries(s.hours_by_state).map(([k, v]) =>
        `<span class="dot" style="background:${STATE_COLORS[k] || "#999"}"></span>` +
        `${STATE_LABELS[k] || k} ${v}h`).join("　") + "</div>";
    // 关键时段缺口（全年日均）
    html += `<h3 class="sub" style="margin-top:8px">关键时段缺口（全年日均小时）</h3>`;
    for (const key of ["morning", "noon", "afternoon"]) {
      const kk = data.months[0].key[key];
      const defs = data.months.reduce((n, m) => n + m.key[key].deficit_hours, 0) / 365;
      html += `<div class="keygap">${esc(kk.label)}：缺口 <b style="color:${defs > 0.3 ? "#c0392b" : "#3b7d52"}">${defs.toFixed(2)} h/日</b></div>`;
    }
    html += `<div class="info small" style="margin-top:6px">点击下方“导出可读分析报告”可下载含样点、逐日判定原因与窗口的 JSON。</div>`;
    $("annualBox").innerHTML = html;
  }

  // ------------------------------------------------------------ 双安装点比较
  function fillCmpSelects() {
    for (const [selId, other] of [["cmpA", "cmpB"], ["cmpB", "cmpA"]]) {
      const sel = $(selId);
      const cur = sel.value;
      sel.innerHTML = "";
      sky.mounts.forEach((m, i) => {
        const o = document.createElement("option");
        o.value = m.id; o.textContent = m.name;
        sel.appendChild(o);
      });
      if (cur) sel.value = cur;
      else sel.value = sky.mounts[selId === "cmpA" ? 0 : 1] ?
        sky.mounts[selId === "cmpA" ? 0 : 1].id : (sky.mounts[0] ? sky.mounts[0].id : "");
    }
  }

  $("cmpRun").addEventListener("click", async () => {
    const a = parseInt($("cmpA").value, 10);
    const b = parseInt($("cmpB").value, 10);
    if (!a || !b || a === b) { alert("请选择两个不同的安装点"); return; }
    const res = await postJSON("/api/shade/compare", { a, b, year: App.getYear() });
    if (!res.ok) { alert((res.errors || ["比较失败"]).join("；")); return; }
    sky.cmp = res.data;
    renderCmp(res.data);
  });

  function renderCmp(d) {
    const maxV = Math.max(1, ...d.months.map((m) => Math.max(m.read_a, m.read_b)));
    let html = `<div class="info small">年可读总时长 Δ（B−A）：<b style="color:${d.total_delta_hours >= 0 ? "#3b7d52" : "#c0392b"}">` +
      `${d.total_delta_hours >= 0 ? "+" : ""}${d.total_delta_hours} h</b><br>` +
      `<span style="color:#2c5d8c">■ ${esc(d.a.name)}</span> ／ <span style="color:#b3541e">■ ${esc(d.b.name)}</span></div>`;
    html += `<table class="cmp"><tr><th>月</th><th>A 日均</th><th>B 日均</th><th>Δ</th>` +
      ["morning", "noon", "afternoon"].map((k) =>
        `<th>${{ morning: "上午缺", noon: "正午缺", afternoon: "下午缺" }[k]} A/B</th>`).join("") + "</tr>";
    for (const m of d.months) {
      html += `<tr><td>${m.month}</td>` +
        `<td><span class="cmpbar" style="width:${m.read_a / maxV * 46}px;background:#2c5d8c"></span>${m.read_a}</td>` +
        `<td><span class="cmpbar" style="width:${m.read_b / maxV * 46}px;background:#b3541e"></span>${m.read_b}</td>` +
        `<td style="color:${m.delta >= 0 ? "#3b7d52" : "#c0392b"}">${m.delta >= 0 ? "+" : ""}${m.delta}</td>`;
      for (const k of ["morning", "noon", "afternoon"]) {
        const kk = m.key[k];
        html += `<td title="${esc(kk.label)}">${kk.deficit_a}/${kk.deficit_b}</td>`;
      }
      html += "</tr>";
    }
    html += "</table>";
    $("cmpBox").innerHTML = html;
  }

  // ------------------------------------------------------------ 报告
  $("mountReport").addEventListener("click", async () => {
    let mid = sky.current && sky.current.id;
    if (!mid) { alert("请先保存安装点"); return; }
    if (sky.dirty && !confirm("当前有未保存修改，报告将基于上次保存的数据。继续？")) return;
    const r = await fetch(`/api/mounts/${mid}/report?year=${App.getYear()}`);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      alert((j.errors || ["导出失败"]).join("；"));
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `mount_report_m${mid}.json`; a.click();
    URL.revokeObjectURL(url);
  });

  // ------------------------------------------------------------ 参数变化联动
  function onParamsChanged() {
    if (!isSkyOpen()) return;
    if (sky.current) {
      loadArcs();
      refreshDay();
    }
  }
  function onYearChanged() {
    if (isSkyOpen() && sky.current) refreshDay();
  }

  window.addEventListener("resize", () => { if (isSkyOpen()) renderSky(); });

  // 暴露给 app.js
  window.SundialShade = {
    setDesign, onParamsChanged, onYearChanged,
    hasAnalysis: () => !!sky.analysis,
    rerun: runAnalyze,
  };
})();

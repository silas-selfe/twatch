/* twatch chart components -- same design system as the node dashboards. */
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
};
const fmt = n => n.toLocaleString("en-US");

const tt = () => document.getElementById("tooltip");
function showTip(x, y, title, rows) {
  const t = tt();
  t.textContent = "";
  const h = document.createElement("div");
  h.className = "tt-title"; h.textContent = title; t.appendChild(h);
  for (const [name, val, color] of rows) {
    const r = document.createElement("div"); r.className = "tt-row";
    if (color) { const k = document.createElement("span"); k.className = "lk"; k.style.background = color; r.appendChild(k); }
    const b = document.createElement("b"); b.textContent = val; r.appendChild(b);
    const nm = document.createElement("span"); nm.className = "name"; nm.textContent = name; r.appendChild(nm);
    t.appendChild(r);
  }
  t.style.display = "block";
  t.style.left = Math.min(x + 14, innerWidth - t.offsetWidth - 8) + "px";
  t.style.top = Math.max(8, y - t.offsetHeight - 12) + "px";
}
const hideTip = () => { tt().style.display = "none"; };

/* Banded bar chart: slots = [{band, tick, v, u, tipTitle, tipRows}].
   Consecutive slots sharing `band` are grouped under one band label.
   u >= 0.95 full color, 0 < u < 0.95 partial gray, u === 0 offline dash. */
function bandedBars(mount, slots, opts = {}) {
  const host = document.getElementById(mount);
  host.textContent = "";
  const W = opts.w ?? 1056, plotH = opts.h ?? 220, padL = 44, padR = 8, axisBand = 40, padT = 18;
  const H = padT + plotH + axisBand;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%",
                          style: `min-width:${opts.minWidth ?? 720}px` }, null);
  host.appendChild(svg);
  const rawMax = Math.max(...slots.map(sl => sl.v), 1);
  const pow = Math.pow(10, Math.floor(Math.log10(rawMax)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * pow).find(st => rawMax <= st * 4) || 10 * pow;
  const yMax = step * 4;
  const slot = (W - padL - padR) / slots.length;
  const barW = Math.min(24, Math.max(2, slot - 2));
  const y = v => padT + plotH - (v / yMax) * plotH;
  const x = i => padL + i * slot + (slot - barW) / 2;
  for (let g = 0; g <= yMax; g += step) {
    el("line", { x1: padL, x2: W - padR, y1: y(g), y2: y(g), stroke: g === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }, svg);
    el("text", { x: padL - 7, y: y(g) + 3.5, "text-anchor": "end", class: "tick" }, svg).textContent = fmt(g);
  }
  const groups = [];
  slots.forEach((sl, i) => {
    const g = groups[groups.length - 1];
    if (!g || g.band !== sl.band) groups.push({ band: sl.band, from: i, to: i });
    else g.to = i;
  });
  groups.forEach((g, gi) => {
    if (gi > 0) el("line", { x1: padL + g.from * slot, x2: padL + g.from * slot, y1: padT, y2: padT + plotH + 30, stroke: css("--grid"), "stroke-width": 1 }, svg);
    el("text", { x: padL + ((g.from + g.to + 1) / 2) * slot, y: padT + plotH + 32, "text-anchor": "middle", class: "dayband" }, svg)
      .textContent = g.band;
  });
  slots.forEach((sl, i) => {
    if (sl.tick != null)
      el("text", { x: padL + i * slot + slot / 2, y: padT + plotH + 15, "text-anchor": "middle", class: "tick" }, svg)
        .textContent = sl.tick;
    let bar = null;
    if (sl.u === 0) {
      bar = el("rect", { x: x(i), y: y(0) - 2.5, width: barW, height: 2.5, fill: css("--muted"), class: "bar" }, svg);
    } else if (sl.v > 0) {
      const hgt = Math.max((sl.v / yMax) * plotH, 1.5);
      const rad = Math.min(4, barW / 2, hgt);
      bar = el("path", {
        d: `M${x(i)},${y(0)} v${-(hgt - rad)} q0,${-rad} ${rad},${-rad} h${barW - 2 * rad} q${rad},0 ${rad},${rad} v${hgt - rad} z`,
        fill: sl.u >= 0.95 ? css("--vol") : css("--part"), class: "bar",
      }, svg);
    }
    const hit = el("rect", { x: padL + i * slot, y: padT, width: slot, height: plotH, fill: "transparent", tabindex: 0, class: "hit" }, svg);
    hit.setAttribute("aria-label", `${sl.band} ${sl.tick ?? ""}: ${sl.v}`);
    const over = ev => {
      if (bar) bar.style.opacity = 0.72;
      const box = hit.getBoundingClientRect();
      showTip(ev.clientX ?? box.x, ev.clientY ?? box.y, sl.tipTitle, sl.tipRows);
    };
    const out = () => { if (bar) bar.style.opacity = 1; hideTip(); };
    hit.addEventListener("pointermove", over);
    hit.addEventListener("pointerleave", out);
    hit.addEventListener("focus", over);
    hit.addEventListener("blur", out);
  });
}

/* Day/hour histogram: adapter over bandedBars. rows: [{d,dow,h,veh,ped,bike,u}] */
function dayHourHistogram(mount, rows, opts = {}) {
  const hourTickEvery = rows.length > 96 ? 12 : 6;
  bandedBars(mount, rows.map(r => {
    const t = r.veh + r.ped + r.bike;
    const cov = r.u === 0 ? "offline" : Math.round(r.u * 100) + "% coverage" + (r.u < 0.95 ? " — undercount" : "");
    return {
      band: `${r.dow} ${r.d.slice(5)}`,
      tick: r.h % hourTickEvery === 0 ? String(r.h).padStart(2, "0") : null,
      v: t, u: r.u,
      tipTitle: `${r.dow} ${r.d.slice(5)} · ${String(r.h).padStart(2, "0")}:00 · ${cov}`,
      tipRows: [["vehicles", fmt(r.veh), css("--vol")], ["pedestrians", fmt(r.ped), null], ["bikes", fmt(r.bike), null]],
    };
  }), { minWidth: rows.length > 200 ? 1400 : 720, ...opts });
}

/* Multi-series line chart over arbitrary x categories.
   series: [{name,color,pts:[v|null]}], labels: x tick labels */
function lineChart(mount, series, labels, opts = {}) {
  const W = opts.w ?? 512, plotH = opts.h ?? 200, padL = 44, padR = opts.padR ?? 60, padT = 14, axisBand = 26;
  const H = padT + plotH + axisBand;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%" });
  document.getElementById(mount).appendChild(svg);
  const N = labels.length;
  const all = series.flatMap(s => s.pts.filter(v => v != null));
  const yMax = opts.yMax || Math.max(4, Math.ceil(Math.max(...all, 1) * 1.15));
  const x = i => padL + (i / Math.max(N - 1, 1)) * (W - padL - padR);
  const y = v => padT + plotH - (v / yMax) * plotH;
  const yStep = yMax / 4;
  for (let g = 0; g <= yMax; g += yStep) {
    el("line", { x1: padL, x2: W - padR, y1: y(g), y2: y(g), stroke: g === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }, svg);
    el("text", { x: padL - 6, y: y(g) + 3.5, "text-anchor": "end", class: "tick" }, svg).textContent = fmt(Math.round(g));
  }
  const tickEvery = Math.max(1, Math.round(N / 8));
  labels.forEach((lab, i) => {
    if (i % tickEvery === 0)
      el("text", { x: x(i), y: padT + plotH + 16, "text-anchor": "middle", class: "tick" }, svg).textContent = lab;
  });
  for (const s of series) {
    let d = "", pen = false;
    s.pts.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1);
      pen = true;
    });
    if (opts.area) {
      let f = s.pts.findIndex(v => v != null), l = s.pts.length - 1;
      while (l >= 0 && s.pts[l] == null) l--;
      if (f >= 0) el("path", { d: d + `L${x(l)},${y(0)}L${x(f)},${y(0)}z`, fill: s.color, opacity: 0.1 }, svg);
    }
    el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    let l = s.pts.length - 1;
    while (l >= 0 && s.pts[l] == null) l--;
    if (l >= 0) {
      el("circle", { cx: x(l), cy: y(s.pts[l]), r: 4.5, fill: s.color, stroke: css("--surface"), "stroke-width": 2 }, svg);
      s.endX = x(l); s.endY = y(s.pts[l]);
    }
  }
  const lab = series.filter(s => s.endX != null).sort((a, b) => a.endY - b.endY);
  for (let i = 1; i < lab.length; i++)
    if (lab[i].endY - lab[i - 1].endY < 13) lab[i].endY = lab[i - 1].endY + 13;
  for (const s of lab)
    el("text", { x: s.endX + 8, y: s.endY + 3.5, class: "dlabel" }, svg).textContent = s.name;
  const cross = el("line", { y1: padT, y2: padT + plotH, stroke: css("--axis"), "stroke-width": 1, visibility: "hidden" }, svg);
  const hit = el("rect", { x: padL, y: padT, width: W - padL - padR, height: plotH, fill: "transparent", tabindex: 0 }, svg);
  hit.setAttribute("aria-label", opts.aria || "line chart");
  const over = ev => {
    const box = hit.getBoundingClientRect();
    const px = ev.clientX ?? box.x + box.width / 2;
    const i = Math.max(0, Math.min(N - 1, Math.round(((px - box.x) / box.width) * (N - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    cross.setAttribute("visibility", "visible");
    showTip(px, ev.clientY ?? box.y, `${labels[i]} ${opts.tipUnit || ""}`,
      series.map(s => [s.name, s.pts[i] == null ? "—" : fmt(s.pts[i]), s.color]));
  };
  const out = () => { cross.setAttribute("visibility", "hidden"); hideTip(); };
  hit.addEventListener("pointermove", over);
  hit.addEventListener("pointerleave", out);
  hit.addEventListener("focus", over);
  hit.addEventListener("blur", out);
}

/* Simple bar chart. bars: [{v,color,tick,aria,tipTitle,tipRows}] */
function barChart(mount, bars, opts = {}) {
  const W = opts.w ?? 512, plotH = opts.h ?? 200, padL = 44, padR = 10, padT = 16, axisBand = 30;
  const H = padT + plotH + axisBand;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%" });
  document.getElementById(mount).appendChild(svg);
  const slot = (W - padL - padR) / bars.length;
  const barW = Math.min(24, Math.max(2, slot - 2));
  const yMax = opts.yMax || Math.max(4, Math.ceil(Math.max(...bars.map(b => b.v), 1) * 1.15));
  const y = v => padT + plotH - (v / yMax) * plotH;
  const step = yMax / 4;
  for (let g = 0; g <= yMax; g += step) {
    el("line", { x1: padL, x2: W - padR, y1: y(g), y2: y(g), stroke: g === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }, svg);
    el("text", { x: padL - 6, y: y(g) + 3.5, "text-anchor": "end", class: "tick" }, svg).textContent = fmt(Math.round(g));
  }
  const maxV = Math.max(...bars.map(b => b.v));
  bars.forEach((b, i) => {
    const xc = padL + i * slot + slot / 2;
    if (b.v > 0) {
      const hgt = Math.max((b.v / yMax) * plotH, 1.5);
      const rad = Math.min(4, barW / 2, hgt);
      el("path", {
        d: `M${xc - barW / 2},${y(0)} v${-(hgt - rad)} q0,${-rad} ${rad},${-rad} h${barW - 2 * rad} q${rad},0 ${rad},${rad} v${hgt - rad} z`,
        fill: b.color, class: "bar",
      }, svg);
    }
    if (b.v === maxV && b.v > 0 && opts.labelMax)
      el("text", { x: xc, y: y(b.v) - 5, "text-anchor": "middle", class: "vlabel" }, svg).textContent = fmt(b.v);
    if (b.tick != null)
      el("text", { x: xc, y: padT + plotH + 16, "text-anchor": "middle", class: "tick" }, svg).textContent = b.tick;
    const hit = el("rect", { x: padL + i * slot, y: padT, width: slot, height: plotH, fill: "transparent", tabindex: 0 }, svg);
    hit.setAttribute("aria-label", b.aria || "");
    const over = ev => {
      const box = hit.getBoundingClientRect();
      showTip(ev.clientX ?? box.x, ev.clientY ?? box.y, b.tipTitle, b.tipRows);
    };
    hit.addEventListener("pointermove", over);
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("focus", over);
    hit.addEventListener("blur", hideTip);
  });
}

window.tw = { bandedBars, dayHourHistogram, lineChart, barChart, css, fmt };

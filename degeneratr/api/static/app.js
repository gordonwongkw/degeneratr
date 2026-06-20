"use strict";

const $ = (id) => document.getElementById(id);
// ---- formatting helpers ----
const money2 = (v) => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v) => (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
const pctStr = (v) => (v >= 0 ? "+" : "") + v.toFixed(1) + "%";

// ============ CHARTS TAB (lightweight-charts) ============
let chartsRegistry = [];   // [{symbol, chart, series, ...}]
let chartsLoaded = false;
let lastCharts = [];       // raw chart dicts from the most recent load/tick (for re-sort + tape)

const IND_COLORS = {
  ema_fast: "#4c8dff", ema_slow: "#e0a23c",
  vwap: "#b06cf0", bb_upper: "rgba(146,155,171,0.55)", bb_lower: "rgba(146,155,171,0.55)",
};
// High-contrast marker palette (item #1 — exits/signals were too dull to see).
const MK = {
  sigBull: "#19e29b", sigBear: "#ff4d5e",
  entLong: "#4c8dff", entShort: "#c884ff",
  exitWin: "#19e29b", exitLoss: "#ff4d5e",
};

function destroyCharts() {
  chartsRegistry.forEach((c) => { try { c.chart.remove(); } catch (e) {} });
  chartsRegistry = [];
  $("charts-grid").innerHTML = "";
}

// epoch (UTC seconds) -> ET wall clock parts (bar time is ET-wall-clock-as-UTC)
const _p2 = (n) => String(n).padStart(2, "0");
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const fmtEpoch = (t) => { const d = new Date(t * 1000); return `${_p2(d.getUTCMonth() + 1)}-${_p2(d.getUTCDate())} ${_p2(d.getUTCHours())}:${_p2(d.getUTCMinutes())}`; };
const fmtFull = fmtEpoch;
const dayKey = (t) => { const d = new Date(t * 1000); return `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`; };
const dayLabel = (t) => { const d = new Date(t * 1000); return `${WD[d.getUTCDay()]} ${d.getUTCMonth() + 1}/${d.getUTCDate()}`; };

const BARS_PER_DAY = { "1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7 };

// Group candles into trading days (item #5 separators + #6 day selector).
function computeDays(candles) {
  const days = [];
  let cur = null;
  candles.forEach((b, i) => {
    const k = dayKey(b.time);
    if (!cur || cur.key !== k) {
      if (cur) days.push(cur);
      cur = { key: k, label: dayLabel(b.time), fromTime: b.time, toTime: b.time, fromIdx: i, toIdx: i };
    } else { cur.toTime = b.time; cur.toIdx = i; }
  });
  if (cur) days.push(cur);
  return days;
}

// ---- ranking (#8) ----
const METRIC = {
  atr: (c) => c.atr_pct || 0,
  day: (c) => Math.abs(c.day_change_pct || 0),
  trades: (c) => (c.trades ? c.trades.length : 0),
  net: (c) => c.net_pnl || 0,
};
function sortCharts(charts, metric) {
  const f = METRIC[metric] || METRIC.atr;
  return [...charts].sort((a, b) => f(b) - f(a));
}
// Re-order existing DOM cards by metric without rebuilding the charts (keeps zoom/state).
function reorderCards(metric) {
  const f = METRIC[metric] || METRIC.atr;
  const grid = $("charts-grid");
  [...chartsRegistry]
    .sort((a, b) => f(b.metrics) - f(a.metrics))
    .forEach((e) => grid.appendChild(e.el.closest(".chart-card")));
}
const metricChip = (c) => {
  const m = $("c-sort").value;
  if (m === "day") return `${pct(c.day_change_pct || 0)} today`;
  if (m === "trades") return `${c.trades ? c.trades.length : 0} trades`;
  if (m === "net") return money2(c.net_pnl || 0);
  return `ATR ${(c.atr_pct || 0).toFixed(2)}%`;
};

// ---- trade log (filtered to the selected day, #6) ----
function tradeRowsHTML(trades) {
  if (!trades.length) return `<div class="no-trades">No trades on this day.</div>`;
  const rows = trades.map((t) => {
    const cls = t.dir === "bull" ? "call" : "put";
    return `<tr>
      <td class="num">${t.n}</td>
      <td><span class="badge ${cls}">${t.dir === "bull" ? "long" : "short"}</span></td>
      <td>${fmtEpoch(t.entry_time)}</td>
      <td class="num">${t.entry_price.toFixed(2)}</td>
      <td>${fmtEpoch(t.exit_time)}</td>
      <td class="num">${t.exit_price.toFixed(2)}</td>
      <td class="num ${t.pnl >= 0 ? "pos" : "neg"}">${money2(t.pnl)}</td>
      <td class="num ${t.pnl >= 0 ? "pos" : "neg"}">${pctStr(t.pnl_pct)}</td>
      <td>${t.exit_reason}</td>
    </tr>`;
  }).join("");
  return `<div class="trade-log-wrap"><table class="trade-log-table">
    <thead><tr><th class="num">#</th><th>Dir</th><th>Entry</th><th class="num">In $</th>
      <th>Exit</th><th class="num">Out $</th><th class="num">P&amp;L</th><th class="num">%</th><th>Exit</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}
const tradesOnDay = (trades, key) => (key === "all" ? trades : trades.filter((t) => dayKey(t.entry_time) === key));

// Refresh a card's trade log, summary line, entry→exit links and zoom for the
// selected day (links are only drawn for this day's trades — cheap).
function applyDay(entry) {
  const sel = entry.daySel.value;
  const trades = tradesOnDay(entry.trades, sel);
  entry.logBody.innerHTML = tradeRowsHTML(trades);
  const net = trades.reduce((a, t) => a + t.pnl, 0);
  entry.logSummary.innerHTML =
    `Trade log — ${trades.length} round-trips · <em class="${net >= 0 ? "pos" : "neg"}">${money2(net)}</em>` +
    (sel === "all" ? "" : ` · ${dayLabel(entry.days.find((d) => d.key === sel)?.fromTime || 0)}`);
  buildLinks(entry, trades);
  // Zoom the chart to the selected day (full series stays loaded; pan still works).
  if (sel !== "all") {
    const d = entry.days.find((x) => x.key === sel);
    if (d) entry.chart.timeScale().setVisibleRange({ from: d.fromTime, to: d.toTime + 60 });
  }
}

// Most recent day that actually has trades; else the most recent day; else "all".
function defaultDayKey(entry) {
  const tradeDays = new Set(entry.trades.map((t) => dayKey(t.entry_time)));
  for (let i = entry.days.length - 1; i >= 0; i--) {
    if (tradeDays.has(entry.days[i].key)) return entry.days[i].key;
  }
  return entry.days.length ? entry.days[entry.days.length - 1].key : "all";
}
function buildDayOptions(entry) {
  const opts = entry.days.slice().reverse()
    .map((d) => `<option value="${d.key}">${d.label}</option>`).join("");
  entry.daySel.innerHTML = opts + `<option value="all">All days</option>`;
  // default to the most recent day with trades, so the log isn't empty when the
  // 5m candles are fresher than the 15m trade data (or on a no-trade session).
  entry.daySel.value = defaultDayKey(entry);
}

// ---- vertical session separators primitive (#5) ----
function makeSessionSep(getDays) {
  let chart = null;
  const view = {
    zOrder: () => "bottom",
    renderer: () => ({
      draw: (target) => {
        if (!chart) return;
        target.useMediaCoordinateSpace((scope) => {
          const ctx = scope.context;
          const ts = chart.timeScale();
          const h = scope.mediaSize.height;
          ctx.save();
          getDays().forEach((d) => {
            const x = ts.timeToCoordinate(d.fromTime);
            if (x === null) return;
            ctx.strokeStyle = "rgba(146,155,171,0.20)";
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 4]);
            ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h); ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = "rgba(180,188,200,0.85)";
            ctx.font = "600 10px Inter, sans-serif";
            ctx.fillText(d.label, x + 5, h - 7);
          });
          ctx.restore();
        });
      },
    }),
  };
  return {
    attached: (p) => { chart = p.chart; },
    detached: () => { chart = null; },
    updateAllViews: () => {},
    paneViews: () => [view],
  };
}

// Signature of a trade list — lets live mode skip rebuilding when nothing changed.
const tradeSig = (trades) => trades.length + ":" + (trades.length ? trades[trades.length - 1].exit_time : 0);

// Trade markers cover ALL trades (one cheap setMarkers call). Entry→exit LINK
// series are expensive (one series each), so they're built per selected day in
// applyDay() — not for the whole 60-day window — which keeps the chart fast.
function buildTradeMarkers(entry, trades) {
  const tradeMarkers = [];
  trades.forEach((t) => {
    tradeMarkers.push({
      time: t.entry_time, position: t.dir === "bull" ? "belowBar" : "aboveBar",
      color: t.dir === "bull" ? MK.entLong : MK.entShort,
      shape: t.dir === "bull" ? "arrowUp" : "arrowDown", text: "#" + t.n, size: 1.4,
    });
    tradeMarkers.push({
      time: t.exit_time, position: t.win ? "aboveBar" : "belowBar",
      color: t.win ? MK.exitWin : MK.exitLoss, shape: "circle", size: 1.8,
    });
  });
  entry.tradeMarkers = tradeMarkers;
  entry.entryByTime = groupByTime(trades, "entry_time");
  entry.exitByTime = groupByTime(trades, "exit_time");
}
function buildLinks(entry, trades) {
  (entry.links || []).forEach((s) => { try { entry.chart.removeSeries(s); } catch (e) {} });
  const links = [];
  trades.forEach((t) => {
    if (t.entry_time >= t.exit_time) return;
    const s = entry.chart.addLineSeries({
      color: t.win ? "rgba(34,211,238,0.9)" : "rgba(251,113,133,0.9)",
      lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false, autoscaleInfoProvider: () => null,
    });
    s.setData([
      { time: t.entry_time, value: t.entry_price },
      { time: t.exit_time, value: t.exit_price },
    ]);
    links.push(s);
  });
  entry.links = links;
  const showLinks = (!$("t-links") || $("t-links").checked) && $("t-trades").checked;
  entry.links.forEach((s) => s.applyOptions({ visible: showLinks }));
}
function buildSignalMarkers(signals) {
  return signals.map((s) => ({
    time: s.time, position: s.dir === "bull" ? "belowBar" : "aboveBar",
    color: s.dir === "bull" ? MK.sigBull : MK.sigBear,
    shape: s.dir === "bull" ? "arrowUp" : "arrowDown", text: "", size: 1.2,
  }));
}
const groupByTime = (trades, key) => {
  const m = new Map();
  trades.forEach((t) => { if (!m.has(t[key])) m.set(t[key], []); m.get(t[key]).push(t); });
  return m;
};

function makeChart(c, period) {
  const card = document.createElement("div");
  card.className = "chart-card";
  const chg = c.day_change_pct >= 0 ? "pos" : "neg";
  const netCls = c.net_pnl >= 0 ? "pos" : "neg";
  card.innerHTML =
    `<div class="chart-card-head">
       <span class="sym">${c.symbol}</span>
       <span class="last">${money2(c.last)} <em class="chg ${chg}">${pct(c.day_change_pct)}</em></span>
       <span class="metric-chip">${metricChip(c)}</span>
       <span class="sig-count">${c.trades.length} trades · <em class="${netCls}">${money2(c.net_pnl)}</em></span>
       <label class="day-pick">Day <select class="day-sel"></select></label>
     </div>
     <div class="lwc-wrap">
       <div class="chart-legend">
         <span class="leg-ema"><i style="background:#4c8dff"></i>EMA 9</span>
         <span class="leg-ema"><i style="background:#e0a23c"></i>EMA 21</span>
         <span class="leg-vwap"><i style="background:#b06cf0"></i>VWAP</span>
         <span class="leg-bb"><i style="background:rgba(146,155,171,0.9)"></i>Bollinger</span>
       </div>
       <div class="lwc" id="lwc-${c.symbol}"></div>
     </div>
     <details class="trade-log">
       <summary class="log-summary">Trade log</summary>
       <div class="log-body"></div>
     </details>`;
  $("charts-grid").appendChild(card);

  const el = card.querySelector(".lwc");
  const chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: el.clientHeight || 340,
    layout: { background: { color: "transparent" }, textColor: "#939bab", fontFamily: "Inter, sans-serif" },
    grid: { vertLines: { color: "rgba(128,128,128,0.05)" }, horzLines: { color: "rgba(128,128,128,0.07)" } },
    timeScale: {
      timeVisible: true, secondsVisible: false, borderColor: "rgba(128,128,128,0.18)",
      barSpacing: 11, minBarSpacing: 4, rightOffset: 6,
    },
    rightPriceScale: { borderColor: "rgba(128,128,128,0.18)", autoScale: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  const candle = chart.addCandlestickSeries({
    upColor: "#21b582", downColor: "#f0595a", wickUpColor: "#21b582",
    wickDownColor: "#f0595a", borderVisible: false,
  });
  candle.setData(c.candles);

  const series = {};
  const addLine = (key, width, dashed) => {
    const s = chart.addLineSeries({
      color: IND_COLORS[key], lineWidth: width, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
      lineStyle: dashed ? LightweightCharts.LineStyle.Dotted : LightweightCharts.LineStyle.Solid,
      autoscaleInfoProvider: () => null,
    });
    s.setData(c.indicators[key] || []);
    series[key] = s;
  };
  addLine("ema_fast", 2); addLine("ema_slow", 2);
  addLine("vwap", 1, true);
  addLine("bb_upper", 1); addLine("bb_lower", 1);

  const entry = {
    symbol: c.symbol, el, chart, candle, series,
    days: computeDays(c.candles),
    trades: c.trades,
    links: [],
    signalMarkers: buildSignalMarkers(c.signals),
    lastSignalTime: c.signals.length ? c.signals[c.signals.length - 1].time : 0,
    lastBarTime: c.candles.length ? c.candles[c.candles.length - 1].time : 0,
    lastPrice: c.last,
    tradeSig: tradeSig(c.trades),
    sigByTime: new Map(c.signals.map((s) => [s.time, s])),
    metrics: c,
    card,
    daySel: card.querySelector(".day-sel"),
    logBody: card.querySelector(".log-body"),
    logSummary: card.querySelector(".log-summary"),
  };

  // vertical day separators (reads entry.days live, so it stays correct on updates)
  if (candle.attachPrimitive) {
    try { candle.attachPrimitive(makeSessionSep(() => entry.days)); } catch (e) {}
  }

  buildTradeMarkers(entry, c.trades);
  buildDayOptions(entry);
  entry.daySel.addEventListener("change", () => applyDay(entry));
  chartsRegistry.push(entry);
  applyToggles(entry);
  applyDay(entry);   // builds the selected day's links + trade log
  wireTooltip(entry);

  requestAnimationFrame(() => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight || 340 });
    // open on the selected day (most recent day with trades) once layout settles
    const sel = entry.daySel.value;
    const d = entry.days.find((x) => x.key === sel) || entry.days[entry.days.length - 1];
    if (d) chart.timeScale().setVisibleRange({ from: d.fromTime, to: d.toTime + 60 });
    else {
      const perDay = BARS_PER_DAY[period] || 78, total = c.candles.length;
      chart.timeScale().setVisibleLogicalRange({ from: total - Math.min(total, perDay), to: total + 2 });
    }
  });
}

// Zoom every chart to the last `days` trading days (0 = fit all).
function setZoom(days) {
  const perDay = BARS_PER_DAY[$("c-period").value] || 78;
  chartsRegistry.forEach((e) => {
    const ts = e.chart.timeScale();
    if (!days) { ts.fitContent(); return; }
    const total = e.candle.data().length;
    ts.setVisibleLogicalRange({ from: total - Math.min(total, days * perDay), to: total + 2 });
  });
}

// ---- crosshair tooltip: OHLC + any signal/trade at the hovered bar ----
function chartTooltipEl() {
  let el = document.getElementById("lwc-tt");
  if (!el) { el = document.createElement("div"); el.id = "lwc-tt"; el.className = "lwc-tt"; document.body.appendChild(el); }
  return el;
}
function wireTooltip(entry) {
  const tt = chartTooltipEl();
  entry.chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0) {
      tt.style.opacity = 0; return;
    }
    const o = param.seriesData.get(entry.candle);
    if (!o) { tt.style.opacity = 0; return; }
    const up = o.close >= o.open;
    let html = `<div class="tt-head"><span class="tt-sym">${entry.symbol}</span>` +
      `<span class="tt-time">${fmtFull(param.time)} ET</span></div>` +
      `<div class="tt-ohlc">` +
      `<span><i>O</i>${o.open.toFixed(2)}</span><span><i>H</i>${o.high.toFixed(2)}</span>` +
      `<span><i>L</i>${o.low.toFixed(2)}</span>` +
      `<span class="${up ? "pos" : "neg"}"><i>C</i>${o.close.toFixed(2)}</span></div>`;
    const sig = entry.sigByTime.get(param.time);
    if (sig) html += `<div class="tt-sig ${sig.dir}"><span class="tt-pill">` +
      `${sig.dir === "bull" ? "▲ BULLISH" : "▼ BEARISH"}</span>` +
      `<span class="tt-sc">confluence ${sig.score}</span>` +
      `${sig.reason ? `<div class="tt-reason">${sig.reason}</div>` : ""}</div>`;
    const ens = entry.entryByTime.get(param.time) || [];
    const exs = entry.exitByTime.get(param.time) || [];
    if (ens.length || exs.length) {
      html += `<div class="tt-trades">`;
      ens.forEach((t) => {
        html += `<div class="tt-trow"><span class="tt-badge ${t.dir === "bull" ? "long" : "short"}">#${t.n}</span>` +
          `<span class="tt-act">Entry ${t.dir === "bull" ? "long" : "short"}</span>` +
          `<b class="tt-px">${t.entry_price.toFixed(2)}</b></div>`;
      });
      exs.forEach((t) => {
        html += `<div class="tt-trow"><span class="tt-badge ${t.win ? "win" : "loss"}">#${t.n}</span>` +
          `<span class="tt-act">Exit · ${t.exit_reason}</span>` +
          `<b class="${t.pnl >= 0 ? "pos" : "neg"}">${(t.pnl >= 0 ? "+$" : "-$") + Math.abs(Math.round(t.pnl))}</b></div>`;
      });
      html += `</div>`;
    }
    tt.innerHTML = html;
    const rect = entry.el.getBoundingClientRect();
    tt.style.opacity = 1;
    let x = rect.left + window.scrollX + param.point.x + 16;
    const y = rect.top + window.scrollY + param.point.y + 14;
    if (x + tt.offsetWidth > window.scrollX + document.documentElement.clientWidth - 8) {
      x = rect.left + window.scrollX + param.point.x - tt.offsetWidth - 16;
    }
    tt.style.left = x + "px"; tt.style.top = y + "px";
  });
}

// Keep charts sized to their grid cells on window resize.
let _chartsResizeWired = false;
function wireChartsResize() {
  if (_chartsResizeWired) return;
  _chartsResizeWired = true;
  let raf = null;
  window.addEventListener("resize", () => {
    if (raf) cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() =>
      chartsRegistry.forEach((e) => e.chart.applyOptions({ width: e.el.clientWidth })));
  });
}

function applyToggles(entry) {
  const showEma = $("t-ema").checked, showVwap = $("t-vwap").checked,
        showBb = $("t-bb").checked, showSig = $("t-sig").checked,
        showTrades = $("t-trades").checked;
  entry.series.ema_fast.applyOptions({ visible: showEma });
  entry.series.ema_slow.applyOptions({ visible: showEma });
  entry.series.vwap.applyOptions({ visible: showVwap });
  entry.series.bb_upper.applyOptions({ visible: showBb });
  entry.series.bb_lower.applyOptions({ visible: showBb });
  const card = entry.el.closest(".chart-card");
  card.querySelectorAll(".leg-ema").forEach((x) => x.classList.toggle("off", !showEma));
  card.querySelector(".leg-vwap").classList.toggle("off", !showVwap);
  card.querySelector(".leg-bb").classList.toggle("off", !showBb);
  const showLinks = !$("t-links") || $("t-links").checked;
  entry.links.forEach((s) => s.applyOptions({ visible: showTrades && showLinks }));
  let markers = [];
  if (showSig) markers = markers.concat(entry.signalMarkers);
  if (showTrades) markers = markers.concat(entry.tradeMarkers);
  markers.sort((a, b) => a.time - b.time);
  entry.candle.setMarkers(markers);
}

// ---- ticker tape (#9) ----
function renderTickerTape(charts) {
  const tape = $("ticker-tape");
  if (!tape || !charts.length) return;
  const item = (c) => {
    const cls = (c.day_change_pct || 0) >= 0 ? "pos" : "neg";
    const arrow = (c.day_change_pct || 0) >= 0 ? "▲" : "▼";
    return `<span class="tape-item"><b>${c.symbol}</b> ${money2(c.last)} ` +
      `<em class="${cls}">${arrow} ${pct(c.day_change_pct || 0)}</em></span>`;
  };
  const row = charts.map(item).join("");
  tape.innerHTML = `<div class="tape-track">${row}${row}</div>`;
}

// ---- algorithm performance panel (persisted trade log) ----
function renderPerformance(perf) {
  const panel = $("perf-panel");
  if (!panel) return;
  const o = perf && perf.overall;
  if (!o || !o.trades) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const pf = o.profit_factor == null ? "∞" : o.profit_factor.toFixed(2);
  const stat = (label, val, cls) =>
    `<div class="perf-stat"><span class="ps-label">${label}</span><span class="ps-val ${cls || ""}">${val}</span></div>`;
  $("perf-stats").innerHTML =
    stat("Win rate", (o.win_rate * 100).toFixed(1) + "%", o.win_rate >= 0.5 ? "pos" : "neg") +
    stat("Net P&L", money2(o.net_pnl), o.net_pnl >= 0 ? "pos" : "neg") +
    stat("Profit factor", pf, (o.profit_factor == null || o.profit_factor >= 1) ? "pos" : "neg") +
    stat("Trades", `${o.trades} <em class="wl">${o.wins}W/${o.losses}L</em>`) +
    stat("Expectancy", money2(o.expectancy) + "/t", o.expectancy >= 0 ? "pos" : "neg");
  const rows = Object.entries(perf.per_symbol || {}).map(([sym, s]) => {
    const spf = s.profit_factor == null ? "∞" : s.profit_factor.toFixed(2);
    return `<tr><td>${sym}</td><td class="num">${s.trades}</td><td class="num">${s.wins}/${s.losses}</td>
      <td class="num ${s.win_rate >= 0.5 ? "pos" : "neg"}">${(s.win_rate * 100).toFixed(0)}%</td>
      <td class="num ${s.net_pnl >= 0 ? "pos" : "neg"}">${money2(s.net_pnl)}</td>
      <td class="num">${spf}</td></tr>`;
  }).join("");
  $("perf-by-symbol").innerHTML = `<table class="perf-table">
    <thead><tr><th>Ticker</th><th class="num">Trades</th><th class="num">W/L</th>
      <th class="num">Win%</th><th class="num">Net P&amp;L</th><th class="num">PF</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}
async function loadPerformance() {
  try { renderPerformance(await (await fetch("/api/performance")).json()); }
  catch (e) { /* leave the panel hidden if the endpoint isn't available */ }
}

async function loadCharts() {
  const btn = $("charts-btn");
  btn.disabled = true; btn.classList.add("loading");
  $("charts-status").className = "status";
  $("charts-status").textContent = "Loading watchlist charts…";
  $("charts-empty").classList.remove("hidden");
  try {
    const period = $("c-period").value, source = $("c-source").value;
    const data = await (await fetch(`/api/charts?period=${period}&source=${source}`)).json();
    destroyCharts();
    if (!data.charts || !data.charts.length) {
      $("charts-empty").querySelector("p").textContent =
        "No data in the store for this period. Run: python -m degeneratr ingest";
      $("charts-status").textContent = "Empty.";
      return;
    }
    $("charts-empty").classList.add("hidden");
    lastCharts = data.charts;
    const sorted = sortCharts(data.charts, $("c-sort").value);
    sorted.forEach((c) => makeChart(c, data.period));
    renderTickerTape(sorted);
    syncLogToggleLabel();
    wireChartsResize();
    loadPerformance();
    chartsLoaded = true;
    const totalTrades = data.charts.reduce((a, c) => a + c.trades.length, 0);
    const totalNet = data.charts.reduce((a, c) => a + c.net_pnl, 0);
    $("charts-status").textContent =
      `${data.charts.length} tickers · ${period} · ${totalTrades} trades · net ${money2(totalNet)} · ${data.source}`;
  } catch (e) {
    $("charts-status").className = "status error";
    $("charts-status").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false; btn.classList.remove("loading");
  }
}

// ============ LIVE MODE (poll during market hours) ============
let liveTimer = null;
const LIVE_INTERVAL_MS = 20000;

function setLiveUI(on) {
  const btn = $("live-btn");
  btn.classList.toggle("active", on);
  btn.querySelector(".live-label").textContent = on ? "Live" : "Go live";
}

function flashCard(entry, dir) {
  const card = entry.el.closest(".chart-card");
  card.classList.remove("flash-bull", "flash-bear");
  void card.offsetWidth;
  card.classList.add(dir === "bull" ? "flash-bull" : "flash-bear");
  setTimeout(() => card.classList.remove("flash-bull", "flash-bear"), 1800);
}
// subtle directional blink on every update (#7)
function pulseCard(entry, dir) {
  const card = entry.el.closest(".chart-card");
  const cls = dir > 0 ? "pulse-up" : "pulse-down";
  card.classList.remove("pulse-up", "pulse-down");
  void card.offsetWidth;
  card.classList.add(cls);
  setTimeout(() => card.classList.remove(cls), 750);
}

async function liveTick() {
  try {
    const period = $("c-period").value;
    // full payload (no light=1) so trades/entries refresh automatically (#2)
    const data = await (await fetch(`/api/charts?period=${period}&source=live&days=3`)).json();
    lastCharts = data.charts;
    const now = new Date();
    const newSignals = [];
    data.charts.forEach((c) => {
      const e = chartsRegistry.find((x) => x.symbol === c.symbol);
      if (!e) return;
      // incrementally update candles + indicators
      const from = e.lastBarTime || 0;
      c.candles.filter((b) => b.time >= from).forEach((bar) => { try { e.candle.update(bar); } catch (x) {} });
      ["ema_fast", "ema_slow", "vwap", "bb_upper", "bb_lower"].forEach((k) =>
        (c.indicators[k] || []).filter((p) => p.time >= from).forEach((p) => { try { e.series[k].update(p); } catch (x) {} }));
      if (c.candles.length) e.lastBarTime = c.candles[c.candles.length - 1].time;

      // refresh days + trades/markers/log only when something actually changed
      const prevDayCount = e.days.length;
      e.days = computeDays(c.candles);
      e.metrics = c;
      e.trades = c.trades;
      const sig = tradeSig(c.trades);
      const tradesChanged = sig !== e.tradeSig;
      const daysChanged = e.days.length !== prevDayCount;
      if (tradesChanged) { e.tradeSig = sig; buildTradeMarkers(e, c.trades); }
      e.signalMarkers = buildSignalMarkers(c.signals);
      e.sigByTime = new Map(c.signals.map((s) => [s.time, s]));
      if (daysChanged) {
        const keep = e.daySel.value;
        buildDayOptions(e);
        if ([...e.daySel.options].some((o) => o.value === keep)) e.daySel.value = keep;
      }
      applyToggles(e);
      if (tradesChanged || daysChanged) applyDay(e);

      // header: live last price + today's change + metric chip
      const card = e.el.closest(".chart-card");
      card.querySelector(".last").innerHTML =
        `${money2(c.last)} <em class="chg ${c.day_change_pct >= 0 ? "pos" : "neg"}">${pct(c.day_change_pct)}</em>`;
      card.querySelector(".metric-chip").textContent = metricChip(c);
      card.querySelector(".sig-count").innerHTML =
        `${c.trades.length} trades · <em class="${c.net_pnl >= 0 ? "pos" : "neg"}">${money2(c.net_pnl)}</em>`;

      // directional blink (#7)
      if (e.lastPrice != null && c.last !== e.lastPrice) pulseCard(e, c.last - e.lastPrice);
      e.lastPrice = c.last;

      // new signal since last tick? → strong flash + notify
      const latest = c.signals.length ? c.signals[c.signals.length - 1] : null;
      if (latest && latest.time > (e.lastSignalTime || 0)) {
        e.lastSignalTime = latest.time;
        flashCard(e, latest.dir);
        newSignals.push(`${c.symbol} ${latest.dir.toUpperCase()}`);
      }
    });
    reorderCards($("c-sort").value);
    renderTickerTape(sortCharts(data.charts, $("c-sort").value));
    const mkt = data.market_open ? "market open" : "market closed";
    $("charts-status").innerHTML =
      `<span class="live-pulse"></span> LIVE · updated ${now.toLocaleTimeString()} · ${mkt}` +
      (newSignals.length ? ` · <b>new: ${newSignals.join(", ")}</b>` : "");
    if (newSignals.length && window.Notification && Notification.permission === "granted") {
      new Notification("degeneratr signal", { body: newSignals.join(", ") });
    }
  } catch (err) {
    $("charts-status").className = "status error";
    $("charts-status").textContent = "Live update failed: " + err.message;
  }
}

async function toggleLive() {
  if (liveTimer) {  // turn OFF
    clearInterval(liveTimer);
    liveTimer = null;
    setLiveUI(false);
    $("charts-status").textContent = "Live stopped.";
    return;
  }
  if (window.Notification && Notification.permission === "default") {
    try { Notification.requestPermission(); } catch (e) {}
  }
  $("c-source").value = "live";
  await loadCharts();
  if (!chartsLoaded) return;
  setLiveUI(true);
  await liveTick();
  liveTimer = setInterval(liveTick, LIVE_INTERVAL_MS);
}

// ---- expand / collapse all trade logs (#10) ----
let _logsOpen = false;
function syncLogToggleLabel() {
  const btn = $("toggle-logs");
  if (btn) btn.textContent = _logsOpen ? "Collapse all" : "Expand all";
}
function toggleAllLogs() {
  _logsOpen = !_logsOpen;
  document.querySelectorAll(".chart-card .trade-log").forEach((d) => { d.open = _logsOpen; });
  syncLogToggleLabel();
}

// ---- wire up ----
$("charts-btn").addEventListener("click", () => { if (liveTimer) toggleLive(); loadCharts(); });
$("live-btn").addEventListener("click", toggleLive);
["c-period", "c-source"].forEach((id) => $(id).addEventListener("change", () => {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; setLiveUI(false); }
  loadCharts();
}));
$("c-sort").addEventListener("change", () => {
  if (chartsLoaded) { reorderCards($("c-sort").value); renderTickerTape(sortCharts(lastCharts, $("c-sort").value)); chartsRegistry.forEach((e) => e.card.querySelector(".metric-chip").textContent = metricChip(e.metrics)); }
});
$("toggle-logs").addEventListener("click", toggleAllLogs);
["t-ema", "t-vwap", "t-bb", "t-sig", "t-trades", "t-links"].forEach((id) =>
  $(id).addEventListener("change", () => chartsRegistry.forEach(applyToggles)));
document.querySelectorAll(".zoom button[data-zoom]").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".zoom button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    setZoom(parseInt(b.dataset.zoom, 10));
  }));

// ---- US market status (live ET clock with trading day + seconds, #12) ----
function updateMarketStatus() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t) => (parts.find((x) => x.type === t) || {}).value;
  const wd = get("weekday"), mo = get("month"), dy = get("day");
  let hh = +get("hour"); const mm = +get("minute"), ss = +get("second");
  if (hh === 24) hh = 0;
  const weekday = !["Sat", "Sun"].includes(wd);
  const mins = hh * 60 + mm;
  const open = weekday && mins >= 9 * 60 + 30 && mins < 16 * 60;
  const el = $("mkt-status");
  el.classList.toggle("open", open);
  el.classList.toggle("closed", !open);
  el.querySelector(".mkt-label").textContent = open ? "Market Open" : "Market Closed";
  $("mkt-clock").textContent = `${wd} ${mo}/${dy} · ${_p2(hh)}:${_p2(mm)}:${_p2(ss)} ET`;
}
updateMarketStatus();
setInterval(updateMarketStatus, 1000);

loadCharts();

"use strict";

const $ = (id) => document.getElementById(id);
// ---- formatting helpers ----
const money = (v) => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
const money2 = (v) => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v) => (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
const expShort = (e) => (e && e.length >= 10 ? e.slice(5) : (e || "—"));
const fmtStrike = (s) => (s ? s.toLocaleString(undefined, { maximumFractionDigits: 1 }) : "—");
const fmtTime = (iso) => iso.replace("T", " ").slice(5, 16);
const fmtAxis = (iso) => (iso ? iso.slice(5, 10) + " " + iso.slice(11, 16) : ""); // MM-DD HH:MM
const contractLabel = (t) => `${t.right || "?"} ${fmtStrike(t.strike)} ${expShort(t.expiry)}`;
const pnlPct = (t) => (t.entry_price > 0 ? (t.pnl / (t.entry_price * t.qty * 100)) * 100 : 0);
const pctStr = (v) => (v >= 0 ? "+" : "") + v.toFixed(1) + "%";

// ============ CHARTS TAB (lightweight-charts) ============
let chartsRegistry = [];   // [{symbol, chart, series:{}, markers:[]}]
let chartsLoaded = false;

const IND_COLORS = {
  ema_fast: "#4c8dff", ema_slow: "#e0a23c",
  vwap: "#b06cf0", bb_upper: "rgba(146,155,171,0.55)", bb_lower: "rgba(146,155,171,0.55)",
};

function destroyCharts() {
  chartsRegistry.forEach((c) => { try { c.chart.remove(); } catch (e) {} });
  chartsRegistry = [];
  $("charts-grid").innerHTML = "";
}

// epoch (UTC seconds) -> "MM-DD HH:MM" matching the bar wall-clock
const fmtEpoch = (t) => {
  const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};
const BARS_PER_DAY = { "1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7 };

function tradeLogHTML(c) {
  if (!c.trades.length) return `<div class="no-trades">No trades in this window.</div>`;
  const rows = c.trades.map((t) => {
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

function makeChart(c, period) {
  const card = document.createElement("div");
  card.className = "chart-card";
  const chg = c.change_pct >= 0 ? "pos" : "neg";
  const netCls = c.net_pnl >= 0 ? "pos" : "neg";
  card.innerHTML =
    `<div class="chart-card-head">
       <span class="sym">${c.symbol}</span>
       <span class="last">${money2(c.last)} <em class="chg ${chg}">${pct(c.change_pct)}</em></span>
       <span class="sig-count">${c.trades.length} trades · <em class="${netCls}">${money2(c.net_pnl)}</em></span>
     </div>
     <div class="lwc" id="lwc-${c.symbol}"></div>
     <details class="trade-log">
       <summary>Trade log — ${c.trades.length} round-trips (entry → exit, P&amp;L)</summary>
       ${tradeLogHTML(c)}
     </details>`;
  $("charts-grid").appendChild(card);

  const el = card.querySelector(".lwc");
  const chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: el.clientHeight || 340,
    layout: { background: { color: "transparent" }, textColor: "#939bab", fontFamily: "Inter, sans-serif" },
    grid: { vertLines: { color: "rgba(128,128,128,0.05)" }, horzLines: { color: "rgba(128,128,128,0.07)" } },
    timeScale: {
      timeVisible: true, secondsVisible: false, borderColor: "rgba(128,128,128,0.18)",
      barSpacing: 13, minBarSpacing: 4, rightOffset: 6,  // readable candle width, can't go hairline
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
    });
    s.setData(c.indicators[key] || []);
    series[key] = s;
  };
  addLine("ema_fast", 2); addLine("ema_slow", 2);
  addLine("vwap", 1, true);
  addLine("bb_upper", 1); addLine("bb_lower", 1);

  // Raw signal-onset markers (every bull/bear flip).
  const signalMarkers = c.signals.map((s) => ({
    time: s.time,
    position: s.dir === "bull" ? "belowBar" : "aboveBar",
    color: s.dir === "bull" ? "#3a7d63" : "#a14647",
    shape: s.dir === "bull" ? "arrowUp" : "arrowDown",
    text: "",
  }));
  // Actual trade markers: entry (circle, direction) + exit (square, win/loss).
  const tradeMarkers = [];
  c.trades.forEach((t) => {
    tradeMarkers.push({
      time: t.entry_time, position: t.dir === "bull" ? "belowBar" : "aboveBar",
      color: "#4c8dff", shape: "circle", text: "#" + t.n,
    });
    tradeMarkers.push({
      time: t.exit_time, position: t.win ? "aboveBar" : "belowBar",
      color: t.win ? "#21b582" : "#f0595a", shape: "square", text: "",
    });
  });

  const entry = {
    symbol: c.symbol, el, chart, candle, series, signalMarkers, tradeMarkers,
    lastSignalTime: c.signals.length ? c.signals[c.signals.length - 1].time : 0,
    lastBarTime: c.candles.length ? c.candles[c.candles.length - 1].time : 0,
    sigByTime: new Map(c.signals.map((s) => [s.time, s])),
    entryByTime: new Map(c.trades.map((t) => [t.entry_time, t])),
    exitByTime: new Map(c.trades.map((t) => [t.exit_time, t])),
  };
  chartsRegistry.push(entry);
  applyToggles(entry);
  wireTooltip(entry);
  // Size after layout settles, then anchor to the most recent bars. Candle
  // width is governed by barSpacing/minBarSpacing, so it stays readable and
  // scales with the chart instead of cramming the whole window in.
  requestAnimationFrame(() => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight || 340 });
    chart.timeScale().scrollToRealTime();
  });
}

// ---- crosshair tooltip: OHLC + any signal/trade at the hovered bar ----
function chartTooltipEl() {
  let el = document.getElementById("lwc-tt");
  if (!el) { el = document.createElement("div"); el.id = "lwc-tt"; el.className = "lwc-tt"; document.body.appendChild(el); }
  return el;
}
function fmtFull(t) {
  const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
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
    let html = `<div class="tt-h">${entry.symbol} · ${fmtFull(param.time)}</div>` +
      `<div class="tt-ohlc"><span>O ${o.open.toFixed(2)}</span><span>H ${o.high.toFixed(2)}</span>` +
      `<span>L ${o.low.toFixed(2)}</span><span class="${up ? "pos" : "neg"}">C ${o.close.toFixed(2)}</span></div>`;
    const sig = entry.sigByTime.get(param.time);
    if (sig) html += `<div class="tt-sig ${sig.dir}">${sig.dir === "bull" ? "▲ BULL" : "▼ BEAR"} signal · score ${sig.score}` +
      `${sig.reason ? ` · ${sig.reason}` : ""}</div>`;
    const en = entry.entryByTime.get(param.time);
    if (en) html += `<div class="tt-tr">● Entry #${en.n} ${en.dir === "bull" ? "LONG" : "SHORT"} @ ${en.entry_price.toFixed(2)}</div>`;
    const ex = entry.exitByTime.get(param.time);
    if (ex) html += `<div class="tt-tr ${ex.win ? "pos" : "neg"}">■ Exit #${ex.n} · ${money2(ex.pnl)} · ${ex.exit_reason}</div>`;
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
  let markers = [];
  if (showSig) markers = markers.concat(entry.signalMarkers);
  if (showTrades) markers = markers.concat(entry.tradeMarkers);
  markers.sort((a, b) => a.time - b.time);
  entry.candle.setMarkers(markers);
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
    data.charts.forEach((c) => makeChart(c, data.period));
    wireChartsResize();
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

// ============ LIVE MODE (poll Tiger during market hours) ============
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
  // reflow so the animation restarts even on back-to-back signals
  void card.offsetWidth;
  card.classList.add(dir === "bull" ? "flash-bull" : "flash-bear");
  setTimeout(() => card.classList.remove("flash-bull", "flash-bear"), 1800);
}

async function liveTick() {
  try {
    const period = $("c-period").value;
    const data = await (await fetch(`/api/charts?period=${period}&source=live&days=3&light=1`)).json();
    const now = new Date();
    const newSignals = [];
    data.charts.forEach((c) => {
      const e = chartsRegistry.find((x) => x.symbol === c.symbol);
      if (!e) return;
      // Incrementally update only the current/new bars (update() can't touch
      // history). Wrapped so an out-of-order point never breaks the tick.
      const from = e.lastBarTime || 0;
      c.candles.filter((b) => b.time >= from).forEach((bar) => {
        try { e.candle.update(bar); } catch (x) {}
      });
      ["ema_fast", "ema_slow", "vwap", "bb_upper", "bb_lower"].forEach((k) =>
        (c.indicators[k] || []).filter((p) => p.time >= from).forEach((p) => {
          try { e.series[k].update(p); } catch (x) {}
        }));
      if (c.candles.length) e.lastBarTime = c.candles[c.candles.length - 1].time;
      // Rebuild signal markers (trade markers are left from the last full load).
      e.signalMarkers = c.signals.map((s) => ({
        time: s.time, position: s.dir === "bull" ? "belowBar" : "aboveBar",
        color: s.dir === "bull" ? "#3a7d63" : "#a14647",
        shape: s.dir === "bull" ? "arrowUp" : "arrowDown", text: "",
      }));
      e.sigByTime = new Map(c.signals.map((s) => [s.time, s]));
      applyToggles(e);
      // Header: live last price + change.
      const card = e.el.closest(".chart-card");
      card.querySelector(".last").innerHTML =
        `${money2(c.last)} <em class="chg ${c.change_pct >= 0 ? "pos" : "neg"}">${pct(c.change_pct)}</em>`;
      // New signal since last tick?
      const latest = c.signals.length ? c.signals[c.signals.length - 1] : null;
      if (latest && latest.time > (e.lastSignalTime || 0)) {
        e.lastSignalTime = latest.time;
        flashCard(e, latest.dir);
        newSignals.push(`${c.symbol} ${latest.dir.toUpperCase()}`);
      }
    });
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
  // turn ON: full reload from live, then poll
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

// ---- wire up ----
$("charts-btn").addEventListener("click", () => { if (liveTimer) toggleLive(); loadCharts(); });
$("live-btn").addEventListener("click", toggleLive);
["c-period", "c-source"].forEach((id) => $(id).addEventListener("change", () => {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; setLiveUI(false); }
  loadCharts();
}));
["t-ema", "t-vwap", "t-bb", "t-sig", "t-trades"].forEach((id) =>
  $(id).addEventListener("change", () => chartsRegistry.forEach(applyToggles)));

loadCharts();

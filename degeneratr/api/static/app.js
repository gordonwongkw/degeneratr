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
      // Don't let indicators (esp. the wide Bollinger band) stretch the price
      // axis — the candles drive the scale so they fill the height.
      autoscaleInfoProvider: () => null,
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
  // Trade markers — clean: entry = a small direction arrow with the trade #,
  // exit = a small win/loss dot. No floating P&L text (it cluttered the chart
  // and collided when trades exited on the same bar); the P&L shows on hover and
  // in the trade log. Wins sit above the bar, losses below, so a same-bar
  // win+loss never overlap.
  const tradeMarkers = [];
  c.trades.forEach((t) => {
    tradeMarkers.push({
      time: t.entry_time, position: t.dir === "bull" ? "belowBar" : "aboveBar",
      color: t.dir === "bull" ? "#4c8dff" : "#b06cf0",
      shape: t.dir === "bull" ? "arrowUp" : "arrowDown", text: "#" + t.n,
    });
    tradeMarkers.push({
      time: t.exit_time, position: t.win ? "aboveBar" : "belowBar",
      color: t.win ? "#21b582" : "#f0595a", shape: "circle",
    });
  });

  // Subtle entry→exit connector for each trade, so a trade is easy to trace.
  // Cyan = win, rose = loss — hues picked to not clash with EMA (blue/amber),
  // VWAP (purple), Bollinger (gray), or the green/red candles.
  const links = [];
  c.trades.forEach((t) => {
    if (t.entry_time >= t.exit_time) return;
    const s = chart.addLineSeries({
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

  // Group trades by time so same-bar entries/exits are ALL captured (the old
  // Map kept only the last one, so colliding trades lost their tooltip).
  const groupByTime = (key) => {
    const m = new Map();
    c.trades.forEach((t) => { if (!m.has(t[key])) m.set(t[key], []); m.get(t[key]).push(t); });
    return m;
  };
  const entry = {
    symbol: c.symbol, el, chart, candle, series, signalMarkers, tradeMarkers, links,
    lastSignalTime: c.signals.length ? c.signals[c.signals.length - 1].time : 0,
    lastBarTime: c.candles.length ? c.candles[c.candles.length - 1].time : 0,
    sigByTime: new Map(c.signals.map((s) => [s.time, s])),
    entryByTime: groupByTime("entry_time"),
    exitByTime: groupByTime("exit_time"),
  };
  chartsRegistry.push(entry);
  applyToggles(entry);
  wireTooltip(entry);
  // Size after layout settles, then anchor to the most recent bars. Candle
  // width is governed by barSpacing/minBarSpacing, so it stays readable and
  // scales with the chart instead of cramming the whole window in.
  requestAnimationFrame(() => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight || 340 });
    // open on roughly the most recent trading day (zoom buttons change this)
    const perDay = BARS_PER_DAY[period] || 78;
    const total = c.candles.length;
    chart.timeScale().setVisibleLogicalRange({ from: total - Math.min(total, perDay), to: total + 2 });
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
  // keep the on-chart legend in sync with what's shown
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
["t-ema", "t-vwap", "t-bb", "t-sig", "t-trades", "t-links"].forEach((id) =>
  $(id).addEventListener("change", () => chartsRegistry.forEach(applyToggles)));
document.querySelectorAll(".zoom button[data-zoom]").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".zoom button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    setZoom(parseInt(b.dataset.zoom, 10));
  }));

// ---- US market status (live ET clock, open/closed) ----
function updateMarketStatus() {
  const p = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "2-digit",
    minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t) => (p.find((x) => x.type === t) || {}).value;
  const wd = get("weekday"), hh = +get("hour"), mm = +get("minute");
  const weekday = !["Sat", "Sun"].includes(wd);
  const mins = hh * 60 + mm;
  const open = weekday && mins >= 9 * 60 + 30 && mins < 16 * 60;
  const el = $("mkt-status");
  el.classList.toggle("open", open);
  el.classList.toggle("closed", !open);
  el.querySelector(".mkt-label").textContent = open ? "Market Open" : "Market Closed";
  $("mkt-clock").textContent = `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")} ET`;
}
updateMarketStatus();
setInterval(updateMarketStatus, 15000);

loadCharts();

"use strict";

const $ = (id) => document.getElementById(id);
let priceChart = null, eqChart = null, pnlChart = null;
let lastData = null;
let sortState = { key: null, dir: 1 };

// ---- tab switching ----
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
  });
});

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

// Register the datalabels plugin once, defaulted OFF so only the price-chart
// markers opt in (equity / P&L charts stay label-free).
if (typeof ChartDataLabels !== "undefined" && typeof Chart !== "undefined") {
  try { Chart.register(ChartDataLabels); } catch (e) { /* already registered */ }
  Chart.defaults.set("plugins.datalabels", { display: false });
}

// ---- load algorithm + components ----
async function loadStrategies() {
  try {
    const data = await (await fetch("/api/strategies")).json();
    const sel = $("f-strategy");
    sel.innerHTML = "";
    const gAlgo = document.createElement("optgroup");
    gAlgo.label = "Algorithm";
    const opt = document.createElement("option");
    opt.value = data.algorithm;
    opt.textContent = `${data.algorithm} (price-action)`;
    opt.selected = true;
    gAlgo.appendChild(opt);
    sel.appendChild(gAlgo);

    if (data.components && data.components.length) {
      const gComp = document.createElement("optgroup");
      gComp.label = "Components (isolate for analysis)";
      data.components.forEach((c) => {
        const o = document.createElement("option");
        o.value = c; o.textContent = c;
        gComp.appendChild(o);
      });
      sel.appendChild(gComp);
    }
    sel.addEventListener("change", () => {
      $("algo-hint").textContent = sel.value === data.algorithm
        ? "Signals from price action (EMA · VWAP · MACD · RSI · Bollinger). Bullish → buy closest OTM call, bearish → closest OTM put."
        : "Isolated component — for analysing one signal source.";
    });
  } catch (e) {
    $("bt-status").textContent = "Could not load strategies: " + e;
  }
}

// ---- run backtest ----
async function runBacktest() {
  const btn = $("run-btn");
  btn.disabled = true; btn.classList.add("loading");
  $("bt-status").className = "status";
  $("bt-status").textContent = "Running — pulling Tiger bars and replaying signals…";

  const body = {
    ticker: $("f-ticker").value.trim().toUpperCase() || "SPY",
    strategy: $("f-strategy").value,
    days: parseInt($("f-days").value, 10),
    period: $("f-period").value,
    source: $("f-source").value,
    max_loss_per_trade: parseFloat($("f-maxloss").value),
    per_trade_fraction: parseFloat($("f-frac").value),
    take_profit_pct: parseFloat($("f-tp").value) / 100,
    stop_loss_pct: parseFloat($("f-sl").value) / 100,
    max_concurrent: parseInt($("f-conc").value, 10),
    cooldown_bars: parseInt($("f-cool").value, 10),
  };

  try {
    const res = await fetch("/api/backtest", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "request failed");
    }
    lastData = await res.json();
    render(lastData);
    $("bt-status").textContent = `Done — ${lastData.trades.length} trades over ${lastData.days}d on ${lastData.ticker}.`;
  } catch (e) {
    $("bt-status").className = "status error";
    $("bt-status").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false; btn.classList.remove("loading");
  }
}

function render(d) {
  $("bt-empty").classList.add("hidden");
  $("bt-results").classList.remove("hidden");
  $("price-title").textContent = `${d.ticker} price action & trades`;
  renderCards(d);
  renderPrice(d);
  renderEquity(d);
  renderPnl(d);
  renderChips(d);
  renderTrades(d);
}

function card(label, value, cls, sub, hero) {
  return `<div class="card ${hero ? "hero" : ""}"><div class="label">${label}</div>` +
    `<div class="value ${cls || ""}">${value}</div>` +
    (sub ? `<div class="sub">${sub}</div>` : "") + `</div>`;
}

function renderCards(d) {
  const pf = d.profit_factor == null ? "∞" : d.profit_factor.toFixed(2);
  const net = d.ending_equity - d.starting_cash;
  $("metric-cards").innerHTML =
    card("Net P&amp;L", money(net), net >= 0 ? "pos" : "neg", `${pct(d.return_pct)} · net of ${money(d.total_commission)} fees`, true) +
    card("Win rate", (d.win_rate * 100).toFixed(1) + "%", "", `${d.wins}W / ${d.losses}L`) +
    card("Profit factor", pf) +
    card("Expectancy", money(d.expectancy), d.expectancy >= 0 ? "pos" : "neg", "per trade") +
    card("Max drawdown", money(d.max_drawdown), "neg") +
    card("Ending equity", money(d.ending_equity), "", `from ${money(d.starting_cash)}`) +
    card("Signals", `${d.trades.length} / ${d.signals_generated}`, "", "taken / generated") +
    card("Commission", money(d.total_commission), "", "round-trips");
}

// ---- custom HTML tooltip for the price chart ----
function ttBadge(right) {
  const cls = (right || "").toLowerCase() === "call" ? "call" : "put";
  return `<span class="badge ${cls}">${right || "—"}</span>`;
}
function dirLabel(right) {
  return (right || "").toUpperCase() === "CALL" ? "long" : "short";
}
function buildTooltipHTML(r) {
  const time = `<div class="tt-title">${fmtAxis(r.time)}</div>`;
  if (!r.t) {
    return time + `<div class="tt-uline">price <b>$${r.y.toFixed(2)}</b></div>`;
  }
  const t = r.t;
  const contract = `<div class="tt-c">${ttBadge(t.right)} ${dirLabel(t.right)} · ${t.qty.toLocaleString()} sh</div>`;
  let html = time + `<div class="tt-uline">price <b>$${r.y.toFixed(2)}</b></div>`;
  if (r.isEntry) {
    html += `<div class="tt-tag entry">TRADE #${r.n} · ENTRY</div>` + contract;
    html += `<div class="tt-row"><span>entry</span><b>${money2(t.entry_price)}</b></div>`;
  } else {
    html += `<div class="tt-tag ${t.win ? "win" : "loss"}">TRADE #${r.n} · EXIT · ${t.win ? "WIN" : "LOSS"}</div>` + contract;
    html += `<div class="tt-row"><span>stock</span><b>${money2(t.entry_price)} → ${money2(t.exit_price)}</b></div>`;
    html += `<div class="tt-row"><span>P&L</span><b class="${t.pnl >= 0 ? "pos" : "neg"}">${money2(t.pnl)} (${pctStr(t.pnl_pct)})</b></div>`;
    html += `<div class="tt-row"><span>exit</span><b>${t.exit_reason}</b></div>`;
  }
  html += `<div class="tt-reason">signal: ${t.entry_reason || "—"}</div>`;
  return html;
}
function priceTooltip(context) {
  const { chart, tooltip } = context;
  let el = document.getElementById("price-tooltip");
  if (!el) {
    el = document.createElement("div");
    el.id = "price-tooltip"; el.className = "chart-tooltip";
    document.body.appendChild(el);
  }
  if (tooltip.opacity === 0) { el.style.opacity = 0; return; }
  const dp = tooltip.dataPoints[0];
  const r = Object.assign({}, dp.raw, { isEntry: dp.dataset.label === "entry" });
  el.innerHTML = buildTooltipHTML(r);
  const rect = chart.canvas.getBoundingClientRect();
  el.style.opacity = 1;
  el.style.left = rect.left + window.scrollX + tooltip.caretX + "px";
  el.style.top = rect.top + window.scrollY + tooltip.caretY + "px";
}

// ---- price chart with trade markers ----
function renderPrice(d) {
  const series = d.price_series; // [[iso, close], ...]
  const idxByTime = new Map();
  series.forEach((p, i) => { if (!idxByTime.has(p[0])) idxByTime.set(p[0], i); });
  const priceAt = (i) => (series[i] ? series[i][1] : null);

  const line = series.map((p, i) => ({ x: i, y: p[1], time: p[0] }));
  const entries = [], winExits = [], lossExits = [];
  d.trades.forEach((t, i) => {
    const n = i + 1; // trade number, matches the table
    const ei = idxByTime.get(t.entry_time);
    const xi = idxByTime.get(t.exit_time);
    if (ei != null) entries.push({ x: ei, y: priceAt(ei), t, n, time: series[ei][0] });
    if (xi != null) (t.win ? winExits : lossExits).push({ x: xi, y: priceAt(xi), t, n, time: series[xi][0] });
  });

  const dl = (color, align) => ({
    display: "auto", formatter: (v) => "#" + v.n, color: color,
    font: { size: 9, weight: "600", family: "JetBrains Mono, monospace" },
    anchor: align === "bottom" ? "start" : "end", align: align, offset: 3, clip: true,
  });

  if (priceChart) priceChart.destroy();
  priceChart = new Chart($("priceChart"), {
    type: "line",
    data: {
      datasets: [
        { type: "line", label: "price", data: line, borderColor: "#8a93c9", backgroundColor: "rgba(124,140,255,0.06)", borderWidth: 1.5, fill: true, pointRadius: 0, tension: 0.12, order: 3, datalabels: { display: false } },
        { type: "scatter", label: "entry", data: entries, pointStyle: "circle", radius: 4, hoverRadius: 7, backgroundColor: "rgba(76,141,255,0.95)", borderColor: "#ffffff", borderWidth: 1.2, order: 1, datalabels: dl("#7aa6f5", "bottom") },
        { type: "scatter", label: "exit win", data: winExits, pointStyle: "triangle", radius: 7, hoverRadius: 10, backgroundColor: "#21b582", borderColor: "#0e5f44", borderWidth: 1, order: 0, datalabels: dl("#21b582", "top") },
        { type: "scatter", label: "exit loss", data: lossExits, pointStyle: "triangle", rotation: 180, radius: 7, hoverRadius: 10, backgroundColor: "#f0595a", borderColor: "#902f30", borderWidth: 1, order: 0, datalabels: dl("#f0595a", "top") },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: true },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false, external: priceTooltip },
      },
      scales: {
        x: {
          type: "linear", min: 0, max: Math.max(series.length - 1, 1),
          ticks: {
            maxTicksLimit: 7, color: "#939bab", font: { size: 10 },
            callback: (v) => { const i = Math.round(v); return series[i] ? fmtAxis(series[i][0]) : ""; },
          },
          grid: { color: "rgba(128,128,128,0.06)" },
        },
        y: { ticks: { callback: (v) => "$" + v.toFixed(0), color: "#939bab", font: { size: 10 } }, grid: { color: "rgba(128,128,128,0.10)" } },
      },
    },
  });
}

function renderEquity(d) {
  const vals = d.equity_curve.map((p) => p[1]);
  const times = d.equity_curve.map((p) => p[0]);
  const start = d.starting_cash;
  if (eqChart) eqChart.destroy();
  eqChart = new Chart($("eqChart"), {
    type: "line",
    data: {
      labels: vals.map((_, i) => i),
      datasets: [
        { data: vals, borderColor: "#4c8dff", backgroundColor: "rgba(76,141,255,0.10)", borderWidth: 1.5, fill: true, pointRadius: 0, tension: 0.15 },
        { data: vals.map(() => start), borderColor: "#939bab", borderWidth: 1, borderDash: [4, 4], pointRadius: 0, fill: false },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {
            title: (items) => fmtAxis(times[items[0].dataIndex]),
            label: (c) => (c.datasetIndex === 1 ? "start: " + money(c.parsed.y) : "equity: " + money(c.parsed.y)),
          },
        },
      },
      scales: {
        x: {
          ticks: {
            maxTicksLimit: 7, autoSkip: true, color: "#939bab", font: { size: 10 },
            callback: (val, index) => fmtAxis(times[index]),
          },
          grid: { display: false },
        },
        y: { ticks: { callback: (v) => "$" + (v / 1000).toFixed(0) + "k", color: "#939bab", font: { size: 10 } }, grid: { color: "rgba(128,128,128,0.10)" } },
      },
    },
  });
}

function renderPnl(d) {
  const pnls = d.trades.map((t) => t.pnl);
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart($("pnlChart"), {
    type: "bar",
    data: { labels: pnls.map((_, i) => i + 1), datasets: [{ data: pnls, backgroundColor: pnls.map((p) => (p >= 0 ? "#21b582" : "#f0595a")), borderRadius: 2 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {
            title: (items) => `trade #${items[0].dataIndex + 1} · ${d.trades[items[0].dataIndex].right} ${fmtStrike(d.trades[items[0].dataIndex].strike)}`,
            label: (c) => `${money2(c.parsed.y)} (${pctStr(pnlPct(d.trades[c.dataIndex]))})`,
          },
        },
      },
      scales: {
        x: { title: { display: true, text: "trade #", color: "#939bab", font: { size: 10 } }, ticks: { font: { size: 10 }, color: "#939bab" }, grid: { display: false } },
        y: { ticks: { callback: (v) => money(v), color: "#939bab", font: { size: 10 } }, grid: { color: "rgba(128,128,128,0.10)" } },
      },
    },
  });
}

function renderChips(d) {
  const counts = {};
  d.trades.forEach((t) => { counts[t.exit_reason] = (counts[t.exit_reason] || 0) + 1; });
  $("exit-chips").innerHTML = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span class="chip">${k} · ${v}</span>`).join("");
}

// ---- sortable trades table ----
function renderTrades(d) {
  $("trades-count").textContent = `${d.trades.length} round-trips`;
  const rows = d.trades.map((t, i) => Object.assign({ idx: i + 1, pnl_pct: pnlPct(t) }, t));
  if (sortState.key) {
    const k = sortState.key, dir = sortState.dir;
    rows.sort((a, b) => {
      let av = a[k], bv = b[k];
      if (typeof av === "string") return av.localeCompare(bv) * dir;
      return ((av ?? 0) - (bv ?? 0)) * dir;
    });
  }
  const body = document.querySelector("#trades-table tbody");
  body.innerHTML = rows.map((t) => {
    const cls = (t.right || "").toLowerCase() === "call" ? "call" : "put";
    return `<tr>
      <td class="num">${t.idx}</td>
      <td><span class="badge ${cls}">${dirLabel(t.right)}</span></td>
      <td>${fmtTime(t.entry_time)}</td>
      <td>${fmtTime(t.exit_time)}</td>
      <td class="num">${t.entry_price.toFixed(2)}</td>
      <td class="num">${t.exit_price.toFixed(2)}</td>
      <td class="num">${t.qty.toLocaleString()}</td>
      <td class="num ${t.pnl >= 0 ? "pos" : "neg"}">${money2(t.pnl)}</td>
      <td class="num ${t.pnl >= 0 ? "pos" : "neg"}">${pctStr(t.pnl_pct)}</td>
      <td class="reason-cell">${t.entry_reason || "—"}</td>
      <td>${t.exit_reason}</td>
    </tr>`;
  }).join("");
  updateSortIndicators("#trades-table");
}

function attachSorting(tableSel, rerender) {
  document.querySelectorAll(`${tableSel} thead th`).forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (!key) return;
      if (sortState.key === key) sortState.dir *= -1;
      else { sortState.key = key; sortState.dir = 1; }
      rerender();
    });
  });
}

function updateSortIndicators(tableSel) {
  document.querySelectorAll(`${tableSel} thead th`).forEach((th) => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.key === sortState.key) th.classList.add(sortState.dir === 1 ? "sorted-asc" : "sorted-desc");
  });
}

// ---- scanner ----
let lastScan = null;
let scanSort = { key: null, dir: 1 };
async function runScan() {
  const btn = $("scan-btn");
  btn.disabled = true; btn.classList.add("loading");
  $("scan-status").className = "status";
  $("scan-status").textContent = "Scanning…";
  try {
    const limit = parseInt($("s-limit").value, 10) || 20;
    const data = await (await fetch(`/api/scan?limit=${limit}`)).json();
    lastScan = data;
    if (data.error) {
      $("scan-status").className = "status error";
      $("scan-status").textContent = "Scanner error: " + data.error;
    } else {
      $("scan-status").textContent = `${data.count} candidates.`;
    }
    renderScan();
  } catch (e) {
    $("scan-status").className = "status error";
    $("scan-status").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false; btn.classList.remove("loading");
  }
}

function renderScan() {
  const data = lastScan;
  if (!data || !data.candidates || !data.candidates.length) return;
  $("scan-empty").classList.add("hidden");
  $("scan-panel").classList.remove("hidden");
  let rows = data.candidates.slice();
  if (scanSort.key) {
    const k = scanSort.key, dir = scanSort.dir;
    rows.sort((a, b) => {
      let av = a[k], bv = b[k];
      if (k === "reasons") { av = (av || []).join(); bv = (bv || []).join(); }
      if (typeof av === "string") return av.localeCompare(bv) * dir;
      return ((av ?? -1e9) - (bv ?? -1e9)) * dir;
    });
  }
  const body = document.querySelector("#scan-table tbody");
  body.innerHTML = rows.map((c) =>
    `<tr>
      <td>${c.symbol}</td>
      <td class="num">${c.score.toFixed(1)}</td>
      <td class="num">${c.iv_rank == null ? "—" : c.iv_rank.toFixed(0)}</td>
      <td class="num">${c.iv_percentile == null ? "—" : c.iv_percentile.toFixed(0)}</td>
      <td class="num">${c.net_inflow == null ? "—" : money(c.net_inflow)}</td>
      <td class="num">${c.earnings_within_days == null ? "—" : c.earnings_within_days + "d"}</td>
      <td>${c.reasons.join(", ")}</td>
    </tr>`).join("");
  document.querySelectorAll("#scan-table thead th").forEach((th) => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.key === scanSort.key) th.classList.add(scanSort.dir === 1 ? "sorted-asc" : "sorted-desc");
  });
}

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
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "rgba(128,128,128,0.18)" },
    rightPriceScale: { borderColor: "rgba(128,128,128,0.18)" },
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

  const entry = { symbol: c.symbol, el, chart, candle, series, signalMarkers, tradeMarkers };
  chartsRegistry.push(entry);
  applyToggles(entry);
  // Size after layout settles, then open zoomed to the most recent ~2 days
  // (intraday candles are unreadable when 60 days are crammed into one view).
  requestAnimationFrame(() => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight || 340 });
    const perDay = BARS_PER_DAY[period] || 26;
    const total = c.candles.length;
    const span = Math.min(total, perDay * 2);
    chart.timeScale().setVisibleLogicalRange({ from: total - span, to: total + 1 });
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

// ---- wire up ----
async function loadCoverage() {
  try {
    const c = await (await fetch("/api/coverage")).json();
    const u = (c.underlying || []);
    const syms = u.map((x) => x.symbol).join(", ") || "none";
    const o = c.options || {};
    const span = o.from ? `${o.from.slice(0, 10)}→${o.to.slice(0, 10)}` : "empty";
    $("cov-hint").textContent = `Archive: ${syms} · ${o.contracts || 0} contracts · ${span}`;
  } catch (e) {
    $("cov-hint").textContent = "Archive unavailable.";
  }
}

$("run-btn").addEventListener("click", runBacktest);
$("scan-btn").addEventListener("click", runScan);
attachSorting("#trades-table", () => { if (lastData) renderTrades(lastData); });
document.querySelectorAll("#scan-table thead th").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    if (!key) return;
    if (scanSort.key === key) scanSort.dir *= -1; else { scanSort.key = key; scanSort.dir = 1; }
    renderScan();
  });
});
// charts tab wiring
$("charts-btn").addEventListener("click", loadCharts);
["c-period", "c-source"].forEach((id) => $(id).addEventListener("change", loadCharts));
["t-ema", "t-vwap", "t-bb", "t-sig", "t-trades"].forEach((id) =>
  $(id).addEventListener("change", () => chartsRegistry.forEach(applyToggles)));
document.querySelector('.tab[data-tab="charts"]').addEventListener("click", () => {
  if (!chartsLoaded) loadCharts();
});

loadStrategies();
loadCoverage();

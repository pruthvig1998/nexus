/**
 * NEXUS Trading Dashboard — Alpine.js Application
 */

document.addEventListener("alpine:init", () => {
  Alpine.data("nexus", () => ({
    view: "dashboard",
    loading: true,
    account: {},
    positions: [],
    signals: [],
    openTrades: [],
    closedTrades: [],
    stats: {},
    pnlHistory: [],
    status: {},
    wsConnected: false,
    lastUpdate: null,
    clock: "",
    activityFeed: [],
    tradeFilter: "open",
    tradeSearch: "",
    tradePage: 1,
    tradesPerPage: 50,
    _pnlChart: null,
    _dailyChart: null,

    async init() {
      this.updateClock();
      setInterval(() => this.updateClock(), 1000);
      await this.fetchAll();
      this.loading = false;
      this.connectWS();
      setInterval(() => this.fetchAll(), 5000);
    },

    updateClock() {
      this.clock = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    },

    async fetchAll() {
      try {
        const [account, positions, signals, openTrades, closedTrades, stats, pnlHistory, status] =
          await Promise.all([
            this.api("/api/account"), this.api("/api/positions"),
            this.api("/api/signals?limit=20"), this.api("/api/trades?status=open"),
            this.api("/api/trades?status=closed&limit=100"), this.api("/api/stats"),
            this.api("/api/pnl-history?days=60"), this.api("/api/status"),
          ]);
        this.account = account; this.positions = positions; this.signals = signals;
        this.openTrades = openTrades; this.closedTrades = closedTrades;
        this.stats = stats; this.pnlHistory = pnlHistory; this.status = status;
        this.lastUpdate = new Date();
      } catch (e) { console.error("Fetch error:", e); }
    },

    async api(path) { return (await fetch(path)).json(); },

    connectWS() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws/events`);
      ws.onopen = () => { this.wsConnected = true; this.addActivity("CONNECTED", "WebSocket connected", "green"); };
      ws.onmessage = (e) => { this.handleEvent(JSON.parse(e.data)); };
      ws.onclose = () => { this.wsConnected = false; this.addActivity("DISCONNECTED", "Reconnecting...", "red"); setTimeout(() => this.connectWS(), 3000); };
      ws.onerror = () => { ws.close(); };
      this._wsPing = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
    },

    handleEvent(msg) {
      const evt = msg.event;
      const labels = {
        SIGNAL_GENERATED: { text: "Signal", color: "blue" },
        ORDER_SUBMITTED: { text: "Order submitted", color: "amber" },
        ORDER_FILLED: { text: "Order filled", color: "green" },
        POSITION_OPENED: { text: "Position opened", color: "green" },
        POSITION_CLOSED: { text: "Position closed", color: "red" },
        SCAN_COMPLETE: { text: "Scan complete", color: "blue" },
        DAILY_HALT: { text: "DAILY HALT", color: "red" },
        BROKER_CONNECTED: { text: "Broker connected", color: "green" },
      };
      const label = labels[evt];
      if (label) { this.addActivity(label.text, this.eventDetail(evt, msg.data), label.color); }
      if (["ORDER_FILLED","POSITION_OPENED","POSITION_CLOSED","ORDER_SUBMITTED","SIGNAL_GENERATED","BROKER_CONNECTED"].includes(evt)) this.fetchAll();
      if (evt === "SCAN_COMPLETE" && msg.data) this.status.scan_count = msg.data;
    },

    eventDetail(evt, data) {
      if (!data || typeof data === "string") return data || "";
      if (data.ticker) { return `${data.ticker} ${data.side || data.direction || ""}${data.pnl != null ? " P&L " + this.fmtPnl(data.pnl) : ""}`; }
      if (typeof data === "number") return `#${data}`;
      return "";
    },

    addActivity(label, detail, color) {
      this.activityFeed.unshift({ label, detail, color, time: new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) });
      if (this.activityFeed.length > 50) this.activityFeed.length = 50;
    },

    switchView(v) {
      this.view = v;
      if (v === "performance") this.$nextTick(() => { this.renderPnlChart(); this.renderDailyChart(); });
    },

    get brokerConnected() { return this.account.broker_connected === true; },

    renderPnlChart() {
      const el = document.getElementById("pnl-chart");
      if (!el || !window.LightweightCharts) return;
      el.innerHTML = "";
      const chart = LightweightCharts.createChart(el, {
        width: el.clientWidth, height: 300,
        layout: { background: { color: "#1E293B" }, textColor: "#94A3B8", fontFamily: "'JetBrains Mono', monospace" },
        grid: { vertLines: { color: "rgba(74,111,165,0.1)" }, horzLines: { color: "rgba(74,111,165,0.1)" } },
        timeScale: { borderColor: "rgba(74,111,165,0.3)" }, rightPriceScale: { borderColor: "rgba(74,111,165,0.3)" },
      });
      const sorted = [...this.pnlHistory].sort((a, b) => a.date.localeCompare(b.date));
      let cum = 0;
      const data = sorted.map(d => { cum += d.pnl || 0; return { time: d.date, value: cum }; });
      if (data.length > 0) { chart.addLineSeries({ color: "#C5A55A", lineWidth: 2 }).setData(data); }
      this._pnlChart = chart;
      new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth })).observe(el);
    },

    renderDailyChart() {
      const el = document.getElementById("daily-chart");
      if (!el || !window.LightweightCharts) return;
      el.innerHTML = "";
      const chart = LightweightCharts.createChart(el, {
        width: el.clientWidth, height: 250,
        layout: { background: { color: "#1E293B" }, textColor: "#94A3B8", fontFamily: "'JetBrains Mono', monospace" },
        grid: { vertLines: { color: "rgba(74,111,165,0.1)" }, horzLines: { color: "rgba(74,111,165,0.1)" } },
        timeScale: { borderColor: "rgba(74,111,165,0.3)" }, rightPriceScale: { borderColor: "rgba(74,111,165,0.3)" },
      });
      const sorted = [...this.pnlHistory].sort((a, b) => a.date.localeCompare(b.date));
      const data = sorted.map(d => ({ time: d.date, value: d.pnl || 0, color: (d.pnl || 0) >= 0 ? "#22C55E" : "#EF4444" }));
      if (data.length > 0) { chart.addHistogramSeries({ priceFormat: { type: "price", precision: 2 } }).setData(data); }
      this._dailyChart = chart;
      new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth })).observe(el);
    },

    fmtMoney(v) {
      if (v == null || Number(v) === 0) return "--";
      const n = Number(v);
      if (Math.abs(n) >= 1e6) return `$${(n/1e6).toFixed(2)}M`;
      if (Math.abs(n) >= 1e3) return `$${n.toLocaleString("en-US",{minimumFractionDigits:0,maximumFractionDigits:0})}`;
      return `$${n.toFixed(2)}`;
    },
    fmtMoneyOrZero(v) {
      if (v == null) return "$0.00";
      const n = Number(v);
      if (Math.abs(n) >= 1e6) return `$${(n/1e6).toFixed(2)}M`;
      if (Math.abs(n) >= 1e3) return `$${n.toLocaleString("en-US",{minimumFractionDigits:0,maximumFractionDigits:0})}`;
      return `$${n.toFixed(2)}`;
    },
    fmtPnl(v) { if (v == null) return "$0.00"; const n = Number(v); return `${n >= 0 ? "+" : ""}$${n.toFixed(2)}`; },
    fmtPct(v) { if (v == null) return "0.0%"; const n = Number(v); return `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`; },
    fmtRate(v) { if (v == null) return "0%"; return `${(Number(v) * 100).toFixed(0)}%`; },
    fmtTime(ts) { if (!ts) return "-"; return new Date(ts).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" }); },
    fmtDate(ts) { if (!ts) return "-"; return ts.substring(0, 10); },
    pnlColor(v) { if (v == null || Number(v) === 0) return "color:var(--nexus-mid)"; return Number(v) >= 0 ? "color:var(--nexus-green)" : "color:var(--nexus-red)"; },
    scoreColor(s) { s = Number(s||0); if (s >= 0.8) return "#22C55E"; if (s >= 0.65) return "#C5A55A"; return "#EF4444"; },
    activityDotColor(c) { return {green:"var(--nexus-green)",red:"var(--nexus-red)",amber:"var(--nexus-amber)",blue:"var(--nexus-border)"}[c] || "var(--nexus-dim)"; },

    get filteredTrades() {
      const src = this.tradeFilter === "open" ? this.openTrades : this.closedTrades;
      if (!this.tradeSearch) return src;
      const q = this.tradeSearch.toUpperCase();
      return src.filter(t => (t.ticker||"").toUpperCase().includes(q) || (t.strategy||"").toUpperCase().includes(q));
    },
    get pagedTrades() { const s = (this.tradePage-1)*this.tradesPerPage; return this.filteredTrades.slice(s, s+this.tradesPerPage); },
    get totalTradePages() { return Math.max(1, Math.ceil(this.filteredTrades.length / this.tradesPerPage)); },
    get longExposure() { return this.positions.filter(p=>(p.side||"LONG")==="LONG").reduce((s,p)=>s+Math.abs(p.market_value||0),0); },
    get shortExposure() { return this.positions.filter(p=>(p.side||"LONG")==="SHORT").reduce((s,p)=>s+Math.abs(p.market_value||0),0); },
    get netExposure() { return this.longExposure - this.shortExposure; },
    exposurePct(exp) { return Math.min(100, (exp / (Number(this.account.portfolio_value) || 1)) * 100); },
    get strategyBreakdown() {
      const m = {};
      for (const t of this.closedTrades) { const s = t.strategy||"unknown"; if (!m[s]) m[s]={strategy:s,count:0,wins:0,totalPnl:0}; m[s].count++; if ((t.pnl||0)>0) m[s].wins++; m[s].totalPnl += t.pnl||0; }
      return Object.values(m).sort((a,b)=>b.count-a.count);
    },
    get recentClosed() { return this.closedTrades.slice(0, 8); },
  }));
});

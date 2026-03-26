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
    brokerOrders: [],
    brokerDeals: [],
    brokerTab: 'orders',
    editingTrade: null,
    swarmDebates: [],
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
        // Client-side safety: filter equity in options mode
        if (status.options_enabled) {
          this.positions = this.positions.filter(p => (p.instrument_type||'EQUITY') !== 'EQUITY');
          this.openTrades = this.openTrades.filter(t => (t.instrument_type||'EQUITY') !== 'EQUITY');
          this.closedTrades = this.closedTrades.filter(t => (t.instrument_type||'EQUITY') !== 'EQUITY');
        }
        // Fetch swarm debates if swarm is enabled
        if (status.swarm_enabled) {
          this.swarmDebates = await this.api("/api/swarm-debates?limit=10");
        }
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
        SWARM_DEBATE: { text: "Swarm debate", color: "cyan" },
      };
      const label = labels[evt];
      if (label) { this.addActivity(label.text, this.eventDetail(evt, msg.data), label.color); }
      if (["ORDER_FILLED","POSITION_OPENED","POSITION_CLOSED","ORDER_SUBMITTED","SIGNAL_GENERATED","BROKER_CONNECTED","SWARM_DEBATE"].includes(evt)) this.fetchAll();
      if (evt === "SCAN_COMPLETE" && msg.data) this.status.scan_count = msg.data;
    },

    eventDetail(evt, data) {
      if (!data || typeof data === "string") return data || "";
      if (evt === "SWARM_DEBATE" && data.ticker) { return `${data.ticker} ${data.consensus} (${Number(data.score).toFixed(2)})${data.vetoed?" VETOED":""}`; }
      if (data.ticker) { return `${data.ticker} ${data.side || data.direction || ""}${data.pnl != null ? " P&L " + this.fmtPnl(data.pnl) : ""}`; }
      if (typeof data === "number") return `#${data}`;
      return "";
    },

    addActivity(label, detail, color) {
      this.activityFeed.unshift({ label, detail, color, time: new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) });
      if (this.activityFeed.length > 50) this.activityFeed.length = 50;
    },

    async closePosition(ticker, optionCode) {
      if (!confirm('Close this position?')) return;
      try {
        const body = { ticker };
        if (optionCode) body.option_code = optionCode;
        const resp = await fetch('/api/close-position', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.success) {
          this.addActivity('CLOSE', `Closing ${ticker}${optionCode ? ' ' + optionCode : ''}`, 'amber');
          this.fetchAll();
        } else {
          alert('Failed to close: ' + (data.error || 'Unknown error'));
        }
      } catch (e) {
        alert('Error: ' + e.message);
      }
    },

    async fetchBrokerData() {
      try {
        const [orders, deals] = await Promise.all([
          this.api('/api/broker-orders?limit=50'),
          this.api('/api/broker-deals?limit=50'),
        ]);
        this.brokerOrders = orders;
        this.brokerDeals = deals;
      } catch (e) { console.error('Broker data fetch error:', e); }
    },

    async modifyTrade(tradeId, stopPrice, targetPrice) {
      try {
        const body = { trade_id: tradeId };
        if (stopPrice !== undefined && stopPrice !== '') body.stop_price = parseFloat(stopPrice);
        if (targetPrice !== undefined && targetPrice !== '') body.target_price = parseFloat(targetPrice);
        const resp = await fetch('/api/modify-trade', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.success) {
          this.editingTrade = null;
          this.addActivity('MODIFIED', `Trade ${tradeId.substring(0,8)} updated`, 'amber');
          this.fetchAll();
        } else {
          alert('Failed: ' + (data.error || 'Unknown error'));
        }
      } catch (e) { alert('Error: ' + e.message); }
    },

    // --- New helper: format symbol like Moomoo app (for position objects) ---
    fmtSymbol(position) {
      const type = (position.instrument_type || 'EQUITY').toUpperCase();
      if (type === 'EQUITY') return position.ticker || '';
      const ticker = position.ticker || '';
      const typeLabel = type; // CALL or PUT
      const exp = position.expiration || '';
      // Format expiration as YYMMDD
      const expFormatted = exp.replace(/-/g, '').substring(2); // "2026-03-27" -> "260327"
      const strike = parseFloat(position.strike || 0).toFixed(2);
      return `${ticker} ${typeLabel} - ${expFormatted} ${strike}`;
    },

    // --- New helper: format symbol like Moomoo app (for trade objects) ---
    fmtSymbolFromTrade(trade) {
      const type = (trade.instrument_type || 'EQUITY').toUpperCase();
      if (type === 'EQUITY') return trade.ticker || '';
      const ticker = trade.ticker || '';
      const typeLabel = type; // CALL or PUT
      const exp = trade.option_expiration || trade.expiration || '';
      const expFormatted = exp.replace(/-/g, '').substring(2);
      const strike = parseFloat(trade.option_strike || trade.strike || 0).toFixed(2);
      return `${ticker} ${typeLabel} - ${expFormatted} ${strike}`;
    },

    // --- New helper: returns instrument type string ---
    positionType(p) {
      const type = (p.instrument_type || 'EQUITY').toUpperCase();
      if (type === 'CALL') return 'CALL';
      if (type === 'PUT') return 'PUT';
      return 'EQUITY';
    },

    // --- Updated fmtContract: cleaner format "YYMMDD $STRIKE C/P" ---
    fmtContract(trade) {
      if (!trade.instrument_type || trade.instrument_type === 'EQUITY') return '';
      const right = trade.instrument_type === 'CALL' ? 'C' : 'P';
      const strike = parseFloat(trade.option_strike || trade.strike || 0).toFixed(2);
      const exp = trade.option_expiration || trade.expiration || '';
      const expFormatted = exp.replace(/-/g, '').substring(2); // "2026-03-27" -> "260327"
      return `${expFormatted} $${strike} ${right}`;
    },

    // --- New helper: quantity label ("229 shares" or "3 contracts") ---
    fmtQtyLabel(p) {
      const type = (p.instrument_type || 'EQUITY').toUpperCase();
      const qty = Math.abs(p.shares || p.quantity || 0);
      if (type === 'EQUITY') return `${qty} share${qty !== 1 ? 's' : ''}`;
      return `${qty} contract${qty !== 1 ? 's' : ''}`;
    },

    // --- New helper: cost vs current price display ---
    fmtCostVsCurrent(p) {
      const cost = parseFloat(p.avg_cost || 0);
      const current = parseFloat(p.current_price || p.last_price || 0);
      return {
        cost: `$${cost.toFixed(2)}`,
        current: `$${current.toFixed(2)}`,
      };
    },

    switchView(v) {
      this.view = v;
      if (v === "performance") this.$nextTick(() => { this.renderPnlChart(); this.renderDailyChart(); });
      if (v === "broker-orders") this.fetchBrokerData();
    },

    get brokerConnected() { return this.account.broker_connected === true; },

    // --- Total P&L computed getters ---
    get totalPnl() { return this.positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0); },
    get totalPnlPct() {
      const cost = this.positions.reduce((s, p) => s + (p.avg_cost * p.shares), 0);
      return cost > 0 ? (this.totalPnl / cost * 100) : 0;
    },

    renderPnlChart() {
      const el = document.getElementById("pnl-chart");
      if (!el || !window.LightweightCharts) return;
      el.innerHTML = "";
      const chart = LightweightCharts.createChart(el, {
        width: el.clientWidth, height: 300,
        layout: { background: { color: "#06060c" }, textColor: "#94a3b8", fontFamily: "'JetBrains Mono', monospace" },
        grid: { vertLines: { color: "rgba(99, 102, 241, 0.06)" }, horzLines: { color: "rgba(99, 102, 241, 0.06)" } },
        timeScale: { borderColor: "rgba(99, 102, 241, 0.15)" }, rightPriceScale: { borderColor: "rgba(99, 102, 241, 0.15)" },
      });
      const sorted = [...this.pnlHistory].sort((a, b) => a.date.localeCompare(b.date));
      let cum = 0;
      const data = sorted.map(d => { cum += d.pnl || 0; return { time: d.date, value: cum }; });
      if (data.length > 0) { chart.addLineSeries({ color: "#818cf8", lineWidth: 2 }).setData(data); }
      this._pnlChart = chart;
      new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth })).observe(el);
    },

    renderDailyChart() {
      const el = document.getElementById("daily-chart");
      if (!el || !window.LightweightCharts) return;
      el.innerHTML = "";
      const chart = LightweightCharts.createChart(el, {
        width: el.clientWidth, height: 250,
        layout: { background: { color: "#06060c" }, textColor: "#94a3b8", fontFamily: "'JetBrains Mono', monospace" },
        grid: { vertLines: { color: "rgba(99, 102, 241, 0.06)" }, horzLines: { color: "rgba(99, 102, 241, 0.06)" } },
        timeScale: { borderColor: "rgba(99, 102, 241, 0.15)" }, rightPriceScale: { borderColor: "rgba(99, 102, 241, 0.15)" },
      });
      const sorted = [...this.pnlHistory].sort((a, b) => a.date.localeCompare(b.date));
      const data = sorted.map(d => ({ time: d.date, value: d.pnl || 0, color: (d.pnl || 0) >= 0 ? "#10b981" : "#ef4444" }));
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
    pnlColor(v) { if (v == null || Number(v) === 0) return "color:var(--nexus-text-muted)"; return Number(v) >= 0 ? "color:var(--nexus-green)" : "color:var(--nexus-red)"; },
    pnlClass(v) { if (v == null || Number(v) === 0) return ""; return Number(v) >= 0 ? "pnl-positive" : "pnl-negative"; },
    scoreColor(s) { s = Number(s||0); if (s >= 0.8) return "#10b981"; if (s >= 0.65) return "#818cf8"; return "#ef4444"; },
    activityDotColor(c) { return {green:"var(--nexus-green)",red:"var(--nexus-red)",amber:"var(--nexus-amber)",blue:"var(--nexus-accent-indigo)"}[c] || "var(--nexus-text-muted)"; },

    get filteredTrades() {
      const src = this.tradeFilter === "open" ? this.openTrades : this.closedTrades;
      if (!this.tradeSearch) return src;
      const q = this.tradeSearch.toUpperCase();
      return src.filter(t => (t.ticker||"").toUpperCase().includes(q) || (t.strategy||"").toUpperCase().includes(q));
    },
    get pagedTrades() { const s = (this.tradePage-1)*this.tradesPerPage; return this.filteredTrades.slice(s, s+this.tradesPerPage); },
    get totalTradePages() { return Math.max(1, Math.ceil(this.filteredTrades.length / this.tradesPerPage)); },
    get callExposure() { return this.positions.filter(p=>(p.instrument_type||"EQUITY")==="CALL").reduce((s,p)=>s+Math.abs(p.market_value||0),0); },
    get putExposure() { return this.positions.filter(p=>(p.instrument_type||"EQUITY")==="PUT").reduce((s,p)=>s+Math.abs(p.market_value||0),0); },
    get longExposure() { return this.positions.filter(p=>(p.side||"LONG")==="LONG"||(p.instrument_type||"")==="CALL").reduce((s,p)=>s+Math.abs(p.market_value||0),0); },
    get shortExposure() { return this.positions.filter(p=>(p.side||"LONG")==="SHORT"||(p.instrument_type||"")==="PUT").reduce((s,p)=>s+Math.abs(p.market_value||0),0); },
    get netExposure() { return this.longExposure - this.shortExposure; },
    get optionsMode() { return this.status.options_enabled === true; },
    exposurePct(exp) { return Math.min(100, (exp / (Number(this.account.portfolio_value) || 1)) * 100); },
    get strategyBreakdown() {
      const m = {};
      for (const t of this.closedTrades) { const s = t.strategy||"unknown"; if (!m[s]) m[s]={strategy:s,count:0,wins:0,totalPnl:0}; m[s].count++; if ((t.pnl||0)>0) m[s].wins++; m[s].totalPnl += t.pnl||0; }
      return Object.values(m).sort((a,b)=>b.count-a.count);
    },
    get recentClosed() { return this.closedTrades.slice(0, 8); },
  }));
});

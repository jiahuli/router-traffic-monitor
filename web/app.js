/* 前端逻辑：定时拉取 + 渲染 */
(function () {
  "use strict";

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];
  const COLORS = { down: "#38d996", up: "#ffa53d", accent: "#4c8dff" };

  const state = {
    tab: "overview",
    rtMinutes: 60,
    devPeriod: "today",
    devices: [],
    devicesPeriod: "today",
    sort: { key: "total", asc: false },
    interval: 10,
    histSeq: 0,
    svcDays: 0,
    svcData: null,
    timers: []
  };

  let chartRT = null;
  let chartHist = null;

  /* ---------------- 格式化 ---------------- */
  function splitBytes(n) {
    n = Number(n) || 0;
    let i = 0;
    while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
    const d = n >= 100 ? 0 : n >= 10 ? 1 : 2;
    return { v: n.toFixed(d), u: UNITS[i] };
  }
  function fmtBytes(n) {
    const s = splitBytes(n);
    return s.v + " " + s.u;
  }
  function fmtRate(n) {
    return fmtBytes(n) + "/s";
  }
  function statVal(el, n) {
    const s = splitBytes(n);
    el.innerHTML = `${s.v}<small>${s.u}</small>`;
  }
  function fmtSpan(sec) {
    sec = Math.round(sec);
    if (sec < 60) return sec + " 秒";
    if (sec < 3600) return Math.floor(sec / 60) + " 分钟";
    if (sec < 86400) return Math.floor(sec / 3600) + " 小时 " + Math.floor((sec % 3600) / 60) + " 分";
    return Math.floor(sec / 86400) + " 天 " + Math.floor((sec % 86400) / 3600) + " 小时";
  }
  function fmtClock(ts) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  function relTime(ts) {
    if (!ts) return "—";
    const diff = Date.now() / 1000 - ts;
    if (diff < 15) return "刚刚";
    if (diff < 90) return Math.round(diff) + " 秒前";
    if (diff < 3600) return Math.round(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.round(diff / 3600) + " 小时前";
    return Math.round(diff / 86400) + " 天前";
  }
  function toInputValue(ts) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fromInputValue(v) {
    if (!v) return 0;
    const t = new Date(v.replace(" ", "T")).getTime();
    return isNaN(t) ? 0 : Math.floor(t / 1000);
  }

  /* ---------------- 请求 ---------------- */
  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let msg = res.status + "";
      try { const j = await res.json(); msg = j.error || msg; } catch (e) {}
      throw new Error(msg);
    }
    return res.json();
  }

  function toast(msg, isErr) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.className = "toast"; }, 2600);
  }

  /* ---------------- 概览 ---------------- */
  async function refreshOverview() {
    let data;
    try {
      data = await api("/api/overview");
    } catch (e) {
      setConn(false, "接口异常：" + e.message);
      return;
    }
    const st = data.status;
    state.interval = Math.max(5, st.poll.interval || 10);

    setConn(st.online, st.last_error);
    renderHeader(st);
    statVal($("#vDown"), st.rate.down_bps);
    statVal($("#vUp"), st.rate.up_bps);
    $("#vDownFoot").textContent = "本次峰值 " + fmtRate(st.peak.down_bps);
    $("#vUpFoot").textContent = "本次峰值 " + fmtRate(st.peak.up_bps);

    const today = data.totals.today;
    statVal($("#vToday"), today.down + today.up);
    $("#vTodayFoot").textContent = `↓ ${fmtBytes(today.down)} · ↑ ${fmtBytes(today.up)}`;

    const month = data.totals.month;
    statVal($("#vMonth"), month.down + month.up);
    $("#vMonthFoot").textContent = `↓ ${fmtBytes(month.down)} · ↑ ${fmtBytes(month.up)}`;

    $("#pollMeta").textContent = `${st.poll.interval}s · 第 ${st.poll.count} 轮 · ${st.counts.devices} 台设备 · ${st.counts.connections} 条连接`;
  }

  function setConn(ok, err) {
    const pill = $("#connPill");
    const txt = $("#connText");
    if (ok) {
      pill.className = "pill ok";
      txt.textContent = "已连接";
      pill.title = "";
    } else {
      pill.className = "pill bad";
      txt.textContent = "连接中断";
      pill.title = err || "";
    }
  }

  function renderHeader(st) {
    const r = st.router;
    const parts = [];
    parts.push(`${st.ssh.host}`);
    if (r.wan_iface) parts.push(`WAN ${r.wan_ip || "—"} · ${r.wan_iface}`);
    if (r.uptime) parts.push("已运行 " + fmtSpan(r.uptime));
    parts.push(`${st.poll.interval}s 采集`);
    if (!st.online && st.last_error) parts.push("错误：" + st.last_error);
    $("#routerSub").textContent = parts.join("  ·  ");
  }

  async function refreshRealtime() {
    let d;
    try { d = await api("/api/realtime?minutes=" + state.rtMinutes); } catch (e) { return; }
    const pts = d.points || [];
    if (!pts.length) {
      chartRT.setData({ series: [] });
      $("#rtSummary").textContent = "";
      return;
    }
    const span = Math.max(1, pts[pts.length - 1][0] - pts[0][0]);
    const res = pts.length > 1 ? Math.max(1, span / (pts.length - 1)) : 10;
    const down = pts.map((p) => [p[0], p[2] / res]);
    const up = pts.map((p) => [p[0], p[1] / res]);
    const sumDown = pts.reduce((a, p) => a + p[2], 0);
    const sumUp = pts.reduce((a, p) => a + p[1], 0);
    const peakDown = Math.max.apply(null, down.map((p) => p[1]));
    const spanTxt = state.rtMinutes >= 60
      ? (state.rtMinutes / 60) + " 小时"
      : state.rtMinutes + " 分钟";
    $("#rtHint").textContent = `最近 ${spanTxt} · ${res <= 12 ? "10 秒" : "1 分钟"}粒度`;

    chartRT.setData({
      series: [
        { name: "下行", color: COLORS.down, values: down },
        { name: "上行", color: COLORS.up, values: up }
      ],
      yFmt: (v) => fmtBytes(v) + "/s"
    });
    $("#rtSummary").textContent =
      `区间 ↓ ${fmtBytes(sumDown)} · ↑ ${fmtBytes(sumUp)} · 峰值 ↓ ${fmtRate(peakDown)}`;
  }

  async function refreshTop() {
    let d;
    try { d = await api("/api/devices?period=today"); } catch (e) { return; }
    state.devices = d.devices;
    const rows = d.devices.slice(0, 8);
    const tb = $("#topDevices tbody");
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="7" class="empty">暂无设备流量数据，等待下一轮采集…</td></tr>`;
      return;
    }
    tb.innerHTML = rows.map((r) => `
      <tr>
        <td>${devCell(r)}</td>
        <td>${r.ip || "—"}</td>
        <td>${kindTag(r)}</td>
        <td class="num">${r.online ? fmtRate(r.down_bps) : "—"}</td>
        <td class="num">${r.online ? fmtRate(r.up_bps) : "—"}</td>
        <td class="num">${fmtBytes(r.down)}</td>
        <td class="num">${fmtBytes(r.up)}</td>
      </tr>`).join("");
  }

  /* ---------------- 设备 ---------------- */
  function devName(r) {
    if (r.hostname) return r.hostname;
    if (r.vendor) return r.vendor;
    if (r.mac.indexOf("ip:") === 0) return r.ip || "未知设备";
    return "未识别设备";
  }
  function devCell(r) {
    const name = devName(r);
    const initial = (name[0] || "?").toUpperCase();
    const cls = r.kind === "无线" ? "avatar wifi" : "avatar";
    const sub = r.mac.indexOf("ip:") === 0 ? (r.ip || "") : r.mac;
    return `<div class="dev-name">
      <div class="${cls}">${initial}</div>
      <div class="dev-meta"><b>${escapeHtml(name)}</b><span>${escapeHtml(sub)}</span></div>
    </div>`;
  }
  function kindTag(r) {
    if (r.kind === "无线") {
      const b = r.band ? " " + r.band : "";
      const sig = r.signal ? ` · ${r.signal}dBm` : "";
      return `<span class="tag wifi">无线${b}${sig}</span>`;
    }
    return `<span class="tag">有线</span>`;
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function refreshDevices() {
    let d;
    try { d = await api("/api/devices?period=" + state.devPeriod); } catch (e) { return; }
    state.devices = d.devices;
    state.devicesPeriod = d.period;
    renderDeviceTable(d);
    fillDeviceSelect(d.devices);
    fillSvcDevices(d.devices);
    const label = { today: "今日", week: "本周", month: "本月" }[d.period] || d.period;
    $("#devHint").textContent = `${label}累计 · 共 ${d.devices.length} 台 · WAN 合计 ↓ ${fmtBytes(d.wan.down)} / ↑ ${fmtBytes(d.wan.up)}`;
  }

  function sortDevices(list) {
    const k = state.sort.key;
    const dir = state.sort.asc ? 1 : -1;
    return list.slice().sort((a, b) => {
      let av, bv;
      if (k === "name") { av = devName(a); bv = devName(b); }
      else if (k === "ip") { av = a.ip; bv = b.ip; }
      else if (k === "kind") { av = a.kind; bv = b.kind; }
      else if (k === "online") { av = a.online ? 1 : 0; bv = b.online ? 1 : 0; }
      else { av = a[k] || 0; bv = b[k] || 0; }
      if (typeof av === "string") return av.localeCompare(bv) * dir;
      return (av - bv) * dir;
    });
  }

  function renderDeviceTable(d) {
    const tb = $("#deviceTable tbody");
    const list = sortDevices(d.devices || []);
    if (!list.length) {
      tb.innerHTML = `<tr><td colspan="10" class="empty">暂无数据，等待采集…</td></tr>`;
      return;
    }
    const maxTotal = Math.max.apply(null, list.map((r) => r.total || 0)) || 1;
    tb.innerHTML = list.map((r) => {
      const pct = Math.round(((r.total || 0) / maxTotal) * 100);
      return `<tr>
        <td>${r.online ? '<span class="tag on">在线</span>' : '<span class="tag off">离线</span>'}</td>
        <td>${devCell(r)}</td>
        <td><span class="dev-meta"><b>${r.ip || "—"}</b><span>${escapeHtml(r.mac)}</span></span></td>
        <td>${kindTag(r)}</td>
        <td class="num">${r.online ? fmtRate(r.down_bps) : "—"}</td>
        <td class="num">${r.online ? fmtRate(r.up_bps) : "—"}</td>
        <td class="num">${fmtBytes(r.down)}</td>
        <td class="num">${fmtBytes(r.up)}</td>
        <td class="bar-cell"><div class="bar"><i style="width:${pct}%"></i></div></td>
        <td><button class="link-btn" data-mac="${escapeHtml(r.mac)}">历史</button>
            <button class="link-btn" data-svcmac="${escapeHtml(r.mac)}">服务</button></td>
      </tr>`;
    }).join("");

    $$("#deviceTable .link-btn[data-mac]").forEach((b) => {
      b.addEventListener("click", () => openDeviceHistory(b.dataset.mac));
    });
    $$("#deviceTable .link-btn[data-svcmac]").forEach((b) => {
      b.addEventListener("click", () => {
        switchTab("services");
        const sel = $("#svcDevice");
        if (sel) { sel.value = b.dataset.svcmac; }
        refreshServices();
      });
    });
  }

  function fillDeviceSelect(devices) {
    const sel = $("#histMac");
    const cur = sel.value;
    const opts = ['<option value="">全部设备（WAN 总流量）</option>'];
    devices.filter((d) => d.total > 0 || d.online).forEach((d) => {
      opts.push(`<option value="${escapeHtml(d.mac)}">${escapeHtml(devName(d))} ${d.ip ? "· " + d.ip : ""}</option>`);
    });
    sel.innerHTML = opts.join("");
    if (cur) sel.value = cur;
  }

  function openDeviceHistory(mac) {
    switchTab("history");
    $("#histMac").value = mac;
    setQuickRange("7d");
    queryHistory();
  }

  /* ---------------- 服务 ---------------- */
  function srcTag(source) {
    const map = { "自定义": "custom", "推断": "infer", "端口": "port", "协议": "port", "未知": "unknown" };
    return `<span class="src-tag ${map[source] || "unknown"}">${escapeHtml(source)}</span>`;
  }

  function fillSvcDevices(devices) {
    const sel = $("#svcDevice");
    if (!sel) return;
    const cur = sel.value;
    const opts = ['<option value="">全部设备（实时）</option>'];
    devices.forEach((d) => {
      opts.push(`<option value="${escapeHtml(d.mac)}">${escapeHtml(devName(d))}${d.ip ? " · " + d.ip : ""}</option>`);
    });
    sel.innerHTML = opts.join("");
    if (cur) sel.value = cur;
  }

  async function refreshServices() {
    const days = state.svcDays;
    const mac = $("#svcDevice") ? $("#svcDevice").value : "";
    let url, data;
    try {
      if (days === 0) {
        url = "/api/services/live" + (mac ? "?mac=" + encodeURIComponent(mac) : "");
      } else {
        if (!mac) {
          renderServicesEmpty("按时间段查看需要先选择一台设备");
          return;
        }
        url = `/api/services?mac=${encodeURIComponent(mac)}&days=${days}`;
      }
      data = await api(url);
    } catch (e) {
      renderServicesEmpty("加载失败：" + e.message);
      return;
    }
    state.svcData = data;
    renderServices(data, days);
  }

  function renderServicesEmpty(msg) {
    $("#svcTable tbody").innerHTML = `<tr><td colspan="7" class="empty">${escapeHtml(msg)}</td></tr>`;
    $("#svcDetailCard").style.display = "none";
  }

  function renderServices(data, days) {
    const groups = data.groups || [];
    const details = data.services || [];
    const tb = $("#svcTable tbody");

    const down = days === 0 ? details.reduce((a, s) => a + s.down, 0) : (data.totals ? data.totals.down : 0);
    const up = days === 0 ? details.reduce((a, s) => a + s.up, 0) : (data.totals ? data.totals.up : 0);
    $("#svcDown").textContent = fmtBytes(down);
    $("#svcUp").textContent = fmtBytes(up);
    $("#svcCount").textContent = `${groups.length} / ${details.length}`;
    $("#svcHint").textContent = days === 0
      ? `当前活跃连接 · 每 ${data.interval || state.interval} 秒刷新一次`
      : `近 ${days} 天累计 · 数据按天聚合`;

    if (!groups.length) {
      renderServicesEmpty(days === 0 ? "暂无活跃连接，等待下一轮采集…" : "该时间段没有服务数据");
      return;
    }

    const total = groups.reduce((a, g) => a + g.down + g.up, 0) || 1;
    tb.innerHTML = groups.map((g) => {
      const pct = Math.round(((g.down + g.up) / total) * 100);
      return `<tr class="svc-row" data-label="${escapeHtml(g.label)}">
        <td><div class="svc-label"><b>${escapeHtml(g.label)}</b></div></td>
        <td>${srcTag(g.source)}</td>
        <td class="num">${g.targets}</td>
        <td class="num">${g.conns || "—"}</td>
        <td class="num">${fmtBytes(g.down)}</td>
        <td class="num">${fmtBytes(g.up)}</td>
        <td class="bar-cell"><div class="bar"><i style="width:${pct}%"></i></div></td>
      </tr>`;
    }).join("");

    $$("#svcTable .svc-row").forEach((tr) => {
      tr.addEventListener("click", () => openSvcDetail(tr.dataset.label, tr));
    });
    $("#svcDetailCard").style.display = "none";
  }

  function openSvcDetail(label, tr) {
    const data = state.svcData || {};
    const items = (data.services || []).filter((s) => s.label === label);
    if (!items.length) return;

    $$("#svcTable .svc-row").forEach((x) => x.classList.remove("open"));
    tr && tr.classList.add("open");
    $("#svcDetailCard").style.display = "";
    $("#svcDetailTitle").textContent = `${label} · ${items.length} 个目标`;

    $("#svcDetailTable tbody").innerHTML = items.map((s) => {
      const addr = s.port ? `${s.ip}:${s.port}/${s.proto}` : `${s.ip} (${s.proto})`;
      return `<tr>
        <td><span class="dev-meta"><b>${escapeHtml(addr)}</b><span>${escapeHtml(s.key)}</span></span></td>
        <td class="num">${s.conns || "—"}</td>
        <td class="num">${fmtBytes(s.down)}</td>
        <td class="num">${fmtBytes(s.up)}</td>
        <td><button class="link-btn" data-skey="${escapeHtml(s.key)}" data-ip="${escapeHtml(s.ip)}">命名</button></td>
      </tr>`;
    }).join("");

    $$("#svcDetailTable .link-btn").forEach((b) => {
      b.addEventListener("click", () => renameService(b.dataset.skey, b.dataset.ip));
    });
  }

  async function renameService(skey, ip) {
    const name = window.prompt(`给 ${ip} 起个名字（留空则恢复默认推断）`, "");
    if (name === null) return;
    try {
      await api("/api/services/alias", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skey, name: name.trim() }),
      });
      toast(name.trim() ? `已命名为「${name.trim()}」` : "已恢复默认名称");
      refreshServices();
    } catch (e) {
      toast("命名失败：" + e.message, true);
    }
  }

  /* ---------------- 历史 ---------------- */
  function setQuickRange(kind) {
    const now = Math.floor(Date.now() / 1000);
    let from, to;
    const d = new Date();
    const midnight = Math.floor(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000);
    if (kind === "today") { from = midnight; to = now; }
    else if (kind === "yesterday") { from = midnight - 86400; to = midnight; }
    else if (kind === "7d") { from = now - 7 * 86400; to = now; }
    else if (kind === "30d") { from = now - 30 * 86400; to = now; }
    else { from = midnight; to = now; }
    $("#histFrom").value = toInputValue(from);
    $("#histTo").value = toInputValue(to);
    $$("#histQuick button").forEach((b) => b.classList.toggle("active", b.dataset.range === kind));
  }

  async function queryHistory() {
    const from = fromInputValue($("#histFrom").value);
    const to = fromInputValue($("#histTo").value);
    if (!from || !to || to <= from) {
      toast("时间范围不合法", true);
      return;
    }
    const mac = $("#histMac").value;
    const res = $("#histRes").value;
    const q = `from=${from}&to=${to}&res=${res}${mac ? "&mac=" + encodeURIComponent(mac) : ""}`;
    const seq = ++state.histSeq;
    let d;
    try {
      d = await api("/api/history?" + q);
    } catch (e) {
      toast("查询失败：" + e.message, true);
      return;
    }
    if (seq !== state.histSeq) return;

    const pts = d.points || [];
    const label = mac ? $("#histMac").selectedOptions[0].textContent : "WAN 总流量";
    $("#histTitle").textContent = label + " · 流量趋势";

    const span = Math.max(1, d.to - d.from);
    const resTxt = d.res <= 10 ? "10 秒" : d.res <= 60 ? "1 分钟" : d.res <= 300 ? "5 分钟"
      : d.res <= 3600 ? "1 小时" : "1 天";
    $("#histHint").textContent =
      `${fmtDateTime(d.from)} 至 ${fmtDateTime(d.to)} · ${resTxt}粒度 · ${pts.length} 个数据点`;

    $("#hDown").textContent = fmtBytes(d.totals.down);
    $("#hUp").textContent = fmtBytes(d.totals.up);
    $("#hPeak").textContent = "↓ " + fmtBytes(d.peak.down) + " / ↑ " + fmtBytes(d.peak.up);

    chartHist.setData({
      series: [
        { name: "下行", color: COLORS.down, values: pts.map((p) => [p[0], p[2]]) },
        { name: "上行", color: COLORS.up, values: pts.map((p) => [p[0], p[1]]) }
      ],
      yFmt: (v) => fmtBytes(v)
    });

    // 明细表（倒序，最多 200 行）
    const rows = pts.slice(-200).reverse();
    const tb = $("#histTable tbody");
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="4" class="empty">该时间段没有数据</td></tr>`;
    } else {
      tb.innerHTML = rows.map((p) => `
        <tr>
          <td>${fmtDateTime(p[0])}</td>
          <td class="num">${fmtBytes(p[2])}</td>
          <td class="num">${fmtBytes(p[1])}</td>
          <td class="num">${fmtBytes(p[1] + p[2])}</td>
        </tr>`).join("");
    }
    $("#histTableHint").textContent =
      pts.length > 200 ? `共 ${pts.length} 行，显示最近 200 行（可导出 CSV 查看全部）` : `共 ${pts.length} 行`;
    state.lastQuery = q;
  }

  function fmtDateTime(ts) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  /* ---------------- 诊断 ---------------- */
  async function refreshDiag() {
    let d;
    try { d = await api("/api/diagnostics"); } catch (e) { return; }
    const st = d.status;
    const kv = [
      ["SSH 状态", st.online ? '<span class="tag on">已连接</span>' : '<span class="tag off">未连接</span>'],
      ["路由器", `${st.router.host} · ${st.router.wan_iface || "—"}`],
      ["WAN IP", st.router.wan_ip || "—"],
      ["LAN IP", st.router.lan_ip || "—"],
      ["运行时长", st.router.uptime ? fmtSpan(st.router.uptime) : "—"],
      ["采集间隔", st.poll.interval + " 秒"],
      ["最近采集", st.poll.last_ts ? relTime(st.poll.last_ts) + `（耗时 ${st.poll.elapsed}s）` : "—"],
      ["累计轮次", `${st.poll.count} 轮 / 失败 ${st.poll.fail_count} 次`],
      ["数据大小", st.poll.raw_size ? (st.poll.raw_size / 1024).toFixed(1) + " KB" : "—"],
      ["连接数", st.counts.connections + " 条 conntrack"],
      ["在线设备", st.counts.devices + " 台"],
      ["建连次数", st.ssh.connections + " 次"],
      ["计数器归零", st.poll.counter_resets + " 次"],
      ["最近错误", st.last_error ? `<span style="color:#ff9c9c">${escapeHtml(st.last_error)}</span>` : "无"]
    ];
    $("#diagStatus").innerHTML = kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

    const db = d.db;
    const kv2 = [
      ["数据库", d.db_path],
      ["文件大小", (db.db_bytes / 1048576).toFixed(2) + " MB"],
      ["实时明细", db.wan_raw_rows + " 行（24 小时内）"],
      ["分钟聚合", db.wan_min_rows + " 行"],
      ["设备聚合", db.dev_rows + " 行"],
      ["设备档案", db.device_rows + " 台"]
    ];
    $("#diagDb").innerHTML = kv2.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    $("#diagConfig").innerHTML =
      `配置文件：${escapeHtml(d.config_path)}<br>paramiko ${escapeHtml(d.paramiko)} · Python ${escapeHtml(d.python.split(" ")[0])}`;

    const evs = (d.events || []).slice().reverse();
    $("#diagEvents").innerHTML = evs.length
      ? evs.map((e) => `<div class="log-item ${e.level}">
          <span class="log-time">${fmtClock(e.ts)}</span>
          <span class="log-msg">${escapeHtml(e.msg)}</span>
        </div>`).join("")
      : `<div class="empty">暂无日志</div>`;
  }

  /* ---------------- 标签页 ---------------- */
  function switchTab(name) {
    if (!["overview", "devices", "services", "history", "diag"].includes(name)) name = "overview";
    state.tab = name;
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel-page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
    if (name === "overview") { chartRT && chartRT.redraw(); refreshTop(); }
    if (name === "devices") refreshDevices();
    if (name === "services") { refreshDevices(); refreshServices(); }
    if (name === "history") { chartHist && chartHist.redraw(); refreshDevices(); }
    if (name === "diag") refreshDiag();
  }

  /* ---------------- 设置 ---------------- */
  async function openSettings() {
    let d;
    try { d = await api("/api/diagnostics"); } catch (e) { return; }
    const c = d.config || {};
    $("#setInterval").value = c.poll_interval || state.interval;
    $("#setWan").value = c.wan_iface || "";
    $("#setKeep").value = (c.storage && c.storage.keep_days) || 90;
    $("#cfgPath").textContent = "配置文件：" + d.config_path;
    $("#modalSettings").classList.add("open");
  }

  async function saveSettings() {
    const body = {
      poll_interval: Number($("#setInterval").value) || 10,
      wan_iface: $("#setWan").value.trim(),
      keep_days: Number($("#setKeep").value) || 90
    };
    try {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      $("#modalSettings").classList.remove("open");
      toast("设置已保存");
      await refreshOverview();
      scheduleAll();
    } catch (e) {
      toast("保存失败：" + e.message, true);
    }
  }

  /* ---------------- 轮询 ---------------- */
  function scheduleAll() {
    state.timers.forEach(clearTimeout);
    state.timers = [];
    const loop = (fn, ms) => {
      const run = async () => {
        if (state.tab !== "hidden") { try { await fn(); } catch (e) {} }
        state.timers.push(setTimeout(run, ms));
      };
      run();
    };
    loop(refreshOverview, Math.max(5, state.interval) * 1000);
    loop(refreshRealtime, Math.max(10, state.interval * 2) * 1000);
    loop(() => (state.tab === "overview" ? refreshTop() : null), Math.max(15, state.interval * 3) * 1000);
    loop(() => (state.tab === "devices" ? refreshDevices() : null), Math.max(15, state.interval * 3) * 1000);
    loop(() => (state.tab === "services" && state.svcDays === 0 ? refreshServices() : null),
         Math.max(15, state.interval * 2) * 1000);
  }

  /* ---------------- 初始化 ---------------- */
  function init() {
    chartRT = ChartX.create($("#chartRealtime"));
    chartHist = ChartX.create($("#chartHistory"));

    $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
    $$("[data-goto]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.goto)));

    $$("#rtRange button").forEach((b) => {
      b.addEventListener("click", () => {
        $$("#rtRange button").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.rtMinutes = Number(b.dataset.min);
        refreshRealtime();
      });
    });

    $$("#devPeriod button").forEach((b) => {
      b.addEventListener("click", () => {
        $$("#devPeriod button").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.devPeriod = b.dataset.period;
        refreshDevices();
      });
    });

    $$("#deviceTable th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (state.sort.key === key) state.sort.asc = !state.sort.asc;
        else { state.sort.key = key; state.sort.asc = key === "name" || key === "ip"; }
        $$("#deviceTable th").forEach((x) => x.classList.remove("sorted", "asc"));
        th.classList.add("sorted");
        if (state.sort.asc) th.classList.add("asc");
        refreshDevices();
      });
    });

    $$("#histQuick button").forEach((b) => {
      b.addEventListener("click", () => {
        setQuickRange(b.dataset.range);
        queryHistory();
      });
    });
    $("#btnQuery").addEventListener("click", queryHistory);

    $$("#svcRange button").forEach((b) => {
      b.addEventListener("click", () => {
        $$("#svcRange button").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.svcDays = Number(b.dataset.days);
        refreshServices();
      });
    });
    if ($("#svcDevice")) $("#svcDevice").addEventListener("change", refreshServices);
    if ($("#svcRefresh")) $("#svcRefresh").addEventListener("click", refreshServices);
    if ($("#svcDetailClose")) {
      $("#svcDetailClose").addEventListener("click", () => {
        $("#svcDetailCard").style.display = "none";
        $$("#svcTable .svc-row").forEach((x) => x.classList.remove("open"));
      });
    }

    $("#btnExport").addEventListener("click", () => {
      const from = fromInputValue($("#histFrom").value);
      const to = fromInputValue($("#histTo").value);
      const mac = $("#histMac").value;
      const res = $("#histRes").value;
      window.open(`/api/export?from=${from}&to=${to}&res=${res}${mac ? "&mac=" + encodeURIComponent(mac) : ""}`, "_blank");
    });

    $("#btnSettings").addEventListener("click", openSettings);
    $("#btnCloseSettings").addEventListener("click", () => $("#modalSettings").classList.remove("open"));
    $("#btnCancelSettings").addEventListener("click", () => $("#modalSettings").classList.remove("open"));
    $("#btnSaveSettings").addEventListener("click", saveSettings);
    $("#modalSettings").addEventListener("click", (e) => {
      if (e.target.id === "modalSettings") $("#modalSettings").classList.remove("open");
    });

    $("#btnReconnect").addEventListener("click", async () => {
      try {
        await api("/api/reconnect", { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
        toast("已触发重连，稍后刷新");
      } catch (e) {
        toast("重连失败：" + e.message, true);
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") $("#modalSettings").classList.remove("open");
    });

    setQuickRange("today");
    const initial = (location.hash || "").replace("#", "");
    refreshOverview().then(() => {
      scheduleAll();
      refreshRealtime();
      refreshTop();
      refreshDevices();
      if (initial && initial !== "overview") switchTab(initial);
      if (initial === "history") queryHistory();
    });
    window.addEventListener("hashchange", () => {
      const t = (location.hash || "").replace("#", "");
      if (t && t !== state.tab) switchTab(t);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

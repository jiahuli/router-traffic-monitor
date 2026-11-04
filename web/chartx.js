/* 轻量自绘折线图（Canvas 2D，无第三方依赖，断网可用） */
(function (global) {
  "use strict";

  const DEFAULTS = {
    padTop: 14,
    padRight: 16,
    padBottom: 26,
    padLeft: 56,
    gridColor: "rgba(255,255,255,.055)",
    axisColor: "#5f6b80",
    font: '11px "Microsoft YaHei", system-ui, sans-serif'
  };

  function niceStep(v) {
    if (!isFinite(v) || v <= 0) return 1;
    const exp = Math.floor(Math.log10(v));
    const base = Math.pow(10, exp);
    const n = v / base;
    const mult = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
    return mult * base;
  }

  function fmtTime(t, span) {
    const d = new Date(t * 1000);
    const p = (n, w = 2) => String(n).padStart(w, "0");
    if (span > 3 * 86400) return `${d.getMonth() + 1}/${d.getDate()}`;
    if (span > 86400) return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
    if (span > 3600) return `${p(d.getHours())}:${p(d.getMinutes())}`;
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function fmtFull(t) {
    const d = new Date(t * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function create(el, opts) {
    opts = opts || {};
    const canvas = document.createElement("canvas");
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    el.appendChild(canvas);
    el.appendChild(tip);

    const ctx = canvas.getContext("2d");
    const o = Object.assign({}, DEFAULTS, opts);

    let series = [];
    let yFmt = (v) => String(v);
    let geom = null;
    let hoverIdx = -1;
    let raf = 0;

    const state = { series, yFmt };

    function schedule() {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        draw();
      });
    }

    function bounds() {
      let t0 = Infinity, t1 = -Infinity, max = 0;
      series.forEach((s) => {
        s.values.forEach(([t, v]) => {
          if (t < t0) t0 = t;
          if (t > t1) t1 = t;
          if (v > max) max = v;
        });
      });
      if (!isFinite(t0)) return null;
      if (t1 === t0) t1 = t0 + 1;
      const step = niceStep(Math.max(max, 1) / 4);
      const top = Math.ceil(Math.max(max, 1) / step) * step;
      return { t0, t1, max: top, step, span: t1 - t0 };
    }

    function xOf(t, b, w) {
      return o.padLeft + ((t - b.t0) / (b.t1 - b.t0)) * w;
    }

    function draw() {
      const dpr = window.devicePixelRatio || 1;
      const W = el.clientWidth || 600;
      const H = el.clientHeight || 300;
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.width = W + "px";
      canvas.style.height = H + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);

      const b = bounds();
      const w = W - o.padLeft - o.padRight;
      const h = H - o.padTop - o.padBottom;
      if (!b || h <= 0 || w <= 0) {
        geom = null;
        ctx.font = o.font;
        ctx.fillStyle = o.axisColor;
        ctx.textAlign = "center";
        ctx.fillText("暂无数据", W / 2, H / 2);
        return;
      }

      geom = { b, w, h, W, H };
      const yOf = (v) => o.padTop + h - (v / b.max) * h;

      // ---- 网格与 Y 轴 ----
      ctx.font = o.font;
      ctx.textBaseline = "middle";
      const ticks = Math.round(b.max / b.step);
      for (let i = 0; i <= ticks; i++) {
        const val = i * b.step;
        const y = Math.round(yOf(val)) + 0.5;
        ctx.beginPath();
        ctx.strokeStyle = o.gridColor;
        ctx.lineWidth = 1;
        ctx.moveTo(o.padLeft, y);
        ctx.lineTo(W - o.padRight, y);
        ctx.stroke();
        ctx.fillStyle = o.axisColor;
        ctx.textAlign = "right";
        ctx.fillText(val === 0 ? "0" : yFmt(val), o.padLeft - 8, y);
      }

      // ---- X 轴刻度 ----
      const xTicks = Math.max(2, Math.min(6, Math.floor(w / 90)));
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (let i = 0; i <= xTicks; i++) {
        const t = b.t0 + ((b.t1 - b.t0) * i) / xTicks;
        const x = xOf(t, b, w);
        ctx.fillStyle = o.axisColor;
        const label = fmtTime(t, b.span);
        const tw = ctx.measureText(label).width;
        let lx = x;
        if (lx - tw / 2 < o.padLeft - 20) lx = o.padLeft + tw / 2 - 20;
        if (lx + tw / 2 > W - 4) lx = W - 4 - tw / 2;
        ctx.fillText(label, lx, o.padTop + h + 8);
      }

      // ---- 数据 ----
      series.forEach((s, si) => {
        if (!s.values.length) return;
        const pts = s.values.map(([t, v]) => [xOf(t, b, w), yOf(v)]);

        if (s.fill !== false) {
          const grad = ctx.createLinearGradient(0, o.padTop, 0, o.padTop + h);
          grad.addColorStop(0, hexA(s.color, 0.26));
          grad.addColorStop(1, hexA(s.color, 0.01));
          ctx.beginPath();
          ctx.moveTo(pts[0][0], o.padTop + h);
          pts.forEach(([x, y]) => ctx.lineTo(x, y));
          ctx.lineTo(pts[pts.length - 1][0], o.padTop + h);
          ctx.closePath();
          ctx.fillStyle = grad;
          ctx.fill();
        }

        ctx.beginPath();
        pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.strokeStyle = s.color;
        ctx.lineWidth = si === 0 ? 2 : 1.6;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.stroke();
      });

      // ---- 悬停十字线 ----
      if (hoverIdx >= 0) {
        const main = series[0];
        if (main && main.values[hoverIdx]) {
          const [t] = main.values[hoverIdx];
          const x = Math.round(xOf(t, b, w)) + 0.5;
          ctx.beginPath();
          ctx.strokeStyle = "rgba(255,255,255,.22)";
          ctx.setLineDash([3, 3]);
          ctx.moveTo(x, o.padTop);
          ctx.lineTo(x, o.padTop + h);
          ctx.stroke();
          ctx.setLineDash([]);
          series.forEach((s) => {
            const p = s.values[hoverIdx];
            if (!p) return;
            ctx.beginPath();
            ctx.arc(x, yOf(p[1]), 3.5, 0, Math.PI * 2);
            ctx.fillStyle = s.color;
            ctx.fill();
            ctx.strokeStyle = "#0b0e14";
            ctx.lineWidth = 1.5;
            ctx.stroke();
          });
        }
      }
    }

    function hexA(hex, a) {
      if (!hex || hex[0] !== "#") return hex;
      const n = parseInt(hex.slice(1), 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    }

    // ---- 交互 ----
    function nearestIndex(px) {
      if (!geom || !series[0] || !series[0].values.length) return -1;
      const { b, w } = geom;
      const t = b.t0 + ((px - o.padLeft) / w) * (b.t1 - b.t0);
      const vals = series[0].values;
      let lo = 0, hi = vals.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (vals[mid][0] < t) lo = mid + 1;
        else hi = mid;
      }
      if (lo > 0 && Math.abs(vals[lo - 1][0] - t) < Math.abs(vals[lo][0] - t)) lo -= 1;
      return lo;
    }

    function onMove(e) {
      if (!geom) return;
      const rect = el.getBoundingClientRect();
      const px = e.clientX - rect.left;
      if (px < o.padLeft - 8 || px > geom.W - o.padRight + 8) {
        hideTip();
        return;
      }
      const idx = nearestIndex(px);
      if (idx < 0) return hideTip();
      if (idx !== hoverIdx) {
        hoverIdx = idx;
        schedule();
      }
      showTip(idx, rect);
    }

    function showTip(idx, rect) {
      const main = series[0];
      const p = main && main.values[idx];
      if (!p) return;
      let html = `<div class="t">${fmtFull(p[0])}</div>`;
      series.forEach((s) => {
        const q = s.values[idx];
        if (!q) return;
        html += `<div class="row"><span><i style="background:${s.color}"></i>${s.name}</span><b>${s.fmt ? s.fmt(q[1]) : yFmt(q[1])}</b></div>`;
      });
      tip.innerHTML = html;
      tip.style.opacity = "1";
      const tw = tip.offsetWidth || 140;
      let left = rect.width * ((idx + 0.5) / Math.max(1, main.values.length));
      const xPix = o.padLeft + ((p[0] - geom.b.t0) / (geom.b.t1 - geom.b.t0)) * geom.w;
      left = xPix + 14;
      if (left + tw > rect.width - 6) left = xPix - tw - 14;
      if (left < 4) left = 4;
      tip.style.left = left + "px";
      tip.style.top = Math.max(6, o.padTop + 4) + "px";
    }

    function hideTip() {
      tip.style.opacity = "0";
      if (hoverIdx !== -1) {
        hoverIdx = -1;
        schedule();
      }
    }

    el.addEventListener("mousemove", onMove);
    el.addEventListener("mouseleave", hideTip);

    const ro = ("ResizeObserver" in window)
      ? new ResizeObserver(() => schedule())
      : null;
    if (ro) ro.observe(el);
    else window.addEventListener("resize", schedule);

    schedule();

    return {
      setData(payload) {
        series = (payload.series || []).map((s) => Object.assign({}, s));
        if (payload.yFmt) yFmt = payload.yFmt;
        hoverIdx = -1;
        tip.style.opacity = "0";
        draw();
      },
      redraw: schedule,
      destroy() {
        if (ro) ro.disconnect();
        else window.removeEventListener("resize", schedule);
        el.removeEventListener("mousemove", onMove);
        el.removeEventListener("mouseleave", hideTip);
        canvas.remove();
        tip.remove();
      }
    };
  }

  global.ChartX = { create, fmtTime, fmtFull };
})(window);

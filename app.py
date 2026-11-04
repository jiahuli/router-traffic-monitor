"""小米万兆路由器流量监控 —— 本地 Web 服务入口。

启动： python app.py            （默认 http://127.0.0.1:8787）
      python app.py --no-browser --port 9000
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FROZEN = bool(getattr(sys, "frozen", False))
# 打包成 exe 后：配置和 data 必须放在 exe 同级目录（否则重启即丢），
# 而 web 静态资源在 PyInstaller 的临时解包目录里。
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
WEB_DIR = os.path.join(RES_DIR, "web")

sys.path.insert(0, APP_DIR)

from collector import Collector          # noqa: E402
from storage import Storage              # noqa: E402
import router_client                     # noqa: E402

DEFAULT_CONFIG = {
    # 仓库内不保留任何凭据：密码留空，首次运行由启动器引导填写
    "router": {"host": "", "port": 22, "username": "root", "password": ""},
    "poll_interval": 10,
    "wan_iface": "",
    "lan_subnets": ["192.168.31.0/24"],
    "ignore_peer_cidrs": ["198.18.0.0/15"],
    "service_overrides": {},
    "http": {"host": "127.0.0.1", "port": 8787, "open_browser": True},
    "storage": {"db": "data/traffic.db", "keep_days": 90, "keep_raw_hours": 24},
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[警告] 配置文件解析失败，使用默认值：{e}")
    else:
        # 首次运行（例如刚解压 exe）时落地一份可编辑的配置
        try:
            save_config(cfg)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 时间工具

def tz_offset(ts: float | None = None) -> int:
    dt = datetime.fromtimestamp(ts) if ts else datetime.now()
    off = dt.astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


def period_bounds(period: str) -> tuple[int, int]:
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(start.timestamp()) + 86400
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "hour":
        start = now - timedelta(hours=1)
    elif period == "day":
        start = now - timedelta(days=1)
    elif period == "7d":
        start = now - timedelta(days=7)
    elif period == "30d":
        start = now - timedelta(days=30)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp()) + 1


def auto_res(span: int, for_device: bool = False) -> int:
    if span <= 3 * 3600 and not for_device:
        return 10
    if span <= 6 * 3600:
        return 60 if not for_device else 300
    if span <= 3 * 86400:
        return 300 if not for_device else 900
    if span <= 14 * 86400:
        return 3600
    return 86400


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 服务

class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        db_path = cfg["storage"]["db"]
        if not os.path.isabs(db_path):
            db_path = os.path.join(APP_DIR, db_path)
        self.db = Storage(db_path)
        self.collector = Collector(cfg, self.db)

    # -------- 组装 API 数据 --------

    def overview(self) -> dict:
        st = self.collector.status()
        off = tz_offset()
        out = {"status": st, "server_time": int(time.time())}

        totals = {}
        for p in ("today", "week", "month"):
            t0, t1 = period_bounds(p)
            up, down = self.db.wan_sum(t0, t1)
            totals[p] = {"up": up, "down": down, "from": t0, "to": t1}
        out["totals"] = totals

        t0, t1 = period_bounds("hour")
        pts = self.db.wan_series(t0, t1, 60, off)
        out["spark"] = pts[-60:]
        out["db"] = self.db.stats()
        return out

    def realtime(self, minutes: int = 120) -> dict:
        off = tz_offset()
        now = int(time.time())
        t0 = now - minutes * 60
        pts = self.db.wan_series(t0, now, 10, off)
        res = 10
        if not pts:  # 首次启动尚无 10 秒明细时，退化为分钟聚合
            pts = self.db.wan_series(t0, now, 60, off)
            res = 60
        return {"points": pts, "from": t0, "to": now, "res": res}

    def devices(self, period: str = "today") -> dict:
        t0, t1 = period_bounds(period)
        hist = self.db.dev_sum(t0, t1)
        meta = {d["mac"]: d for d in self.db.devices()}
        live = {d["mac"]: d for d in self.collector.devices_live()}
        now = int(time.time())

        rows = []
        for mac in set(list(hist.keys()) + list(live.keys()) + list(meta.keys())):
            m = meta.get(mac, {})
            l = live.get(mac, {})
            up, down = hist.get(mac, [0, 0])
            last_seen = max(m.get("last_seen") or 0, l.get("last_seen") or 0)
            online = bool(l) or (now - last_seen < 180)
            rows.append({
                "mac": mac,
                "ip": l.get("ip") or m.get("ip") or "",
                "hostname": l.get("hostname") or m.get("hostname") or "",
                "vendor": l.get("vendor") or m.get("vendor") or "",
                "kind": l.get("kind") or m.get("kind") or "",
                "band": l.get("band") or m.get("band") or "",
                "signal": l.get("signal", m.get("signal")),
                "ssid": l.get("ssid", ""),
                "online": online,
                "down_bps": round(l.get("down_bps", 0), 1),
                "up_bps": round(l.get("up_bps", 0), 1),
                "up": up,
                "down": down,
                "total": up + down,
                "last_seen": last_seen,
            })
        rows.sort(key=lambda r: (not r["online"], -r["total"]))
        wan_up, wan_down = self.db.wan_sum(t0, t1)
        return {"devices": rows, "period": period, "from": t0, "to": t1,
                "wan": {"up": wan_up, "down": wan_down}}

    def history(self, q: dict) -> dict:
        off = tz_offset()
        now = int(time.time())
        t0 = int(q.get("from", 0) or 0)
        t1 = int(q.get("to", 0) or 0)
        if not t0:
            t0, t1 = period_bounds("today")
        if not t1:
            t1 = now
        mac = (q.get("mac") or "").strip() or None
        span = t1 - t0
        res = int(q.get("res") or 0) or auto_res(span, for_device=bool(mac))
        res = max(10, min(86400, res))

        if mac:
            pts = self.db.dev_series(t0, t1, res, off, mac)
        else:
            pts = self.db.wan_series(t0, t1, res, off)

        up = sum(p[1] for p in pts)
        down = sum(p[2] for p in pts)
        return {
            "from": t0, "to": t1, "res": res, "mac": mac,
            "points": pts,
            "totals": {"up": up, "down": down},
            "peak": {
                "up": max([p[1] for p in pts], default=0),
                "down": max([p[2] for p in pts], default=0),
            },
        }

    def services_range(self, mac: str, days: int = 7) -> dict:
        if not mac:
            return {"services": [], "groups": [], "totals": {"up": 0, "down": 0},
                    "error": "缺少 mac 参数"}
        days = max(1, min(90, days))
        return {"mac": mac, "days": days, **self.collector.services_range(mac, days)}

    def services_live(self, mac: str | None = None) -> dict:
        return self.collector.services_live(mac)

    def set_service_alias(self, skey: str, name: str) -> dict:
        skey = (skey or "").strip()
        if not skey:
            return {"ok": False, "error": "缺少服务标识"}
        ov = dict(self.cfg.get("service_overrides") or {})
        target = skey.split("|")[0] if "|" in skey else skey   # 允许按 IP 命名
        if name:
            ov[target] = name
        else:
            ov.pop(target, None)
        self.collector.set_service_overrides(ov)
        save_config(self.cfg)
        self.collector._log("info", f"服务命名已更新：{target} → {name or '(清除)'}")
        return {"ok": True, "overrides": ov}

    def export_csv(self, q: dict) -> bytes:
        data = self.history(q)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "下载(字节)", "上传(字节)", "下载(MB)", "上传(MB)"])
        for t, up, down in data["points"]:
            w.writerow([fmt_ts(t), down, up, round(down / 1048576, 3), round(up / 1048576, 3)])
        w.writerow(["合计", data["totals"]["down"], data["totals"]["up"],
                    round(data["totals"]["down"] / 1048576, 3),
                    round(data["totals"]["up"] / 1048576, 3)])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    def diagnostics(self) -> dict:
        st = self.collector.status()
        cfg_safe = json.loads(json.dumps(self.cfg))
        cfg_safe.setdefault("router", {})["password"] = "***"
        return {
            "status": st,
            "config": cfg_safe,
            "events": self.collector.events[-120:],
            "persisted_events": self.db.recent_events(40),
            "db": self.db.stats(),
            "remote_script": router_client.REMOTE_SCRIPT.strip(),
            "python": sys.version,
            "paramiko": getattr(router_client.paramiko, "__version__", "未安装"),
            "config_path": CONFIG_PATH,
            "db_path": self.db.path,
        }


class Handler(SimpleHTTPRequestHandler):
    app: App = None  # type: ignore
    server_version = "MiRouterTraffic/1.0"

    def log_message(self, fmt, *args):  # 静音，避免刷屏
        pass

    # ---------- 工具 ----------

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, code: int = 500) -> None:
        self._json({"error": msg}, code)

    def _file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._err("not found", 404)
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        try:
            if path == "/" or path == "/index.html":
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if path.startswith("/static/"):
                name = os.path.basename(path)
                return self._file(os.path.join(WEB_DIR, name))
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/overview":
                return self._json(self.app.overview())
            if path == "/api/realtime":
                return self._json(self.app.realtime(int(q.get("minutes", 120))))
            if path == "/api/devices":
                return self._json(self.app.devices(q.get("period", "today")))
            if path == "/api/history":
                return self._json(self.app.history(q))
            if path == "/api/diagnostics":
                return self._json(self.app.diagnostics())
            if path == "/api/services":
                return self._json(self.app.services_range(
                    q.get("mac", ""), int(q.get("days", 7) or 7)))
            if path == "/api/services/live":
                return self._json(self.app.services_live(q.get("mac") or None))
            if path == "/api/export":
                body = self.app.export_csv(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="traffic_{int(time.time())}.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._err("not found", 404)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}")

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        try:
            if u.path == "/api/settings":
                cfg = self.app.cfg
                if "poll_interval" in body:
                    iv = max(3, min(300, int(body["poll_interval"])))
                    cfg["poll_interval"] = iv
                    self.app.collector.set_interval(iv)
                if "wan_iface" in body:
                    cfg["wan_iface"] = str(body["wan_iface"]).strip()
                    self.app.collector.wan_iface = cfg["wan_iface"]
                if "keep_days" in body:
                    cfg["storage"]["keep_days"] = max(1, min(3650, int(body["keep_days"])))
                save_config(cfg)
                self.app.collector._log("info", "配置已更新")
                return self._json({"ok": True, "config": cfg})
            if u.path == "/api/reconnect":
                self.app.collector.force_reconnect()
                return self._json({"ok": True})
            if u.path == "/api/services/alias":
                return self._json(self.app.set_service_alias(
                    body.get("skey", ""), body.get("name", "")))
            return self._err("not found", 404)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}")


def pick_port(host: str, port: int) -> int:
    for p in range(port, port + 30):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"端口 {port}-{port+30} 均被占用")


def main() -> None:
    ap = argparse.ArgumentParser(description="小米万兆路由器流量监控")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--interval", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config()
    if args.port:
        cfg["http"]["port"] = args.port
    if args.host:
        cfg["http"]["host"] = args.host
    if args.interval:
        cfg["poll_interval"] = max(3, args.interval)

    print("=" * 66)
    print(" 小米万兆路由器 · 流量监控")
    print("=" * 66)
    print(f" 路由器      : {cfg['router']['username']}@{cfg['router']['host']}:{cfg['router']['port']}")
    print(f" 采集间隔    : {cfg['poll_interval']}s")
    print(f" paramiko    : {getattr(router_client.paramiko, '__version__', '未安装')}")

    app = App(cfg)
    app.collector.start()

    host = cfg["http"]["host"]
    port = pick_port(host, int(cfg["http"]["port"]))
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f" 控制台      : {url}")
    print(f" 数据库      : {app.db.path}")
    print("-" * 66)
    print(" 按 Ctrl+C 退出\n")

    if cfg["http"].get("open_browser", True) and not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def shutdown(*_):
        print("\n正在退出…")
        app.collector.stop()
        app.db.close()
        httpd.shutdown()

    signal.signal(signal.SIGINT, lambda *a: (shutdown(), sys.exit(0)))
    try:
        signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))
    except (AttributeError, ValueError):
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()

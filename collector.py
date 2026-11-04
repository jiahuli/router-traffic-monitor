"""采集引擎：定时轮询 + 增量计算 + 落库。

关键算法说明
------------
1) WAN 总流量：读 /proc/net/dev 的累计计数器，做差值。计数器是自开机累计，
   不受 conntrack 老化影响，精度最高。检测到计数器变小则判定路由器重启，本轮差值记 0。
2) 各设备流量：路由器没有 nlbwmon/vnstat，只能用 /proc/net/nf_conntrack 的
   bytes 计数器做「逐连接增量」。每个连接记录上一次的字节数，本轮取差值累加到设备。
   - 新建连接直接计入全量（连接建立时从 0 开始，误差 <= 一个采样周期）
   - 连接消失时其残余字节会丢失（误差 <= 一个采样周期）
   - 断线重连后第一轮只建立基线不计增量，避免把历史累计值当成瞬时流量
"""
from __future__ import annotations

import threading
import time

from router_client import (RouterClient, RouterError, band_of, collect_once,
                           ip_in_networks, make_networks, vendor_of)
from services import ServiceNamer


def now_local_offset() -> int:
    return -time.timezone if not time.daylight else -time.altzone


class Collector:
    def __init__(self, cfg: dict, storage):
        self.cfg = cfg
        self.db = storage
        self.nets = make_networks(cfg.get("lan_subnets", []))
        # 透明代理（ShellCrash/Clash 等）的 fake-IP 网关段：这些连接不承载真实外网流量，
        # 真实流量体现在代理自身的出站连接上，不过滤会双倍计算。
        self.ignore_nets = make_networks(cfg.get("ignore_peer_cidrs", []))
        self.interval = max(3, int(cfg.get("poll_interval", 10)))
        self.client = RouterClient(cfg["router"], logger=self._log)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

        # 采集状态
        self.online = False
        self.last_poll_ts = 0.0
        self.last_success_ts = 0.0
        self.last_error = ""
        self.poll_count = 0
        self.fail_count = 0
        self.wan_iface = cfg.get("wan_iface", "")
        self.wan_ip = ""
        self.lan_ip = ""
        self.uptime = 0.0
        self.route = ""
        self.elapsed = 0.0
        self.raw_size = 0
        self.sections: dict = {}
        self.device_count = 0
        self.conn_count = 0

        # 速率
        self.down_bps = 0.0
        self.up_bps = 0.0
        self.peak_down = 0.0
        self.peak_up = 0.0

        # 上一轮
        self._prev_wan: tuple[int, int, int] | None = None   # (ts, rx, tx)
        self._prev_conns: dict[str, tuple[int, int]] = {}
        self._warm = False
        self._counter_resets = 0
        self._last_prune = 0.0
        self._session_up = 0
        self._session_down = 0
        self._started_at = int(time.time())

        # 当前设备视图（供 API 直读）
        self.live_devices: dict[str, dict] = {}
        self._diag_raw = ""

        # 服务维度
        self.namer = ServiceNamer(cfg.get("service_overrides") or {})
        self._svc_live: dict[str, dict[str, list]] = {}      # mac -> {skey: [up,down,conns]}（本轮）
        self._svc_today: dict[tuple[str, str], list[int]] = {}  # (mac, skey) -> [up, down]
        self._svc_day = time.strftime("%Y-%m-%d")
        self._svc_last_flush = 0.0
        self._svc_dirty = True
        self._load_svc_today()

        self.events: list[dict] = []
        self.started_at = int(time.time())

    # ---------------- 日志 ----------------

    def _log(self, level: str, msg: str) -> None:
        item = {"ts": int(time.time()), "level": level, "msg": msg}
        with self._lock:
            self.events.append(item)
            if len(self.events) > 300:
                del self.events[:-300]
        try:
            self.db.log_event(level, msg)
        except Exception:
            pass

    # ---------------- 线程控制 ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self._flush_services(self._svc_day)   # 退出前落盘，避免丢当天末尾数据
        except Exception:
            pass
        self.client.close()

    def set_interval(self, sec: int) -> None:
        with self._lock:
            self.interval = max(3, min(300, int(sec)))

    def force_reconnect(self) -> None:
        self.client.close()
        self._warm = False
        self._log("info", "手动触发重连")

    # ---------------- 主循环 ----------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except RouterError as e:
                self._on_fail(str(e))
            except Exception as e:  # 兜底，绝不让采集线程死掉
                self._on_fail(f"{type(e).__name__}: {e}")
            cost = time.time() - t0
            self._stop.wait(max(1.0, self.interval - cost))

    def _on_fail(self, msg: str) -> None:
        with self._lock:
            self.online = False
            self.fail_count += 1
            self.last_error = msg
            self.down_bps = 0.0
            self.up_bps = 0.0
            self._warm = False
            for d in self.live_devices.values():
                d["down_bps"] = 0.0
                d["up_bps"] = 0.0

    def _tick(self) -> None:
        data = collect_once(self.client, self.nets, self.wan_iface)
        self._diag_raw = f"elapsed={data['elapsed']}s size={data['raw_size']}B " \
                         f"sections={data['sections']}"
        self._account(data)

    # ---------------- 计算 ----------------

    def _account(self, data: dict) -> None:
        ts = data["ts"]
        with self._lock:
            self.online = True
            self.last_poll_ts = ts
            self.last_success_ts = time.time()
            self.last_error = ""
            self.poll_count += 1
            self.fail_count = 0
            self.wan_iface = data["wan_iface"] or self.wan_iface
            self.wan_ip = data["wan_ip"]
            self.lan_ip = data["lan_ip"]
            self.uptime = data["uptime"]
            self.route = data["route"]
            self.elapsed = data["elapsed"]
            self.raw_size = data["raw_size"]
            self.sections = data["sections"]
            self.conn_count = len(data["conntrack"])

        # ---- WAN 累计计数器差值 ----
        up_bytes = down_bytes = 0
        dt = 0.0
        with self._lock:
            prev = self._prev_wan
            if prev is not None:
                dt = max(1.0, ts - prev[0])
                drx = data["wan_rx"] - prev[1]
                dtx = data["wan_tx"] - prev[2]
                if drx < 0 or dtx < 0:
                    self._counter_resets += 1
                    self._log("warn", "WAN 计数器归零（路由器重启或接口重置），本轮差值按 0 处理")
                    drx = dtx = 0
                down_bytes, up_bytes = drx, dtx
                self.down_bps = down_bytes / dt
                self.up_bps = up_bytes / dt
                self.peak_down = max(self.peak_down, self.down_bps)
                self.peak_up = max(self.peak_up, self.up_bps)
            self._prev_wan = (ts, data["wan_rx"], data["wan_tx"])
            self._session_down += down_bytes
            self._session_up += up_bytes

        try:
            self.db.add_wan_tick(ts, up_bytes, down_bytes)
        except Exception as e:
            self._log("error", f"写入数据库失败：{e}")

        # ---- 设备流量（conntrack 增量）----
        subnets = self.nets
        ignore = self.ignore_nets
        # 路由器自身接口地址不算终端设备
        router_ips = {ip for ip in (data.get("addrs") or {}).values()}
        router_ips.add(data.get("lan_ip") or "")

        deltas: dict[str, list[float]] = {}
        cur_conns: dict[str, tuple[int, int]] = {}
        svc_poll: dict[str, dict[str, list]] = {}
        if not data.get("ct_complete", True):
            # 读取不完整时若照常计算，漏掉的连接会在下一轮被当成新连接全量计入，必须跳过
            self._log("warn", f"conntrack 读取不完整（{data.get('ct_lines', 0)}/{data.get('ct_expect', 0)} 条），"
                              f"本轮跳过设备流量统计")
            with self._lock:
                self._warm = False          # 下一轮重新建立基线，避免误算
                self._prev_conns = {}
        else:
            for c in data["conntrack"]:
                key = c["key"]
                ob, rb = c["obytes"], c["rbytes"]
                cur_conns[key] = (ob, rb)
                if self._warm:
                    po, pr = self._prev_conns.get(key, (0, 0))
                    do, dr = ob - po, rb - pr
                    if do < 0:
                        do = 0
                    if dr < 0:
                        dr = 0
                else:
                    do = dr = 0
                s, d = c["osrc"], c["odst"]
                s_lan, d_lan = ip_in_networks(s, subnets), ip_in_networks(d, subnets)
                if s_lan and not d_lan:
                    ip, peer, u, dn = s, d, do, dr    # 内网设备发起，上行=原始方向
                elif d_lan and not s_lan:
                    ip, peer, u, dn = d, s, dr, do    # 外网入站，下行=原始方向
                else:
                    continue                          # 纯内网互访（含路由器代理的本地转接）不计
                if ip in router_ips:
                    continue                          # 路由器自身产生的连接
                if ignore and ip_in_networks(peer, ignore):
                    continue                          # fake-IP 代理段，真实流量在代理出站连接上

                if u or dn:
                    slot = deltas.setdefault(ip, [0.0, 0.0])
                    slot[0] += u
                    slot[1] += dn

                # 服务维度：按 目标IP|端口/协议 聚合。
                # 连接数统计所有存活连接（不受增量影响），字节数只累加本轮增量。
                skey = f"{peer}|{c.get('dport', '')}/{c['proto']}"
                entry = svc_poll.setdefault(ip, {}).setdefault(skey, [0.0, 0.0, 0])
                entry[0] += u
                entry[1] += dn
                entry[2] += 1

            with self._lock:
                self._prev_conns = cur_conns
                self._warm = True

        # ---- 设备档案 ----
        ip2mac: dict[str, str] = {}
        ip2host: dict[str, str] = {}
        for ip, (mac, host) in data["leases"].items():
            ip2mac[ip] = mac
            if host:
                ip2host[ip] = host
        for ip, mac in data["neigh"].items():
            ip2mac.setdefault(ip, mac)
        wifi = data["wifi"]

        live: dict[str, dict] = {}
        for ip, (u, d) in deltas.items():
            mac = ip2mac.get(ip) or f"ip:{ip}"
            host = ip2host.get(ip, "")
            info = wifi.get(mac.lower()) if mac in wifi else wifi.get(mac)
            kind = "无线" if info else "有线"
            band = band_of(info["channel"]) if info else ""
            item = live.setdefault(mac, {
                "mac": mac, "ip": ip, "hostname": host,
                "vendor": vendor_of(mac) if ":" in mac else "",
                "kind": kind, "band": band,
                "signal": info["signal"] if info else None,
                "ssid": info["ssid"] if info else "",
                "up_bytes": 0.0, "down_bytes": 0.0,
                "up_bps": 0.0, "down_bps": 0.0,
            })
            item["up_bytes"] += u
            item["down_bytes"] += d
            if dt > 0:
                item["up_bps"] += u / dt
                item["down_bps"] += d / dt

        # 合并无线客户端（即使本轮无流量也展示在线设备）
        for mac, info in wifi.items():
            if any(v["mac"] == mac for v in live.values()):
                continue
            ip = next((i for i, m in ip2mac.items() if m == mac), "")
            if not ip and not info.get("signal"):
                continue
            live[mac] = {
                "mac": mac, "ip": ip, "hostname": ip2host.get(ip, ""),
                "vendor": vendor_of(mac), "kind": "无线",
                "band": band_of(info["channel"]), "signal": info["signal"],
                "ssid": info["ssid"],
                "up_bytes": 0.0, "down_bytes": 0.0, "up_bps": 0.0, "down_bps": 0.0,
            }

        today0 = int(time.time()) // 86400 * 86400 + now_local_offset()
        today_totals = self.db.dev_sum(today0, int(time.time()) + 60)

        with self._lock:
            for mac, item in live.items():
                hist = today_totals.get(mac, [0, 0])
                item["today_up"] = hist[0] + int(item["up_bytes"])
                item["today_down"] = hist[1] + int(item["down_bytes"])
                item["last_seen"] = ts
            # 把上一轮仍在线的设备也保留（短暂无流量不应消失）
            for mac, old in self.live_devices.items():
                if mac not in live and ts - old.get("last_seen", 0) <= max(60, self.interval * 6):
                    keep = dict(old)
                    keep["up_bps"] = 0.0
                    keep["down_bps"] = 0.0
                    keep["up_bytes"] = 0.0
                    keep["down_bytes"] = 0.0
                    live[mac] = keep
            self.live_devices = live
            self.device_count = len(live)

        # ---- 落库 ----
        for mac, item in live.items():
            if item["up_bytes"] or item["down_bytes"]:
                try:
                    self.db.add_dev_traffic(ts, mac, int(item["up_bytes"]), int(item["down_bytes"]))
                except Exception:
                    pass
            try:
                self.db.upsert_device(
                    mac=mac, ip=item.get("ip", ""), hostname=item.get("hostname", ""),
                    vendor=item.get("vendor", ""), kind=item.get("kind", ""),
                    band=item.get("band", ""), signal=item.get("signal"),
                    online=True, ts=ts,
                )
            except Exception:
                pass

        # ---- 服务维度：IP 归到设备，并按天累计 ----
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        with self._lock:
            if day != self._svc_day:            # 跨天：重置当天累计
                self._svc_day = day
                self._svc_today = {}
                self._svc_dirty = True

        svc_live: dict[str, dict[str, list]] = {}
        for ip, skv in svc_poll.items():
            mac = ip2mac.get(ip) or f"ip:{ip}"
            bucket = svc_live.setdefault(mac, {})
            for skey, v in skv.items():
                cur = bucket.setdefault(skey, [0.0, 0.0, 0])
                cur[0] += v[0]
                cur[1] += v[1]
                cur[2] += v[2]

        with self._lock:
            for mac, skv in svc_live.items():
                for skey, (u, dn, conns) in skv.items():
                    agg = self._svc_today.setdefault((mac, skey), [0, 0])
                    agg[0] += int(u)
                    agg[1] += int(dn)
            self._svc_live = svc_live
            # 单个设备的服务条目限流，防止极端情况下内存膨胀
            if len(svc_live) > 200:
                self._svc_live = dict(sorted(svc_live.items(),
                                             key=lambda kv: -sum(v[1] for v in kv[1].values()))[:200])

        # ---- 定期落盘（每 60 秒，每设备只存 Top 20）----
        if ts - self._svc_last_flush >= 60:
            self._svc_last_flush = ts
            self._flush_services(day)

        # ---- 周期性清理 ----
        if time.time() - self._last_prune > 600:
            self._last_prune = time.time()
            try:
                self.db.prune(int(time.time()),
                              int(self.cfg["storage"].get("keep_days", 90)),
                              int(self.cfg["storage"].get("keep_raw_hours", 24)))
            except Exception:
                pass

    # ---------------- 服务维度 ----------------

    def _load_svc_today(self) -> None:
        """重启后把当天的服务累计读回来，避免从零开始。"""
        try:
            rows = self.db.load_svc_day(self._svc_day)
        except Exception:
            return
        data: dict[tuple[str, str], list[int]] = {}
        for mac, skey, up, down, _conns in rows:
            data[(mac, skey)] = [int(up), int(down)]
        with self._lock:
            self._svc_today = data

    def _flush_services(self, day: str) -> None:
        """把当天累计的前 N 个服务写库（覆盖式，行数可控）。"""
        with self._lock:
            by_mac: dict[str, list[tuple[str, int, int]]] = {}
            for (mac, skey), (up, down) in self._svc_today.items():
                if up <= 0 and down <= 0:
                    continue
                by_mac.setdefault(mac, []).append((skey, up, down))
            payload: dict[str, list[tuple[str, int, int, int]]] = {}
            for mac, rows in by_mac.items():
                rows.sort(key=lambda r: -(r[1] + r[2]))
                payload[mac] = [
                    (skey, up, down, int(self._svc_live.get(mac, {}).get(skey, (0, 0, 0))[2]))
                    for skey, up, down in rows[:20]
                ]
            self._svc_dirty = False

        for mac, rows in payload.items():
            try:
                self.db.save_svc_snapshot(day, mac, rows)
            except Exception as e:
                self._log("error", f"服务统计写入失败：{e}")
        try:
            self.db.trim_svc_day(day, 60)
        except Exception:
            pass

    @staticmethod
    def _group_by_label(rows: list[dict], limit: int) -> list[dict]:
        """把明细按服务标签聚合，避免列表被一堆 IP 淹没。"""
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.get(r["label"])
            if not g:
                g = groups[r["label"]] = {
                    "label": r["label"], "source": r["source"], "vendor": r["vendor"],
                    "up": 0, "down": 0, "conns": 0, "targets": set(),
                }
            g["up"] += r["up"]
            g["down"] += r["down"]
            g["conns"] += r["conns"]
            g["targets"].add(r["ip"])
        out = []
        for g in groups.values():
            g["targets"] = len(g["targets"])
            out.append(g)
        out.sort(key=lambda x: -(x["up"] + x["down"]))
        return out[:limit]

    def services_live(self, mac: str | None = None, limit: int = 200) -> dict:
        """本轮采集的实时服务视图（明细 + 按标签聚合）。"""
        with self._lock:
            src = {mac: self._svc_live.get(mac, {})} if mac else self._svc_live
            raw = [(m, skey, v[0], v[1], v[2]) for m, skv in src.items() for skey, v in skv.items()]
            interval = max(1, self.interval)
        rows = []
        for m, skey, u, dn, conns in raw:
            if u <= 0 and dn <= 0:
                continue                      # 本轮无流量，不占用展示位
            info = self.namer.describe_key(skey)
            rows.append({
                "mac": m, **info,
                "up": int(u), "down": int(dn), "conns": conns,
                "up_bps": round(u / interval, 1), "down_bps": round(dn / interval, 1),
            })
        rows.sort(key=lambda r: -(r["up"] + r["down"]))
        return {"services": rows[:limit], "groups": self._group_by_label(rows, 30),
                "interval": interval}

    def services_range(self, mac: str, days: int = 7, limit: int = 200) -> dict:
        day_to = time.strftime("%Y-%m-%d")
        day_from = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        try:
            db_rows = self.db.svc_range(mac, day_from, day_to)
        except Exception:
            db_rows = []
        rows = []
        for skey, up, down, conns in db_rows:
            info = self.namer.describe_key(skey)
            rows.append({**info, "up": up, "down": down, "conns": conns})
        return {"services": rows[:limit], "groups": self._group_by_label(rows, 30),
                "totals": {"up": sum(r["up"] for r in rows),
                           "down": sum(r["down"] for r in rows)}}

    def set_service_overrides(self, overrides: dict) -> None:
        self.namer.set_overrides(overrides)
        self.cfg["service_overrides"] = overrides

    # ---------------- 对外状态 ----------------

    def status(self) -> dict:
        with self._lock:
            return {
                "online": self.online,
                "ssh": self.client.status(),
                "router": {
                    "host": self.cfg["router"]["host"],
                    "wan_iface": self.wan_iface,
                    "wan_ip": self.wan_ip,
                    "lan_ip": self.lan_ip,
                    "uptime": round(self.uptime),
                    "route": self.route,
                },
                "rate": {"down_bps": round(self.down_bps, 1), "up_bps": round(self.up_bps, 1)},
                "peak": {"down_bps": round(self.peak_down, 1), "up_bps": round(self.peak_up, 1)},
                "session": {"up": self._session_up, "down": self._session_down,
                            "started_at": self._started_at},
                "poll": {
                    "interval": self.interval,
                    "last_ts": int(self.last_poll_ts),
                    "count": self.poll_count,
                    "fail_count": self.fail_count,
                    "elapsed": self.elapsed,
                    "raw_size": self.raw_size,
                    "sections": self.sections,
                    "counter_resets": self._counter_resets,
                },
                "counts": {"devices": self.device_count, "connections": self.conn_count,
                           "services": len(self._svc_today)},
                "last_error": self.last_error,
            }

    def devices_live(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self.live_devices.values()]

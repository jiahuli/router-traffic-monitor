"""SQLite 持久化层。

表设计：
  wan_10s     实时明细（每轮采集一行，保留 24 小时）
  wan_minute  WAN 总流量分钟聚合（长期保留）
  dev_5min    各设备 5 分钟聚合（长期保留）
  devices     设备档案（IP/MAC/主机名/在线状态）
  events      连接与异常事件日志
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS wan_10s (
    ts   INTEGER PRIMARY KEY,
    up   INTEGER NOT NULL,
    down INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wan_minute (
    ts   INTEGER PRIMARY KEY,
    up   INTEGER NOT NULL,
    down INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dev_5min (
    ts   INTEGER NOT NULL,
    mac  TEXT    NOT NULL,
    up   INTEGER NOT NULL,
    down INTEGER NOT NULL,
    PRIMARY KEY (ts, mac)
);
CREATE INDEX IF NOT EXISTS idx_dev5_mac ON dev_5min (mac, ts);
CREATE TABLE IF NOT EXISTS devices (
    mac        TEXT PRIMARY KEY,
    ip         TEXT,
    hostname   TEXT,
    vendor     TEXT,
    kind       TEXT,
    band       TEXT,
    signal     INTEGER,
    first_seen INTEGER,
    last_seen  INTEGER,
    online     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    INTEGER,
    level TEXT,
    msg   TEXT
);
CREATE TABLE IF NOT EXISTS svc_day (
    day   TEXT    NOT NULL,
    mac   TEXT    NOT NULL,
    skey  TEXT    NOT NULL,
    up    INTEGER NOT NULL,
    down  INTEGER NOT NULL,
    conns INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, mac, skey)
);
CREATE INDEX IF NOT EXISTS idx_svc_day ON svc_day (day, mac);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# 允许的时间粒度（秒）
RES_10S, RES_1M, RES_5M, RES_1H, RES_1D = 10, 60, 300, 3600, 86400


class Storage:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------- 写入 ----------------

    def add_wan_tick(self, ts: int, up: int, down: int) -> None:
        """写一条实时明细 + 累加到分钟桶。"""
        minute = ts - ts % 60
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO wan_10s (ts, up, down) VALUES (?,?,?)",
                (ts, up, down),
            )
            self._conn.execute(
                "INSERT INTO wan_minute (ts, up, down) VALUES (?,?,?) "
                "ON CONFLICT(ts) DO UPDATE SET up = up + excluded.up, down = down + excluded.down",
                (minute, up, down),
            )
            self._conn.commit()

    def add_dev_traffic(self, ts: int, mac: str, up: int, down: int) -> None:
        if up <= 0 and down <= 0:
            return
        bucket = ts - ts % 300
        with self._lock:
            self._conn.execute(
                "INSERT INTO dev_5min (ts, mac, up, down) VALUES (?,?,?,?) "
                "ON CONFLICT(ts, mac) DO UPDATE SET up = up + excluded.up, down = down + excluded.down",
                (bucket, mac, int(up), int(down)),
            )
            self._conn.commit()

    def upsert_device(self, mac: str, ip: str, hostname: str, vendor: str,
                      kind: str, band: str, signal, online: bool, ts: int) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT first_seen FROM devices WHERE mac=?", (mac,)).fetchone()
            first = cur[0] if cur and cur[0] else ts
            self._conn.execute(
                "INSERT INTO devices (mac, ip, hostname, vendor, kind, band, signal, first_seen, last_seen, online) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(mac) DO UPDATE SET "
                "  ip=excluded.ip, hostname=excluded.hostname, vendor=excluded.vendor, "
                "  kind=excluded.kind, band=excluded.band, signal=excluded.signal, "
                "  last_seen=excluded.last_seen, online=excluded.online",
                (mac, ip, hostname, vendor, kind, band, signal, first, ts, 1 if online else 0),
            )
            self._conn.commit()

    def log_event(self, level: str, msg: str, ts: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, level, msg) VALUES (?,?,?)",
                (ts or int(time.time()), level, msg),
            )
            self._conn.commit()

    def set_meta(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, str(v)))
            self._conn.commit()

    def get_meta(self, k: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    # ---------------- 清理 ----------------

    # ---------------- 服务维度 ----------------

    def save_svc_snapshot(self, day: str, mac: str, rows: list[tuple[str, int, int, int]]) -> None:
        """覆盖写入某设备当天的服务累计快照（rows: skey, up, down, conns）。"""
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO svc_day (day, mac, skey, up, down, conns) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(day, mac, skey) DO UPDATE SET "
                "  up=excluded.up, down=excluded.down, conns=excluded.conns",
                [(day, mac, r[0], int(r[1]), int(r[2]), int(r[3])) for r in rows],
            )
            self._conn.commit()

    def load_svc_day(self, day: str, mac: str | None = None) -> list[tuple[str, str, int, int, int]]:
        sql = "SELECT mac, skey, up, down, conns FROM svc_day WHERE day = ?"
        args: list = [day]
        if mac:
            sql += " AND mac = ?"
            args.append(mac)
        with self._lock:
            return [tuple(r) for r in self._conn.execute(sql, args).fetchall()]

    def svc_range(self, mac: str, day_from: str, day_to: str) -> list[tuple[str, int, int, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT skey, SUM(up), SUM(down), MAX(conns) FROM svc_day "
                "WHERE mac = ? AND day BETWEEN ? AND ? GROUP BY skey "
                "ORDER BY SUM(up) + SUM(down) DESC",
                (mac, day_from, day_to),
            ).fetchall()
        return [(r[0], int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)) for r in rows]

    def trim_svc_day(self, day: str, keep_per_device: int = 60) -> None:
        """每设备每天只保留流量最大的若干条，防止条目无限增长。"""
        with self._lock:
            macs = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT mac FROM svc_day WHERE day = ?", (day,)).fetchall()]
            for mac in macs:
                self._conn.execute(
                    "DELETE FROM svc_day WHERE day = ? AND mac = ? AND skey NOT IN ("
                    "  SELECT skey FROM svc_day WHERE day = ? AND mac = ? "
                    "  ORDER BY up + down DESC LIMIT ?)",
                    (day, mac, day, mac, keep_per_device),
                )
            self._conn.commit()

    def prune(self, now: int, keep_days: int, keep_raw_hours: int) -> None:
        raw_cut = now - keep_raw_hours * 3600
        day_cut = now - keep_days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM wan_10s WHERE ts < ?", (raw_cut,))
            self._conn.execute("DELETE FROM wan_minute WHERE ts < ?", (day_cut,))
            self._conn.execute("DELETE FROM svc_day WHERE day < ?",
                               (time.strftime("%Y-%m-%d", time.localtime(day_cut)),))
            # 设备 5 分钟粒度数据量更大，保留一半时间
            self._conn.execute("DELETE FROM dev_5min WHERE ts < ?", (now - max(7, keep_days // 2) * 86400,))
            self._conn.execute("DELETE FROM events WHERE ts < ?", (now - 30 * 86400,))
            self._conn.commit()

    # ---------------- 查询 ----------------

    @staticmethod
    def _bucket_expr(res: int, offset: int) -> str:
        """把 ts 对齐到本地时区的 res 秒桶。offset = 本地时区相对 UTC 的秒偏移。"""
        return f"((ts + {offset}) / {res}) * {res} - {offset}"

    def wan_series(self, t0: int, t1: int, res: int, offset: int) -> list[list[int]]:
        with self._lock:
            if res <= RES_10S:
                rows = self._conn.execute(
                    "SELECT ts, up, down FROM wan_10s WHERE ts BETWEEN ? AND ? ORDER BY ts",
                    (t0, t1),
                ).fetchall()
            else:
                b = self._bucket_expr(res, offset)
                rows = self._conn.execute(
                    f"SELECT {b} AS bkt, SUM(up), SUM(down) FROM wan_minute "
                    f"WHERE ts BETWEEN ? AND ? GROUP BY bkt ORDER BY bkt",
                    (t0, t1),
                ).fetchall()
        return [[int(r[0]), int(r[1] or 0), int(r[2] or 0)] for r in rows]

    def dev_series(self, t0: int, t1: int, res: int, offset: int, mac: str | None) -> list[list[int]]:
        res = max(res, RES_5M)
        b = self._bucket_expr(res, offset)
        sql = (f"SELECT {b} AS bkt, SUM(up), SUM(down) FROM dev_5min "
               f"WHERE ts BETWEEN ? AND ?")
        args = [t0, t1]
        if mac:
            sql += " AND mac = ?"
            args.append(mac)
        sql += " GROUP BY bkt ORDER BY bkt"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [[int(r[0]), int(r[1] or 0), int(r[2] or 0)] for r in rows]

    def wan_sum(self, t0: int, t1: int) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(up), SUM(down) FROM wan_minute WHERE ts BETWEEN ? AND ?", (t0, t1)
            ).fetchone()
        return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)

    def dev_sum(self, t0: int, t1: int) -> dict[str, list[int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, SUM(up), SUM(down) FROM dev_5min WHERE ts BETWEEN ? AND ? GROUP BY mac",
                (t0, t1),
            ).fetchall()
        return {r[0]: [int(r[1] or 0), int(r[2] or 0)] for r in rows}

    def dev_totals_since(self, t0: int) -> dict[str, list[int]]:
        return self.dev_sum(t0, int(time.time()) + 60)

    def devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, ip, hostname, vendor, kind, band, signal, first_seen, last_seen, online "
                "FROM devices"
            ).fetchall()
        keys = ["mac", "ip", "hostname", "vendor", "kind", "band", "signal",
                "first_seen", "last_seen", "online"]
        return [dict(zip(keys, r)) for r in rows]

    def recent_events(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, level, msg FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"ts": r[0], "level": r[1], "msg": r[2]} for r in rows]

    def stats(self) -> dict:
        def one(sql):
            return self._conn.execute(sql).fetchone()[0] or 0

        size = 0
        for suffix in ("", "-wal", "-shm"):
            f = self.path + suffix
            if os.path.exists(f):
                size += os.path.getsize(f)
        with self._lock:
            return {
                "wan_raw_rows": one("SELECT COUNT(*) FROM wan_10s"),
                "wan_min_rows": one("SELECT COUNT(*) FROM wan_minute"),
                "dev_rows": one("SELECT COUNT(*) FROM dev_5min"),
                "device_rows": one("SELECT COUNT(*) FROM devices"),
                "db_bytes": size,
            }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass

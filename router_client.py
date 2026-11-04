"""SSH 连接管理与路由器数据采集。

小米万兆路由器实测结论（固件 OpenWrt 18.06 / aarch64，出厂主机名 XiaoQiang）：
  * Dropbear 只提供 ssh-rsa 主机密钥、aes*-ctr、hmac-sha1/256，kex 支持 curve25519
  * 因此必须使用 paramiko < 4.0（4.0 起移除 ssh-rsa 支持）
  * 可用数据源：/proc/net/dev、/proc/net/nf_conntrack、/tmp/dhcp.leases、ip neigh、iwinfo
  * 无 nlbwmon / vnstat，各设备流量只能靠 conntrack 增量推算
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None

MARKERS = ["@@@DEV", "@@@CT", "@@@CTN", "@@@LEASE", "@@@NEIGH", "@@@WIFI", "@@@ROUTE", "@@@ADDR",
           "@@@UP", "@@@END"]

# 一次性取回本轮所有数据，单次 exec，避免多通道开销
REMOTE_SCRIPT = r"""
echo "@@@DEV"
cat /proc/net/dev 2>/dev/null
echo "@@@CT"
if command -v conntrack >/dev/null 2>&1; then
  conntrack -L -o extended 2>/dev/null
else
  cat /proc/net/nf_conntrack 2>/dev/null
fi
echo "@@@CTN"
cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null
echo "@@@LEASE"
cat /tmp/dhcp.leases 2>/dev/null
echo "@@@NEIGH"
ip neigh show 2>/dev/null
echo "@@@WIFI"
for i in wl0 wl1 wl2 wl5 wl7 wl9 wl11 wl13; do
  echo "#I $i"
  iwinfo $i assoclist 2>/dev/null
done
echo "@@@ROUTE"
ip route show default 2>/dev/null
echo "@@@ADDR"
ip -4 -o addr show 2>/dev/null
echo "@@@UP"
cat /proc/uptime 2>/dev/null
echo "@@@END"
"""

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
MAC_IN_LINE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\s")


class RouterError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------

def split_sections(text: str) -> dict[str, str]:
    """按 @@MARKER 切分单次输出。"""
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s in MARKERS:
            cur = s.strip("@")
            out[cur] = []
            continue
        if cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in out.items()}


def parse_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """解析 /proc/net/dev → {iface: (rx_bytes, tx_bytes)}。"""
    res: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        parts = rest.split()
        if len(parts) < 9:
            continue
        try:
            res[name] = (int(parts[0]), int(parts[8]))
        except ValueError:
            continue
    return res


PROTOS = ("tcp", "udp", "icmp", "sctp", "icmpv6", "dccp", "gre")


def parse_conntrack(text: str) -> list[dict]:
    """解析 conntrack 输出，返回连接及其双向字节数。

    兼容两种格式：
      * `conntrack -L -o extended`：`ipv4  2 tcp  6 ...`（首选，netlink 全量且一致）
      * `/proc/net/nf_conntrack`：同上格式，但高并发下会被 seq_file 截断，仅作兜底
    返回项：key / proto / osrc / odst / obytes / rbytes
    """
    conns: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] in ("ipv4", "ipv6"):
            if toks[0] == "ipv6":
                continue
            if len(toks) < 4:
                continue
            proto = toks[2]
        elif toks[0] in PROTOS:
            if len(toks) < 3:
                continue
            proto = toks[0]
        else:
            continue
        groups: list[dict] = []
        cur: dict | None = None
        for t in toks[3:]:
            if t.startswith("src="):
                cur = {"src": t[4:]}
                groups.append(cur)
            elif cur is not None:
                for k in ("dst", "sport", "dport", "bytes", "packets", "type", "code", "id"):
                    if t.startswith(k + "="):
                        cur[k] = t[len(k) + 1:]
                        break
        if not groups:
            continue
        o = groups[0]
        r = groups[1] if len(groups) > 1 else {}
        if "src" not in o or "dst" not in o:
            continue
        try:
            ob = int(o.get("bytes", 0))
            rb = int(r.get("bytes", 0))
        except ValueError:
            ob = rb = 0
        # ICMP 没有端口，用 type/code/id 组成指纹
        extra = ""
        if proto == "icmp":
            extra = f"|{o.get('type','')}/{o.get('code','')}/{o.get('id','')}"
        key = f"{proto}|{o['src']}:{o.get('sport','')}|{o['dst']}:{o.get('dport','')}{extra}"
        conns.append({
            "key": key, "proto": proto,
            "osrc": o["src"], "odst": o["dst"],
            "sport": o.get("sport", ""), "dport": o.get("dport", ""),
            "obytes": ob, "rbytes": rb,
        })
    return conns


def parse_leases(text: str) -> dict[str, tuple[str, str]]:
    """解析 /tmp/dhcp.leases → {ip: (mac, hostname)}。"""
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 4 and MAC_RE.match(p[1]):
            host = p[3] if p[3] != "*" else ""
            out[p[2]] = (p[1].lower(), host)
    return out


def parse_neigh(text: str) -> dict[str, str]:
    """解析 ip neigh → {ip: mac}，忽略 FAILED / 无效项。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) < 2 or ":" in p[0]:
            continue
        if "lladdr" in p and "FAILED" not in p and "INCOMPLETE" not in p:
            try:
                idx = p.index("lladdr")
                mac = p[idx + 1].lower()
            except (ValueError, IndexError):
                continue
            if MAC_RE.match(mac):
                out[p[0]] = mac
    return out


def parse_wifi(text: str) -> dict[str, dict]:
    """解析 iwinfo assoclist 组合输出 → {mac: {iface, ssid, channel, signal}}。

    输出格式为每个网卡前一行 "#I wlX"，随后是 iwinfo 的文本表。
    """
    out: dict[str, dict] = {}
    iface = ""
    ssid = ""
    channel = 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#I"):
            iface = s[2:].strip()
            ssid, channel = "", 0
            continue
        if s.startswith("ssid:"):
            ssid = s.split(":", 1)[1].strip().strip('"')
            continue
        if s.startswith("channel:"):
            m = re.search(r"\d+", s)
            channel = int(m.group()) if m else 0
            continue
        if not MAC_IN_LINE.match(s):
            continue
        mac = s.split()[0].lower()
        m = re.search(r"(-\d+)\s*dBm", s)
        signal = int(m.group(1)) if m else None
        out[mac] = {"iface": iface, "ssid": ssid, "channel": channel, "signal": signal}
    return out


def band_of(channel: int) -> str:
    if not channel:
        return ""
    if channel <= 14:
        return "2.4G"
    if channel <= 233:
        return "5G"
    return "6G"


def parse_uptime(text: str) -> float:
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return 0.0


def parse_addrs(text: str) -> dict[str, str]:
    """解析 `ip -4 -o addr show` → {iface: ip}（只取第一个 IPv4）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line.strip())
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


def detect_wan_iface(route_text: str, netdev: dict[str, tuple[int, int]], configured: str) -> str:
    """优先用配置，其次默认路由，最后按常见名字兜底。"""
    if configured and configured in netdev:
        return configured
    for line in route_text.splitlines():
        if line.startswith("default"):
            m = re.search(r"\bdev\s+(\S+)", line)
            if m and m.group(1) in netdev:
                return m.group(1)
    for cand in ("pppoe-wan", "wan", "wwan", "eth3", "eth4"):
        if cand in netdev:
            return cand
    return ""


# --------------------------------------------------------------------------
# 厂商识别（常见 OUI，仅用于让设备列表更易读）
# --------------------------------------------------------------------------
OUI_VENDOR = {
    "b0:82:e2": "华硕", "50:88:11": "小米", "f6:e1:ed": "小米", "ce:82:7d": "小米",
    "4c:c6:4c": "小米", "52:c6:4c": "小米", "64:64:9b": "小米", "78:11:dc": "小米",
    "8c:53:c3": "小米", "f0:b4:29": "小米", "28:6c:07": "小米", "ac:c1:ee": "小米",
    "f4:8c:50": "Apple", "a4:83:e7": "Apple", "dc:a9:04": "Apple", "f0:18:98": "Apple",
    "3c:22:fb": "Apple", "9c:fc:01": "Apple", "14:7d:da": "Apple",
    "00:50:56": "VMware", "00:0c:29": "VMware", "00:1c:42": "Parallels",
    "b8:27:eb": "树莓派", "dc:a6:32": "树莓派", "e4:5f:01": "树莓派",
    "18:c0:4d": "Intel", "3c:7c:3f": "Intel", "8c:16:45": "Intel",
    "00:1b:21": "Intel", "a0:36:9f": "Intel",
    "00:e0:4c": "Realtek", "52:54:00": "QEMU",
    "ec:fa:bc": "TP-Link", "50:c7:bf": "TP-Link", "c4:6e:1f": "TP-Link",
    "d8:47:32": "华为", "48:8f:5a": "华为", "f4:cb:52": "华为", "00:e0:fc": "华为",
    "bc:32:5f": "群晖", "00:11:32": "群晖", "24:5e:be": "群晖",
    "00:08:9b": "海康威视", "44:19:b6": "海康威视", "3c:ef:8c": "海康威视",
    "d4:61:9d": "Sony", "f8:e4:3b": "Sony", "78:84:3c": "Sony",
    "f0:9f:c2": "Ubiquiti", "24:5a:4c": "Ubiquiti",
    "cc:2d:e0": "乐视/其他", "e8:4e:ce": "Nintendo",
    "34:ea:e7": "Sonos", "5c:aa:fd": "Sonos",
    "00:1a:79": "安卓盒子", "7c:11:be": "安卓盒子",
    "30:76:6f": "LG", "58:fd:b1": "LG", "bc:76:5e": "LG",
    "44:65:0d": "Amazon", "f0:27:2d": "Amazon", "74:c2:46": "Amazon",
    "3c:5a:b4": "Google", "f4:f5:d8": "Google", "a4:77:33": "Google",
}


def vendor_of(mac: str) -> str:
    return OUI_VENDOR.get(mac[:8].lower(), "")


# --------------------------------------------------------------------------
# SSH 客户端（带自动重连）
# --------------------------------------------------------------------------

class RouterClient:
    def __init__(self, cfg: dict, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda level, msg: None)
        self._cli = None
        self._fail_count = 0
        self._next_try = 0.0
        self.connected = False
        self.last_error = ""
        self.last_error_ts = 0.0
        self.connect_count = 0
        self.last_ok_ts = 0.0
        self._version_warned = False

    # --- 内部 ---

    def _check_version(self) -> None:
        if paramiko is None:
            raise RouterError("未安装 paramiko，请执行：pip install -r requirements.txt")
        ver = getattr(paramiko, "__version__", "0")
        try:
            major = int(ver.split(".")[0])
        except ValueError:
            return
        if major >= 4 and not self._version_warned:
            self._version_warned = True
            self.log("warn", f"paramiko {ver} 已移除 ssh-rsa 支持，可能无法连接小米路由器；"
                             f"请降级：pip install 'paramiko==3.4.1'")

    def _close_quietly(self) -> None:
        if self._cli is not None:
            try:
                self._cli.close()
            except Exception:
                pass
        self._cli = None
        self.connected = False

    def _backoff(self) -> float:
        table = [2, 3, 5, 8, 13, 20, 30, 45, 60]
        return table[min(self._fail_count, len(table) - 1)]

    def _friendly(self, err: Exception) -> str:
        host = self.cfg.get("host", "")
        port = self.cfg.get("port", 22)
        name = type(err).__name__
        text = str(err)
        if isinstance(err, (socket.timeout, TimeoutError)) or "timed out" in text.lower():
            return f"连接超时：{host}:{port} 无响应（检查路由器 IP、网线/Wi-Fi 是否同一网段）"
        if "refused" in text.lower():
            return f"连接被拒绝：{host} 的 SSH 服务未开启"
        if "NoValidConnections" in name or "Unable to connect" in text:
            return f"无法连接到 {host}:{port}"
        if "host key" in text.lower() or "Incompatible" in name:
            return "SSH 算法不兼容（小米固件只支持 ssh-rsa），请安装 paramiko==3.4.1"
        if "auth" in text.lower():
            return "认证失败：用户名或密码错误"
        return f"{name}: {text}"

    def _mark_failed(self, err: Exception) -> None:
        self._close_quietly()
        self._fail_count += 1
        self._next_try = time.time() + self._backoff()
        msg = self._friendly(err)
        if msg != self.last_error:
            self.log("error", f"{msg}（{self._backoff():.0f}s 后重试）")
        self.last_error = msg
        self.last_error_ts = time.time()

    # --- 对外 ---

    def connect(self) -> bool:
        self._check_version()
        r = self.cfg
        if not r.get("host"):
            self._mark_failed(RouterError("未配置路由器地址，请在启动器中填写连接信息"))
            return False
        if not str(r.get("password") or ""):
            self._mark_failed(RouterError("未配置路由器密码，请在启动器中填写连接信息"))
            return False
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            cli.connect(
                hostname=r["host"], port=int(r.get("port", 22)),
                username=r["username"], password=r["password"],
                timeout=12, banner_timeout=20, auth_timeout=20,
                look_for_keys=False, allow_agent=False,
            )
            tr = cli.get_transport()
            if tr:
                tr.set_keepalive(25)
            self._cli = cli
            self.connected = True
            self._fail_count = 0
            self.last_error = ""
            self.connect_count += 1
            self.log("info", f"已连接路由器 {r['host']}")
            return True
        except paramiko.AuthenticationException:
            self._mark_failed(RouterError("认证失败，请检查用户名/密码"))
            return False
        except (socket.timeout, socket.error) as e:
            self._mark_failed(e)
            return False
        except Exception as e:  # 包含 IncompatiblePeer
            hint = ""
            if "host key" in str(e).lower() or "Incompatible" in type(e).__name__:
                hint = "（算法不兼容，需 paramiko==3.4.1）"
            self._mark_failed(RouterError(f"{e}{hint}"))
            return False

    def ensure(self) -> bool:
        """确保连接可用；必要时按退避策略重连。"""
        if self.connected and self._cli is not None:
            tr = self._cli.get_transport()
            if tr is not None and tr.is_active():
                return True
            self.log("warn", "SSH 连接已断开")
            self._mark_failed(RouterError("连接已断开"))
        now = time.time()
        if now < self._next_try:
            return False
        return self.connect()

    def run(self, script: str, timeout: float = 25) -> str:
        if not self.ensure():
            raise RouterError(self.last_error or "SSH 未连接")
        try:
            stdin, stdout, stderr = self._cli.exec_command(script, timeout=timeout, get_pty=False)
            chan = stdout.channel
            chan.settimeout(timeout)
            chunks = []
            while True:
                data = chan.recv(65536)
                if not data:
                    break
                chunks.append(data)
            out = b"".join(chunks).decode("utf-8", "replace")
            self.last_ok_ts = time.time()
            self._fail_count = 0
            return out
        except Exception as e:
            self._mark_failed(e)
            raise RouterError(f"执行命令失败：{e}") from e

    def close(self) -> None:
        self._close_quietly()

    def status(self) -> dict:
        tr = self._cli.get_transport() if self._cli else None
        active = bool(tr and tr.is_active())
        return {
            "connected": active,
            "host": self.cfg["host"],
            "connections": self.connect_count,
            "last_ok": self.last_ok_ts,
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
            "next_retry_in": max(0, round(self._next_try - time.time())) if not active else 0,
            "transport_active": active,
        }


# --------------------------------------------------------------------------
# 单轮采集
# --------------------------------------------------------------------------

def collect_once(client: RouterClient, lan_networks: list, wan_iface_cfg: str) -> dict:
    """执行一次采集并解析全部数据。"""
    t0 = time.time()
    raw = client.run(REMOTE_SCRIPT)
    elapsed = time.time() - t0
    sec = split_sections(raw)
    netdev = parse_net_dev(sec.get("DEV", ""))
    conns = parse_conntrack(sec.get("CT", ""))
    leases = parse_leases(sec.get("LEASE", ""))
    neigh = parse_neigh(sec.get("NEIGH", ""))
    wifi = parse_wifi(sec.get("WIFI", ""))
    addrs = parse_addrs(sec.get("ADDR", ""))
    wan_iface = detect_wan_iface(sec.get("ROUTE", ""), netdev, wan_iface_cfg)

    wan = netdev.get(wan_iface, (0, 0))
    lan_total = (0, 0)
    for name in ("br-lan", "br-guest", "br-miot"):
        if name in netdev:
            lan_total = (lan_total[0] + netdev[name][0], lan_total[1] + netdev[name][1])

    # conntrack 一致性校验：内核报告的总条目数 vs 实际读到的条目数
    try:
        ct_expect = int((sec.get("CTN", "0") or "0").strip().split()[0])
    except (ValueError, IndexError):
        ct_expect = 0
    ct_lines = 0
    for line in sec.get("CT", "").splitlines():
        t = line.strip().split()
        if t and (t[0] in ("ipv4", "ipv6") or t[0] in PROTOS):
            ct_lines += 1

    return {
        "ts": int(time.time()),
        "elapsed": round(elapsed, 3),
        "netdev": netdev,
        "wan_iface": wan_iface,
        "wan_rx": wan[0],
        "wan_tx": wan[1],
        "lan_rx": lan_total[0],
        "lan_tx": lan_total[1],
        "conntrack": conns,
        "ct_expect": ct_expect,
        "ct_lines": ct_lines,
        "ct_complete": (ct_lines >= ct_expect * 0.85) if ct_expect else True,
        "leases": leases,
        "neigh": neigh,
        "wifi": wifi,
        "uptime": parse_uptime(sec.get("UP", "")),
        "route": sec.get("ROUTE", "").strip(),
        "addrs": addrs,
        "wan_ip": addrs.get(wan_iface, ""),
        "lan_ip": addrs.get("br-lan", ""),
        "sections": {k: len(v) for k, v in sec.items()},
        "raw_size": len(raw),
    }


def ip_in_networks(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def make_networks(subnets: list) -> list:
    nets = []
    for s in subnets:
        try:
            nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            continue
    return nets

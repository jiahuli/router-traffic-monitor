"""服务/目标地址识别。

为什么不做"网站域名"级别的识别
--------------------------------
在本项目的目标固件（OpenWrt 18.06 + dnsmasq）上逐项实测过：

* dnsmasq 未开启 log-queries，logread 里没有任何查询记录，拿不到 域名→IP 映射
* 反向 DNS 命中率仅 16%，命中内容多为 `unassigned.psychz.net` 这类无意义结果，
  且每次失败要等 4.7 秒的超时，上百个 IP 无法接受
* TLS SNI 抓包看似可行，但当前网络绝大部分流量是 QUIC(UDP/443)，
  SNI 加密不可见；且 libpcap 在 10G 路由器上全量过包 CPU 风险高
* 路由器上的 ShellCrash 未暴露可用的 RESTful API

因此本模块给的是"**服务/目标地址**"级别的识别：按 目标 IP + 端口 归类，
再用「用户覆盖规则 > 已知服务网段 > 端口语义 > 未知」四级推断出可读名称。
它回答的是"这台设备主要在跟谁通信、占多少流量"，
**不承诺给出具体网站域名**。凡是推断出来的名称，UI 上都会标注来源。
"""
from __future__ import annotations

import ipaddress
import threading

# --------------------------------------------------------------------------
# 端口语义（只在没有更具体信息时使用）
# --------------------------------------------------------------------------
PORT_HINTS = {
    "443/tcp": "HTTPS",
    "443/udp": "QUIC",
    "80/tcp": "HTTP",
    "8080": "HTTP-alt",
    "8443": "HTTPS-alt",
    "53": "DNS",
    "853": "DoT",
    "123": "NTP",
    "67": "DHCP",
    "5228": "Google 推送",
    "5223": "APNs 推送",
    "5222": "XMPP",
    "3478": "STUN",
    "19302": "WebRTC",
    "1900": "SSDP",
    "5353": "mDNS",
    "6881": "BT",
    "51413": "BT",
    "22": "SSH",
    "23": "Telnet",
    "445": "SMB",
    "3389": "RDP",
    "5900": "VNC",
    "1194": "OpenVPN",
    "51820": "WireGuard",
    "500": "IPSec",
    "4500": "IPSec NAT-T",
    "8888": "HTTP-alt",
    "9999": "自定义服务",
    "1935": "RTMP",
    "554": "RTSP",
    "5060": "SIP",
    "143": "IMAP",
    "993": "IMAPS",
    "110": "POP3",
    "995": "POP3S",
    "25": "SMTP",
    "587": "SMTP",
    "20": "FTP",
    "21": "FTP",
    "1234": "自定义服务",
}

# --------------------------------------------------------------------------
# 已知服务网段（置信度较高、长期稳定的公网段）
# 说明：这里刻意只收录归属明确、变动少的网段。标记为"推断"，
#       命中不代表一定就是该服务（CDN/云厂商存在共用与转租）。
# --------------------------------------------------------------------------
KNOWN_CIDRS: list[tuple[str, str]] = [
    # 公共 DNS
    ("8.8.8.0/24", "Google DNS"),
    ("8.8.4.0/24", "Google DNS"),
    ("1.1.1.0/24", "Cloudflare DNS"),
    ("9.9.9.0/24", "Quad9 DNS"),
    ("208.67.222.0/24", "OpenDNS"),
    ("223.5.5.0/24", "阿里 DNS"),
    ("223.6.6.0/24", "阿里 DNS"),
    ("119.29.29.0/24", "腾讯 DNS"),
    ("114.114.114.0/24", "114 DNS"),
    # Cloudflare
    ("104.16.0.0/13", "Cloudflare"),
    ("104.24.0.0/14", "Cloudflare"),
    ("162.158.0.0/15", "Cloudflare"),
    ("172.64.0.0/13", "Cloudflare"),
    ("188.114.96.0/20", "Cloudflare"),
    ("162.159.128.0/18", "Cloudflare"),
    # Google
    ("142.250.0.0/15", "Google"),
    ("172.217.0.0/16", "Google"),
    ("172.253.0.0/16", "Google"),
    ("216.58.192.0/19", "Google"),
    ("74.125.0.0/16", "Google"),
    ("35.190.0.0/17", "Google"),
    ("64.233.160.0/19", "Google"),
    # Apple
    ("17.0.0.0/8", "Apple"),
    # Meta
    ("31.13.0.0/16", "Meta"),
    ("157.240.0.0/16", "Meta"),
    ("163.70.128.0/17", "Meta"),
    # GitHub
    ("140.82.112.0/20", "GitHub"),
    ("185.199.108.0/22", "GitHub Pages"),
    ("192.30.252.0/22", "GitHub"),
    # Telegram
    ("91.108.0.0/16", "Telegram"),
    ("149.154.160.0/20", "Telegram"),
    # 微软
    ("13.107.0.0/16", "微软"),
    ("204.79.197.0/24", "微软"),
    # Netflix
    ("45.57.0.0/17", "Netflix"),
    ("64.120.128.0/17", "Netflix"),
    # Steam
    ("155.133.224.0/19", "Steam"),
    ("162.254.192.0/18", "Steam"),
    # 腾讯
    ("119.28.0.0/16", "腾讯云"),
    ("129.28.0.0/16", "腾讯云"),
    ("101.91.0.0/16", "腾讯"),
    ("203.205.128.0/17", "腾讯"),
    ("113.96.0.0/12", "腾讯"),
    # 阿里
    ("106.11.0.0/16", "阿里"),
    ("121.0.0.0/8", "阿里"),
    # 字节跳动
    ("110.249.192.0/18", "字节跳动"),
    ("111.225.0.0/16", "字节跳动"),
    # B 站
    ("119.3.0.0/16", "哔哩哔哩"),
    ("120.92.0.0/16", "哔哩哔哩"),
]


def _compile_cidrs(items) -> list[tuple[ipaddress.IPv4Network, str]]:
    out = []
    for cidr, name in items:
        try:
            out.append((ipaddress.ip_network(cidr, strict=False), name))
        except ValueError:
            continue
    # 前缀长的优先匹配
    out.sort(key=lambda x: x[0].prefixlen, reverse=True)
    return out


class ServiceNamer:
    """把 (ip, port, proto) 解析成可读名称，带缓存。"""

    def __init__(self, overrides: dict | None = None):
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self.known = _compile_cidrs(KNOWN_CIDRS)
        self.set_overrides(overrides or {})

    # ---------------- 覆盖规则 ----------------

    def set_overrides(self, overrides: dict) -> None:
        exact, cidrs = {}, []
        for key, name in (overrides or {}).items():
            key = str(key).strip()
            name = str(name).strip()
            if not key or not name:
                continue
            if "/" in key:
                try:
                    cidrs.append((ipaddress.ip_network(key, strict=False), name))
                except ValueError:
                    continue
            else:
                exact[key] = name
        cidrs.sort(key=lambda x: x[0].prefixlen, reverse=True)
        with self._lock:
            self._exact = exact
            self._cidrs = cidrs
            self._cache.clear()

    # ---------------- 解析 ----------------

    def resolve(self, ip: str, port: str, proto: str = "tcp") -> dict:
        skey = f"{ip}|{port}/{proto}"
        with self._lock:
            hit = self._cache.get(skey)
            if hit:
                return hit

        label, source, vendor = None, None, ""

        # 1) 精确 ip:port / ip
        with self._lock:
            exact = self._exact
            cidrs = self._cidrs
        label = exact.get(f"{ip}:{port}")
        if label:
            source = "自定义"
        if not label:
            label = exact.get(ip)
            if label:
                source = "自定义"

        # 2) 自定义网段
        if not label:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                addr = None
            if addr is not None:
                for net, name in cidrs:
                    if addr in net:
                        label, source = name, "自定义"
                        break

        # 3) 内置已知网段
        if not label and addr is not None:
            for net, name in self.known:
                if addr in net:
                    label, source, vendor = name, "推断", name
                    break

        # 4) 端口语义（先按 端口/协议 精确匹配，再按端口）
        port_hint = PORT_HINTS.get(f"{port}/{proto}") or PORT_HINTS.get(str(port), "")
        if not port_hint and proto in ("icmp", "icmpv6"):
            port_hint = "ICMP"
        if not label:
            if port_hint:
                label, source = port_hint, "端口"
            elif not port:
                label, source = proto.upper(), "协议"
            else:
                label, source = "未知服务", "未知"
        elif port_hint and source == "推断":
            label = f"{label} · {port_hint}"

        result = {
            "key": skey,
            "ip": ip,
            "port": str(port),
            "proto": proto,
            "label": label,
            "source": source,
            "vendor": vendor,
            "port_hint": port_hint,
        }
        with self._lock:
            if len(self._cache) > 4000:
                self._cache.clear()
            self._cache[skey] = result
        return result

    def describe_key(self, skey: str) -> dict:
        """从 svc_key（ip|port/proto）反解。"""
        try:
            host, tail = skey.rsplit("|", 1)
            port, _, proto = tail.partition("/")
        except ValueError:
            return {"key": skey, "ip": skey, "port": "", "proto": "",
                    "label": "未知服务", "source": "未知", "vendor": "", "port_hint": ""}
        return self.resolve(host, port, proto or "tcp")

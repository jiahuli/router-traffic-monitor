"""路由器流量监控 - 桌面启动器

两个按钮：
  1. 开机自启     —— 在注册表 HKCU\\...\\Run 写入/删除启动项（登录后静默启动监控服务）
  2. 打开监控页面 —— 确保后台服务在跑，然后打开浏览器

打包：pyinstaller --noconsole --onefile --add-data "web;web" --name 路由器流量监控 launcher.py
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "XiaoMiRouterTraffic"
DEFAULT_PORT = 8787

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# 配色（与网页端一致）
BG = "#0f141d"
CARD = "#161e2c"
LINE = "#243044"
TEXT = "#e7eefb"
MUTED = "#8b98ae"
ACCENT = "#4c8dff"
OK = "#38d996"
WARN = "#ffa53d"
BAD = "#ff6b6b"


# ------------------------------------------------------------------ 基础工具

def resource_path(name: str) -> str:
    """打包后静态资源在 _MEIPASS，开发时在脚本目录。"""
    return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), name)


def read_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_port() -> int:
    try:
        return int(read_cfg().get("http", {}).get("port", DEFAULT_PORT))
    except Exception:
        return DEFAULT_PORT


def config_ready(cfg: dict | None = None) -> bool:
    """仓库里不带任何凭据，连接信息必须由用户自己填。"""
    cfg = cfg if cfg is not None else read_cfg()
    r = cfg.get("router") or {}
    return bool(str(r.get("host") or "").strip() and str(r.get("password") or "").strip())


def save_connection(host: str, username: str, password: str) -> tuple[bool, str]:
    """校验并写入连接信息，返回 (是否成功, 错误信息)。"""
    host = (host or "").strip()
    username = (username or "").strip() or "root"
    if not host:
        return False, "路由器地址不能为空"
    if not password:
        return False, "SSH 密码不能为空"
    if any(c in host for c in " \t\r\n/\\"):
        return False, "路由器地址格式不正确"

    cfg = read_cfg()
    cfg.setdefault("router", {})
    cfg["router"].update({"host": host, "username": username, "password": password})
    cfg.setdefault("http", {}).update({"host": "127.0.0.1", "port": cfg.get("http", {}).get("port") or DEFAULT_PORT})
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        return False, f"写入配置失败：{ex}"
    return True, ""


def server_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def is_server_up(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def server_argv() -> list[str]:
    if FROZEN:
        return [sys.executable, "--server"]
    return [sys.executable, os.path.abspath(__file__), "--server"]


def clean_env() -> dict:
    """剥掉 PyInstaller 的解包目录环境变量。

    GUI 进程用 Popen 再拉起自己（--server）时，子进程会继承
    _PYI_APPLICATION_HOME_DIR / _MEI* 这类变量，导致它不重新解包、
    而是复用父进程的 _MEIxxxx 临时目录。父进程一退出就会删掉那个目录，
    后台服务随即失效（网页打不开，还会弹 "Failed to remove temporary directory"）。
    清掉这些变量后，子进程解包到自己的目录，两者互不影响。
    """
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(("_PYI", "_MEI"))}


def spawn_server() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    kwargs = dict(
        cwd=APP_DIR, env=clean_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    base = CREATE_NO_WINDOW | DETACHED_PROCESS
    try:
        # 若 GUI 被放进某个 job（任务计划、启动器等），退出时会连子进程一起杀。
        # 带上 BREAKAWAY 让后台服务真正独立；job 不允许时自动退回。
        subprocess.Popen(server_argv(), creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        subprocess.Popen(server_argv(), creationflags=base, **kwargs)


def api_ok(port: int, timeout: float = 3.0) -> bool:
    """服务不只是端口在听，API 还得真能返回数据。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=timeout) as r:
            body = r.read(8192)
            return r.status == 200 and b'"status"' in body
    except Exception:
        return False


def ensure_server(port: int, wait: float = 30.0) -> bool:
    if is_server_up(port):
        # 端口在监听但接口不应答，多半是被删掉文件的残留进程，清掉重来
        for _ in range(4):
            if api_ok(port):
                return True
            time.sleep(1.0)
        stop_server(port)
        time.sleep(0.8)

    spawn_server()
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_server_up(port) and api_ok(port, 2.0):
            return True
        time.sleep(0.4)
    return False


def stop_server(port: int) -> bool:
    """按端口找到监听进程并结束（仅用于退出时停止后台服务）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             creationflags=CREATE_NO_WINDOW, timeout=10).stdout.decode("gbk", "ignore")
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                pids.add(parts[4])
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=10)
        return bool(pids)
    except Exception:
        return False


# ------------------------------------------------------------------ 开机自启

def autostart_target() -> str:
    if FROZEN:
        return f'"{sys.executable}" --server'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --server'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_NAME)
            return bool(val)
    except OSError:
        return False


def set_autostart(on: bool) -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_target())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return autostart_enabled() == on
    except OSError:
        return False


# ------------------------------------------------------------------ 服务模式

def run_server_mode() -> None:
    """--server：无界面后台运行，日志写 data/monitor.log。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    print(f"\n==== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ====")
    sys.argv = [sys.argv[0], "--no-browser"]
    try:
        from app import main as app_main
        app_main()
    except Exception:
        import traceback
        traceback.print_exc()
        raise


# ------------------------------------------------------------------ 界面

def fmt_rate(bps: float) -> str:
    n = float(bps or 0)
    for u in ("B/s", "KB/s", "MB/s", "GB/s"):
        if n < 1024 or u == "GB/s":
            return (f"{n:.0f} {u}" if n >= 10 else f"{n:.1f} {u}")
        n /= 1024
    return f"{n:.1f} GB/s"


class App:
    def __init__(self, root):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.port = read_port()
        self.stop_flag = threading.Event()
        self.busy = False
        self._fail_streak = 0

        root.title("路由器流量监控")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.geometry("400x276")
        self.center(400, 276)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        ico = resource_path("icon.ico")
        if os.path.exists(ico):
            try:
                root.iconbitmap(ico)
            except Exception:
                pass

        ui_font = ("Microsoft YaHei UI", 10)
        title_font = ("Microsoft YaHei UI", 14, "bold")
        small_font = ("Microsoft YaHei UI", 9)

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=22, pady=(20, 0))
        tk.Label(head, text="路由器流量监控", bg=BG, fg=TEXT, font=title_font).pack(anchor="w")
        self.sub = tk.Label(head, text="", bg=BG, fg=MUTED, font=small_font)
        self.sub.pack(anchor="w", pady=(3, 0))

        status = tk.Frame(root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        status.pack(fill="x", padx=22, pady=(14, 0), ipady=9)
        self.dot = tk.Label(status, text="●", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.dot.pack(side="left", padx=(12, 6))
        self.status = tk.Label(status, text="正在检查监控服务…", bg=CARD, fg=MUTED, font=ui_font)
        self.status.pack(side="left")

        self.btn_auto = self.make_button(root, "开机自启：读取中…", self.toggle_autostart, primary=False)
        self.btn_auto.pack(fill="x", padx=22, pady=(16, 8), ipady=7)
        self.btn_open = self.make_button(root, "打开监控页面", self.open_page, primary=True)
        self.btn_open.pack(fill="x", padx=22, pady=(0, 4), ipady=7)

        self.refresh_autostart_label()
        self.sub.config(text=f"{self.host_text()}  ·  端口 {self.port}")

        if config_ready():
            threading.Thread(target=self.startup_worker, daemon=True).start()
        else:
            self.set_status("未配置路由器，请先填写连接信息", WARN)
            self.root.after(300, self.open_setup)
        self.poll_status()

    # ---- 首次配置 ----

    def open_setup(self):
        tk = self.tk
        cfg = read_cfg()
        router = cfg.get("router") or {}

        win = tk.Toplevel(self.root)
        win.title("首次配置")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        w, h = 380, 384
        x = self.root.winfo_x() + (400 - w) // 2
        y = max(60, self.root.winfo_y() - 30)
        win.geometry(f"{w}x{h}+{x}+{y}")

        tk.Label(win, text="连接路由器", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=22, pady=(18, 2))
        tk.Label(win, text="需要先在路由器后台开启 SSH（小米：常用设置 → 安全设置 → SSH）",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9), wraplength=336,
                 justify="left").pack(anchor="w", padx=22)

        form = tk.Frame(win, bg=BG)
        form.pack(fill="x", padx=22, pady=(12, 0))
        entries = {}
        fields = [
            ("host", "路由器地址", router.get("host") or "192.168.31.1"),
            ("username", "SSH 用户名", router.get("username") or "root"),
            ("password", "SSH 密码", ""),
        ]
        for key, label, default in fields:
            tk.Label(form, text=label, bg=BG, fg=MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            e = tk.Entry(form, bg=CARD, fg=TEXT, insertbackground=TEXT,
                         relief="flat", highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACCENT, font=("Microsoft YaHei UI", 10))
            e.insert(0, default)
            if key == "password":
                e.config(show="*")
            e.pack(fill="x", ipady=5, pady=(3, 9))
            entries[key] = e

        err = tk.Label(win, text="", bg=BG, fg=BAD, font=("Microsoft YaHei UI", 9))
        err.pack(anchor="w", padx=22)

        def save():
            ok, msg = save_connection(entries["host"].get(),
                                      entries["username"].get(),
                                      entries["password"].get())
            if not ok:
                err.config(text=msg)
                return
            win.destroy()
            self.port = read_port()
            self.sub.config(text=f"{self.host_text()}  ·  端口 {self.port}")
            self.set_status("正在连接路由器…", MUTED)
            threading.Thread(target=self.startup_worker, daemon=True).start()

        btn = tk.Button(win, text="保存并启动", command=save,
                        bg=ACCENT, fg="#ffffff", activebackground="#3d7cf0",
                        activeforeground="#ffffff", relief="flat", bd=0,
                        font=("Microsoft YaHei UI", 10), cursor="hand2")
        btn.pack(fill="x", padx=22, pady=(8, 4), ipady=6)
        for e in entries.values():          # 任意输入框回车即提交
            e.bind("<Return>", lambda ev: save())
        entries["host"].focus_set()

    def center(self, w, h):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2 - 40
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def host_text(self):
        host = (read_cfg().get("router") or {}).get("host")
        return host or "未配置路由器"

    def make_button(self, parent, text, cmd, primary):
        tk = self.tk
        bg = ACCENT if primary else CARD
        fg = "#ffffff" if primary else TEXT
        hover = "#3d7cf0" if primary else "#1d2637"
        b = tk.Button(
            parent, text=text, command=cmd, font=("Microsoft YaHei UI", 10),
            bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=ACCENT if primary else LINE,
            cursor="hand2", takefocus=0,
        )
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        b._base_bg = bg
        b._hover_bg = hover
        return b

    # ---- 状态 ----

    def set_status(self, text, color):
        self.dot.config(fg=color)
        self.status.config(text=text, fg=color if color != MUTED else MUTED)

    def startup_worker(self):
        ok = ensure_server(self.port)
        if not ok:
            self.root.after(0, lambda: self.set_status("监控服务启动失败，详见 data/monitor.log", BAD))

    def poll_status(self):
        if self.stop_flag.is_set():
            return

        def work():
            text, color = self.query_status()
            if not self.stop_flag.is_set():
                self.root.after(0, lambda: self.set_status(text, color))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(3000, self.poll_status)

    def query_status(self):
        if not is_server_up(self.port, 0.4):
            self._fail_streak = 0
            return "监控服务未运行", WARN
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/overview", timeout=4) as r:
                data = json.loads(r.read().decode("utf-8"))
            self._fail_streak = 0
            st = data["status"]
            if st.get("online"):
                rate = st["rate"]
                return (f"运行中 · 已连接路由器 · ↓ {fmt_rate(rate['down_bps'])} ↑ {fmt_rate(rate['up_bps'])}",
                        OK)
            err = (st.get("last_error") or "正在连接路由器…").strip()
            if len(err) > 26:
                err = err[:26] + "…"
            return f"运行中 · 路由器未连接（{err}）", WARN
        except Exception:
            # 端口在听但接口拿不到数据：可能是残留的坏进程
            self._fail_streak += 1
            if self._fail_streak >= 3:
                return "服务异常，点「打开监控页面」自动修复", BAD
            return "服务响应超时…", WARN

    # ---- 按钮 ----

    def refresh_autostart_label(self):
        on = autostart_enabled()
        self.btn_auto.config(
            text="开机自启：已开启（点击关闭）" if on else "开机自启：已关闭（点击开启）",
            fg="#9ef0c6" if on else TEXT,
        )

    def toggle_autostart(self):
        want = not autostart_enabled()
        ok = set_autostart(want)
        self.refresh_autostart_label()
        if not ok:
            self.set_status("开机自启设置失败（注册表写入被拒绝）", BAD)
        else:
            self.set_status(f"已{'开启' if want else '关闭'}开机自启" +
                            ("，下次登录自动在后台采集" if want else ""), OK if want else MUTED)
            if want:
                ensure_server(self.port)

    def open_page(self):
        if self.busy:
            return
        self.busy = True
        label = self.btn_open.cget("text")
        self.btn_open.config(text="正在启动服务…", state="disabled")

        def work():
            ok = ensure_server(self.port)
            def done():
                self.btn_open.config(text=label, state="normal")
                self.busy = False
                if ok:
                    webbrowser.open(server_url(self.port))
                else:
                    self.set_status("启动失败：请查看 data/monitor.log", BAD)
            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        from tkinter import messagebox
        ans = messagebox.askyesnocancel(
            "退出",
            "关闭窗口后，后台监控仍会继续运行（浏览器可继续访问）。\n\n"
            "是否同时停止后台监控服务？\n\n"
            "是 = 停止监控并退出\n否 = 保持后台运行并退出\n取消 = 返回",
            icon="question",
        )
        if ans is None:
            return
        if ans:
            self.set_status("正在停止监控服务…", MUTED)
            stop_server(self.port)
        self.stop_flag.set()
        self.root.destroy()


def main():
    if "--server" in sys.argv:
        run_server_mode()
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    import tkinter as tk
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

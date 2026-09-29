"""Double-click launcher for the CallRec cockpit (no console window).

Called by 「双击启动 CallRec.bat」 / 「停止 CallRec.bat」 with pythonw, so it stays
stdlib-only and never pops a black window. What it does:

  (no args) 已在跑就只开浏览器；没跑则后台拉起 Web 服务，等它就绪再开浏览器
  --auto    起来之后顺手打开「整夜挂机」（等价于在页面上按那个开关）
  --stop    确认后杀掉服务进程树（正在跑的阶段按通话粒度可续跑）

Windows' Hyper-V/WSL can reserve a whole port range at boot — on a machine with
8710-8809 excluded, ``web.port: 8760`` fails to bind with WSAEACCES 10013. So
the launcher picks a bindable port and records it; the page itself is
same-origin and does not care which port it came from.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "web.log"
STATE = ROOT / "data" / "web_server.json"
TITLE = "通话档案 CallRec"


# ---------- config.yaml, read without pyyaml (any python may run this) ----------
def cfg() -> dict:
    out = {"host": "127.0.0.1", "port": 8760, "python": sys.executable}
    p = ROOT / "config.yaml"
    if not p.exists():
        p = ROOT / "config.example.yaml"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out
    section = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            section = line.partition(":")[0].strip()
            if section == "python_env":
                v = line.partition(":")[2].strip().strip('"').strip("'")
                if v:
                    out["python"] = v
            continue
        key, _, val = line.strip().partition(":")
        key, val = key.strip(), val.split("#")[0].strip().strip('"').strip("'")
        if section == "web" and key == "host" and val:
            out["host"] = val
        if section == "web" and key == "port" and val.isdigit():
            out["port"] = int(val)
    return out


def server_python(c):
    """Plain python.exe: CREATE_NO_WINDOW keeps it console-free, and unlike
    pythonw it leaves a real stdout for uvicorn's logging to write into."""
    py = Path(c["python"])
    return str(py) if py.exists() else sys.executable


# ---------- port / liveness ----------
def http_host(c):
    return "127.0.0.1" if c["host"] in ("0.0.0.0", "::", "") else c["host"]


def probe(port, timeout=1.5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def bindable(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, int(port)))
            return True
        except OSError:
            return False


def pick_port(c):
    want = int(c["port"])
    if bindable(want):
        return want
    for p in list(range(want + 1, want + 41)) + [18760, 28760]:
        if bindable(p):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:   # 让系统给一个
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def running_port(c):
    """已有服务在哪个端口：先信记录，再看配置默认。"""
    st = read_state()
    for p in ([st["port"]] if st else []) + [int(c["port"])]:
        if p and probe(p, 0.8):
            return int(p)
    return None


def read_state():
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def write_state(pid, port):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"pid": pid, "port": port, "at": time.strftime("%Y-%m-%d %H:%M:%S")}),
                     encoding="utf-8")


def hide() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def pid_on_port(port: int):
    """服务可能是用 scripts/*.bat 起的（没有 state 文件）。"""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, creationflags=hide(), timeout=15).stdout
    except Exception:
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[0] == "TCP" and cols[3] == "LISTENING" \
                and cols[1].rsplit(":", 1)[-1] == str(port) and cols[4].isdigit():
            return int(cols[4])
    return None


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=hide())
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


# ---------- how to talk to a human when there is no console ----------
def notify(msg: str, warn: bool = False) -> None:
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x30 if warn else 0x40)
            return
        except Exception:
            pass
    print(msg)


def confirm(msg: str) -> bool:
    if os.name == "nt":
        import ctypes
        try:
            return ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x1 | 0x20) == 1
        except Exception:
            pass
    try:
        return input(msg + " [y/N] ").strip().lower() == "y"
    except Exception:
        return False


def logged_tail(n=1400):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def post(port: int, path: str) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                     data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 400
    except Exception:
        return False


# ---------- the two actions ----------
def start(c, auto: bool) -> int:
    port = running_port(c)
    if port:
        if auto:
            post(port, "/api/jobs/auto")
        webbrowser.open(f"http://{http_host(c)}:{port}/")
        return 0

    port = pick_port(c)
    want = int(c["port"])
    if port != want:
        # 端口被 Windows 保留段占了不是错，只要让用户知道他地址栏里为什么不是 8760
        LOG.parent.mkdir(exist_ok=True)
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 配置端口 {want} 无法绑定（多半被 Windows/Hyper-V "
                    f"保留），改用 {port}\n")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               CALLREC_HOST=http_host(c), CALLREC_PORT=str(port))
    LOG.parent.mkdir(exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)
    logf = open(LOG, "a", encoding="utf-8", errors="replace")
    logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 由启动器拉起，端口 {port} =====\n")
    logf.flush()
    try:
        proc = subprocess.Popen([server_python(c), str(ROOT / "pipeline.py"), "web"],
                                cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, creationflags=hide())
    except OSError as e:
        notify(f"起不来：{e}\n\n请检查 config.yaml 里的 python_env 是否存在。", True)
        return 1
    write_state(proc.pid, port)
    for _ in range(60):                       # 最多等 30 秒
        if probe(port, 0.8):
            break
        if proc.poll() is not None:
            logf.close()
            notify("启动失败，服务进程退出了。\n\n日志最后几行：\n" + logged_tail(), True)
            return 1
        time.sleep(0.5)
    else:
        logf.close()
        notify(f"等了 30 秒服务还没就绪（这台机器可能正忙）。\n\n日志最后几行：\n{logged_tail()}", True)
        return 1
    if auto:
        post(port, "/api/jobs/auto")
    webbrowser.open(f"http://{http_host(c)}:{port}/")
    logf.close()
    return 0


def stop(c) -> int:
    st = read_state()
    port = (st or {}).get("port") or int(c["port"])
    pid = (st or {}).get("pid") or pid_on_port(port)
    if not pid and not probe(port, 0.8):
        notify("没有在跑的服务，无需停止。")
        return 0
    if not confirm("停止后台服务？\n\n正在跑的阶段（转写/精修/摘要）会被中断——"
                   "进度按通话保存，下次双击启动后到「工作台」点一下就会接着跑。"):
        return 0
    if pid:
        kill_tree(int(pid))
    for _ in range(20):
        if not probe(port, 0.6):
            break
        time.sleep(0.4)
    try:
        STATE.unlink()
    except OSError:
        pass
    still = probe(port, 0.6)
    notify("已停止。" if not still else
           f"发了停止信号但端口 {port} 还开着：可能起了不止一份服务。\n"
           f"可在任务管理器里结束 PID {pid}。", still)
    return 0


def main():
    args = [a.lower() for a in sys.argv[1:]]
    c = cfg()
    if "--stop" in args:
        return stop(c)
    return start(c, auto=("--auto" in args or "auto" in args))


if __name__ == "__main__":
    sys.exit(main())

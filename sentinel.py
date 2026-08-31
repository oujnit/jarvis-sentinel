#!/usr/bin/env python3
"""
JARVIS Sentinel — 本地 AI 编码代理活动哨兵
检测 Codex / Claude Code / ZCode 的进程与会话活动，通过 SSE 推送给 jarvis.html。

用法:
    python3 sentinel.py            # 启动并自动打开浏览器
    python3 sentinel.py --no-open  # 启动但不打开浏览器
纯 Python 标准库，零依赖。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
PORT = int(os.environ.get("JARVIS_PORT", "8766"))
HTML = Path(__file__).with_name("jarvis.html")

# ---------------- 工具定义：进程匹配规则 + 活动信号文件 ----------------
TOOLS = {
    "codex": {
        "label": "CODEX",
        "home": HOME / ".codex",
        "proc_in": [
            r"ChatGPT\.app/Contents/(Resources|Frameworks)/.*[Cc]odex",
            r"codex-code-mode-host",
            r"openai\.chatgpt-[^ ]*/codex\b",
            r"^[^ ]*/codex app-server",
        ],
        "proc_ex": [r"opencodex", r"CodexBar"],
        # rollout 会话文件在干活时增长；sqlite WAL 属后台常写，不可靠，不用
        "signals": [
            (HOME / ".codex", "sessions/**/*.jsonl"),
            (HOME / ".codex", "session_index.jsonl"),
        ],
    },
    "claude": {
        "label": "CLAUDE CODE",
        "home": HOME / ".claude",
        "proc_in": [
            r"Caskroom/claude-code",
            r"homebrew/[^ ]*/claude\b",
            r"@anthropic-ai/claude-code",
            r"^[^ ]*claude\b",
        ],
        "proc_ex": [r"sentinel", r"\.claude/plugins", r"claude-code-syntax"],
        # 每会话一个 <uuid>.jsonl，写入即干活
        "signals": [
            (HOME / ".claude", "projects/*/*.jsonl"),
            (HOME / ".claude", "history.jsonl"),
        ],
    },
    "zcode": {
        "label": "ZCODE",
        "home": HOME / ".zcode",
        "proc_in": [
            r"^[^ ]*zcode-cli\b",
            r"/Applications/ZCode\.app/Contents/MacOS",
        ],
        "proc_ex": [],
        # 当日日志是最强信号；rollout 随每次模型请求增长
        "signals": [
            (HOME / ".zcode", "cli/log/zcode-*.jsonl"),
            (HOME / ".zcode", "cli/rollout/*.jsonl"),
        ],
    },
}
for cfg in TOOLS.values():
    cfg["proc_in"] = [re.compile(p) for p in cfg["proc_in"]]
    cfg["proc_ex"] = [re.compile(p) for p in cfg["proc_ex"]]

# ---------------- 共享状态 ----------------
LOCK = threading.Lock()
STATE = {
    "ts": 0,
    "tools": {
        k: {"label": c["label"], "installed": c["home"].exists(),
            "state": "offline", "procs": 0, "cpu": 0.0,
            "level": 0.0, "lastActive": 0, "events": 0}
        for k, c in TOOLS.items()
    },
}


def scan_processes():
    """返回 {tool: (进程数, cpu%总和)}"""
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True,
                             timeout=3).stdout
    except Exception:
        return {k: (0, 0.0) for k in TOOLS}
    stats = {k: [0, 0.0] for k in TOOLS}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        cmd = parts[10]
        try:
            cpu = float(parts[2])
        except ValueError:
            cpu = 0.0
        for k, cfg in TOOLS.items():
            if any(p.search(cmd) for p in cfg["proc_ex"]):
                continue
            if any(p.search(cmd) for p in cfg["proc_in"]):
                stats[k][0] += 1
                stats[k][1] += cpu
    return {k: (v[0], round(v[1], 1)) for k, v in stats.items()}


def scan_signals(snapshots):
    """对每个工具的信号文件做 mtime/size 快照，与上次比较得出活动增量。
    首轮只建立基线快照，不产生活动（避免历史文件被误判为活动）。"""
    result = {}
    for k, cfg in TOOLS.items():
        snap = {}
        for base, pattern in cfg["signals"]:
            if not base.exists():
                continue
            try:
                for f in base.glob(pattern):
                    try:
                        st = f.stat()
                        snap[str(f)] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        pass
            except OSError:
                pass
        if k in snapshots:
            prev = snapshots[k]
            changed = sum(1 for p, v in snap.items() if prev.get(p) != v)
        else:
            changed = 0  # 冷启动：仅建立基线
        snapshots[k] = snap
        result[k] = changed
    return result


def collector(interval=1.0):
    snapshots = {}
    levels = {k: 0.0 for k in TOOLS}
    while True:
        procs = scan_processes()
        changed = scan_signals(snapshots)
        now = time.time()
        tools = {}
        for k, cfg in TOOLS.items():
            n, cpu = procs[k]
            # 活跃度：事件驱动增长，指数衰减
            levels[k] = min(1.0, levels[k] * 0.80 + changed[k] * 0.45)
            tool = STATE["tools"][k]
            if levels[k] >= 0.08:
                state = "active"
            elif n > 0:
                state = "running"
            elif not cfg["home"].exists():
                state = "absent"
            else:
                state = "offline"
            if changed[k] > 0:
                tool["lastActive"] = now
            tools[k] = {
                "label": cfg["label"],
                "installed": cfg["home"].exists(),
                "state": state,
                "procs": n,
                "cpu": cpu,
                "level": round(levels[k], 3),
                "lastActive": tool["lastActive"],
                "events": changed[k],
            }
        with LOCK:
            STATE["ts"] = now
            STATE["tools"] = tools
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html", "/jarvis.html"):
            try:
                body = HTML.read_bytes()
            except OSError:
                self.send_error(500, "jarvis.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    with LOCK:
                        frame = json.dumps(STATE)
                    self.wfile.write(f"data: {frame}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # 静默访问日志


def main():
    threading.Thread(target=collector, daemon=True).start()
    if "--no-open" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"JARVIS sentinel listening → http://127.0.0.1:{PORT}  (Ctrl+C 退出)")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nsentinel stopped.")


if __name__ == "__main__":
    main()

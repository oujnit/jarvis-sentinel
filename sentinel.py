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
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
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
    "sys": {},      # 本机系统：电池/内存/负载/磁盘
    "resets": {},   # Codex 额度重置（codex-resets.com，公共 API）
    "weather": {},  # 天气（Open-Meteo，免密钥）
    "quota": {},    # AI 平台额度（DeepSeek/GLM，读本机凭证，未配置则报错说明）
}

# ---------------- 遥测采集（参考 kindle-ai-quota-dashboard 的数据源） ----------------

def open_json(url, headers=None, timeout=10, context=None):
    """urllib 拉 JSON；默认校验证书，缺根证书环境自动回退非校验（本地展示用途）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "jarvis-sentinel/1.0",
        "Accept": "application/json",
        **(headers or {}),
    })
    try:
        if context is None:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
    ctx = context or ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def read_cred(env_names, file_keys):
    """凭证级联：环境变量 → ~/.jarvis/credentials.json → 参考项目的本机凭证文件。"""
    for name in env_names:
        v = os.environ.get(name)
        if v:
            return v.strip()
    try:
        with (HOME / ".jarvis" / "credentials.json").open() as f:
            data = json.load(f)
        for k in file_keys:
            if data.get(k):
                return str(data.get(k))
    except (OSError, ValueError):
        pass
    return None


def read_battery():
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return None
    m = re.search(r"(\d+)%", out)
    if not m:
        return None
    charging = "charging" in out or "charged" in out
    return {"pct": int(m.group(1)), "charging": charging}


def read_mem():
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                   text=True, timeout=3).stdout.strip())
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    pg = 4096
    m = re.search(r"page size of (\d+)", out)
    if m:
        pg = int(m.group(1))
    vals = {}
    for line in out.splitlines():
        mm = re.match(r"(.+?):\s+(\d+)", line.strip())
        if mm:
            vals[mm.group(1)] = int(mm.group(2))
    used = (vals.get("Pages active", 0) + vals.get("Pages wired down", 0)
            + vals.get("Pages occupied by compressor", 0)) * pg
    return {"used_gb": round(used / 1e9, 1), "total_gb": round(total / 1e9, 1)}


def read_disk():
    try:
        u = shutil.disk_usage("/")
        return {"free_gb": round(u.free / 1e9, 0), "total_gb": round(u.total / 1e9, 0)}
    except Exception:
        return None


WMO = [
    ((0,), "晴"), ((1,), "基本晴"), ((2,), "多云"), ((3,), "阴"),
    ((45, 48), "雾"), ((51, 53, 55, 56, 57), "毛毛雨"), ((61, 63, 65, 66, 67), "雨"),
    ((71, 73, 75, 77, 85, 86), "雪"), ((80, 81, 82), "阵雨"), ((95, 96, 99), "雷阵雨"),
]


def weather_city(lat, lon):
    """城市名：环境变量优先 → 反向地理编码自动识别 → 默认深圳。"""
    city = os.environ.get("JARVIS_CITY", "").strip()
    if city:
        return city.rstrip("市")
    try:
        d = open_json(
            "https://api.bigdatacloud.net/data/reverse-geocode-client"
            "?latitude=" + lat + "&longitude=" + lon + "&localityLanguage=zh",
            timeout=8)
        name = d.get("city") or d.get("locality") or ""
        if name:
            return str(name).rstrip("市")
    except Exception:
        pass
    return "深圳"


def fetch_weather():
    lat = os.environ.get("JARVIS_LAT", "22.5455")    # 默认深圳（参考 kindle 项目）
    lon = os.environ.get("JARVIS_LON", "114.0683")
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + lat + "&longitude=" + lon
           + "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code"
           + "&timezone=Asia%2FShanghai")
    try:
        d = open_json(url, timeout=10)
        cur = d.get("current") or {}
        code = int(cur.get("weather_code") or 0)
        text = "天气"
        for codes, txt in WMO:
            if code in codes:
                text = txt
                break
        return {"ok": True, "city": weather_city(lat, lon),
                "temp": cur.get("temperature_2m"),
                "feels": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"), "text": text}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def fetch_resets():
    try:
        d = open_json("https://codex-resets.com/api/v1/status", timeout=12)
        data = d.get("data") or {}
        latest = (data.get("latest_reset") or {}).get("announced_at")
        stats = data.get("stats") or {}
        return {"ok": True,
                "last": latest or stats.get("last_reset_at"),
                "total": stats.get("total"),
                "days_since": stats.get("days_since_last"),
                "avg_days": stats.get("avg_interval_days")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def fetch_deepseek():
    key = read_cred(["DEEPSEEK_API_KEY"], ["deepseek"])
    # 兜底：kindle-ai-quota-dashboard 的本机密钥文件
    if not key:
        try:
            key = (Path(os.environ.get("HOME", "~")) /
                   "Project/kindle-ai-quota-dashboard/config/deepseek.key").read_text().strip()
        except OSError:
            pass
    if not key:
        return {"ok": False, "error": "NO KEY"}
    try:
        d = open_json("https://api.deepseek.com/user/balance",
                      headers={"Authorization": "Bearer " + key}, timeout=10)
        rows = d.get("balance_infos") or []
        if not rows:
            return {"ok": False, "error": "empty"}
        row = rows[0]
        return {"ok": True, "balance": float(row.get("total_balance") or 0),
                "currency": row.get("currency") or "CNY"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def fetch_glm():
    key = read_cred(["GLM_API_KEY"], ["glm"])
    if not key:
        # 兜底：opencodex 配置里的 zai 密钥（与参考项目一致）
        try:
            cfg = json.loads((HOME / ".opencodex" / "config.json").read_text())
            key = ((cfg.get("providers", {}).get("zai") or {}).get("apiKey") or "")
        except (OSError, ValueError):
            pass
    if not key:
        return {"ok": False, "error": "NO KEY"}
    try:
        d = open_json("https://open.bigmodel.cn/api/monitor/usage/quota/limit",
                      headers={"Authorization": "Bearer " + key}, timeout=10)
        limits = [(x.get("nextResetTime") or 0, x) for x
                  in (((d.get("data") or {}).get("limits")) or [])
                  if x.get("type") == "CREDIT_LIMIT"]
        limits.sort(key=lambda p: p[0])
        wins = [{"usedPct": int(item.get("percentage") or 0),
                 "resetAt": int(item.get("nextResetTime") or 0)}
                for _, item in limits[:3]]
        return {"ok": True, "windows": wins}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def telemetry(interval=60):
    """独立的遥测线程：系统数据 + 外部数据，每 60s 刷新一次。"""
    while True:
        low = {"battery": read_battery(), "mem": read_mem(),
               "disk": read_disk(), "load": list(os.getloadavg())}
        with LOCK:
            STATE["sys"] = low
        resets = {"_wrap": fetch_resets()}
        ws = fetch_weather()
        ds = fetch_deepseek()
        glm = fetch_glm()
        with LOCK:
            STATE["resets"] = resets["_wrap"]
            STATE["weather"] = ws
            STATE["quota"] = {"deepseek": ds, "glm": glm}
        time.sleep(interval)


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
    threading.Thread(target=telemetry, daemon=True).start()
    if "--no-open" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"JARVIS sentinel listening → http://127.0.0.1:{PORT}  (Ctrl+C 退出)")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nsentinel stopped.")


if __name__ == "__main__":
    main()

# J.A.R.V.I.S. Sentinel — AI 编码代理活动监控

贾维斯风格的全息监控台：中央全息核心球 + 三条轨道卫星，实时监控本机 **Codex / Claude Code / ZCode** 的运行状况。哪个工具在终端里干活，对应的卫星就会点亮、加速、向核心发射数据脉冲——就像钢铁侠的贾维斯看着自己的系统一样。

```
jarvis/
├── sentinel.py  # 本地哨兵：进程 + 会话活动检测，SSE 推送并托管页面
└── jarvis.html  # 贾维斯页面：Canvas 2D 手写 3D 渲染，单文件零依赖
```

## ✨ 功能

- 🌐 **全息核心球**：点云 + 经纬线 + 双反向旋转弧环，能量跟随工具活跃度增强（更亮、更快、脉冲更强），可鼠标拖拽旋转
- 🛰️ **三条倾斜轨道 + 卫星**：CODEX / CLAUDE CODE / ZCODE 各一，四级状态：
  - **OFFLINE** — 熄灭休眠（灰色虚线点）
  - **RUNNING** — 进程在跑，青色常亮慢转
  - **ACTIVE** — 正在干活，金色高能加速 + 轨道粒子流 + 数据脉冲飞向核心
- 📊 **状态面板**：每工具状态卡（状态徽章、进程数、CPU%、最近活动、活跃度迷你波形）+ 系统总览（活跃数、总 CPU、核心能量、链路状态）
- 📈 **总神经负载波形**：过去 80 秒三个工具合计活跃度实时折线
- 🌡️ **SYSTEM & QUOTA 遥测卡**（数据源参照 [kindle-ai-quota-dashboard](https://github.com/softmutiny/kindle-ai-quota-dashboard)）：
  - 本机系统：电池电量/充电状态、CPU 负载、内存、磁盘（`pmset`/`vm_stat`/`os.getloadavg`）
  - **Codex 额度重置**：最近一次重置时间、重置次数、平均间隔（codex-resets.com 公共 API，免密钥）
  - **天气**：实时温度与天气描述（Open-Meteo，免密钥；默认深圳，可用 `JARVIS_LAT`/`JARVIS_LON` 改）
  - **DeepSeek / GLM 余额与额度窗口进度条**：GLM 每个额度窗口显示已用百分比进度条（≥70% 金色、≥90% 红色报警）、剩余额度与重置倒计时；Codex（调本机 `codex app-server`）的额度窗口同样尝试采集，未配置则省略。凭证自动读本机（见下文）
- 🚀 **J.A.R.V.I.S. 开机自检动画** + 全屏扫描线特效
- 🔒 **纯本地**：检测数据仅在本机 127.0.0.1 上流转，不向任何外部发送（除免密钥的公共天气/重置接口外）

## 🚀 快速开始

```bash
cd jarvis
python3 sentinel.py          # 启动并自动打开浏览器
python3 sentinel.py --no-open  # 只启动服务不打开浏览器
```

然后访问 `http://127.0.0.1:8766`（可用环境变量 `JARVIS_PORT` 改端口）。零依赖：只用到 Python 3 标准库。

直接用浏览器打开 `jarvis.html` 也能看到页面，但无法收到数据（页面会提示运行 sentinel）。

## 🔍 检测原理

浏览器无法感知终端进程，所以 `sentinel.py` 是本地桥接，每秒采集一次：

1. **进程检测**（`ps aux` + 正则匹配规则，可配置于 `TOOLS` 字典）：
   - Codex：ChatGPT 桌面版内置 / Cursor 扩展 / `codex app-server`，排除混淆项（如 opencodex、CodexBar）
   - Claude Code：Homebrew + node 安装形态
   - ZCode：`zcode-cli` / ZCode.app（Electron）
2. **会话活动检测**（区分"进程在跑"与"真的在干活"）：监控各工具的会话记录文件写入增量
   - Codex：`~/.codex/sessions/**/rollout-*.jsonl`、`session_index.jsonl`
   - Claude Code：`~/.claude/projects/*/*.jsonl`、`history.jsonl`
   - ZCode：`~/.zcode/cli/log/zcode-*.jsonl`（最强信号）、`~/cli/rollout/*.jsonl`
   - 检测到写入 → 活跃度 +0.45（封顶 1），闲置时指数衰减；≥0.08 判定为 ACTIVE

两种信号结合：`claude -p` 一条命令即可看到卫星从熄灭点亮成金色。

## 🧩 扩展其他工具

在 `sentinel.py` 的 `TOOLS` 字典里加一条即可（进程匹配正则 + 信号文件 glob）：

```python
TOOLS = {
    "mytool": {
        "label": "MY TOOL",
        "home": HOME / ".mytool",
        "proc_in": [r"mytool[- ]cli"],
        "proc_ex": [r"something-to-ignore"],
        "signals": [(HOME / ".mytool", "sessions/*.jsonl")],
    },
}
```

页面端在 `jarvis.html` 的 `TOOLS` 数组和 `ORBITS` 字典里加上对应的 key 与轨道参数（半径、倾斜角、相位、标签）即可。

## 💳 额度凭证（可选）

DeepSeek/GLM 余额读取凭证的顺序：

1. 环境变量 `DEEPSEEK_API_KEY` / `GLM_API_KEY`
2. `~/.jarvis/credentials.json`：`{"deepseek": "sk-...", "glm": "..."}`
3. 本机已有的凭证文件（自动兼容）：`kindle-ai-quota-dashboard/config/deepseek.key` 与 `~/.opencodex/config.json` 里的 `providers.zai.apiKey`

凭证只在本机哨兵进程内使用，不会写入仓库或页面。

## 🌐 兼容性

- 进程匹配规则以 macOS 实测为准；Linux 路径（proc 名相近）可直接用，Windows 需自行调整 `ps` 匹配
- Chrome / Edge / Safari / Firefox 近年版本均可

## 📄 License

[MIT](./LICENSE)

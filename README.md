# Agent Monitor —— Agent 订阅余量监控器

Windows 桌面托盘小工具，监控 **ChatGPT（Codex）**、**火山方舟**、**千问** 三个平台 Plan 的使用状态、余量、用量详情。

> 实现方式：三家均走 **官方 CLI 客户端**（无 Cookie 抓取、无逆向接口）：
> - 火山 → `arkcli usage plan / stats / plan-details`（官方余量查询命令）
> - ChatGPT → 解析 Codex CLI 本地 `~/.codex/sessions` 日志（与 codex-ratelimit 同款方案）
> - 千问 → `qianwen usage summary --format json`
>
> CLI 未安装/未登录时，面板会给出明确的安装指引。

## 功能

- 右下角托盘图标实时显示总体剩余百分比
- 右键托盘显示菜单：打开详情 / 悬浮卡片 / 立即刷新 / 设置 / 退出
- 悬浮卡片：贴屏幕右下角，展示三家 5h/周/月限额百分比
  - ChatGPT：5h 窗口 + 周额度
  - 火山方舟：5h 窗口 + 周额度（如有月度也会展示）
  - 千问：仅订阅额度（无 5h）
  - 正常绿色，剩余 < 5% 红色
  - 可鼠标拖动，可点击 ✕ 隐藏（托盘菜单可重新唤出）
- 点击托盘图标打开详情面板：
  - 单平台余量百分比 + 重置倒计时
  - 多窗口余量（ChatGPT 的 5h 窗口 / 周额度分别展示）
  - 今日调用 / Tokens / 费用
  - 按模型分布进度条
  - 近 30 天用量趋势
- 设置面板：CLI 连通性测试、火山 AK/SK 选填（自动登录）、轮询间隔
- 进程单例：重复启动会唤醒已运行实例的窗口，不会驻留多进程
- ChatGPT 余量口径与 Codex 官方 /status 一致（周额度为主指标）

## 运行方式（开发）

```powershell
# 1. 建环境
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 运行
.venv\Scripts\python.exe main.py
```

首次启动会弹出设置窗，按提示填入凭证即可。

## 打包成单个 exe

```powershell
# 确保已安装 pyinstaller
.venv\Scripts\python.exe -m pip install pyinstaller

# 自动打包（单个可执行文件）
.venv\Scripts\python.exe build.py
```

产物在 `dist/AgentMonitor.exe`，约 50~70 MB。直接双击运行即可。

## CLI 前置条件（一次性配置）

### ChatGPT（Codex）

```powershell
npm install -g @openai/codex
codex            # 首次登录 ChatGPT 账号，跑一次任意任务
```

程序直接读 `~/.codex/sessions` 下的用量与限额日志，无需任何凭证。

### 火山方舟

1. 安装：`npm install -g @volcengine/ark-cli@latest`
2. 一次性 SSO 登录：`arkcli auth login volc-sso`（会打开浏览器）
   - 无浏览器/远程环境：`arkcli auth login volc-sso --no-browser` 拿到 URL，
     任意设备打开授权后复制授权码，再 `arkcli auth login --no-browser --code <授权码>`
3. 登录态存于 `~/.arkcli/`，程序自动复用；AK/SK 可选（设置里填写即可）

> ⚠️ 企业安全软件（如 ESET）可能拦截新构建的未签名 exe。若 exe 启动后秒退，
> 请在安全软件中将 agent-monitor 目录加入白名单，或用 `python main.py` 从源码运行。

> 💡 卡片右上角时间戳精确到秒（HH:MM:SS），可直接确认自动刷新是否在工作；
> `monitor.log` 中的 `[ui] card applied` 行是 UI 刷新的实证记录。

```powershell
npm install -g @volcengine/ark-cli@latest
arkcli login --ak <你的AK> --sk <你的SK>     # 或 arkcli auth login volc-sso
```

AK/SK 也可以填在程序设置里，程序会自动执行登录；留空则复用 CLI 已有登录态。

### 千问

```powershell
npm install -g @qianwenai/qianwen-cli
qianwen auth login
```

程序通过 `qianwen usage summary --format json` 读取 Token Plan 额度。

## 项目结构

```
agent-monitor/
├── main.py                 # 入口
├── requirements.txt        # 依赖
├── build.py                # 打包脚本
├── src/
│   ├── models.py           # 数据模型
│   ├── config.py           # 配置加密存储
│   ├── scheduler.py        # 定时轮询
│   ├── icon_render.py      # 托盘图标绘制
│   ├── detail_window.py    # 详情面板
│   ├── settings_dialog.py  # 设置面板
│   ├── platforms/          # 三平台适配器
│   │   ├── base.py
│   │   ├── chatgpt.py
│   │   ├── volcengine.py
│   │   └── qwen.py
│   └── utils/              # 加密、日志
└── assets/                 # 图标、测试截图
```

## 测试

```powershell
.venv\Scripts\python.exe cli_mode_test.py   # CLI 适配器解析单测（fixture，不联网）
.venv\Scripts\python.exe smoke_test.py      # 模块 / 加密 / 配置
.venv\Scripts\python.exe ui_render_test.py  # 生成 assets/ui_*.png 预览
```

## 已知限制

- 火山 `arkcli usage plan` 的 JSON 输出结构官方未严格固定文档化，解析器做了多形态兼容；若 CLI 升级后仍解析失败，会在详情面板显示原始错误。
- ChatGPT 依赖 Codex CLI 产生的本地日志——CLI 版本更新若改动 `rollout-*.jsonl` 格式，解析同样做了宽松兼容。
- 千问 Token Plan 的 resetDate 来自订阅实例 EndTime（到期时间），非月度重置时间。
- 程序需要能找到 `arkcli` / `qianwen` 在 PATH 中（或 npm 全局 bin 目录）。

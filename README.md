# 闲鱼商品监控 + 邮件通知

定时抓取**闲鱼**（[goofish.com](https://www.goofish.com/)）网页版上指定关键词（默认 `像章`）的最新发布商品，去重后通过 QQ 邮箱 SMTP 把新增商品以 HTML 邮件的形式推送到你的收件箱。

## 功能特性

- 通过 Playwright 真实浏览器抓取，支持登录态复用，避滑块更稳。
- 双路径解析：优先拦截闲鱼 mtop 接口 JSON；DOM 解析作为兜底。
- 基于 SQLite 的 `item_id` 持久化去重，**只推送增量**。
- HTML 邮件带缩略图、价格、卖家、地区、详情链接，一目了然。
- `login` / `once` / `run` 三个子命令满足首次登录、调试、定时运行场景。

### 重要说明

- **必须先完成 `python main.py login`**：`data/storage_state.json` 中若没有任何 cookie（例如空模板文件），程序会视为未登录并拒绝抓取；未登录时搜索接口常返回「被挤爆」类错误，列表也为空。
- 抓取使用 **1920×1080** 视口并在打开搜索页后等待 **networkidle**，确保闲鱼 SPA 能发起 `h5api.m.goofish.com` 上的 PC 搜索请求。
- **`once` / `run` 每轮抓取前**会先探测登录态（能否正常走搜索接口）；若无效或文件无 cookie，会**跳过抓取**并（在冷却时间内最多发一次）向 `MAIL_TO` 发送**「需重新登录」**提醒邮件，避免因 cookie 过期长期静默抓空。

环境变量 **`LOGIN_REMINDER_COOLDOWN_SECONDS`**（默认 21600，即 6 小时）控制同一类提醒的最小间隔；设为 `0` 则每次失败都发邮件。

**`EMAIL_MAX_ITEMS`**（默认 `30`）：单封商品通知邮件中最多包含的新增条数；超出部分仍写入数据库并标记为「未通知」，后续 `once`/`run` 会继续推送（避免单封邮件过长）。

## 项目结构

```
xianyu/
├── main.py                 # CLI 入口
├── config.py               # 读取 .env
├── requirements.txt
├── .env.example            # 环境变量模板
├── src/
│   ├── auth.py             # 扫码登录 + 保存 storage_state
│   ├── crawler.py          # Playwright 抓取
│   ├── session_probe.py    # 抓取前探测登录态是否可用
│   ├── login_reminder.py   # 登录提醒邮件冷却
│   ├── storage.py          # SQLite 去重
│   ├── notifier.py         # SMTP 发送邮件
│   └── models.py
├── data/                   # 运行时生成（gitignore）
│   ├── storage_state.json
│   └── seen_items.db
└── logs/                   # 日志（gitignore）
```

## 环境要求

- Windows / macOS / Linux
- Python 3.10+
- 网络可访问 `goofish.com` 与 `smtp.qq.com`
- 一个可登录的闲鱼账号（手机淘宝扫码即可）
- 一个开启了 SMTP 的 QQ 邮箱

## 安装

```powershell
# 1. 创建虚拟环境（可选但推荐）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 安装依赖
pip install -r requirements.txt

# 3. 安装 Playwright 浏览器（首次运行必须）
python -m playwright install chromium
```

## 配置

复制 `.env.example` 为 `.env` 并按你的实际情况修改：

```powershell
copy .env.example .env
notepad .env
```

**与 `.env.example` 保持同步**：仓库里更新 `.env.example` 后，本地 `.env` 应合并新键并**保留你已有密钥**。可手动执行：

```powershell
python scripts/sync_env_from_example.py
```

使用 `--dry-run` 可先预览合并结果不写盘。若使用 Cursor 且已启用本项目 [`.cursor/hooks.json`](.cursor/hooks.json)，Agent 保存 `.env.example` 后会尝试自动运行上述脚本（钩子依赖本机 `python` 在 PATH 中）。

`.env` 中需要重点关注的几项：


| 配置项                       | 说明                               |
| ------------------------- | -------------------------------- |
| `KEYWORD`                 | 搜索关键词，默认 `像章`                    |
| `INTERVAL_SECONDS`        | 轮询间隔，**建议不低于 600（10 分钟）**        |
| `PAGE_LIMIT`              | 每轮最多抓取多少条商品                      |
| `EMAIL_MAX_ITEMS`         | 单封商品邮件最多展示几条新增，默认 `30`，超出部分下轮再发 |
| `HEADLESS`                | `true` 后台运行；调试时设为 `false` 看浏览器界面 |
| `NOTIFY_ON_FIRST_RUN`     | `false`（默认）首次运行只入库不发邮件，避免一封邮件几十条 |
| `LOGIN_REMINDER_COOLDOWN_SECONDS` | 登录失效提醒邮件冷却（秒），默认 `21600` |
| `SMTP_HOST` / `SMTP_PORT` | QQ 邮箱默认 `smtp.qq.com:465`        |
| `SMTP_USER`               | 你的 QQ 邮箱地址，例如 `123456@qq.com`    |
| `SMTP_PASS`               | **QQ 邮箱授权码**（不是登录密码！见下文）         |
| `MAIL_FROM`               | 发件人，一般和 `SMTP_USER` 相同           |
| `MAIL_TO`                 | 收件人邮箱，多个用英文逗号分隔                  |


### 如何获取 QQ 邮箱授权码

1. 用浏览器登录 [QQ 邮箱网页版](https://mail.qq.com/)。
2. 顶部点击「设置 → 账号」。
3. 翻到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」一栏。
4. 开启「IMAP/SMTP 服务」（按提示发短信验证）。
5. 开启成功后会显示一串 16 位的授权码，复制粘贴到 `.env` 的 `SMTP_PASS`。

> 注意：授权码只显示一次，请妥善保存；它就是 SMTP 登录密码。

## 使用流程

### 1) 首次扫码登录闲鱼

```powershell
python main.py login
```

会弹出一个 Chromium 窗口并打开闲鱼首页，请在该窗口里：

1. 点击右上角「请先登录」。
2. 用手机淘宝/闲鱼 APP 扫码确认登录。
3. **确认保存登录态**（二选一）：
   - **在系统自带终端**（PowerShell / CMD）里运行 `login` 时：回到该窗口，确认网页已登录后 **按 Enter**，才会写入 `data/storage_state.json` 并关闭浏览器。
   - **在 IDE 后台等非交互环境**里运行时：没有可用的「按 Enter」；请在浏览器登录完成后，在项目中手动新建空文件 **`data/login_ready.flag`**（内容可为空），程序在 **300 秒内**检测到后会保存并删除该文件。

若未确认就结束，会出现 `EOFError` 类错误——请改用上述任一方式。

如果遇到滑块验证，**手动滑过即可**（用真人手势效果最好）。

### 2) 跑一次抓取（调试 / 验证邮件）

```powershell
python main.py once
```

第一次运行（数据库为空且 `NOTIFY_ON_FIRST_RUN=false`）只会把当前结果入库，不发邮件。
再次运行时若 `crawler` 抓到新商品就会发邮件，可以借此验证整个链路。

> 想立刻收一封测试邮件？可以临时把 `.env` 里的 `NOTIFY_ON_FIRST_RUN=true`，然后**先删除 `data/seen_items.db`**，再 `python main.py once`。

### 3) 长期定时运行

```powershell
python main.py run
```

按 `INTERVAL_SECONDS` 周期轮询，按 `Ctrl+C` 退出。
建议放到一个常驻终端 / `nssm` / `pm2` / Linux `systemd` / `screen` / `tmux` 里跑。

#### 在 Windows 上做开机自启的简易方案

```powershell
# 编辑一个启动脚本 run.cmd
@echo off
cd /d C:\projcet\xianyu
call .\.venv\Scripts\activate.bat
python main.py run >> logs\run.out 2>&1
```

把它放到「任务计划程序」的开机触发任务里即可。

## 故障排查

### Q1：`python main.py login` 没弹出浏览器

- 确认已执行 `python -m playwright install chromium`。
- 临时把 `HEADLESS=false`，并确保你不是在远程无桌面的机器上跑。

### Q2：登录后抓不到商品 / 一直 0 条

- 大概率是登录态过期或被风控了，重新执行 `python main.py login`。
- 把 `HEADLESS=false` 看一眼浏览器里发生了什么；如果是滑块，手动滑过。
- 检查 `logs/app.log` 里是否有 `检测到风控页` 的日志。

### Q3：邮件发不出去

- QQ 邮箱必须使用「授权码」而不是 QQ 密码登录 SMTP。
- 端口 `465` 用 SSL，端口 `587` 用 STARTTLS，本程序根据端口自动选择。
- 网络环境是否能访问 `smtp.qq.com:465`（公司/学校网络可能封 SMTP 端口）。

### Q4：刚启动收到了一大堆历史商品的邮件

- 把 `NOTIFY_ON_FIRST_RUN` 设为 `false` 并删除 `data/seen_items.db` 重新启动。
- 之后只会推送增量。

### Q5：闲鱼页面 DOM 改了，抓不到数据

- 程序会优先解析 mtop JSON 接口，DOM 失效一般不影响主路径。
- 真的失效时把 `HEADLESS=false` 跑 `once`，看一下 `logs/app.log` 中的提示，必要时更新 `src/crawler.py` 里的选择器或字段映射。

## 注意事项

- **请合理设置 `INTERVAL_SECONDS`**（≥10 分钟），高频抓取很容易触发风控甚至封号。
- 本工具仅供个人学习/自用监控，请勿用于商业爬取或绕过平台规则。
- `data/storage_state.json` 含登录态，**不要**提交到 Git 或分享给他人。


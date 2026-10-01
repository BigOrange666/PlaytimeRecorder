# Playtime Recorder

一个 MCDR 插件，做两件事：

1. **记录游玩时长**：监听玩家进入/退出/AFK，统计本次与累计游玩、AFK、活跃时长，写入日志与数据文件
2. **接入 QQ**：通过 NapCat（OneBot v11）把记录播报到 QQ 群，群里发 `#游玩历史` 就能查

> 这个版本把原来两个独立插件（`PlaytimeRecorder` + `PlaytimeQQBridge`）**合并成了一个**：
> 记录侧和查询侧共用同一套日志格式与解析器，不再有"日志写出来但解析器读不懂"的错位风险，
> 也不再需要在两个插件之间同步配置。

- QQ 侧：**NapCat**，协议 **OneBot v11**（WebSocket）
- 连接方向：**MCDR → NapCat**。Minecraft 侧没有公网 IP，插件主动连出去，
  不需要开任何入站端口、不需要内网穿透
- 依赖：**零第三方库**（WebSocket 客户端是手写的 RFC 6455，只用标准库）

```
玩家进出/AFK ──► qqbridge/recorder.py ──► logs/playtime_recorder/playtime.log
                     │                        │
                     │                        ▼
                     │             qqbridge/records.py（同一个解析器）
                     ▼                        │
              主动播报（可配）                  ▼
                     └──► QQNotifier ──(ws)──► NapCat ──► QQ 群
                                  ▲
                    群里发 #游玩历史 ┘
```

## 安装

### 方式一：`.mcdr` 单文件（推荐）

从 [Releases](https://github.com/BigOrange666/PlaytimeRecorder/releases) 下载
`playtime_recorder-v2.0.2.mcdr`，丢进 MCDR 的 `plugins/` 目录，重启 MCDR。

> MCDR **不会解压** `.mcdr`，而是把归档本身加进 `sys.path`，用 zipimport 按
> `entrypoint` 加载包。所以包结构必须严格符合下面的约定，`build.py` 会逐条校验。

### 方式二：源码

把仓库目录整个拷进 `plugins/`，**目录名必须是 `playtime_recorder`**（= 插件 id）：

```
<MCDR根目录>/plugins/playtime_recorder/     ← 目录名 = 插件 id
├─ mcdreforged.plugin.json
├─ playtime_recorder/                       ← 插件包（entrypoint 指向它）
│   ├─ __init__.py                          插件主体 + MCDR 事件钩子
│   ├─ qqbridge/                            OneBot 客户端、记录核心与解析工具
│   │   ├─ __init__.py
│   │   ├─ recorder.py                      游玩记录核心
│   │   ├─ records.py
│   │   ├─ ws_client.py
│   │   ├─ onebot.py
│   │   └─ logging_util.py
│   └─ lang/zh_cn.yml
├─ build.py / run_all_tests.py / tests/ / tools/   仓库自用，运行插件不需要
```

> ⚠️ 手动拷源码时**目录名和包目录名都不能改**：MCDR 会把插件目录加进 `sys.path`，
> 再用 `entrypoint`（`playtime_recorder`）当模块名 import。大小写敏感的系统上
> 目录名不一致会报 `No module named 'playtime_recorder'`。
> **用 `.mcdr` 安装则完全不涉及目录名**——按 `entrypoint` 从归档里加载包。

> MCDR 打包插件的三条硬约束（都踩过坑，`build.py` 会逐条校验）：
> 1. **`entrypoint` 必须等于插件 id 或以 `<id>.` 开头**，否则报
>    `Invalid entry point 'xx' for plugin id 'yy'`
> 2. **`entrypoint` 对应的包必须是插件归档内的子目录**（`.mcdr` 里要有
>    `playtime_recorder/__init__.py`），把 `__init__.py` 放归档根部不行，
>    会报 `No module named 'playtime_recorder'`
> 3. **包目录里不能有其它散落的顶层 `.py`**，否则报
>    `Packed plugin cannot contain other module`

## NapCat 侧配置

1. NapCat WebUI → 网络配置 → 添加 **WebSocket 服务端**（反向 WS 服务端）
   主机 `0.0.0.0`，端口 `12538`，Token 可留空
2. 安全组 + 防火墙放行 TCP `12538`
3. 域名解析到 NapCat 机器，且不要经过不支持 WebSocket 的 CDN

> ⚠️ 必须是「WebSocket **服务端**」。选成「WebSocket 客户端」方向就反了——
> 那要求 NapCat 主动连 MCDR，而 Minecraft 侧没有公网 IP。

## 配置

首次加载生成 `config/playtime_recorder/config.json`（会自动兼容读取旧版的
`config/qq_bridge/config.json`）：

```jsonc
{
  "enabled": true,
  "recorder": {
    "data_dir": "config/playtime_recorder",   // 累计数据存放目录
    "log_dir": "logs/playtime_recorder",      // 日志存放目录
    "include_afk": true                       // 查询结果里是否显示 AFK 行
  },
  "connection": {
    "ws_url": "ws://997879.xyz:12538",        // NapCat 的 WebSocket 服务端地址
    "access_token": "",                       // 与 NapCat 里的 Token 保持一致
    "access_token_in_query": false,
    "verify_ssl": true,
    "reconnect_min_seconds": 3,
    "reconnect_max_seconds": 60,
    "action_timeout_seconds": 15
  },
  "access": {
    "allow_private": true,
    "allow_group": true,
    "group_whitelist": [],                    // 群号白名单，空 = 所有群
    "private_whitelist": [],                  // QQ 白名单，空 = 所有人
    "group_require_at": true                  // 群里是否必须 @机器人
  },
  "commands": {
    "history_aliases": ["#游玩历史", "#历史", "#playtime"],
    "help_aliases": ["#游玩帮助", "#帮助"],
    "status_aliases": ["#游玩状态", "#状态"],
    "stats_aliases": ["#游玩统计", "#排行"]
  },
  "history": {
    "title": "游玩历史",
    "max_day_span": 90,
    "max_lines_per_query": 200,
    "include_absent_totals": true
  },
  "notify": {
    "on_join": false,                         // 玩家进入时主动播报
    "on_leave": false,                        // 玩家退出时主动播报
    "on_afk": false,                          // 结束 AFK 时播报
    "session_min_seconds": 0,                 // 少于这个时长的会话不播报
    "targets": [],                            // 播报到哪些群，空 = 用群白名单
    "join_text": "[游玩] {player} 进入了服务器 (累计 {total})",
    "leave_text": "[游玩] {player} 离开了服务器 | 本次 {session} | AFK {afk} | 活跃 {active} | 累计 {total}",
    "afk_text": "[游玩] {player} 结束 AFK，时长 {afk}",
    "send_online_notice": false,
    "online_notice_text": "[MCDR] 游玩记录插件已上线，发送 #游玩历史 可查询游玩记录"
  },
  "message": { "chunk_size": 1200, "reply_chunk_size": 1000 },
  "debug": false
}
```

改完 `config.json` 后在控制台执行 `!!qqbridge reload`；如果改的是**代码**，请重启 MCDR。

## QQ 指令

| 指令 | 效果 |
| --- | --- |
| `#游玩历史` | **默认：上一天 00:00 → 现在** |
| `#游玩历史 今天` / `昨天` | 当天 / 昨天整天 |
| `#游玩历史 近7天` / `3天` | 最近 N 天（上限 90 天） |
| `#游玩历史 2025-06-01` / `06-01` | 指定某一天 |
| `#游玩历史 上限50` | 最多显示 50 条 |
| `#游玩统计` | 所有人累计游玩排行 |
| `#游玩统计 Steve` | 某个玩家的累计时长与 AFK |
| `#游玩帮助` / `#游玩状态` | 帮助 / 连接与记录状态 |

群聊默认需要 @机器人。输出示例：

```
===== 游玩历史 =====
范围: 2025-06-09 00:00 至 06-10 15:30:00
【Steve】
  06-09 00:30 进入服务器
  06-09 01:00 开始 AFK
  06-09 01:15 结束 AFK | 时长 15分钟
  06-09 02:30 离开服务器 | 本次 2小时 | AFK 15分钟 | 活跃 1小时45分钟
【Alex】
  06-09 23:50 进入服务器
  06-10 08:00 离开服务器 | 本次 8小时10分钟 | AFK 1小时 | 活跃 7小时10分钟

----- 统计 -----
记录 6 条，玩家 2 人
  Alex: 上线 1 次，合计 8小时10分钟
  Steve: 上线 1 次，合计 2小时
```

## MCDR 控制台指令

| 指令 | 作用 |
| --- | --- |
| `!!playtime` / `!!playtime status` | 在线玩家、记录人数、累计时长、日志与数据文件位置 |
| `!!playtime top` / `!!playtime top Steve` | 累计排行 / 指定玩家 |
| `!!playtime save` | 立即保存累计数据 |
| `!!qqbridge` / `!!qqbridge status` | QQ 连接状态、事件计数、重连次数 |
| `!!qqbridge reload` | 重读配置并重建 QQ 连接 |
| `!!qqbridge test` | 往配置的群发一条测试消息 |

## 数据文件

| 文件 | 内容 |
| --- | --- |
| `logs/playtime_recorder/playtime.log` | 逐条记录（进出、AFK、时长明细），查询的数据源 |
| `config/playtime_recorder/playtime_data.json` | 累计游玩 / 累计 AFK，用于"未上线玩家的历史累计" |

日志格式（解析器与写入方共用同一套约定，改格式请同时更新 `qqbridge/records.py` 的注释）：

```
[2025-06-09 00:30:00] 玩家 Steve 进入服务器 (时间: 2025-06-09 00:30:00)
[2025-06-09 01:00:00] 玩家 Steve 开始 AFK
[2025-06-09 01:15:00] 玩家 Steve 结束 AFK，本次 AFK: 15分钟
[2025-06-09 02:30:00] 玩家 Steve 退出服务器 | 本次游玩: 2小时 | AFK: 15分钟 | 活跃: 1小时45分钟 | 累计游玩: 3小时 | 累计AFK: 15分钟
```

## 开发

```bash
# 跑全部离线测试（不需要 MCDR / QQ / 联网）
python -B run_all_tests.py

# 或者用 unittest（CI 用的是这个）
python -m unittest discover -s tests -v

# 单独跑
python tests/test_records.py       # 解析 / 时间范围 / 统计 / 格式化
python tests/test_ws_client.py     # WebSocket 握手、掩码、超长帧、分段、重连
python tests/test_plugin_e2e.py    # 端到端：记录器 -> 日志 -> 解析 -> QQ 回复

# 打包
python build.py                    # 产物在 dist/*.mcdr
```

`test_plugin_e2e.py` 会把机器人**实际会发到 QQ 的整段回复**打印出来，可以直接当格式预览。

## 发布

打 tag 即触发 Release（`.github/workflows/release.yml`）：

```bash
# 1. 改 mcdreforged.plugin.json 里的 version，例如 2.0.3
# 2. 同步更新 RELEASE_NOTES.md
git commit -am "chore: release 2.0.3"
git push

# 3. 打 tag（必须 = v + 元数据版本）并推送
git tag v2.0.3
git push origin v2.0.3
```

workflow 会：校验 tag 与元数据版本一致 → `python build.py` → `python -m unittest discover -s tests -v`
→ 创建 Release 并附上 `.mcdr` 与 `SHA256SUMS`。

## 设计说明

- **记录与查询同源**：日志的写入方（`qqbridge/recorder.py`）和解析方
  （`qqbridge/records.py`）在同一个插件里，格式约定集中在一处；端到端测试会真写一条
  记录再读回来验证，防止两边漂移。
- **零第三方依赖**：MCDR 的嵌入式 Python 通常没装 `websockets` / `websocket-client`，
  所以 `qqbridge/ws_client.py` 用标准库手写了 RFC 6455 客户端（掩码、126/127 扩展长度、
  分片重组、ping/pong、关闭握手）。
- **导入用包相对导入**：MCDR 把 `.mcdr`（zip）加进 `sys.path` 后用 zipimport 按
  `entrypoint` 加载包，所以入口里用 `importlib.import_module('.qqbridge.records', package=__package__)`
  最稳；`os.path` 探测在 zip 内部会全部失效。引导器保留了"按路径显式装载"和
  "`sys.path` 绝对导入"两级兜底，用于非 MCDR 环境（见 `__init__.py` 的 `_bootstrap_imports`）。
- **不阻塞主线程**：查询与发送在工作线程池里执行；所有 QQ 发送先入队，由独立线程串行发出；
  长回复按 `chunk_size` 自动分段并带 `(1/2)` 序号。
- **断线自愈**：指数退避重连（3s → 60s），NapCat 重启或网络抖动不用手工干预。

# Playtime Recorder v2.0.2

MCDR 插件：记录玩家游玩/AFK 时长，并通过 QQ（NapCat / OneBot v11）查询与播报。

## 本版内容

- 适配 MCDR 的 `.mcdr` 打包插件加载方式（zipimport，不解压）
- 修正入口结构：`entrypoint` = `playtime_recorder`，插件包放在同名子目录下
- 版本号统一从 `mcdreforged.plugin.json` 读取，不再需要改两处
- 打包时自动校验 MCDR 的元数据规则与文件结构，避免"本地正常、装进服务器才报错"

## 功能

- QQ 指令：`#游玩历史`（默认"上一天 00:00 到现在"）、`#游玩统计`、`#游玩帮助`、`#游玩状态`
- 控制台指令：`!!playtime [status|top|save]`、`!!qqbridge [status|reload|test]`
- 支持玩家进出 / 结束 AFK 时主动播报到群（`notify` 配置，默认关闭）
- 记录玩家进出、AFK、本次与累计游玩时长，写入日志与数据文件
- 零第三方依赖：WebSocket 客户端为纯标准库实现（RFC 6455）

## 安装

1. 下载下方的 `.mcdr` 文件
2. 放进 MCDR 的 `plugins/` 目录
3. 重启 MCDR

首次启动会生成 `config/playtime_recorder/config.json`，填写 NapCat 地址与 Token。

## 前置条件

- MCDR >= 2.0
- NapCat 侧已开启 **WebSocket 服务端**（反向 WS），并放行对应端口
- Minecraft 侧无需公网 IP：插件作为 WebSocket 客户端主动连出去

## 校验

```bash
sha256sum -c SHA256SUMS
```

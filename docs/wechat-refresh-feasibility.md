# 微信 `/刷新`：配置边界内的可行性复核

调查日期：2026-09-05。范围为当前安装版本的只读原生实现，未改生产机器人配置、未发送 API 请求、未启动第二个消息消费者。此文记录可行性，不表示命令已实现。

## 目标与允许范围

- 用户在微信发送精确的 `/刷新`，只更新该用户的 iLink 上下文，不派发模型、不创建 ZCode 对话，也不重发旧通知。
- 可以修改用户部署后的 ClawBot 配置及增加外部脚本；不修改 ZCode 安装包，不破坏原有聊天、命令和自动化功能。
- 不竞争原生的消息消费游标，不解密或迁移原生机器人凭据，不通过全局证书或应用注入拦截通信。

## 已核验的接口

原生证据位于动态发现的 `resources/app.asar` 中 `out/host/index.js` 及其直接导入的共享模块。内部符号是版本相关实现，不是稳定公开 API。

1. `parseBotCommand` 没有 `/刷新`、`/refresh`；`allowedCommands` 是固定键集合，不接受任意新命令或别名。`/status`、`/reconnect` 不持久化微信上下文，不能替代刷新。
2. `readWeixinContextToken` 从入站消息读取 token，`createWeixinBotProvider.send` 使用当前 actor 携带的 `providerContextToken`。轮询持久化的是 `weixinGetUpdatesBuf`，bot-state schema 没有可写的历史 context token 字段。
3. `getWeixinApiBaseUrl` 使用内置 iLink 基址。没有确认 Weixin bot 配置支持 `baseUrl`、`apiEndpoint`、消息中间件或专用代理入口；任意添加这些键不能证明请求会走外部脚本。
4. 通用 `provider: webhook` 的确存在，支持 `webhookUrl`、`webhookSecretRef`、`webhookAuthHeaderName`，但它是独立通道。`normalizeBotConfig` 会移除 Weixin bot 的 `webhookUrl`，不能直接把 webhook 挂在原生 Weixin polling 上。
5. `createWebhookBotProvider.send` 向配置 URL POST `zcode.bot.message`；`parseCallback`、`botsService.handleProviderCallback` 只证明内部解析/处理能力。限定检查未确认可由外部脚本调用的入站 HTTP listener、路由和完整鉴权契约。
6. 自动化的 `botDeliveryTarget` schema 只接受 `feishu`、`lark`、`weixin`，不接受 `webhook`。通用 provider 支持 webhook，不代表原生 cron 投递也支持它。

## 方案比较

| 方案 | 核验结果 | 对当前功能的影响 |
| --- | --- | --- |
| 在现有 Weixin bot 配置里增加命令别名、token 或代理地址 | 未找到生效入口，不能形成刷新闭环 | 不应把未识别配置键当成功能上线 |
| 为同一个 Weixin bot 增加 webhook | 字段会被归一化移除；cron 目标也不接受 webhook | 不能透明接管原有收发 |
| 外置 ClawBot 作为唯一 iLink 消费者，桥内处理 `/刷新` | 原理上可在桥内更新 token；ZCode 双向适配尚缺受控入口和验收 | 停用原生 polling 后，必须先证明普通聊天、命令、通知均能由桥接替 |

第三种方案不需要向原生状态注入 token：最终 `sendmessage` 可由桥负责。但是，仅停用原生 polling、启动外部消费者会中断原有微信入口；这不满足“不影响原有功能”。也不能同时保留两个消费者抢消息。

## 建议的下一步验证

若继续该方向，应单独做隔离的适配器验证，先解决：

- 外部如何通过已配对、带鉴权的受控接口调用机器人入站处理，并保留身份与工作区授权。
- 原生摘要完成后如何只投递一次到外置桥；不能把不受支持的 webhook 值写进自动化 schema。
- ClawBot 独立授权、最小 token 存储、单消费者切换、回滚和普通聊天/命令回归。
- `/刷新` 不产生模型调用、新会话或补发；其他消息顺序和去重不变。

当前结论：**仅修改现有 ClawBot 配置还不能交付 `/刷新`；独立微信网关与 ZCode 适配器是待验证方向，不是已部署功能。** 通知修复与这项调查分开验收。

# 原生能力调查与外置接入边界

调查日期：2026-09-05。以下来自已安装 ZCode 的只读实现核查，属于版本相关内部协议，不是承诺稳定的公开 API。安装位置由 Agent 发现，本文不记录真实路径、账号或凭据。

## 微信上下文刷新

原版代码包相对位置：`resources/app.asar` 中的 `out/host/index.js`。

- `readWeixinContextToken` 从入站消息读取 `context_token`。
- `getWeixinUpdates` 调用 iLink 的 `getupdates`，使用 `get_updates_buf` 维护消费游标。
- `createWeixinBotProvider` 发送消息时把当前入站 actor 携带的 `providerContextToken` 交给 `sendmessage`。
- `parseBotCommand` 中暂未发现 `/刷新`、`/refresh` 或可供外部脚本调用的上下文设置接口。

已核验：`/status` 只查询任务状态，不派发模型任务，但不持久化新的 token，不能当作静默刷新。
`providerContextToken` 仅用于当前入站 actor、typing 和出站消息；bot context 的
`readContext` / `writeContext` 不保存它，更新轮询游标的函数也只保存
`weixinGetUpdatesBuf`。自动化的 `resolveAutomationBotDeliveryTarget` 仅选择
provider/bot/user/chatType，没有历史 token 读取链。

结论：尚不能在当前外置边界内交付 `/刷新`。不能启动第二个消费者争抢同一 bot
的 `getupdates`，不能先触发对话再删除记录冒充“未触发”。原版状态命令仍可使用，
但不能宣传为修复通知上下文过期的功能。

## 计划审批

原版存在 `pendingElicitation` 和 `interaction: "plan_approval"`。
`submitPendingElicitation` 检查 actor、当前任务与处理状态，再经
`respondElicitation` / `sendConversationCommandV4` 发送 `resolveInteraction`，
关联 `interactionId`、`requestId` 和 `answer.action`。

这证明原生 Agent 有审批执行能力，但不等于外置通知器已有调用权限。
Web Remote Control 使用配对后的设备标识、鉴权材料、HMAC 和 `rpc-frame`
建立 workspace bridge，service channel 才能到达 `ZCodeAgent` / `ZCodeSession`。
目前没有确认独立的公开 HTTP/IPC 审批端点。不得通过读取其他会话的私密凭据、
模拟点击或注入运行中应用内存来绕开配对鉴权。

第三版还必须证明微信消息能在普通对话前被精确识别、鉴权，并唯一关联到当前
未过期的待审批计划。只有“有一个内部 resolveInteraction 方法”不足以发布可用的微信审批功能。

补充核验：`/approve` 处理的是 `pendingPermissionOptions`，不是计划审批，不能混用。
真正 `plan_approval` 的选项值为 `approve`、显示名为“批准”。机器人自身 watcher 已经
保存 `pendingElicitation` 时，同一 actor 可以发送 `批准`、`/回答 批准` 或
`/回答 approve` 进入原生审批链；没有确认“同意”别名，也没有时间 TTL。
`/task` 可以选择桌面任务，但不会导入桌面已有的 pending interaction，因此不能
仅靠切换任务批准桌面会话中的已有计划。此原生流程不满足本项目第三版的全会话、
唯一当前计划、有效期与微信“同意”要求，不能宣传第三版已经完成。

第三版当前缺口是受控的入站控制扩展点和桌面会话审批入口；需要新的原生扩展能力，
或单独配对并明确授权的独立控制链路。通知器不会读取配对凭据或绕过原生鉴权来填补缺口。

## 通知历史与自动化定义不能混同

### 原生投递绑定核验修正

`dispatchCronRun` 根据工作区路径计算原生 key，调用 `watchCronRunBotDelivery`，后者通过 `AutomationRepo.getBotDeliveryTarget(automationId, workspaceKey)` 精确查询自动化。只有目标存在才调用 `botsService.watchAutomationRun`；查询返回空时不会订阅，也不一定留下发送失败日志。

无 workspaceIdentity 的本地工作区使用完整路径作为 key。把 key 写成末级目录名会造成任务已经执行、投递查询却返回空；把 `bot_delivery_target` 留空也会阻断订阅。旧设计中“NULL 是已验证契约”的结论有误，已纠正：新建自动化写入动态发现的四字段路由 JSON，不写入凭据或 token，不重放旧任务。

微信路由配置正确、原生摘要成功、iLink 接受发送、用户手机实际收到是不同验证层次；前两项不能代替后两项。

- `tasks.cron_automation_id` 关联 `automations.automation_id`；任务查询支持按自动化归属且过滤 `deleted=0`。
- `TaskIndex.updateTaskState` 的软删除会设置任务的 `deleted` 并清理任务组引用，不删除会话文件。
- `AutomationRepo.delete` 是自动化定义硬删除，并不负责同时删除 `automation_runs`，不适合用来实现侧边栏通知历史整理。
- `automations` 没有任务意义上的 `deleted` 字段。暂停自动化也不等于清理任务历史。

外置历史整理只能沿任务软删除语义实施；修改前验证精确归属、终态、任务组引用和 schema。不能仅凭 `[agent]` 标题或 ID 前缀删数据。

# ZCode / Codex 任务停顿微信通知器

公开仓库：<https://github.com/M1gr4ine/zcode-task-notifier>

当前代码版本 `0.2.1`：修正原生自动化的工作区标识与微信投递目标绑定。摘要任务完成不代表微信已送达，必须另行验证发送链路。路线图中的 `/刷新` 和微信远程计划审批仍未实现，具体限制见下文。第一版发布标签不被覆盖。

把下面这段话复制给 ZCode Agent：

```text
请从 https://github.com/M1gr4ine/zcode-task-notifier 下载并注册 skills/zcode-task-notifier-install/SKILL.md；下载、注册完成后等待用户手动输入 /zcode-task-notifier-install，不要自动触发或隐式调用安装技能。技能先说明如何在 ZCode 的机器人管理中启用微信机器人；若缺少或未激活则停下教学，等我回复“已启用”后再复检；然后单独询问我是否同时监控 Codex，我选择否时只配置 ZCode，最后由技能调用外部 scripts/install.ps1 和 doctor。不要读取、打印、解密或上传任何机器人凭据正文；只读检查如需确认加密引用前缀，不得持久化其值。
```

## 用途和边界

本项目在 ZCode 外部运行，读取本机 ZCode 工作区和可选 Codex 的任务停顿事件，并通过用户已经启用的 ZCode 微信机器人发送摘要。它不修改 ZCode 安装文件、应用包或业务代码；卸载只移除本产品自己的计划任务及选定的产品数据，保留其他产品数据。

安装技能是显式入口：注册 `skills/zcode-task-notifier-install/SKILL.md` 后，必须由用户输入 `/zcode-task-notifier-install` 才会触发。技能只编排引导、确认、Codex 选择、安装器和 `doctor` 调用，调用结束即退出；常驻监控由外部 Windows 计划任务每分钟启动，不由技能驻留。

支持 Windows 10/11、PowerShell 5.1 及以上、Python 3.10 及以上。仓库只包含源代码和合成测试数据，运行配置、状态和锁保存在本机应用数据目录；来源会话只读扫描，仅向 ZCode 写入本产品通知自动化及下述归属明确的历史软删除。运行数据不应提交。

## 安装前提：先启用微信机器人

安装前在 ZCode 中完成以下动作：

1. 打开远程控制或机器人管理。
2. 新建微信机器人并用手机扫码确认。
3. 确认机器人开关已启用。
4. 在微信中给机器人发送一条消息完成激活。
5. 回到技能或安装器，回复“已启用”或 `ready`，让它重新只读复检。

安装器只验证机器人配置、启用状态、激活时间、凭据引用和通知工作区授权；只读检查会读取必要的本地加密引用元数据来确认 `enc:v1:` 前缀，但不会解密、打印、上传或持久化凭据正文，也不会替用户扫码。

## 一键安装和手动回退

在已下载的仓库目录执行：

```powershell
.\scripts\install.ps1
```

技能确认后会明确传入 `-EnableCodex` 或 `-DisableCodex`。手动交互安装未指定开关时会询问“是否同时监控 Codex 任务？[y/N]”。自动化环境使用：

```powershell
.\scripts\install.ps1 -DisableCodex -NonInteractive
.\scripts\install.ps1 -EnableCodex -ZCodeHome $env:ZCODE_HOME -CodexHome $env:CODEX_HOME -NonInteractive
```

可选参数为 `-ZCodeHome`、`-CodexHome`、`-InstallDir`。默认安装目录由系统的 LocalApplicationData 动态生成，并使用 `ZCodeTaskNotifier` 产品子目录；不会把开发机器路径写进仓库。没有技能运行时可用同目录的 `scripts\install.cmd` 转发到 PowerShell 安装器。

选择 Codex 的影响：选择 **ZCode-only** 时不读取 Codex 目录、数据库或 rollout；选择启用时才按显式参数、环境变量、用户目录和结构标志发现 Codex，发现失败不会破坏 ZCode 监控。

## 自动发现和运行

ZCode 发现顺序为显式参数、`ZCODE_HOME`、用户目录下的 `.zcode`；当前版本不从运行中进程推断根目录，若这些候选均不可用请提供 `-ZCodeHome`。候选必须有任务索引和至少一个稳定目录标志，多个候选时停止并要求明确选择。通知工作区优先使用已确认 ZCode 根下的 `workspace/default`，不存在时回退到 `workspace`。Codex 发现顺序为显式参数、`CODEX_HOME`、用户目录下的 `.codex`，并要求 `sessions` 或稳定状态库结构。

安装器将源程序放进本机产品目录，保留已有配置和状态，先在同一产品目录创建时间戳备份，再切换 `app/zcode_task_notifier` 程序包。`app` 根目录保持稳定，避免仅因目录共享句柄而无法整体移动；升级不会为此关闭其他应用。它会执行 `baseline`，避免安装前历史事件洪泛；如果发现 ZCode 根下可验证的旧 `task-watch\snapshot.json`，只迁移已处理事件键。随后创建首个触发时间至少在一分钟后的当前用户、每分钟、隐藏运行的 `ZCodeTaskNotifier` 计划任务，运行 `doctor` 成功后才停用动作精确指向已验证旧监控脚本的旧任务；失败时旧 watcher 不会提前停用，也不按模糊名称处理其他任务。

计划任务使用已选 Python 安装内的 `pythonw.exe`，每分钟运行时不创建控制台黑框；安装、手动诊断仍使用 `python.exe` 显示结果。安装器会在改动任务前确认 `pythonw.exe` 存在。计划任务的 `Hidden` 只控制任务条目的显示，不负责隐藏进程窗口。

本机运行状态包括配置、状态和锁；`doctor` 只输出健康布尔值、计数、脱敏路径和告警，并汇总 Windows Task Scheduler 最近运行结果，不回显目标、凭据、会话正文或令牌。

通知自动化的本地 `workspace_key` 使用原生完整工作区路径，不使用目录名。`bot_delivery_target` 保存动态发现的 provider、botId、providerUserId、chatType 四个路由字段，供 ZCode 建立微信投递订阅；它们不包含机器人凭据或上下文令牌，只保存在本机数据库中。旧版本错误地将该字段留空，不能以自动化执行成功证明微信已发送。升级不重启已结束的自动化，也不补发旧通知。

## 诊断与限制

已支持的来源标签为 `[zcode]`、`[codex]`，自动化标题与微信通知首行均使用对应标签；重复的同类标签会归一为一个。`claudecode`、`dsh` 仅在来源注册表中保留未适配占位，不扫描、不允许启用，也不会产生通知。

只在实际任务停下时通知，不把初始化确认 Harness/规则、表示就绪、等待第一项任务或普通执行进度当成完成。来源读取本回合的任务输入证据、结构化停止信息与最终输出；不调用摘要模型判断“是否值得通知”。

| 状态 | 通知含义 |
| --- | --- |
| `awaiting_approval` | 计划待审批，尚未同意，不自动执行 |
| `awaiting_input` | 待用户选择或补充信息，不作为计划批准 |
| `completed` | 任务完成 |
| `error` | 任务失败，不自动重发 |

等待状态显示“停顿时间”，不会写成“完成时间”。判定保留来源、会话、回合和必要指纹；不会为识别任务而把完整用户输入另存到监控状态文件。旧版未记录输入的来源保留最终结果兼容，并排除明确的初始化和进度回复。

ZCode 优先使用当前 `model_io` 的回复与结束原因，避免把中间工具调用或旧索引摘要当成完成；仍在运行的任务不靠正文关键词伪造停顿。原生只保存在应用内存、没有可读持久化事件的审批弹窗不在当前外置读取能力内。文本判定只是通知分类，不构成远程执行授权。

`/刷新` token 和微信“同意”批准桌面会话计划尚未实现：原版未确认可供本产品安全接入的入站控制和桌面审批接口。原版 `/status` 不等于 token 刷新，`/approve` 是权限审批而非计划审批；不要据此向普通聊天发送控制词期待本产品启动任务。调查结论及后续门槛见 [原生能力边界](docs/native-control-capabilities.md) 和 [第三版计划](docs/superpowers/plans/2026-09-05-v3-wechat-plan-approval.md)。

安装完成后可运行：

```powershell
python -m zcode_task_notifier doctor --config <本机配置路径> --json
python -m zcode_task_notifier run --config <本机配置路径> --state <本机状态路径>
```

监控器按完成回合去重；Codex 通知标题以 `[codex]` 开头。通知器只提交每个事件的首发自动化，不读取 `sendmessage failed` 或 `context_token` 失败日志，也不会根据它们自动创建后续自动化；如确需补发，只能由用户显式执行 `backfill --codex-thread`。外部服务没有可靠成功回执时，极端情况下可能出现一次重复投递。真实微信到达依赖用户的机器人状态和网络，本仓库的合成测试、`doctor` 或计划任务创建成功都不能替代端到端到达验证。

常见故障：机器人未激活时回到第 1 节完成激活；多个 ZCode/Codex 候选时使用显式参数；Codex 不可用时选择 ZCode-only；状态损坏时保留隔离副本并先执行 `doctor`，不要删除 ZCode 数据。

## 通知历史自动整理

每个增量监控轮次按 `[zcode]`、`[codex]` 分别保留最新十条合格通知历史，超过十条才软删除多余项。显式 `baseline` 和首次运行的静默基线不清理，从下一次增量轮次开始整理。

清理必须同时验证本产品已提交事件的稳定自动化 ID、精确工作区及父自动化关联、通知任务终态和来源标签。待审批、执行中、置顶、归档、手动改名、用户编辑过调度、无归属证明以及旧版未加标签的 ZCode 记录均保留。因此界面总记录数可能超过十条；不会为了凑数量删除受保护记录或业务任务。

任务行只设置软删除标记，并移除该通知的精确分组引用；不删除会话文件、自动化定义或执行记录。软删除标记可由维护工具恢复，但分组关系不会自动恢复。含歧义的旧版工作区排序节点保留并记录跳过原因。

本地 `history-ownership.json` 只长期保存必要的来源、事件键、源任务标识、时间和稳定自动化 ID，不保存标题、正文、路径或令牌，避免 outbox 正文七天过期后丢失归属。删除前必须把脱敏意图审计写入 `history-cleanup.jsonl` 并刷盘，提交后再记结果；前置审计失败不删除，后置审计失败明确报告而不宣称回滚。`run --json` 返回 `cleanup_deleted` 和 `cleanup_warnings`；清理异常不会阻断正常任务通知。

## 升级和卸载

重复运行安装器会保留 `config.json`、`state.json` 和已有计划任务信息，先备份旧程序包，失败时恢复备份和旧任务，不移动稳定的 `app` 根目录或删除其中的其他文件。配置和状态只有完成对应备份检查后才进入回滚范围；备份缺失时保留当前文件并明确报错，不假装已恢复。默认安装目录执行 `scripts\uninstall.ps1 -KeepData` 只注销 `ZCodeTaskNotifier` 并删除本产品程序，保留本地状态和其他产品数据；如果安装时使用了自定义目录，卸载必须传入同一个目录，例如 `scripts\uninstall.ps1 -InstallDir $env:LOCALAPPDATA\ZCodeTaskNotifier -KeepData`。不带 `-KeepData` 时会先询问是否删除产品数据。卸载器会验证目标确实位于系统 LocalApplicationData 下且末级目录为产品名，不删除 ZCode 或 Codex 文件。

## 隐私发布门禁和许可证

发布前运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\privacy-check.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\privacy-check.ps1 -History
```

门禁检查当前分发文件和 Git 历史中的绝对路径、用户名、机器人标识、令牌、数据库、日志、快照和锁文件；通过时输出固定成功消息，失败时只输出稳定错误码 `PRIVACY_CHECK_VIOLATION` 或 `PRIVACY_CHECK_ERROR`，不回显路径、对象 ID、异常正文或秘密。发现问题时请在仓库问题中提供脱敏复现步骤，不要上传运行目录或凭据。

许可证：MIT，见 [LICENSE](LICENSE)。

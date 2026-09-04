# ZCode / Codex 任务完成微信通知器

公开仓库：<https://github.com/M1gr4ine/zcode-task-notifier>

把下面这段话复制给 ZCode Agent：

```text
请从 https://github.com/M1gr4ine/zcode-task-notifier 下载并注册 skills/zcode-task-notifier-install/SKILL.md；下载、注册完成后等待用户手动输入 /zcode-task-notifier-install，不要自动触发或隐式调用安装技能。技能先说明如何在 ZCode 的机器人管理中启用微信机器人；若缺少或未激活则停下教学，等我回复“已启用”后再复检；然后单独询问我是否同时监控 Codex，我选择否时只配置 ZCode，最后由技能调用外部 scripts/install.ps1 和 doctor。不要读取、打印、解密或上传任何机器人凭据正文；只读检查如需确认加密引用前缀，不得持久化其值。
```

## 用途和边界

本项目在 ZCode 外部运行，读取本机 ZCode 工作区和可选 Codex 的完成事件，并通过用户已经启用的 ZCode 微信机器人发送摘要。它不修改 ZCode 安装文件、应用包或业务代码；卸载只移除本产品自己的计划任务、本地状态和其他产品数据。

安装技能是显式入口：注册 `skills/zcode-task-notifier-install/SKILL.md` 后，必须由用户输入 `/zcode-task-notifier-install` 才会触发。技能只编排引导、确认、Codex 选择、安装器和 `doctor` 调用，调用结束即退出；常驻监控由外部 Windows 计划任务每分钟启动，不由技能驻留。

支持 Windows 10/11、PowerShell 5.1 及以上、Python 3.10 及以上。仓库只包含源代码和合成测试数据，运行配置、状态和锁保存在本机应用数据目录；来源数据库和其他运行数据只读处理，不应提交。

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

安装器将源程序放进本机产品目录，保留已有配置和状态，先在同一产品目录创建时间戳备份，再原子切换 `app`。它会执行 `baseline`，避免安装前历史事件洪泛；如果发现 ZCode 根下可验证的旧 `task-watch\snapshot.json`，只迁移已处理事件键。随后创建首个触发时间至少在一分钟后的当前用户、每分钟、隐藏运行的 `ZCodeTaskNotifier` 计划任务，运行 `doctor` 成功后才停用动作精确指向已验证旧监控脚本的旧任务；失败时旧 watcher 不会提前停用，也不按模糊名称处理其他任务。

本机运行状态包括配置、状态和锁；`doctor` 只输出健康布尔值、计数、脱敏路径和告警，并汇总 Windows Task Scheduler 最近运行结果，不回显目标、凭据、会话正文或令牌。

## 诊断与限制

安装完成后可运行：

```powershell
python -m zcode_task_notifier doctor --config <本机配置路径> --json
python -m zcode_task_notifier run --config <本机配置路径> --state <本机状态路径>
```

监控器按完成回合去重；Codex 通知标题以 `[codex]` 开头。通知器只提交每个事件的首发自动化，不读取 `sendmessage failed` 或 `context_token` 失败日志，也不会根据它们自动创建后续自动化；如确需补发，只能由用户显式执行 `backfill --codex-thread`。外部服务没有可靠成功回执时，极端情况下可能出现一次重复投递。真实微信到达依赖用户的机器人状态和网络，本仓库的合成测试、`doctor` 或计划任务创建成功都不能替代端到端到达验证。

常见故障：机器人未激活时回到第 1 节完成激活；多个 ZCode/Codex 候选时使用显式参数；Codex 不可用时选择 ZCode-only；状态损坏时保留隔离副本并先执行 `doctor`，不要删除 ZCode 数据。

## 升级和卸载

重复运行安装器会保留 `config.json`、`state.json` 和已有计划任务信息，先备份旧 `app`，失败时恢复备份和旧任务。默认安装目录执行 `scripts\uninstall.ps1 -KeepData` 只注销 `ZCodeTaskNotifier` 并删除本产品程序，保留本地状态和其他产品数据；如果安装时使用了自定义目录，卸载必须传入同一个目录，例如 `scripts\uninstall.ps1 -InstallDir $env:LOCALAPPDATA\ZCodeTaskNotifier -KeepData`。不带 `-KeepData` 时会先询问是否删除产品数据。卸载器会验证目标确实位于系统 LocalApplicationData 下且末级目录为产品名，不删除 ZCode 或 Codex 文件。

## 隐私发布门禁和许可证

发布前运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\privacy-check.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\privacy-check.ps1 -History
```

门禁检查当前分发文件和 Git 历史中的绝对路径、用户名、机器人标识、令牌、数据库、日志、快照和锁文件；通过时输出固定成功消息，失败时只输出稳定错误码 `PRIVACY_CHECK_VIOLATION` 或 `PRIVACY_CHECK_ERROR`，不回显路径、对象 ID、异常正文或秘密。发现问题时请在仓库问题中提供脱敏复现步骤，不要上传运行目录或凭据。

许可证：MIT，见 [LICENSE](LICENSE)。

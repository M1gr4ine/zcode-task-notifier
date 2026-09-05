# ZCode / Codex 任务完成微信通知器设计

## 1. 目标

提供一个运行在 ZCode 外部的 Windows 监控器，不修改 ZCode 安装目录、应用包或业务代码。它持续监控当前 Windows 用户下的所有 ZCode 工作区及可选的 Codex 会话；任务完成后，通过用户已经启用的 ZCode 微信机器人生成通知。

核心目标：

- ZCode：覆盖所有工作区中的普通会话完成事件。
- Codex：覆盖所有项目、无项目任务和工作区中的任务完成事件；消息统一以 `[codex]` 开头。
- 发送失败：不根据 `sendmessage failed`、`context_token` 或其他失败日志自动创建后续自动化；需要时仅允许用户显式执行定向补发。
- 隔离：不改 ZCode 本体；卸载后不影响 ZCode 与 Codex。
- 隐私：公开仓库、提交历史、文档、测试和示例配置中不得包含开发者或安装用户的用户名、盘符、绝对路径、机器人标识、令牌、会话内容或数据库副本。
- 易安装：其他用户把 GitHub 地址交给 ZCode Agent 后，先下载并注册显式调用的 `zcode-task-notifier-install` 技能；用户输入 `/zcode-task-notifier-install` 后，技能先引导用户启用微信机器人，再询问是否监控 Codex。技能只编排安装交互，常驻监控仍由外部 Windows 计划任务负责。

## 2. 范围与非目标

### 2.1 包含

- Windows 10/11，PowerShell 5.1 及以上。
- Python 3.10 及以上；优先复用系统或 ZCode 可发现的 Python。
- ZCode 本地任务索引、自动化数据库和运行日志的只读检测。
- Codex 本地目录、状态数据库和 rollout JSONL 的只读检测。
- 通过 ZCode 现有 GLM 自动化生成并发送微信摘要。
- Windows 计划任务安装、升级、自检与卸载。
- 本地状态、来源游标和 outbox。

### 2.2 不包含

- 不修改 `app.asar`、ZCode 可执行文件或其源码。
- 不解密、导出、复制或上传微信机器人凭据。
- 不直接调用非公开的微信 iLink 发送接口。
- 不把用户会话正文、任务数据库或日志上传到本仓库或第三方服务。
- 不自动完成手机扫码、手机确认或首次给机器人发消息。
- 不读取失败日志来推断补发；`sendmessage failed` 或 `context_token` 失败不会触发后续自动化。

## 3. 发布结构

```text
zcode-task-notifier/
├─ README.md
├─ LICENSE
├─ .gitignore
├─ pyproject.toml
├─ skills/
│  └─ zcode-task-notifier-install/
│     └─ SKILL.md
├─ src/zcode_task_notifier/
│  ├─ __init__.py
│  ├─ cli.py
│  ├─ config.py
│  ├─ discovery.py
│  ├─ zcode_source.py
│  ├─ codex_source.py
│  ├─ notifier.py
│  ├─ models.py
│  ├─ service.py
│  ├─ migration.py
│  ├─ __main__.py
│  └─ state.py
├─ scripts/
│  ├─ install.ps1
│  ├─ install.cmd
│  ├─ uninstall.ps1
│  └─ privacy-check.ps1
├─ config.example.json
├─ tests/
└─ docs/superpowers/specs/
```

运行文件默认放在安装用户的本地应用数据目录，不进入仓库：

```text
%LOCALAPPDATA%\ZCodeTaskNotifier\
├─ app\
├─ config.json
├─ state.json
└─ notifier.lock
```

## 4. 路径发现与隐私设计

### 4.1 路径表达原则

源码和配置使用语义路径，不保存开发机器的展开结果：

- 用户目录：Python `Path.home()` 或 PowerShell `[Environment]::GetFolderPath('UserProfile')`。
- 本地应用数据：`LOCALAPPDATA` 或系统 Known Folder API。
- ZCode 根：按“显式参数 → 环境变量 → 用户目录下标准候选 → 已验证状态结构”顺序发现。
- Codex 根：按“显式参数 → `CODEX_HOME` → 用户目录下标准候选 → 状态库反向验证”顺序发现。
- 安装目录：`LOCALAPPDATA` 下的产品子目录，允许 `--install-dir` 覆盖。

任何绝对路径只在安装机器生成的 `config.json` 中出现；该文件被 `.gitignore` 排除，且诊断输出路径时默认折叠为 `%USERPROFILE%`、`%LOCALAPPDATA%`、`%ZCODE_HOME%`、`%CODEX_HOME%`。

### 4.2 ZCode 根目录探测

发现器依次尝试：

1. 命令行 `--zcode-home`。
2. 环境变量 `ZCODE_HOME`。
3. 用户目录下的 `.zcode`。

候选必须同时满足结构校验：任务索引和日志位于 `v2`；v2 机器人配置必须是同一目录下完整的
`v2/bot-config.json`、`v2/bot-state.v2.json`、`v2/credentials.json` 三件套。为兼容旧部署，
只有在 v2 三件套不完整时才允许使用完整的根级 `bot-config.json`、`bot-state.v2.json`、
`credentials.json` 三件套；三件套不得跨布局混用。自动通知工作区优先选 `workspace/default`，
不存在时回退到 `workspace`。无法唯一确认时安装器停止并给出脱敏候选，不擅自选择。

### 4.3 Codex 根目录探测

仅当用户选择启用 Codex 时执行：

1. 命令行 `--codex-home`。
2. 环境变量 `CODEX_HOME`。
3. 用户目录下的 `.codex`。

候选必须能定位状态数据库或 `sessions` 目录。检测失败不影响 ZCode-only 安装；安装器明确报告 Codex 未启用。

### 4.4 发布前隐私门禁

发布流水线和本地发布脚本必须失败关闭，至少检查：

- Windows 用户目录模式、常见盘符绝对路径、当前用户名。
- 真实 `botId`、`providerUserId`、`credentialRef`、token、`enc:v1:` 密文。
- `.sqlite`、`.db`、`.jsonl`、运行日志、快照、锁文件、缓存目录。
- 测试夹具只能使用临时目录、合成 UUID 和虚构消息。
- 对 Git 全历史做同样扫描，避免“当前文件已删但历史仍泄露”。

通过时输出固定成功消息；发现违规或检查异常时只输出稳定错误码
`PRIVACY_CHECK_VIOLATION` 或 `PRIVACY_CHECK_ERROR`，不输出文件路径、Git 对象 ID、规则名、异常正文或秘密。

## 5. 组件设计

### 5.1 `discovery`

负责发现并验证 ZCode、Codex、Python、数据库、日志和通知工作区。返回结构化的路径集合以及验证证据，不承担事件解析。

通知工作区使用 `notification_workspace` 配置；配置为 `auto` 时，在已确认的 ZCode 根下优先选择
`workspace/default`，不存在时回退到 `workspace`。监控范围与通知执行工作区分离：一个通知工作区负责发送，
但事件来源仍覆盖全部工作区。

### 5.2 `zcode_source`

以只读 SQLite 连接读取 ZCode 中央任务索引，扫描全部工作区，不按当前工作目录过滤。忽略通知器自己创建的自动化任务，防止递归通知。

ZCode 的通知粒度是“完成回合”，不是“任务首次进入 completed”。同一个会话完成后可以继续提问，后续回合结束时任务状态仍为 `completed`，只比较状态会漏报。现代 ZCode 的稳定回合标识来自其 CLI rollout 目录中的 `model-io-{session-id}.jsonl`：记录必须为 `type=model_io`、`querySource=main_turn`，并具有非空 `turnId` 与 `completedAt`。监控器增量读取该文件，记录每个会话最新观测到的 turn；中央任务索引显示任务处于终态时，尚未通知过的最新 turn 生成事件。这样即使一分钟轮询没有观察到中间的 running 状态，仍能识别新回合。

同一 turn 可能包含多次模型调用；只有该 turn 的最后一条完整 model-io 记录用于完成时间和耗时，事件只生成一次。JSONL 尾部不完整时不推进游标。没有 model-io 文件的旧 ZCode 版本退回到一次性任务状态检测，并由 `doctor` 报告“无法保证同会话后续回合通知”，不使用不稳定的元数据更新时间反复推送。

事件键：

```text
zcode:<session-id>:<turn-id>
```

没有对应 model-io 文件时退回兼容事件：若有终态时间，事件键包含任务 ID、终态时间和
`searchable_text` 的短 SHA-256；标题不参与版本键，避免只改标题造成重复通知。终态时间缺失时
保留固定兼容键，只保证旧环境的一次性通知；同一版本重复扫描不重复，终态时间或摘要变化会
生成新版本事件。键中不写入摘要原文。

### 5.3 `codex_source`

Codex 采用双源设计：

- 主源：状态数据库中用户任务的当前 `rollout_path`，增量读取 JSONL；`event_msg.payload.type == "task_complete"` 为完成权威事件。
- 兼容源：历史数据库中的已完成 turn，用于旧版本和主源暂不可用时补充。

监控器按每个 rollout 文件保存字节游标，遇到未换行的尾部记录时不推进该行游标，下次继续读取。rollout 切段或路径变化时将新文件作为独立流处理；事件按 thread、turn 和最终消息摘要去重。

事件键：

```text
codex:<thread-id>:<turn-id>:<final-message-hash>
```

兼容源没有完整 turn 标识时使用数据库主键和完成时间构造稳定键。所有 Codex 通知标题必须以 `[codex]` 开头。

### 5.4 `notifier`

通知器不直接连接微信。它向 ZCode v2 `automations` 表写入一次性 GLM 自动化；目标表按
真实 33 列契约校验 `automation_id` 的 TEXT 类型及唯一/主键约束、必填列类型与 NOT NULL，
未知 NOT NULL 且无默认值的列直接失败关闭。自动化行只使用 `title` 和 `prompt` 承载事件详情，
`automation_id` 由事件键按首发哈希规则确定性编码并仅按自身查询幂等；来源元数据留在本机 state/outbox。
2026-09-05 修正：`bot_delivery_target` 必须保存动态发现的四字段路由 JSON（provider、botId、providerUserId、chatType），不保存凭据或 token；原先要求 NULL 的假设导致微信订阅未建立。本地工作区 key 为完整路径，不能用目录名。参见 `docs/native-control-capabilities.md`。

提示词要求 GLM：

- 只概括指定完成事件，不扫描或混入其他任务。
- 输出简洁的任务名、完成时间、耗时和摘要。
- Codex 标题保留 `[codex]` 前缀。
- 若源内容无法读取，明确写“摘要不可用”，仍发送完成通知。

写入采用事务并检查目标表结构；未知 schema 时停止写入并记录可操作错误，避免破坏 ZCode 数据。

### 5.5 `state`

状态文件原子写入：先写同目录临时文件，刷新后替换。内容包括：

- ZCode 与 Codex 已处理事件键。
- rollout 文件游标与文件身份信息。
- 首发 outbox 与提交时间。
- schema/version 和最近一次健康检查结果。

升级读取旧状态时，只保留事件键、首发自动化 ID 和已提交时间；旧的
`retry_wait`/`exhausted` 状态按 `submitted` 处理，旧 due/attempt 和失败日志游标
被忽略并清零，不把事件重新放回 `pending`。

使用独占锁防止计划任务重叠。状态损坏时保留坏文件副本并进入安全基线模式，不直接把所有历史任务当作新任务发送。

## 6. 事件数据流

```text
计划任务每分钟启动
  → 获取独占锁
  → 路径/Schema 快速校验
  → 扫描全部 ZCode 工作区完成事件
  → 若启用，扫描全部 Codex rollout + 兼容历史库
  → 合并、去重、写入 outbox
  → 投递到 ZCode GLM 一次性自动化
  → 原子保存状态
  → 释放锁
```

安装技能不参与上述常驻数据流。`skills/zcode-task-notifier-install/SKILL.md` 只在用户明确输入 `/zcode-task-notifier-install`（或明确点名该技能）时被调用，完成前置引导和安装器编排后结束；监控器进程由已注册的 Windows 计划任务按轮次启动，不由技能驻留、轮询或保活。

首次安装默认执行“从当前时点开始”：

- ZCode 已完成会话只建基线，不补发。
- Codex 现有 rollout 游标定位到文件末尾，历史完成事件只建基线。
- `backfill --codex-thread` 是唯一的手动补发 CLI，只允许对指定 Codex thread 补发，避免安装时消息洪泛。

本机升级时可以对已经确认漏发的单个任务执行一次定向补发；它不进入公共安装器默认流程。

## 7. 一键安装交互

### 7.1 显式安装技能与 ZCode Agent 的入口

公开仓库提供 `skills/zcode-task-notifier-install/SKILL.md` 作为安装技能。它采用与 `ft-busi-init` 相同的显式 `/技能名` 调用方式，frontmatter 至少为：

```yaml
---
name: zcode-task-notifier-install
description: |
  仅当用户明确点名 `zcode-task-notifier-install` 或输入 `/zcode-task-notifier-install` 时使用此技能。
user-invocable: true
---
```

`description` 只描述显式触发条件，不写安装流程摘要；技能不因“安装通知器”“配置微信”等泛化描述隐式触发。README 提供一段可复制给 ZCode Agent 的引导语，要求 Agent 先下载并注册该技能，再提示用户输入 `/zcode-task-notifier-install`：

1. 克隆或下载仓库到临时目录并注册 `skills/zcode-task-notifier-install/SKILL.md`。
2. 提示用户显式输入 `/zcode-task-notifier-install`，不得代替用户隐式调用技能。
3. 由技能在用户确认后调用外部 `scripts/install.ps1`、`doctor` 和基线命令。
4. 不替用户完成微信扫码，不回显任何凭据。

同时提供 `install.cmd` 和 `scripts/install.ps1` 作为技能不可用时的手动安装回退入口；它们只定位同目录脚本并传递参数，不包含机器路径，也不承载常驻监控逻辑。

### 7.2 微信机器人前置引导

安装器先只读检测微信机器人配置。若未满足条件，则打印操作步骤并暂停：

1. 打开 ZCode 的远程控制/机器人管理。
2. 新建微信机器人并用手机扫码确认。
3. 确认机器人开关已启用。
4. 在微信中给机器人发送一条消息完成激活。
5. 回到安装器输入确认后重新检测。

校验只检查配置存在性、启用状态、凭据引用存在、激活时间和工作区授权，不读取或解密凭据正文。校验失败时说明缺失项，允许用户修复后重试或退出。

### 7.3 Codex 选择

微信前置校验通过后询问：

```text
是否同时监控 Codex 任务？[y/N]
```

默认 `N`。选择 `N` 时不读取 Codex 数据，也不要求安装 Codex；选择 `Y` 时执行 Codex 路径发现与结构校验。

### 7.4 安装动作

安装技能的编排顺序固定为“微信机器人引导 → 等待用户确认 → 复检 → 单独询问是否启用 Codex → 调用外部安装器和 `doctor`”。缺少或未激活微信机器人时必须停在教学步骤，只有用户确认后才能继续；选择不启用 Codex 时只调用 ZCode-only 安装路径，不读取 Codex 数据。

外部安装器：

- 将版本化源码复制到本地应用数据安装目录。
- 生成仅本机可读的 `config.json`。
- 注册每分钟运行且不弹窗的 Windows 计划任务。
- 执行 `doctor` 和一次基线扫描。
- 调用 `doctor`，展示监控来源数量、Codex 是否启用、下次执行时间和 Windows Task Scheduler 最近运行结果。

安装完成后，技能调用即结束；每分钟计划任务启动外部监控器完成扫描和通知，技能不驻留、不轮询、不重复注册计划任务。

升级安装保留配置和状态，先备份再原子替换程序文件。卸载只删除本产品的计划任务与安装目录，并明确询问是否保留本地状态和其他产品数据；不删除 ZCode 或 Codex 文件。

## 8. 配置

公共示例仅包含相对或语义值：

```json
{
  "schema_version": 1,
  "zcode_home": "auto",
  "notification_workspace": "auto",
  "codex_enabled": false,
  "codex_home": "auto",
  "interval_seconds": 60,
  "model": "builtin:bigmodel-coding-plan/GLM-5-Turbo",
  "codex_prefix": "[codex]",
  "outbox_retention_days": 7
}
```

真实机器人标识由运行时从 ZCode 配置读取并仅在内存使用，不写入公共示例；确需本地缓存时只写安装目录下的私有配置，并保持输出脱敏。

## 9. 安全与故障处理

- 所有源数据库使用只读 URI 打开；写操作仅限 ZCode 自动化数据库中经过 schema 验证的目标表。
- SQL 参数化，不拼接会话正文或路径。
- 不跟随来源目录外的 rollout 路径；规范化后验证其位于已确认 Codex 根内。
- 诊断与任务结果输出默认不包含完整提示词、最终回答、机器人 ID 或原始路径。
- 单个损坏会话、锁库或截断 JSONL 不阻塞其他工作区；记录来源级错误并在后续轮次继续读取。
- ZCode schema 不兼容、通知工作区不唯一或机器人未激活时失败关闭，不尝试猜测写入。
- 自动化插入和状态推进遵循“先保存 outbox、后插入、确认插入后标记已投递”；任何一步失败均可幂等恢复。

## 10. 测试策略

### 10.1 单元测试

- 环境变量、Known Folder、候选冲突及路径折叠。
- 全工作区 ZCode 完成检测、通知器自任务过滤和事件去重。
- 同一 ZCode 会话连续完成多个 turn 时逐回合通知；一分钟轮询漏过中间 running 状态时仍能依靠新 `turnId` 检出。
- Codex rollout 增量读取、半行恢复、切段、重复 `task_complete`、历史库兼容。
- `[codex]` 前缀不可被摘要生成覆盖。
- `sendmessage failed`/`context_token` 失败不会自动创建后续自动化；显式定向补发与旧状态迁移。
- 原子状态写入、损坏恢复、锁竞争。
- 首次基线不补发，定向补发只命中指定事件。

### 10.2 集成测试

使用临时目录创建合成的 ZCode/Codex SQLite 和 JSONL；验证扫描到自动化插入的完整链路。测试不得读取真实用户目录，也不得依赖真实微信或智谱网络。

### 10.3 安装测试

- 安装技能静态门禁：frontmatter 的 `name` 与目录一致、`description` 仅允许显式点名/斜杠调用、`user-invocable: true`，不含本机路径或真实标识。
- 安装技能场景：未显式点名不触发；显式输入 `/zcode-task-notifier-install` 触发；缺少微信机器人时停下教学；用户确认后继续；Codex 选择否时只安装 ZCode-only。
- 干净用户环境、已有安装升级、ZCode-only、ZCode+Codex。
- 路径含空格和非 ASCII 字符。
- 无管理员权限下的当前用户计划任务。
- 微信未配置、未激活、多个机器人、工作区授权不足。
- 卸载不影响 ZCode/Codex 文件。

### 10.4 发布门禁

- 全部测试通过。
- Python 编译与 PowerShell 语法检查通过。
- 真实 Git 差异审查通过。
- 当前树与 Git 全历史隐私扫描通过。
- 从公开仓库地址在临时用户目录完成一次全新安装演练。

真实微信端到端验证需要用户已启用机器人和网络可用；未执行时必须明确标注，不能用模拟测试冒充。

## 11. 本机迁移与修复

现有外置监控器升级流程：

1. 只读记录现有脚本哈希、计划任务、配置和状态版本。
2. 备份现有程序与状态到产品安装目录下带时间戳的备份目录。
3. 将旧快照转换为新 schema；已有事件保持已处理，避免重复推送。
4. 保留原计划任务触发频率，切换到新入口。
5. 运行 `doctor`、基线扫描和合成事件测试。
6. 对已确认漏发的指定 Codex 任务执行一次定向补发。
7. 若任一步失败，恢复旧脚本和计划任务；不改 ZCode 本体。

本机部署闸门记录：基于脱敏的 v2 契约复核，Task 2 因根级机器人路径假设被退回，Task 5
因虚构事件列及 `event_key+attempt` 唯一索引假设被退回；两项均先以合成夹具补红测再修复。
本轮只验证源码、合成数据和静态隐私边界，未读取或写入真实配置、数据库行、日志、会话，
因此不宣称本机部署或真实微信端到端已完成。

## 12. README 内容

README 必须包含：

- 用途、支持范围和“不修改 ZCode 本体”的保证。
- 安装前提与微信机器人启用图文步骤。
- 给 ZCode Agent 的一段复制即用指令：下载并注册技能后，由用户显式输入 `/zcode-task-notifier-install`。
- 手动一键安装回退、静默参数、ZCode-only/Codex 可选项；明确技能只编排安装，常驻监控由外部计划任务负责。
- 路径自动发现规则与隐私说明。
- `doctor`、Windows Task Scheduler 最近结果、常见故障、升级与卸载。
- 明确发送失败不会自动创建后续自动化；需要时只能由用户显式执行定向补发。
- 安全报告方式和 MIT 许可证。

## 13. 版本与发布

- 初始版本 `v0.1.0`。
- 公开仓库名：`zcode-task-notifier`。
- 默认分支：`main`。
- 许可证：MIT。
- 发布前不上传任何本机运行文件；通过隐私门禁后再创建公开 GitHub 仓库并推送。
- GitHub 登录采用交互式浏览器授权，凭据不写入项目文件或命令日志。

## 14. 验收标准

1. 不修改 ZCode 安装文件或应用包，监控器可独立安装和卸载。
2. 任意 ZCode 工作区中的每个完成回合只生成一次微信通知；同一会话继续提问并再次完成时会生成新通知。
3. 启用 Codex 后，任意 Codex 工作区/项目/无项目任务的 `task_complete` 只生成一次带 `[codex]` 的通知。
4. rollout 切段后仍能发现新完成事件，不依赖可能滞后的历史数据库。
5. `sendmessage failed`、`context_token` 或其他发送失败不会自动创建后续自动化；需要时支持用户显式指定单事件补发。
6. 首次安装不洪泛历史消息；支持显式指定单事件补发。
7. 安装器先验证并引导微信机器人，再询问是否启用 Codex；选择否时为 ZCode-only。
8. 在路径含空格、中文且非系统盘的环境可自动发现或给出无歧义选择。
9. 公开当前树和 Git 全历史均不含任何真实用户名、盘符绝对路径、机器人标识、凭据、会话内容或运行数据库。
10. 单元、集成、安装与隐私测试全部通过；真实微信端到端验证状态如实报告。
11. 安装技能仅在用户明确点名或输入 `/zcode-task-notifier-install` 时触发；未显式点名不触发，缺少微信机器人时停下教学，确认后才继续，选择不启用 Codex 时仅配置 ZCode。
12. 安装技能调用结束后不驻留；常驻扫描和通知仅由外部 Windows 计划任务按轮次执行。

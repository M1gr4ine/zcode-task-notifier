[CmdletBinding()]
param(
    [switch]$EnableCodex,
    [switch]$DisableCodex,
    [string]$ZCodeHome,
    [string]$CodexHome,
    [string]$InstallDir,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

$script:TaskName = "ZCodeTaskNotifier"
$script:ProductName = "ZCodeTaskNotifier"
$script:SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$script:Python = $null
$script:WindowlessPython = $null
$script:InstallRoot = $null
$script:StageRoot = $null
$script:BackupRoot = $null
$script:AppBackupPath = $null
$script:AppSwitched = $false
$script:TaskRegistered = $false
$script:RegisteredTaskPath = "\"
$script:PreviousTask = $null
$script:PreviousTaskXml = $null
$script:ConfigBackup = $null
$script:StateBackup = $null
$script:ConfigExisted = $false
$script:StateExisted = $false
$script:LegacyTaskRecords = @()
$script:LegacyTaskWarnings = @()
$script:NotifierTriggerAt = $null
$script:InstallStage = "bootstrap"

function Get-LocalApplicationData {
    $value = [Environment]::GetFolderPath('LocalApplicationData')
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Cannot determine the local application data directory"
    }
    return $value
}

function Normalize-PathValue {
    param([Parameter(Mandatory = $true)][string]$Value)
    try {
        return ([IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Value))).TrimEnd([IO.Path]::DirectorySeparatorChar)
    }
    catch {
        throw "Cannot normalize a path"
    }
}

function Resolve-InstallRoot {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        $Value = Join-Path (Get-LocalApplicationData) $script:ProductName
    }
    $candidate = Normalize-PathValue $Value
    $localRoot = Normalize-PathValue (Get-LocalApplicationData)
    $localPrefix = $localRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The installation directory must be below local application data"
    }
    if ((Split-Path -Leaf $candidate) -ne $script:ProductName) {
        throw "The installation directory must end in the product name"
    }
    $source = Normalize-PathValue $script:SourceRoot
    if ($candidate.Equals($source, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The installation directory cannot be the source directory"
    }
    return $candidate
}

function Find-Python {
    foreach ($name in @("python", "python3", "py")) {
        try {
            $command = Get-Command $name -ErrorAction Stop
            if ($command.CommandType -eq "Application") {
                return $command.Source
            }
        }
        catch {
            continue
        }
    }
    throw "No usable Python interpreter was found"
}

function Find-WindowlessPython {
    param([Parameter(Mandatory = $true)][string]$ConsolePython)

    # 使用已选解释器报告的真实位置，兼容 py 启动器与虚拟环境。
    $runtimePaths = @(& $ConsolePython -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $runtimePaths.Count -ne 1) {
        throw "windowless-python-discovery-failed"
    }
    $runtimePath = ([string]$runtimePaths[0]).Trim()
    if (-not [IO.Path]::IsPathRooted($runtimePath) -or -not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
        throw "windowless-python-runtime-invalid"
    }
    $windowless = Join-Path (Split-Path -Parent $runtimePath) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $windowless -PathType Leaf)) {
        throw "windowless-python-unavailable"
    }
    return (Resolve-Path -LiteralPath $windowless).Path
}

function Invoke-PythonModule {
    param(
        [Parameter(Mandatory = $true)][string]$ModuleRoot,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Capture
    )

    $oldPythonPath = $env:PYTHONPATH
    if ([string]::IsNullOrWhiteSpace($oldPythonPath)) {
        $env:PYTHONPATH = $ModuleRoot
    }
    else {
        $env:PYTHONPATH = $ModuleRoot + [IO.Path]::PathSeparator + $oldPythonPath
    }
    try {
        if ($Capture) {
            $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) ("zcode-task-notifier-stdout-" + [Guid]::NewGuid().ToString("N") + ".tmp")
            $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ("zcode-task-notifier-stderr-" + [Guid]::NewGuid().ToString("N") + ".tmp")
            $oldErrorActionPreference = $ErrorActionPreference
            try {
                # 原生命令的 stderr 可能只是 doctor 警告；先分离保存，避免 PowerShell 将其升级为 RemoteException。
                $ErrorActionPreference = "Continue"
                $global:LASTEXITCODE = $null
                try {
                    & $script:Python @Arguments 1> $stdoutPath 2> $stderrPath
                }
                catch {
                    throw "The Python command could not be started"
                }
                $code = $LASTEXITCODE
                if ($null -eq $code) {
                    throw "The Python command could not be started"
                }
                $output = if (Test-Path -LiteralPath $stdoutPath) { [IO.File]::ReadAllText($stdoutPath) } else { "" }
                $errorOutput = if (Test-Path -LiteralPath $stderrPath) { [IO.File]::ReadAllText($stderrPath) } else { "" }
                return [pscustomobject]@{ Output = $output; ErrorOutput = $errorOutput; ExitCode = $code }
            }
            finally {
                $ErrorActionPreference = $oldErrorActionPreference
                Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
            }
        }
        $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ("zcode-task-notifier-stderr-" + [Guid]::NewGuid().ToString("N") + ".tmp")
        $oldErrorActionPreference = $ErrorActionPreference
        try {
            # 非 Capture 调用仍保留 stdout；stderr 单独接收，成功警告不应中止安装。
            $ErrorActionPreference = "Continue"
            $global:LASTEXITCODE = $null
            try {
                & $script:Python @Arguments 2> $stderrPath
            }
            catch {
                throw "The Python command could not be started"
            }
            $code = $LASTEXITCODE
            if ($null -eq $code) {
                throw "The Python command could not be started"
            }
            if ($code -ne 0) {
                throw "The Python command returned a non-zero status"
            }
        }
        finally {
            $ErrorActionPreference = $oldErrorActionPreference
            Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
        }
    }
    finally {
        if ($null -eq $oldPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
        else {
            $env:PYTHONPATH = $oldPythonPath
        }
    }
}

function New-ConfigObject {
    param([bool]$CodexEnabled)

    $zcode = "auto"
    if (-not [string]::IsNullOrWhiteSpace($ZCodeHome)) {
        $zcode = $ZCodeHome
    }
    $codex = "auto"
    if (-not [string]::IsNullOrWhiteSpace($CodexHome)) {
        $codex = $CodexHome
    }
    return [ordered]@{
        schema_version = 1
        zcode_home = $zcode
        notification_workspace = "auto"
        codex_enabled = $CodexEnabled
        codex_home = $codex
        interval_seconds = 60
        model = "builtin:bigmodel-coding-plan/GLM-5-Turbo"
        codex_prefix = "[codex]"
        outbox_retention_days = 7
    }
}

function Write-JsonAtomic {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Value)

    $temporary = $Path + ".tmp-" + [Guid]::NewGuid().ToString("N")
    try {
        $parent = Split-Path -Parent $Path
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        $json = $Value | ConvertTo-Json -Depth 8
        $encoding = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($temporary, $json, $encoding)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function New-ProbeConfig {
    param([bool]$CodexEnabled)

    $probe = Join-Path ([IO.Path]::GetTempPath()) ("zcode-task-notifier-probe-" + [Guid]::NewGuid().ToString("N") + ".json")
    Write-JsonAtomic -Path $probe -Value (New-ConfigObject -CodexEnabled $CodexEnabled)
    return $probe
}

function Test-WeixinTarget {
    param([bool]$CodexEnabled)

    $probe = New-ProbeConfig -CodexEnabled $CodexEnabled
    try {
        $arguments = @("-m", "zcode_task_notifier", "doctor", "--config", $probe, "--json")
        $result = Invoke-PythonModule -ModuleRoot (Join-Path $script:SourceRoot "src") -Arguments $arguments -Capture
        # doctor 的整体退出码还包含 state/source 等安装后检查；预检只消费
        # 下方明确列出的只读目标检查，避免无关项把可用目标误判为失败。
        if ([string]::IsNullOrWhiteSpace($result.Output)) {
            return $false
        }
        try {
            $payload = $result.Output | ConvertFrom-Json
            $weixinOk = [bool]$payload.checks.weixin_target
            if ($CodexEnabled) {
                return $weixinOk -and [bool]$payload.checks.codex_discovered
            }
            return $weixinOk
        }
        catch {
            return $false
        }
    }
    finally {
        if (Test-Path -LiteralPath $probe) {
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        }
    }
}

function Show-WeixinGuide {
    Write-Host "The Weixin bot did not pass the read-only check. Complete these steps:"
    Write-Host "1. Open ZCode remote control or bot management."
    Write-Host "2. Create a Weixin bot and confirm it with the phone scan."
    Write-Host "3. Confirm that the bot switch is enabled."
    Write-Host "4. Send one message to the bot in Weixin to activate it."
    Write-Host "5. Return here and enter ready for a second check."
}

function Confirm-Weixin {
    if (Test-WeixinTarget -CodexEnabled $false) {
        return
    }
    if ($NonInteractive) {
        throw "The Weixin bot is not ready"
    }
    Show-WeixinGuide
    $answer = Read-Host "Enter ready when complete, or another value to exit"
    if ($answer.Trim().ToLowerInvariant() -ne "ready" -and $answer.Trim().ToLowerInvariant() -ne "enabled") {
        throw "The user did not confirm that the Weixin bot is enabled"
    }
    if (-not (Test-WeixinTarget -CodexEnabled $false)) {
        throw "The Weixin bot is still not ready after the second check"
    }
}

function Select-Codex {
    if ($EnableCodex -and $DisableCodex) {
        throw "EnableCodex and DisableCodex cannot be used together"
    }
    if ($EnableCodex) {
        return $true
    }
    if ($DisableCodex) {
        return $false
    }
    if ($NonInteractive) {
        throw "NonInteractive mode requires EnableCodex or DisableCodex"
    }
    $answer = Read-Host "Monitor Codex tasks too? [y/N]"
    return $answer.Trim().ToLowerInvariant() -eq "y" -or $answer.Trim().ToLowerInvariant() -eq "yes"
}

function Read-OrCreateConfig {
    param([string]$Path, [bool]$CodexEnabled)

    if (-not (Test-Path -LiteralPath $Path)) {
        return New-ConfigObject -CodexEnabled $CodexEnabled
    }
    try {
        $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Cannot read the existing configuration"
    }
    if ($null -eq $value) {
        throw "The existing configuration is empty"
    }
    if (-not [string]::IsNullOrWhiteSpace($ZCodeHome)) {
        $value.zcode_home = $ZCodeHome
    }
    $value.codex_enabled = $CodexEnabled
    if (-not [string]::IsNullOrWhiteSpace($CodexHome)) {
        $value.codex_home = $CodexHome
    }
    return $value
}

function Get-MigrationSnapshot {
    $candidate = $null
    if (-not [string]::IsNullOrWhiteSpace($ZCodeHome)) {
        $candidate = $ZCodeHome
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:ZCODE_HOME)) {
        $candidate = $env:ZCODE_HOME
    }
    else {
        $profile = [Environment]::GetFolderPath("UserProfile")
        if (-not [string]::IsNullOrWhiteSpace($profile)) {
            $candidate = Join-Path $profile ".zcode"
        }
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        return $null
    }
    $snapshot = Join-Path $candidate "task-watch\snapshot.json"
    if (Test-Path -LiteralPath $snapshot -PathType Leaf) {
        return $snapshot
    }
    return $null
}

function Quote-TaskArgument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function New-NotifierTaskAction {
    param([string]$AppPath, [string]$ConfigPath, [string]$StatePath)

    $arguments = "-m zcode_task_notifier run --config " + (Quote-TaskArgument $ConfigPath) + " --state " + (Quote-TaskArgument $StatePath)
    if ([string]::IsNullOrWhiteSpace($script:WindowlessPython)) {
        throw "windowless-python-not-selected"
    }
    return New-ScheduledTaskAction -Execute $script:WindowlessPython -Argument $arguments -WorkingDirectory $AppPath
}

function Get-ExistingTask {
    param([string]$Root)

    try {
        $tasks = @(Get-ScheduledTask -TaskName $script:TaskName -ErrorAction Stop)
        if ($tasks.Count -gt 1) {
            throw "Multiple product tasks were found"
        }
    }
    catch {
        if ($_.Exception.Message -eq "Multiple product tasks were found") {
            throw
        }
        if (Test-TaskNotFoundError $_) {
            return $null
        }
        throw "The existing product task could not be inspected"
    }
    if ($tasks.Count -eq 0 -or $null -eq $tasks[0]) {
        return $null
    }
    if (-not [string]::IsNullOrWhiteSpace($Root) -and
        -not (Test-NotifierTaskActionBelongsToRoot -Task $tasks[0] -Root $Root)) {
        throw "The existing product task does not belong to this installation"
    }
    return $tasks[0]
}

function Test-TaskNotFoundError {
    param($ErrorRecord)
    if ($null -eq $ErrorRecord -or $null -eq $ErrorRecord.Exception) {
        return $false
    }
    $exception = $ErrorRecord.Exception
    if ($exception -is [System.Management.Automation.ItemNotFoundException]) {
        return $true
    }
    if ($exception.GetType().FullName -match 'TaskNotFound') {
        return $true
    }
    $errorId = [string]$ErrorRecord.FullyQualifiedErrorId
    if ($errorId.StartsWith("CmdletizationQuery_NotFound", [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return [int64]$exception.HResult -eq -2147024894
}

function Get-NormalizedTaskPath {
    param([string]$TaskPath)
    if ([string]::IsNullOrWhiteSpace($TaskPath)) {
        return "\"
    }
    $value = $TaskPath.Replace('/', '\')
    if (-not $value.StartsWith('\')) {
        $value = '\' + $value
    }
    if ($value.Length -gt 1) {
        $value = $value.TrimEnd('\')
    }
    return $value
}

function Get-TaskPathHash {
    param([string]$TaskPath)
    $normalized = Get-NormalizedTaskPath $TaskPath
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($normalized)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant().Substring(0, 16)
    }
    finally {
        $sha.Dispose()
    }
}

function Write-TaskBackup {
    param([string]$Path, [string]$Xml)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Xml, $encoding)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Task XML backup was not created"
    }
}

function Get-TaskEnabled {
    param($Task)
    if ($null -ne $Task.State -and [string]$Task.State -eq "Disabled") {
        return $false
    }
    return $true
}

function Get-TaskRunning {
    param($Task)
    return $null -ne $Task -and [string]$Task.State -eq "Running"
}

function Get-TaskArgumentPath {
    param([string]$Arguments, [string]$Name)
    $pattern = '(?i)(?:^|\s)' + [regex]::Escape($Name) + '\s+(?:"([^"]+)"|(\S+))'
    $match = [regex]::Match($Arguments, $pattern)
    if (-not $match.Success) {
        return $null
    }
    $value = $match.Groups[1].Value
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = $match.Groups[2].Value
    }
    return Resolve-ActionPath $value
}

function Test-NotifierTaskActionBelongsToRoot {
    param($Task, [string]$Root)
    if ($null -eq $Task) {
        return $false
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        return $false
    }
    $action = $actions[0]
    $expectedApp = Resolve-ActionPath (Join-Path $Root "app")
    $workingDirectory = Resolve-ActionPath ([string]$action.WorkingDirectory)
    if ([string]::IsNullOrWhiteSpace($workingDirectory) -or
        -not $workingDirectory.Equals($expectedApp, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $arguments = [Environment]::ExpandEnvironmentVariables([string]$action.Arguments).Trim()
    if ($arguments -notmatch '(?i)(?:^|\s)-m\s+zcode_task_notifier\s+run(?:\s|$)') {
        return $false
    }
    $expectedConfig = Resolve-ActionPath (Join-Path $Root "config.json")
    $expectedState = Resolve-ActionPath (Join-Path $Root "state.json")
    $config = Get-TaskArgumentPath -Arguments $arguments -Name "--config"
    $state = Get-TaskArgumentPath -Arguments $arguments -Name "--state"
    return $null -ne $config -and $null -ne $state -and
        $config.Equals($expectedConfig, [StringComparison]::OrdinalIgnoreCase) -and
        $state.Equals($expectedState, [StringComparison]::OrdinalIgnoreCase)
}

function Save-ExistingTaskXml {
    param([string]$Directory, [string]$Root)

    $task = Get-ExistingTask -Root $Root
    if ($null -eq $task) {
        return $null
    }
    $taskPath = Get-NormalizedTaskPath ([string]$task.TaskPath)
    $path = Join-Path $Directory ("task-current-" + (Get-TaskPathHash $taskPath) + ".xml")
    $xml = Export-ScheduledTask -TaskName $script:TaskName -TaskPath $taskPath
    Write-TaskBackup -Path $path -Xml ([string]$xml)
    $script:PreviousTask = [pscustomobject]@{
        TaskName = $script:TaskName
        TaskPath = $taskPath
        Enabled = Get-TaskEnabled $task
        Running = Get-TaskRunning $task
        XmlPath = $path
    }
    $script:PreviousTaskXml = $path
    if ([bool]$script:PreviousTask.Running) {
        Stop-ScheduledTask -TaskName $script:TaskName -TaskPath $taskPath -ErrorAction Stop
    }
    if ([bool]$script:PreviousTask.Enabled) {
        Disable-ScheduledTask -TaskName $script:TaskName -TaskPath $taskPath -ErrorAction Stop | Out-Null
    }
    return $path
}

function Get-ZCodeRoots {
    $result = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($ZCodeHome)) {
        $candidates += $ZCodeHome
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ZCODE_HOME)) {
        $candidates += $env:ZCODE_HOME
    }
    $profile = [Environment]::GetFolderPath("UserProfile")
    if (-not [string]::IsNullOrWhiteSpace($profile)) {
        $candidates += (Join-Path $profile ".zcode")
    }
    foreach ($candidate in $candidates) {
        try {
            $root = Normalize-PathValue ([string]$candidate)
            $watcher = Join-Path $root "task-watch\watch.py"
            if (Test-Path -LiteralPath $watcher -PathType Leaf) {
                [void]$result.Add($root)
            }
        }
        catch {
            $script:LegacyTaskWarnings += "legacy-path-invalid"
        }
    }
    return @($result)
}

function Get-PythonCommandPaths {
    $result = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
    foreach ($name in @("python", "python3", "py")) {
        try {
            $commands = @(Get-Command $name -ErrorAction Stop)
            foreach ($command in $commands) {
                if ($command.CommandType -eq "Application" -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
                    [void]$result.Add((Normalize-PathValue ([string]$command.Source)))
                }
            }
        }
        catch {
            continue
        }
    }
    return @($result)
}

function Resolve-ActionPath {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($Value.Trim().Trim('"'))
        return Normalize-PathValue $expanded
    }
    catch {
        return $null
    }
}

function Test-LegacyWatcherAction {
    param(
        $Action,
        [string]$WatcherPath,
        [string]$ZCodeRoot
    )
    if ($null -eq $Action -or [string]::IsNullOrWhiteSpace([string]$Action.Execute)) {
        return $false
    }
    $execute = Resolve-ActionPath ([string]$Action.Execute)
    $allowedExecutables = @(Get-PythonCommandPaths)
    if ([string]::IsNullOrWhiteSpace($execute) -or
        -not ($allowedExecutables | Where-Object { $_.Equals($execute, [StringComparison]::OrdinalIgnoreCase) })) {
        return $false
    }
    $rawArguments = [Environment]::ExpandEnvironmentVariables([string]$Action.Arguments).Trim()
    # The complete legacy action is the same shape as New-ScheduledTaskAction -Execute python -Argument "-File watcher".
    $match = [regex]::Match($rawArguments, '(?i)^\s*-File\s+(?:"([^"]+)"|(\S+))\s*$')
    if (-not $match.Success) {
        return $false
    }
    $workingDirectory = [string]$Action.WorkingDirectory
    $resolvedWorking = $null
    if (-not [string]::IsNullOrWhiteSpace($workingDirectory)) {
        $resolvedWorking = Resolve-ActionPath $workingDirectory
        $expectedRoot = Resolve-ActionPath $ZCodeRoot
        if ([string]::IsNullOrWhiteSpace($resolvedWorking) -or
            -not $resolvedWorking.Equals($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }
    $rawWatcher = $match.Groups[1].Value
    if ([string]::IsNullOrWhiteSpace($rawWatcher)) {
        $rawWatcher = $match.Groups[2].Value
    }
    $expandedWatcher = [Environment]::ExpandEnvironmentVariables($rawWatcher.Trim())
    if ([IO.Path]::IsPathRooted($expandedWatcher)) {
        $resolvedWatcher = Resolve-ActionPath $expandedWatcher
    }
    elseif ([string]::IsNullOrWhiteSpace($resolvedWorking)) {
        # 不把安装器当前目录当作旧任务的执行目录；缺失 WD 必须告警并保持旧任务。
        $script:LegacyTaskWarnings += "legacy-task-working-directory-missing"
        return $false
    }
    else {
        $resolvedWatcher = Resolve-ActionPath (Join-Path $resolvedWorking $expandedWatcher)
    }
    $expectedWatcher = Resolve-ActionPath $WatcherPath
    if ([string]::IsNullOrWhiteSpace($resolvedWatcher) -or
        -not $resolvedWatcher.Equals($expectedWatcher, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    return $true
}

function Get-LegacyTaskCandidates {
    # Legacy matching uses [IO.Path]::GetFullPath and the complete action contract,
    # equivalent to New-ScheduledTaskAction -Execute plus its exact Arguments.
    $result = @()
    $roots = @(Get-ZCodeRoots)
    if ($roots.Count -eq 0) {
        return $result
    }
    try {
        $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    }
    catch {
        $script:LegacyTaskWarnings += "legacy-task-enumeration-failed"
        return $result
    }
    $seen = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
    foreach ($task in $tasks) {
        if ([string]$task.TaskName -eq $script:TaskName) {
            continue
        }
        $actions = @($task.Actions)
        if ($actions.Count -ne 1) {
            continue
        }
        $taskPath = Get-NormalizedTaskPath ([string]$task.TaskPath)
        $taskKey = ([string]$task.TaskName) + "`n" + $taskPath
        if (-not $seen.Add($taskKey)) {
            continue
        }
        foreach ($root in $roots) {
            $watcherPath = Join-Path $root "task-watch\watch.py"
            if (Test-LegacyWatcherAction -Action $actions[0] -WatcherPath $watcherPath -ZCodeRoot $root) {
                $result += [pscustomobject]@{
                    Task = $task
                    TaskName = [string]$task.TaskName
                    TaskPath = $taskPath
                    ScriptPath = (Resolve-Path -LiteralPath $watcherPath -ErrorAction Stop).Path
                }
                break
            }
        }
    }
    return $result
}

function Suspend-VerifiedLegacyTasks {
    param([string]$Directory)

    $candidates = @(Get-LegacyTaskCandidates)
    if ($script:LegacyTaskWarnings.Count -gt 0) {
        throw "The existing legacy watcher tasks could not be verified"
    }
    foreach ($entry in $candidates) {
        $name = [string]$entry.TaskName
        $path = Get-NormalizedTaskPath ([string]$entry.TaskPath)
        $record = [pscustomobject]@{
            TaskName = $name
            TaskPath = $path
            Enabled = Get-TaskEnabled $entry.Task
            Running = Get-TaskRunning $entry.Task
            BackupPath = $null
            Suspended = $false
        }
        $script:LegacyTaskRecords += $record
        $safeName = ($name -replace '[^A-Za-z0-9._-]', '_')
        $xmlPath = Join-Path $Directory ("legacy-" + $safeName + "-" + (Get-TaskPathHash $path) + ".xml")
        $xml = Export-ScheduledTask -TaskName $name -TaskPath $path
        Write-TaskBackup -Path $xmlPath -Xml ([string]$xml)
        $record.BackupPath = $xmlPath
        if ([string]$entry.Task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop
        }
        if ([bool]$record.Enabled) {
            Disable-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop | Out-Null
            $record.Suspended = $true
        }
    }
}

function Restore-LegacyTaskRecords {
    foreach ($record in @($script:LegacyTaskRecords)) {
        $name = [string]$record.TaskName
        $path = Get-NormalizedTaskPath ([string]$record.TaskPath)
        try {
            $existing = @(Get-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop)
        }
        catch {
            $existing = @()
        }
        if ($null -ne $record.BackupPath -and
            (Test-Path -LiteralPath $record.BackupPath -PathType Leaf)) {
            $xml = [IO.File]::ReadAllText($record.BackupPath, (New-Object Text.UTF8Encoding($false)))
            Register-ScheduledTask -TaskName $name -TaskPath $path -Xml $xml -Force -ErrorAction Stop | Out-Null
        }
        if ([bool]$record.Enabled) {
            Enable-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop | Out-Null
        }
        else {
            Disable-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop | Out-Null
        }
        if ([bool]$record.Running) {
            Start-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop
        }
    }
}

function Register-NotifierTask {
    param([string]$AppPath, [string]$ConfigPath, [string]$StatePath)

    $action = New-NotifierTaskAction -AppPath $AppPath -ConfigPath $ConfigPath -StatePath $StatePath
    $script:NotifierTriggerAt = (Get-Date).AddMinutes(1)
    $trigger = New-ScheduledTaskTrigger -Once -At $script:NotifierTriggerAt -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $taskPath = if ($null -ne $script:PreviousTask) { Get-NormalizedTaskPath $script:PreviousTask.TaskPath } else { "\" }
    $script:RegisteredTaskPath = $taskPath
    # Set before registration so a partially-created task is removed during rollback.
    $script:TaskRegistered = $true
    Register-ScheduledTask -TaskName $script:TaskName -TaskPath $taskPath -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
}

function Assert-NotifierTaskTrigger {
    $task = Get-ScheduledTask -TaskName $script:TaskName -TaskPath (Get-NormalizedTaskPath $script:RegisteredTaskPath) -ErrorAction Stop
    $triggers = @($task.Triggers)
    if ($triggers.Count -eq 0 -or [string]::IsNullOrWhiteSpace([string]$triggers[0].StartBoundary)) {
        throw "notifier-trigger-missing"
    }
    try {
        $startBoundary = [datetime]::Parse(
            [string]$triggers[0].StartBoundary,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeLocal
        )
    }
    catch {
        throw "notifier-trigger-invalid"
    }
    if ($startBoundary -lt $script:NotifierTriggerAt.AddSeconds(-5)) {
        throw "notifier-trigger-too-soon"
    }
}

function Suspend-CurrentNotifierTask {
    if ([string]::IsNullOrWhiteSpace($script:InstallRoot)) {
        return
    }
    $task = Get-ExistingTask -Root $script:InstallRoot
    if ($null -eq $task) {
        $script:TaskRegistered = $false
        return
    }
    $taskPath = Get-NormalizedTaskPath ([string]$task.TaskPath)
    $failure = $null
    if (Get-TaskRunning $task) {
        try {
            Stop-ScheduledTask -TaskName $script:TaskName -TaskPath $taskPath -ErrorAction Stop
        }
        catch {
            $failure = $_
        }
    }
    if (Get-TaskEnabled $task) {
        try {
            Disable-ScheduledTask -TaskName $script:TaskName -TaskPath $taskPath -ErrorAction Stop | Out-Null
        }
        catch {
            if ($null -eq $failure) {
                $failure = $_
            }
        }
    }
    if ($script:TaskRegistered) {
        try {
            Unregister-ScheduledTask -TaskName $script:TaskName -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
            $script:TaskRegistered = $false
        }
        catch {
            if ($null -eq $failure) {
                $failure = $_
            }
        }
    }
    if ($null -ne $failure) {
        throw "current-task-suspend-failed"
    }
}

function Restore-PreviousTask {
    if ($script:TaskRegistered) {
        Unregister-ScheduledTask -TaskName $script:TaskName -TaskPath (Get-NormalizedTaskPath $script:RegisteredTaskPath) -Confirm:$false -ErrorAction SilentlyContinue
    }
    if ($null -ne $script:PreviousTask -and (Test-Path -LiteralPath $script:PreviousTask.XmlPath -PathType Leaf)) {
        $path = Get-NormalizedTaskPath $script:PreviousTask.TaskPath
        $xml = [IO.File]::ReadAllText($script:PreviousTask.XmlPath, (New-Object Text.UTF8Encoding($false)))
        Register-ScheduledTask -TaskName ([string]$script:PreviousTask.TaskName) -TaskPath $path -Xml $xml -Force -ErrorAction Stop | Out-Null
        if ([bool]$script:PreviousTask.Enabled) {
            Enable-ScheduledTask -TaskName ([string]$script:PreviousTask.TaskName) -TaskPath $path -ErrorAction Stop | Out-Null
        }
        else {
            Disable-ScheduledTask -TaskName ([string]$script:PreviousTask.TaskName) -TaskPath $path -ErrorAction Stop | Out-Null
        }
        if ([bool]$script:PreviousTask.Running) {
            Start-ScheduledTask -TaskName ([string]$script:PreviousTask.TaskName) -TaskPath $path -ErrorAction Stop
        }
    }
}

function Restore-FileBackup {
    param([string]$Path, [string]$BackupPath, [bool]$Existed)
    if ($null -ne $BackupPath -and (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
        Copy-Item -LiteralPath $BackupPath -Destination $Path -Force
    }
    elseif (-not $Existed -and (Test-Path -LiteralPath $Path)) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "The new state path is not a file"
        }
        Remove-Item -LiteralPath $Path -Force
    }
}

function Restore-InstallFiles {
    if ($null -eq $script:InstallRoot) {
        return
    }
    $currentApp = Join-Path $script:InstallRoot "app"
    $oldApp = $script:AppBackupPath
    if ($null -eq $oldApp -and $null -ne $script:BackupRoot) {
        $oldApp = Join-Path $script:BackupRoot "app"
    }
    if ($null -ne $oldApp -and (Test-Path -LiteralPath $oldApp -PathType Container)) {
        if (Test-Path -LiteralPath $currentApp) {
            Remove-Item -LiteralPath $currentApp -Recurse -Force
        }
        Move-Item -LiteralPath $oldApp -Destination $currentApp -Force
    }
    elseif ($script:AppSwitched -and (Test-Path -LiteralPath $currentApp)) {
        Remove-Item -LiteralPath $currentApp -Recurse -Force
    }
    Restore-FileBackup -Path (Join-Path $script:InstallRoot "config.json") -BackupPath $script:ConfigBackup -Existed $script:ConfigExisted
    Restore-FileBackup -Path (Join-Path $script:InstallRoot "state.json") -BackupPath $script:StateBackup -Existed $script:StateExisted
}

function Invoke-Install {
    $script:InstallStage = "preflight-python"
    $sourcePackage = Join-Path $script:SourceRoot "src\zcode_task_notifier"
    if (-not (Test-Path -LiteralPath $sourcePackage -PathType Container)) {
        throw "The source package is incomplete"
    }
    $script:Python = Find-Python
    $script:WindowlessPython = Find-WindowlessPython -ConsolePython $script:Python
    $script:InstallStage = "preflight-weixin"
    Confirm-Weixin
    $script:InstallStage = "preflight-codex"
    $codexEnabled = Select-Codex
    if ($codexEnabled -and -not (Test-WeixinTarget -CodexEnabled $true)) {
        throw "The source check before enabling Codex did not pass"
    }

    $script:InstallStage = "filesystem-prepare"
    $script:InstallRoot = Resolve-InstallRoot -Value $InstallDir
    if (Test-Path -LiteralPath $script:InstallRoot) {
        $installItem = Get-Item -LiteralPath $script:InstallRoot -Force
        if (-not $installItem.PSIsContainer -or (($installItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "The installation directory is not a normal directory"
        }
    }
    New-Item -ItemType Directory -Path $script:InstallRoot -Force | Out-Null
    $appPath = Join-Path $script:InstallRoot "app"
    $configPath = Join-Path $script:InstallRoot "config.json"
    $statePath = Join-Path $script:InstallRoot "state.json"
    $script:StageRoot = Join-Path $script:InstallRoot (".staging-" + [Guid]::NewGuid().ToString("N"))
    $script:BackupRoot = Join-Path $script:InstallRoot ("backup-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ") + "-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:StageRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $script:BackupRoot -Force | Out-Null

    # 旧产品任务必须在首次改写配置、状态或应用前完成备份并暂停。
    $script:InstallStage = "task-backup"
    Save-ExistingTaskXml -Directory $script:BackupRoot -Root $script:InstallRoot | Out-Null
    if (Test-Path -LiteralPath $appPath) {
        $appItem = Get-Item -LiteralPath $appPath -Force
        if (-not $appItem.PSIsContainer -or (($appItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "The existing application path is not a normal directory"
        }
        $script:AppBackupPath = Join-Path $script:BackupRoot "app"
        Move-Item -LiteralPath $appPath -Destination $script:AppBackupPath
    }
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $script:ConfigExisted = $true
        $script:ConfigBackup = Join-Path $script:BackupRoot "config.json"
        Copy-Item -LiteralPath $configPath -Destination $script:ConfigBackup
    }
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $script:StateExisted = $true
        $script:StateBackup = Join-Path $script:BackupRoot "state.json"
        Copy-Item -LiteralPath $statePath -Destination $script:StateBackup
    }
    $script:InstallStage = "config-write"
    $config = Read-OrCreateConfig -Path $configPath -CodexEnabled $codexEnabled
    Write-JsonAtomic -Path $configPath -Value $config

    $script:InstallStage = "app-stage"
    $stageApp = Join-Path $script:StageRoot "app"
    New-Item -ItemType Directory -Path $stageApp -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $script:SourceRoot "src\zcode_task_notifier") -Destination $stageApp -Recurse

    $snapshot = Get-MigrationSnapshot
    if ($null -ne $snapshot) {
        $script:InstallStage = "state-migrate"
        Invoke-PythonModule -ModuleRoot $stageApp -Arguments @("-m", "zcode_task_notifier", "migrate", "--snapshot", $snapshot, "--state", $statePath)
    }
    $script:InstallStage = "state-baseline"
    Invoke-PythonModule -ModuleRoot $stageApp -Arguments @("-m", "zcode_task_notifier", "baseline", "--config", $configPath, "--state", $statePath, "--json")

    $script:InstallStage = "app-switch"
    if (Test-Path -LiteralPath $appPath) {
        throw "The old application is still present before the directory switch"
    }
    Move-Item -LiteralPath $stageApp -Destination $appPath
    $script:AppSwitched = $true
    $script:InstallStage = "task-register"
    Register-NotifierTask -AppPath $appPath -ConfigPath $configPath -StatePath $statePath
    $script:InstallStage = "doctor"
    $doctorResult = Invoke-PythonModule -ModuleRoot $appPath -Arguments @("-m", "zcode_task_notifier", "doctor", "--config", $configPath, "--state", $statePath, "--json") -Capture
    if ($doctorResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($doctorResult.Output)) {
        throw "doctor-check-failed"
    }
    try {
        $doctorPayload = $doctorResult.Output | ConvertFrom-Json
        if ($null -eq $doctorPayload -or $null -eq $doctorPayload.healthy) {
            throw "doctor-payload-invalid"
        }
    }
    catch {
        throw "doctor-payload-invalid"
    }
    $script:InstallStage = "trigger-verify"
    Assert-NotifierTaskTrigger
    # 新任务已完成 doctor，且首个 trigger 至少在一分钟后；最后才提交旧 watcher 停用。
    $script:InstallStage = "legacy-suspend"
    Suspend-VerifiedLegacyTasks -Directory $script:BackupRoot

    $script:InstallStage = "cleanup"
    if (Test-Path -LiteralPath $script:StageRoot) {
        Remove-Item -LiteralPath $script:StageRoot -Recurse -Force
    }
    Write-Host "Feature: explicit install skill and one-click installation"
    $codexText = if ($codexEnabled) { "enabled" } else { "disabled" }
    Write-Host ("Installation complete: task ZCodeTaskNotifier is registered; Codex: " + $codexText)
    Write-Host ("Doctor: healthy=" + ([string][bool]$doctorPayload.healthy))
    Write-Host ("Task Scheduler: " + $script:TaskName + " registered; first trigger=" + ([string]$script:NotifierTriggerAt))
    Write-Host "The upgrade backup is retained; run doctor for a redacted health result."
}

try {
    Invoke-Install
    exit 0
}
catch {
    $failedStage = $script:InstallStage
    $failureType = $_.Exception.GetType().Name
    $rollbackFailed = $false
    try {
        Suspend-CurrentNotifierTask
    }
    catch {
        $rollbackFailed = $true
    }
    try {
        Restore-InstallFiles
    }
    catch {
        $rollbackFailed = $true
    }
    try {
        Restore-PreviousTask
    }
    catch {
        $rollbackFailed = $true
    }
    try {
        Restore-LegacyTaskRecords
    }
    catch {
        $rollbackFailed = $true
    }
    try {
        if ($null -ne $script:StageRoot -and (Test-Path -LiteralPath $script:StageRoot)) {
            Remove-Item -LiteralPath $script:StageRoot -Recurse -Force
        }
    }
    catch {
        $rollbackFailed = $true
    }
    if ($rollbackFailed) {
        [Console]::Error.WriteLine("Rollback did not complete")
    }
    [Console]::Error.WriteLine("Installation failed: " + $failureType + "; stage=" + $failedStage)
    exit 1
}

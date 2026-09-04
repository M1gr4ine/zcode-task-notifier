[CmdletBinding()]
param(
    [switch]$KeepData,
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"
$taskName = "ZCodeTaskNotifier"
$productName = "ZCodeTaskNotifier"

function Get-LocalApplicationData {
    $value = [Environment]::GetFolderPath('LocalApplicationData')
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Cannot determine the local application data directory"
    }
    return $value
}

function Normalize-InstallRoot {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        $Value = Join-Path (Get-LocalApplicationData) $productName
    }
    try {
        $candidate = ([IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Value))).TrimEnd([IO.Path]::DirectorySeparatorChar)
        $localRoot = ([IO.Path]::GetFullPath((Get-LocalApplicationData))).TrimEnd([IO.Path]::DirectorySeparatorChar)
    }
    catch {
        throw "Cannot normalize the product directory"
    }
    $prefix = $localRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The product directory is outside local application data"
    }
    if ((Split-Path -Leaf $candidate) -ne $productName) {
        throw "The product directory name check failed"
    }
    return $candidate
}

function Resolve-InstallRoot {
    param([string]$Value)
    return Normalize-InstallRoot $Value
}

function Get-ProductRoot {
    $candidate = Resolve-InstallRoot $InstallDir
    if (-not (Test-Path -LiteralPath $candidate)) {
        return $candidate
    }
    try {
        $item = Get-Item -LiteralPath $candidate -Force
    }
    catch {
        throw "The product directory cannot be inspected"
    }
    if (-not $item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "The product path is not a normal directory"
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "The product path is not a directory"
    }
    try {
        $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
    }
    catch {
        throw "The product directory cannot be resolved"
    }
    if (-not $resolved.Equals($candidate, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The resolved product directory does not match"
    }
    return $candidate
}

function Get-SafeChildDirectory {
    param([string]$Root, [string]$Name)
    $candidate = Join-Path $Root $Name
    if (-not (Test-Path -LiteralPath $candidate)) {
        return $null
    }
    $item = Get-Item -LiteralPath $candidate -Force
    if (-not $item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "The application path is not a normal directory"
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "The application path is not a directory"
    }
    $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
    if (-not $resolved.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $resolved) -ne $Name) {
        throw "The application path boundary check failed"
    }
    return $resolved
}

function Assert-NoDescendantReparsePoints {
    param([Parameter(Mandatory = $true)][string]$Root)
    # Enumerate one level at a time and never recurse through a reparse point.
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        try {
            $children = @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)
        }
        catch {
            throw "descendant-enumeration-failed"
        }
        foreach ($child in $children) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "descendant-reparse-point"
            }
            if ($child.PSIsContainer) {
                $pending.Push($child.FullName)
            }
        }
    }
}

function Remove-ProductApp {
    param([string]$Root)
    $app = Get-SafeChildDirectory -Root $Root -Name "app"
    if ($null -ne $app) {
        Assert-NoDescendantReparsePoints -Root $app
        Remove-Item -LiteralPath $app -Recurse -Force
    }
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
    # Task Scheduler reports a missing task as ERROR_FILE_NOT_FOUND.
    return [int64]$exception.HResult -eq -2147024894
}

function Resolve-TaskActionPath {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($Value.Trim().Trim('"'))
        return ([IO.Path]::GetFullPath($expanded)).TrimEnd([IO.Path]::DirectorySeparatorChar)
    }
    catch {
        return $null
    }
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
    return Resolve-TaskActionPath $value
}

function Test-TaskActionBelongsToRoot {
    param($Task, [string]$Root)
    if ($null -eq $Task) {
        return $false
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        return $false
    }
    $action = $actions[0]
    $expectedApp = Resolve-TaskActionPath (Join-Path $Root "app")
    $workingDirectory = Resolve-TaskActionPath ([string]$action.WorkingDirectory)
    if ([string]::IsNullOrWhiteSpace($workingDirectory) -or
        -not $workingDirectory.Equals($expectedApp, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $arguments = [Environment]::ExpandEnvironmentVariables([string]$action.Arguments).Trim()
    if ($arguments -notmatch '(?i)(?:^|\s)-m\s+zcode_task_notifier\s+run(?:\s|$)') {
        return $false
    }
    $expectedConfig = Resolve-TaskActionPath (Join-Path $Root "config.json")
    $expectedState = Resolve-TaskActionPath (Join-Path $Root "state.json")
    $config = Get-TaskArgumentPath -Arguments $arguments -Name "--config"
    $state = Get-TaskArgumentPath -Arguments $arguments -Name "--state"
    return $null -ne $config -and $null -ne $state -and
        $config.Equals($expectedConfig, [StringComparison]::OrdinalIgnoreCase) -and
        $state.Equals($expectedState, [StringComparison]::OrdinalIgnoreCase)
}

function Get-ProductTask {
    param([string]$Root)
    try {
        $tasks = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
    }
    catch {
        if (Test-TaskNotFoundError $_) {
            return $null
        }
        throw "task-enumeration-failed"
    }
    if ($tasks.Count -gt 1) {
        throw "task-duplicate"
    }
    if ($tasks.Count -eq 0) {
        return $null
    }
    if (-not (Test-TaskActionBelongsToRoot -Task $tasks[0] -Root $Root)) {
        throw "task-root-mismatch"
    }
    return $tasks[0]
}

function Unregister-ProductTask {
    param([string]$Root)
    $task = Get-ProductTask -Root $Root
    if ($null -eq $task) {
        return
    }
    Unregister-ScheduledTask -TaskName ([string]$task.TaskName) -TaskPath ([string]$task.TaskPath) -Confirm:$false -ErrorAction Stop
}

try {
    $root = Get-ProductRoot
    # Do not touch the fixed task name unless the exact product root exists.
    if (Test-Path -LiteralPath $root -PathType Container) {
        Unregister-ProductTask -Root $root
    }
    if ($KeepData) {
        Remove-ProductApp -Root $root
        Write-Host "ZCodeTaskNotifier was removed; local runtime data was kept."
        exit 0
    }

    $answer = Read-Host "Also delete local state and logs for this product? [y/N]"
    if ($answer.Trim().ToLowerInvariant() -eq "y" -or $answer.Trim().ToLowerInvariant() -eq "yes") {
        if (Test-Path -LiteralPath $root) {
            Assert-NoDescendantReparsePoints -Root $root
            Remove-Item -LiteralPath $root -Recurse -Force
        }
        Write-Host "ZCodeTaskNotifier and its local data were removed."
    }
    else {
        Remove-ProductApp -Root $root
        Write-Host "ZCodeTaskNotifier was removed; local runtime data was kept."
    }
    exit 0
}
catch {
    Write-Error ("Uninstallation failed: " + $_.Exception.GetType().Name)
    exit 1
}

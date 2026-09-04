[CmdletBinding()]
param(
    [switch]$History
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$violations = New-Object System.Collections.Generic.List[object]
$script:MaxBlobBytes = [int64]20971520

function Add-Violation {
    param([string]$Location, [string]$Rule)
    $violations.Add([pscustomobject]@{ Location = $Location; Rule = $Rule })
}

function ConvertTo-RelativePath {
    param([string]$Path)
    $base = [IO.Path]::GetFullPath($repoRoot)
    if (-not $base.EndsWith([IO.Path]::DirectorySeparatorChar)) {
        $base += [IO.Path]::DirectorySeparatorChar
    }
    $target = [IO.Path]::GetFullPath($Path)
    $baseUri = New-Object Uri($base)
    $targetUri = New-Object Uri($target)
    return [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

function Is-IgnoredWorkingDirectory {
    param([string]$Path)
    $relative = ConvertTo-RelativePath $Path
    $parts = $relative -split '[\\/]'
    foreach ($part in $parts) {
        if ($part -in @('.git', '.venv', '__pycache__', '.pytest_cache', 'target')) {
            return $true
        }
    }
    return $false
}

function Test-PathRules {
    param([string]$Path)
    try {
        $leaf = [IO.Path]::GetFileName($Path).ToLowerInvariant()
    }
    catch {
        return "invalid-path"
    }
    # Runtime SQLite files use the suffix family below.
    if ($leaf -match '(?i)\.(?:sqlite|db)') {
        return 'runtime-data'
    }
    foreach ($suffix in @('.jsonl', '.log', '.lock', '.pyc')) {
        if ($leaf.EndsWith($suffix, [StringComparison]::OrdinalIgnoreCase)) {
            return 'runtime-data'
        }
    }
    if ($leaf -match '^(?:state|credentials|bot-config|bot-state[^.]*|snapshot)\.json$' -or
        $leaf -match '^state\.json\.migrate-backup-[A-Za-z0-9-]+\.json$') {
        return 'runtime-data'
    }
    return $null
}

function Get-RelativeLabel {
    param([string]$Path)
    try {
        return ConvertTo-RelativePath $Path
    }
    catch {
        return '<working-tree-file>'
    }
}

function Get-TextContent {
    param([string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ([int64]$item.Length -gt $script:MaxBlobBytes) {
            Add-Violation -Location (Get-RelativeLabel $Path) -Rule 'oversize-file'
            return $null
        }
    }
    catch {
        Add-Violation -Location (Get-RelativeLabel $Path) -Rule 'unreadable-file'
        return $null
    }
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
    }
    catch {
        Add-Violation -Location (Get-RelativeLabel $Path) -Rule 'unreadable-file'
        return $null
    }
    if ($bytes.Length -eq 0) {
        return ''
    }
    foreach ($byte in $bytes) {
        if ($byte -eq 0) {
            Add-Violation -Location (Get-RelativeLabel $Path) -Rule 'binary-file'
            return $null
        }
    }
    try {
        $encoding = New-Object Text.UTF8Encoding($false, $true)
        $text = $encoding.GetString($bytes)
        if ($text.IndexOf([char]0xFFFD) -ge 0) {
            Add-Violation -Location (Get-RelativeLabel $Path) -Rule 'invalid-encoding'
            return $null
        }
        return $text
    }
    catch {
        Add-Violation -Location (Get-RelativeLabel $Path) -Rule 'invalid-encoding'
        return $null
    }
}

function Test-TextRules {
    param([string]$Location, [string]$Text)
    if ($null -eq $Text) {
        return
    }
    # Build rule fragments so this source is not matched as a credential.
    $drivePattern = '(?i)(?<![A-Za-z0-9])(?<drive>[A-Z])' + ':' + '[\\/]'
    $uncPattern = '(?i)(?<![A-Za-z0-9%])' + '\\\\' + '[A-Za-z0-9$._-]+' + '\\' + '[A-Za-z0-9$._-]+'
    $userPathPattern = '(?i)' + 'Users' + '[\\/]' + '[^\\/\s]+'
    if ($Text -match $drivePattern -or $Text -match $uncPattern) {
        Add-Violation -Location $Location -Rule 'absolute-path'
    }
    if ($Text -match $userPathPattern) {
        Add-Violation -Location $Location -Rule 'user-path'
    }

    $currentUser = [Environment]::UserName
    if (-not [string]::IsNullOrWhiteSpace($currentUser)) {
        $userPattern = '(?i)(?<![A-Za-z0-9])' + [regex]::Escape($currentUser) + '(?![A-Za-z0-9])'
        if ($Text -match $userPattern) {
            Add-Violation -Location $Location -Rule 'current-username'
        }
    }

    $credentialPrefix = '(?i)(?:providerUserId|credentialRef|botId|token|secret|password)' + '\s*[:=]\s*[' + '"' + "']"
    $credentialMatches = [regex]::Matches($Text, $credentialPrefix)
    foreach ($match in $credentialMatches) {
        $fragment = $Text.Substring($match.Index, [Math]::Min(96, $Text.Length - $match.Index))
        if ($fragment -notmatch '(?i)example|synthetic|opaque-test-value|placeholder') {
            Add-Violation -Location $Location -Rule 'credential-assignment'
            break
        }
    }

    $uuidPattern = '(?i)' + 'bot-' + '(?!example(?:[-_]|$))' + '[0-9a-f]{8}-[0-9a-f-]{27,}'
    $encryptedPattern = 'enc:v1:' + '(?!opaque-test-value|synthetic)' + '[A-Za-z0-9+/=_-]{16,}'
    $wechatIdPattern = '(?i)' + 'wxid_' + '[a-z0-9_-]{6,}'
    $secretAssignment = '(?i)(?:token|secret|password)' + '\s*[:=]\s*[' + '"' + "'][A-Za-z0-9+/=_-]{32,}['" + "']"
    if ($Text -match $uuidPattern) {
        Add-Violation -Location $Location -Rule 'bot-identifier'
    }
    if ($Text -match $encryptedPattern) {
        Add-Violation -Location $Location -Rule 'encrypted-credential'
    }
    if ($Text -match $wechatIdPattern) {
        Add-Violation -Location $Location -Rule 'wechat-user-id'
    }
    if ($Text -match $secretAssignment) {
        Add-Violation -Location $Location -Rule 'high-entropy-assignment'
    }
}

function Read-NullDelimitedRecord {
    param(
        [Parameter(Mandatory = $true)][IO.Stream]$Stream,
        [int]$MaxBytes = 1048576
    )
    $bytes = New-Object 'System.Collections.Generic.List[byte]'
    while ($true) {
        $value = $Stream.ReadByte()
        if ($value -lt 0) {
            if ($bytes.Count -eq 0) {
                return $null
            }
            break
        }
        if ($value -eq 0) {
            break
        }
        if ($bytes.Count -ge $MaxBytes) {
            throw 'git path record is too large'
        }
        [void]$bytes.Add([byte]$value)
    }
    try {
        return (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes.ToArray())
    }
    catch {
        throw 'git path record is not valid UTF-8'
    }
}

function Get-IgnoredRuntimeFiles {
    # Git 递归列出被忽略的路径；这里只检查运行态敏感文件名，不读取正文。
    $startInfo = New-GitProcessStartInfo '-C {repoRoot} ls-files --others --ignored --exclude-standard -z'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $stream = $process.StandardOutput.BaseStream
        while ($true) {
            $relative = Read-NullDelimitedRecord -Stream $stream
            if ($null -eq $relative) {
                break
            }
            if ([string]::IsNullOrWhiteSpace([string]$relative)) {
                continue
            }
            $candidate = Join-Path $repoRoot ([string]$relative)
            if (Is-IgnoredWorkingDirectory -Path $candidate) {
                continue
            }
            if ($null -eq (Test-PathRules ([string]$relative))) {
                continue
            }
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                Write-Output $candidate
            }
            else {
                Add-Violation -Location ([string]$relative) -Rule 'missing-file'
            }
        }
        [void]$process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw 'git ignored-files failed'
        }
    }
    finally {
        Stop-GitProcessIfRunning -Process $process
        $process.Dispose()
    }
}

function Get-WorkingTreeFiles {
    $seen = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
    $startInfo = New-GitProcessStartInfo '-C {repoRoot} ls-files --cached --others --exclude-standard -z'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $stream = $process.StandardOutput.BaseStream
        while ($true) {
            $relative = Read-NullDelimitedRecord -Stream $stream
            if ($null -eq $relative) {
                break
            }
            if ([string]::IsNullOrWhiteSpace([string]$relative)) {
                continue
            }
            try {
                $candidate = Join-Path $repoRoot ([string]$relative)
                $pathRule = Test-PathRules ([string]$relative)
                if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                    if ($null -ne $pathRule) {
                        Add-Violation -Location ([string]$relative) -Rule 'missing-file'
                    }
                    continue
                }
                $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
                if ($seen.Add($resolved)) {
                    Write-Output $resolved
                }
            }
            catch {
                Add-Violation -Location '<working-tree-file>' -Rule 'unresolvable-file'
            }
        }
        [void]$process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw 'git ls-files failed'
        }
    }
    finally {
        Stop-GitProcessIfRunning -Process $process
        $process.Dispose()
    }

    # Ignored files still need runtime-data checks before release.
    foreach ($path in Get-IgnoredRuntimeFiles) {
        try {
            $resolved = (Resolve-Path -LiteralPath $path -ErrorAction Stop).Path
            if ($seen.Add($resolved)) {
                Write-Output $resolved
            }
        }
        catch {
            Add-Violation -Location '<working-tree-file>' -Rule 'unresolvable-file'
        }
    }
}

function New-GitProcessStartInfo {
    param([string]$Arguments)
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'git'
    $quotedRoot = '"' + $repoRoot.Replace('"', '\"') + '"'
    $startInfo.Arguments = $Arguments.Replace('{repoRoot}', $quotedRoot)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    # 头部只有元数据；blob 正文通过 BaseStream 有界读取，再做严格 UTF-8 校验。
    $startInfo.StandardOutputEncoding = New-Object Text.UTF8Encoding($false, $false)
    $startInfo.StandardErrorEncoding = New-Object Text.UTF8Encoding($false, $false)
    return $startInfo
}

function Read-StreamLine {
    param(
        [Parameter(Mandatory = $true)][IO.Stream]$Stream,
        [int]$MaxBytes = 4096
    )
    $bytes = New-Object 'System.Collections.Generic.List[byte]'
    while ($true) {
        $value = $Stream.ReadByte()
        if ($value -lt 0) {
            return $null
        }
        if ($value -eq 10) {
            return [Text.Encoding]::ASCII.GetString($bytes.ToArray())
        }
        if ($bytes.Count -ge $MaxBytes) {
            throw 'git cat-file header is too large'
        }
        [void]$bytes.Add([byte]$value)
    }
}

function Read-StreamBytes {
    param(
        [Parameter(Mandatory = $true)][IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][int64]$Count
    )
    $memory = New-Object IO.MemoryStream
    try {
        $buffer = New-Object byte[] 1048576
        $remaining = $Count
        while ($remaining -gt 0) {
            $requested = [int][Math]::Min([int64]$buffer.Length, $remaining)
            $read = $Stream.Read($buffer, 0, $requested)
            if ($read -le 0) {
                throw 'git cat-file blob is truncated'
            }
            $memory.Write($buffer, 0, $read)
            $remaining -= $read
        }
        return $memory.ToArray()
    }
    finally {
        $memory.Dispose()
    }
}

function Stop-GitProcessIfRunning {
    param([Diagnostics.Process]$Process)
    try {
        if ($null -ne $Process -and -not $Process.HasExited) {
            $Process.Kill()
            $Process.WaitForExit()
        }
    }
    catch {
        # 进程清理失败仍由调用方输出固定 PRIVACY_CHECK_ERROR。
    }
}

function Get-HistoryObjectMetadata {
    param([string]$ObjectId)
    $startInfo = New-GitProcessStartInfo '-C {repoRoot} cat-file --batch-check'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $process.StandardInput.WriteLine($ObjectId)
        $process.StandardInput.Close()
        # --batch-check 只返回一行 type/size 元数据，不能读取原始 blob 正文。
        $line = $process.StandardOutput.ReadLine()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($line)) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-exit'
            return $null
        }
        $header = ([string]$line).TrimEnd("`r")
        if ($header -notmatch '^\S+\s+(?<type>\S+)(?:\s+(?<size>\d+))?$') {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-header'
            return $null
        }
        $type = $Matches['type']
        if ($type -eq 'missing') {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-missing'
            return $null
        }
        if ([string]::IsNullOrWhiteSpace($Matches['size'])) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-size'
            return $null
        }
        try {
            $size = [int64]$Matches['size']
        }
        catch {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-size'
            return $null
        }
        return [pscustomobject]@{ Type = $type; Size = $size }
    }
    catch {
        Add-Violation -Location ('git:' + $ObjectId) -Rule 'git cat-file failed'
        return $null
    }
    finally {
        Stop-GitProcessIfRunning -Process $process
        $process.Dispose()
    }
}

function Get-HistoryBlobText {
    param([string]$ObjectId)
    $metadata = Get-HistoryObjectMetadata -ObjectId $ObjectId
    if ($null -eq $metadata) {
        return $null
    }
    if ($metadata.Type -ne 'blob') {
        if ($metadata.Type -notin @('commit', 'tree', 'tag')) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-type'
        }
        return $null
    }
    # 先由 --batch-check 得到大小；超限对象不启动原始内容读取。
    if ([int64]$metadata.Size -gt $script:MaxBlobBytes) {
        Add-Violation -Location ('git:' + $ObjectId) -Rule 'oversize-object'
        return $null
    }

    $startInfo = New-GitProcessStartInfo '-C {repoRoot} cat-file --batch'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $process.StandardInput.WriteLine($ObjectId)
        $process.StandardInput.Close()
        $stream = $process.StandardOutput.BaseStream
        $header = Read-StreamLine -Stream $stream
        if ([string]::IsNullOrWhiteSpace($header) -or
            $header -notmatch '^\S+\s+(?<type>\S+)(?:\s+(?<size>\d+))?$') {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-header'
            return $null
        }
        if ($Matches['type'] -ne 'blob' -or [int64]$Matches['size'] -ne [int64]$metadata.Size) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-size-mismatch'
            return $null
        }
        $bodyBytes = Read-StreamBytes -Stream $stream -Count ([int64]$metadata.Size)
        if ($stream.ReadByte() -ne 10) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-trailer'
            return $null
        }
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'cat-file-exit'
            return $null
        }
        if ([Array]::IndexOf($bodyBytes, [byte]0) -ge 0) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'binary-file'
            return $null
        }
        try {
            $body = (New-Object Text.UTF8Encoding($false, $true)).GetString($bodyBytes)
        }
        catch {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'invalid-encoding'
            return $null
        }
        if ($body.IndexOf([char]0xFFFD) -ge 0) {
            Add-Violation -Location ('git:' + $ObjectId) -Rule 'invalid-encoding'
            return $null
        }
        return $body
    }
    catch {
        Add-Violation -Location ('git:' + $ObjectId) -Rule 'git cat-file failed'
        return $null
    }
    finally {
        Stop-GitProcessIfRunning -Process $process
        $process.Dispose()
    }
}

function Scan-WorkingTree {
    foreach ($path in Get-WorkingTreeFiles) {
        $label = Get-RelativeLabel $path
        $pathRule = Test-PathRules $path
        if ($null -ne $pathRule) {
            Add-Violation -Location $label -Rule $pathRule
            continue
        }
        $text = Get-TextContent $path
        Test-TextRules -Location $label -Text $text
    }
}

function Scan-GitHistory {
    $startInfo = New-GitProcessStartInfo '-C {repoRoot} rev-list --objects --all'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $seen = New-Object System.Collections.Generic.HashSet[string]
    try {
        [void]$process.Start()
        $reader = $process.StandardOutput
        while (($line = $reader.ReadLine()) -ne $null) {
            if ([string]::IsNullOrWhiteSpace([string]$line)) {
                continue
            }
            $pieces = ([string]$line).Split(' ', 2)
            if ($pieces.Count -lt 1 -or $pieces[0] -notmatch '^[0-9a-fA-F]{7,64}$') {
                Add-Violation -Location 'git:<unknown>' -Rule 'rev-list-format'
                continue
            }
            $objectId = $pieces[0]
            $historyPath = if ($pieces.Count -gt 1) { $pieces[1] } else { $null }
            if (-not $seen.Add($objectId)) {
                continue
            }
            if ($null -ne $historyPath) {
                $pathRule = Test-PathRules $historyPath
                if ($null -ne $pathRule) {
                    Add-Violation -Location ('git:' + $objectId) -Rule $pathRule
                    continue
                }
            }
            $text = Get-HistoryBlobText -ObjectId $objectId
            Test-TextRules -Location ('git:' + $objectId) -Text $text
        }
        [void]$process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw 'git rev-list failed'
        }
    }
    finally {
        Stop-GitProcessIfRunning -Process $process
        $process.Dispose()
    }
}

try {
    Scan-WorkingTree
    if ($History) {
        Scan-GitHistory
    }
    if ($violations.Count -gt 0) {
        # 不把路径、对象 ID 或读取错误细节写到终端；调用方只依赖稳定错误码。
        Write-Host 'Privacy check failed [PRIVACY_CHECK_VIOLATION]'
        exit 1
    }
    if ($History) {
        Write-Host ([string]::Concat([char]0x5de5, [char]0x4f5c, [char]0x6811, [char]0x4e0e, ' Git ', [char]0x5386, [char]0x53f2, [char]0x9690, [char]0x79c1, [char]0x68c0, [char]0x67e5, [char]0x901a, [char]0x8fc7))
    }
    else {
        Write-Host ([string]::Concat([char]0x9690, [char]0x79c1, [char]0x68c0, [char]0x67e5, [char]0x901a, [char]0x8fc7))
    }
    exit 0
}
catch {
    # 不回显异常类型、路径或异常正文，避免将本地信息带入日志。
    Write-Host 'Privacy check could not complete [PRIVACY_CHECK_ERROR]'
    exit 2
}

param([switch]$Offline, [switch]$CheckOnly, [string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$runtimeRoot = Join-Path $projectRoot 'runtime'
$reportPath = Join-Path $projectRoot 'storage\creator\logs\runtime-health.json'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json

function Assert-CreatorChildPath {
    param([string]$Path, [string]$Parent)
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $resolvedParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    if (-not $resolvedPath.StartsWith($resolvedParent, [StringComparison]::OrdinalIgnoreCase)) { throw '安装目标超出应用目录，已停止。' }
    return $resolvedPath
}

function Get-CreatorDownload {
    param([object]$Item, [string]$Destination)
    $Destination = Assert-CreatorChildPath -Path $Destination -Parent $runtimeRoot
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    if ((Test-Path -LiteralPath $Destination -PathType Leaf) -and ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Item.sha256)) { return $Destination }
    if ($Offline -or $CheckOnly) { throw '首次使用需要联网准备组件，或使用完整离线包。' }
    if (-not $Item.url.StartsWith('https://')) { throw '组件下载必须使用 HTTPS。' }
    $partialPath = $Destination + '.part'
    $curlPath = Join-Path $env:SystemRoot 'System32\curl.exe'
    Write-Host '正在准备应用专用运行环境；不会安装到系统目录。'
    if (Test-Path -LiteralPath $curlPath -PathType Leaf) {
        & $curlPath --fail --location --connect-timeout 20 --max-time 1800 --speed-limit 1024 --speed-time 30 --silent --show-error --output $partialPath --url $Item.url
        if ($LASTEXITCODE -ne 0) {
            Write-Host '正在尝试备用下载方式；仍会验证文件来源和完整性。'
            $ProgressPreference = 'SilentlyContinue'
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $Item.url -OutFile $partialPath -UseBasicParsing -TimeoutSec 1800
        }
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Item.url -OutFile $partialPath -UseBasicParsing -TimeoutSec 1800
    }
    if ((Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Item.sha256) { throw '下载校验未通过，文件未安装，请检查网络后重试。' }
    Move-Item -LiteralPath $partialPath -Destination $Destination -Force
    return $Destination
}

if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { throw '此双击启动器适用于 Windows 10/11 64 位系统。' }
if (-not $PythonPath) {
    $ownPython = Join-Path $runtimeRoot 'python\python.exe'
    $legacyPython = Join-Path (Split-Path -Parent $projectRoot) 'lib\python\python.exe'
    if (Test-Path -LiteralPath $ownPython -PathType Leaf) { $PythonPath = $ownPython }
    elseif (($Offline -or $CheckOnly) -and (Test-Path -LiteralPath $legacyPython -PathType Leaf)) { $PythonPath = $legacyPython }
    else {
        $archivePath = Get-CreatorDownload -Item $manifest.python -Destination (Join-Path $runtimeRoot 'cache\python.tar.gz')
        $tarPath = Join-Path $env:SystemRoot 'System32\tar.exe'
        if (-not (Test-Path -LiteralPath $tarPath -PathType Leaf)) { throw '未找到 Windows 解压工具，请使用 Windows 10/11 的完整便携包。' }
        $stagingPath = Assert-CreatorChildPath -Path (Join-Path $runtimeRoot ('setup-staging\python-' + [Guid]::NewGuid().ToString('N'))) -Parent $runtimeRoot
        New-Item -ItemType Directory -Path $stagingPath -Force | Out-Null
        $archiveEntries = & $tarPath -tzf $archivePath
        if ($LASTEXITCODE -ne 0) { throw '运行环境压缩包无法读取。' }
        foreach ($entry in $archiveEntries) {
            if ($entry -match '(^[/\\])|(^[A-Za-z]:)|((^|[/\\])\.\.([/\\]|$))' -or -not $entry.StartsWith('python/')) { throw '压缩包路径不符合安装要求，已停止。' }
        }
        & $tarPath -xzf $archivePath -C $stagingPath
        if ($LASTEXITCODE -ne 0) { throw '运行环境解压失败，请检查剩余空间后重试。' }
        $sourcePython = Assert-CreatorChildPath -Path (Join-Path $stagingPath 'python') -Parent $stagingPath
        $targetPython = Assert-CreatorChildPath -Path (Join-Path $runtimeRoot 'python') -Parent $runtimeRoot
        if (Test-Path -LiteralPath $targetPython) { throw '应用运行环境目录已存在但不完整，安装器不会覆盖它；请保留该目录并联系支持。' }
        if (-not (Test-Path -LiteralPath (Join-Path $sourcePython 'python.exe') -PathType Leaf)) { throw '下载的运行环境缺少启动程序。' }
        Move-Item -LiteralPath $sourcePython -Destination $targetPython
        $PythonPath = Join-Path $targetPython 'python.exe'
    }
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw '应用 Python 不存在，请重新解压完整便携包。' }
New-Item -ItemType Directory -Path (Split-Path -Parent $reportPath) -Force | Out-Null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONNOUSERSITE = '1'
$action = if ($CheckOnly) { 'check' } else { 'prepare' }
$setupArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'portable_entry.py'), $action, '--report', $reportPath)
if ($Offline) { $setupArguments += '--offline' }
& $PythonPath @setupArguments
if ($LASTEXITCODE -ne 0) { throw "应用组件准备未完成。详情已保存到：$reportPath" }
Write-Output $reportPath

param([switch]$NoBrowser, [switch]$Offline, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$setupPath = Join-Path $PSScriptRoot 'creator\setup_creator.ps1'
& $setupPath -Offline:$Offline -CheckOnly:$CheckOnly
$healthPath = Join-Path $projectRoot 'storage\creator\logs\runtime-health.json'
$runtimeHealth = Get-Content -LiteralPath $healthPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $runtimeHealth.ready) { throw '运行环境尚未完整准备，请再次双击启动并查看提示。' }
if ($CheckOnly) { Write-Output '运行环境已就绪。'; return }
$pythonPath = $runtimeHealth.python
foreach ($property in $runtimeHealth.environment.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process')
}
$logRoot = Join-Path $projectRoot 'storage\creator\logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$port = 8501
while ($port -lt 8600) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { break }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
    if ($owner.CommandLine -and $owner.CommandLine.Contains($projectRoot) -and $owner.CommandLine.Contains('streamlit')) { break }
    $port++
}
if ($port -eq 8600) { throw '没有可用端口，请关闭不需要的本地服务后重试。' }
if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
    $mainPath = Join-Path $projectRoot 'webui\Main.py'
    $arguments = @('-m','streamlit','run',('"' + $mainPath + '"'),'--server.address=127.0.0.1',"--server.port=$port",'--browser.gatherUsageStats=False','--server.headless=True','--server.maxUploadSize=1024')
    Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'webui.stdout.log') -RedirectStandardError (Join-Path $logRoot 'webui.stderr.log') | Out-Null
}
$url = "http://127.0.0.1:$port/?workspace=creator"
$deadline = (Get-Date).AddSeconds(45)
$ready = $false
Add-Type -AssemblyName System.Net.Http
$healthHandler = [System.Net.Http.HttpClientHandler]::new()
$healthHandler.UseProxy = $false
$healthClient = [System.Net.Http.HttpClient]::new($healthHandler)
$healthClient.Timeout = [TimeSpan]::FromSeconds(2)
try {
    do {
        $healthResponse = $null
        try {
            $healthResponse = $healthClient.GetAsync("http://127.0.0.1:$port/_stcore/health").GetAwaiter().GetResult()
            if ($healthResponse.IsSuccessStatusCode) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
        finally { if ($healthResponse) { $healthResponse.Dispose() } }
    } while ((Get-Date) -lt $deadline)
} finally {
    $healthClient.Dispose()
    $healthHandler.Dispose()
}
if (-not $ready) { throw '工作台尚未启动，请查看 storage/creator/logs 中的启动日志。' }
Set-Content -LiteralPath (Join-Path $logRoot 'address.txt') -Value $url -Encoding UTF8
if (-not $NoBrowser) { Start-Process -FilePath $url }
Write-Output $url

param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$portableRoot = Split-Path -Parent $projectRoot
$pythonPath = Join-Path $portableRoot 'lib\python\python.exe'
$ffmpegPath = Join-Path $portableRoot 'lib\ffmpeg\ffmpeg-7.0-essentials_build\ffmpeg.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '未找到 MoneyPrinterTurbo 自带的 Python，请在便携版目录中启动。' }
$env:PYTHONPATH = $projectRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HUB_DISABLE_XET = '1'
$env:FFMPEG_BINARY = $ffmpegPath
$env:IMAGEIO_FFMPEG_EXE = $ffmpegPath
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

param(
    [string]$DataRoot = 'D:\duix_avatar_data',
    [ValidateSet('docker.io', 'docker.m.daocloud.io', 'docker.1ms.run')][string]$Registry = 'docker.io',
    [switch]$SkipPull,
    [ValidateRange(10, 1800)][int]$ReadyTimeoutSeconds = 300
)
$ErrorActionPreference = 'Stop'
$containerName = 'duix-avatar-gen-video'
$pinnedImage = 'guiji2025/duix.avatar@sha256:1970424d219cbb6aebc7566f069041f057ccad618a395139dce002e1fb25d5ed'
$imageReference = if ($Registry -eq 'docker.io') { $pinnedImage } else { $Registry + '/' + $pinnedImage }
$acceptedImageDigests = @($pinnedImage, ('docker.io/' + $pinnedImage), ('docker.m.daocloud.io/' + $pinnedImage), ('docker.1ms.run/' + $pinnedImage))
$composePath = Join-Path $PSScriptRoot 'docker-compose.avatar.yml'

# Do not replace existing containers, reset Docker, or alter model files.
# The only host directory mounted into the image is the existing face2face data.
if (-not [IO.Path]::IsPathRooted($DataRoot)) { throw '数字人数据目录必须是绝对路径。' }
$resolvedDataRoot = [IO.Path]::GetFullPath($DataRoot)
$sharedPath = Join-Path $resolvedDataRoot 'face2face'
if (-not (Test-Path -LiteralPath $sharedPath -PathType Container)) {
    throw "未找到已有的数字人共享目录：$sharedPath。请指定现有 Duix 数据目录，不要新建空模型目录。"
}
if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) { throw '恢复配置文件不完整，请重新安装创作工作台。' }

$dockerPath = $null
$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($dockerCommand) { $dockerPath = $dockerCommand.Source }
if (-not $dockerPath) {
    $dockerCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe')
    )
    foreach ($candidate in $dockerCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $dockerPath = $candidate; break }
    }
}
if (-not $dockerPath) { throw '未找到 Docker Desktop，请先安装并启动它。' }

function Invoke-CreatorDocker {
    param([string[]]$Arguments)
    # Windows PowerShell 5 treats native stderr progress as ErrorRecord objects.
    # The actual exit status decides success; pull progress must not abort setup.
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $dockerPath @Arguments 2>&1
        $dockerExitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousErrorPreference }
    if ($dockerExitCode -ne 0) {
        throw ("Docker 操作失败：" + ($output -join [Environment]::NewLine))
    }
    return $output
}

function Normalize-CreatorPath {
    param([string]$Value)
    return $Value.Replace('\', '/').TrimEnd('/').ToLowerInvariant()
}

function Test-CreatorAvatarImageIdentity {
    param([object]$Image)
    # The Docker/containerd store reports its manifest as Id; the classic store
    # reports the configuration digest instead. A tag alone proves neither.
    $manifestDigest = 'sha256:1970424d219cbb6aebc7566f069041f057ccad618a395139dce002e1fb25d5ed'
    $configDigest = 'sha256:24aeba09f70b4a8ba0badae9290704990018462a92a81d6f0e0d8466676f271d'
    $verifiedLocalTag = 'guiji2025/duix.avatar:creator-verified-1970424d219c'
    $expectedLayers = @(
        'sha256:3681c53cb0cd24bbb06d42fdb32c9f1dd5d49d75e85d773e5a8116439e53d84e',
        'sha256:4038060c6d9326c24ba4c132cfa4490c8ccfe03093d14917536e2d11faad098b'
    )
    if (-not $Image -or $Image.Os -ne 'linux' -or $Image.Architecture -ne 'amd64' -or @($manifestDigest, $configDigest) -notcontains $Image.Id) { return $false }
    $actualLayers = @($Image.RootFS.Layers)
    if ($actualLayers.Count -ne 2 -or $actualLayers[0] -ne $expectedLayers[0] -or $actualLayers[1] -ne $expectedLayers[1]) { return $false }
    if (@($Image.RepoDigests | Where-Object { $acceptedImageDigests -contains $_ }).Count -gt 0) { return $true }
    $hasVerifiedTag = @($Image.RepoTags) -contains $verifiedLocalTag -or @($Image.RepoTags) -contains ('docker.io/' + $verifiedLocalTag)
    if (-not $hasVerifiedTag) { return $false }
    if ($Image.Descriptor -and $Image.Descriptor.digest -ne $manifestDigest) { return $false }
    return $Image.Descriptor.digest -eq $manifestDigest -or $Image.Id -eq $configDigest -or $Image.Id -eq $manifestDigest
}

function Get-CreatorCachedAvatarImage {
    param([string]$PreferredReference)
    $candidates = @($PreferredReference, 'guiji2025/duix.avatar:creator-verified-1970424d219c')
    foreach ($reference in $candidates) {
        $candidateImage = $null
        try { $candidateImage = (Invoke-CreatorDocker -Arguments @('image', 'inspect', $reference) | ConvertFrom-Json)[0] }
        catch { continue }
        if (Test-CreatorAvatarImageIdentity $candidateImage) { return $candidateImage }
    }
    return $null
}

$serverVersion = Invoke-CreatorDocker -Arguments @('info', '--format', '{{.ServerVersion}}')
Write-Output ("Docker 已连接：" + ($serverVersion -join ''))
Invoke-CreatorDocker -Arguments @('compose', 'version', '--short') | Out-Null
$existingNames = @(Invoke-CreatorDocker -Arguments @('ps', '-a', '--format', '{{.Names}}'))
$existingContainer = $null
if ($existingNames -contains $containerName) {
    $existingContainer = (Invoke-CreatorDocker -Arguments @('inspect', $containerName) | ConvertFrom-Json)[0]
    $existingImage = (Invoke-CreatorDocker -Arguments @('image', 'inspect', $existingContainer.Image) | ConvertFrom-Json)[0]
    $dataMount = @($existingContainer.Mounts | Where-Object { $_.Destination -eq '/code/data' })
    $bindings = @($existingContainer.HostConfig.PortBindings.'8383/tcp')
    $gpuRequests = @($existingContainer.HostConfig.DeviceRequests | Where-Object { $null -ne $_ -and ($_.Capabilities | ForEach-Object { $_ }) -contains 'gpu' })
    $hasGpu = ($existingContainer.HostConfig.Runtime -eq 'nvidia') -or $gpuRequests.Count -gt 0
    $matchingImage = Test-CreatorAvatarImageIdentity $existingImage
    $matchingMount = $dataMount.Count -eq 1 -and $dataMount[0].Type -eq 'bind' -and (Normalize-CreatorPath $dataMount[0].Source) -eq (Normalize-CreatorPath $sharedPath)
    # Restored official Duix containers may already publish 8383 on all IPv4
    # interfaces. Reuse that existing localhost route without changing it;
    # newly created containers still bind only 127.0.0.1 in our Compose file.
    $matchingPort = $bindings.Count -eq 1 -and $bindings[0].HostPort -eq '8383' -and ($bindings[0].HostIp -in @('', '0.0.0.0', '127.0.0.1'))
    if (-not ($matchingImage -and $matchingMount -and $matchingPort -and $hasGpu)) {
        $projectName = $existingContainer.Config.Labels.'com.docker.compose.project'
        throw "已存在同名数字人容器（项目：$projectName），镜像、共享目录、端口或 GPU 配置与创作工作台不同。已保留该容器及全部模型，请检查现有部署。"
    }
    if (-not $existingContainer.State.Running) {
        Invoke-CreatorDocker -Arguments @('start', $containerName) | Out-Null
    }
    Write-Output '已复用现有数字人口型容器。'
} else {
    $portInUse = Get-NetTCPConnection -State Listen -LocalPort 8383 -ErrorAction SilentlyContinue
    if ($portInUse) { throw '8383 端口正被其他服务使用。现有服务已保留，请处理端口冲突后重试。' }
    if ($SkipPull) {
        $cachedImage = Get-CreatorCachedAvatarImage $imageReference
        if (-not $cachedImage) { throw '未找到通过固定摘要校验的数字人口型镜像。请先下载官方固定版本，或加载经过完整校验的离线归档。' }
    } else {
        Write-Output '正在下载官方数字人口型镜像；配音和识别镜像无需下载。'
        Invoke-CreatorDocker -Arguments @('pull', $imageReference) | ForEach-Object { Write-Output $_ }
        $cachedImage = Get-CreatorCachedAvatarImage $imageReference
        if (-not $cachedImage) { throw '数字人口型镜像未通过固定版本校验，未创建容器。' }
    }
    $previousSharedPath = [Environment]::GetEnvironmentVariable('DUIX_CREATOR_FACE2FACE', 'Process')
    $previousImageReference = [Environment]::GetEnvironmentVariable('DUIX_CREATOR_IMAGE', 'Process')
    try {
        $env:DUIX_CREATOR_FACE2FACE = $sharedPath.Replace('\', '/')
        # Use the daemon's immutable content ID after validation. This also
        # supports verified OCI archives without inventing registry RepoDigests.
        $env:DUIX_CREATOR_IMAGE = $cachedImage.Id
        Invoke-CreatorDocker -Arguments @('compose', '-f', $composePath, 'config', '--quiet') | Out-Null
        Invoke-CreatorDocker -Arguments @('compose', '-f', $composePath, 'up', '-d', '--no-deps', '--pull', 'never', $containerName) | ForEach-Object { Write-Output $_ }
    } finally {
        [Environment]::SetEnvironmentVariable('DUIX_CREATOR_FACE2FACE', $previousSharedPath, 'Process')
        [Environment]::SetEnvironmentVariable('DUIX_CREATOR_IMAGE', $previousImageReference, 'Process')
    }
}

# Local services must not use the user's system HTTP proxy.
Add-Type -AssemblyName System.Net.Http
$healthHandler = [System.Net.Http.HttpClientHandler]::new()
$healthHandler.UseProxy = $false
$healthClient = [System.Net.Http.HttpClient]::new($healthHandler)
$healthClient.Timeout = [TimeSpan]::FromSeconds(3)
$deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
$ready = $false
Write-Output '正在等待口型引擎加载。'
try {
    do {
        $healthResponse = $null
        try {
            $healthResponse = $healthClient.GetAsync('http://127.0.0.1:8383/easy/query?code=creator-health-probe').GetAwaiter().GetResult()
            if ($healthResponse.IsSuccessStatusCode) {
                $healthBody = $healthResponse.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
                $ready = $null -ne $healthBody.PSObject.Properties['code']
            }
        } catch { $ready = $false }
        finally { if ($healthResponse) { $healthResponse.Dispose() } }
        if (-not $ready) { Start-Sleep -Seconds 2 }
    } while (-not $ready -and (Get-Date) -lt $deadline)
} finally {
    $healthClient.Dispose()
    $healthHandler.Dispose()
}
if (-not $ready) {
    throw '数字人容器已保留，但口型引擎尚未就绪。请检查 Docker 中 duix-avatar-gen-video 的日志后重试；已有模型未作修改。'
}
Write-Output '数字人口型引擎已就绪，可返回工作台生成口播视频。'

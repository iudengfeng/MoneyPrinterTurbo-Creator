# MoneyPrinterTurbo Creator 安装说明

本仓库发布增强版源代码，不包含 Windows 便携安装包、Duix 模型、账号登录数据或个人 API 配置。下面的命令在 Windows PowerShell 中运行；从项目根目录启动。其他系统尚未完成这套增强流程的整体验证。

## 可以使用的六个步骤

| 步骤 | 功能 | 主要依赖 |
| --- | --- | --- |
| 1. 选题和文案 | 参考库、账号定位、AI 选题、仿写、原创与稿件编辑 | AI 功能使用自己配置的文案模型；手工稿件不需要接口 |
| 2. 配音 | Edge 音色、短试听与完整配音、语速调整、已有 Duix 音色、VoxCPM 云端克隆 | Edge 需要联网；Duix 和 VoxCPM 需要各自的可用服务 |
| 3. 数字人 | 确认完整配音，选择或导入视频形象，生成口播视频 | 独立安装的 Duix 口型引擎、已有模型和共享目录、可用的 Docker GPU 环境 |
| 4. 模板剪辑 | Whisper 字幕、四种包装、背景音乐、画幅、调色、定时画中画 | faster-whisper、Node.js、Remotion、FFmpeg |
| 5. 标题和封面 | AI 标题、正文与话题、真实视频截帧、四种封面排版、上传封面 | AI 标题需要文案模型；手工文字与本机封面不需要接口 |
| 6. 发布中心 | 抖音和小红书账号、独立文案、发布预览、素材 ZIP、逐项确认发布 | 本地预览和 ZIP 无需账号；浏览器发布需要登录、文案模型与 Playwright MCP |

使用方法及各步骤的限制见[创作工作台说明](creator-workspace.md)。可以先用本地视频完成剪辑、封面和素材包，再配置数字人或发布账号。

## 准备环境

- Git、Python 3.11、Node.js 22 或更新版本，以及 npm。Python 项目声明为 `>=3.11`，本文采用已验证的 3.11 环境；Node.js 已在 24 上实测。两个 Node 组件自身没有声明 `engines`，其中锁定的 Playwright 依赖要求 Node.js 至少为 20，本文的安装基线为 22。
- FFmpeg 和 ffprobe，解压后把包含两个程序的目录加入 `PATH`。便携版可使用它自带的 FFmpeg。
- 已安装的 Microsoft Edge 或 Google Chrome，用于剪辑渲染和账号专用浏览器。Node.js 也要能从 `PATH` 找到，单独设置发布组件的 Node 路径不能代替剪辑的这个要求。
- 可联网下载 Python/npm 依赖以及首次使用的 Whisper 模型。Duix 仅在使用本机克隆配音或数字人时需要；增加系统内存不能代替口型引擎需要的 GPU 环境。

源码方式先检查当前终端能够找到工具：

```powershell
py -3.11 --version
node --version
npm --version
ffmpeg -version
ffprobe -version
```

## 方式一：从源码安装

在希望保存代码的目录打开 PowerShell，克隆增强版：

```powershell
git clone https://github.com/iudengfeng/MoneyPrinterTurbo-Creator.git
Set-Location .\MoneyPrinterTurbo-Creator
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
& .\.venv\Scripts\python.exe -m pip install -r .\creator-requirements.txt
```

`requirements.txt` 安装主程序和 `faster-whisper`。增强版补充文件 `creator-requirements.txt` 安装固定版本的 `yt-dlp`；两个文件都需要。每条安装命令成功后再继续下一条。

安装两个 Node 组件的锁定依赖：

```powershell
Push-Location .\creator-renderer
npm ci
Pop-Location
Push-Location .\creator-browser
npm ci
Pop-Location
```

首次创建配置；已有 `config.toml` 时这段命令会保留它：

```powershell
if (-not (Test-Path -LiteralPath '.\config.toml')) {
    Copy-Item -LiteralPath '.\config.example.toml' -Destination '.\config.toml'
}
```

在同一终端启动：

```powershell
$env:PYTHONPATH = (Get-Location).Path
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HUB_DISABLE_XET = '1'
$env:FFMPEG_BINARY = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
$env:IMAGEIO_FFMPEG_EXE = $env:FFMPEG_BINARY
$env:MPT_FFPROBE = (Get-Command ffprobe.exe -ErrorAction Stop).Source
& .\.venv\Scripts\python.exe -m streamlit run .\webui\Main.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true --server.maxUploadSize=1024 --browser.gatherUsageStats=false
```

浏览器打开 [http://127.0.0.1:8501/?workspace=creator](http://127.0.0.1:8501/?workspace=creator)。这个终端保持运行；停止时按 `Ctrl+C`。若 8501 已被占用，修改命令中的端口并使用相应网址。

## 方式二：使用已有 Windows 便携版环境

已有可正常使用的 MoneyPrinterTurbo 便携版时，可以在它的根目录另建增强版代码目录，保留原项目。`scripts/start_creator.ps1` 按以下相对位置查找 Python 和 FFmpeg，单独克隆源码后不具备这些文件：

```text
便携版根目录/
├─ lib/
│  ├─ python/python.exe
│  └─ ffmpeg/ffmpeg-7.0-essentials_build/
│     ├─ ffmpeg.exe
│     └─ ffprobe.exe
├─ MoneyPrinterTurbo/          原项目，可保留
└─ MoneyPrinterTurbo-Creator/  此增强版代码
   ├─ scripts/start_creator.ps1
   └─ webui/Main.py
```

在含有 `lib` 的便携版根目录打开 PowerShell：

```powershell
git clone https://github.com/iudengfeng/MoneyPrinterTurbo-Creator.git
Set-Location .\MoneyPrinterTurbo-Creator
$creatorPortablePython = (Resolve-Path '..\lib\python\python.exe').Path
& $creatorPortablePython -m pip install -r .\creator-requirements.txt
Push-Location .\creator-renderer
npm ci
Pop-Location
Push-Location .\creator-browser
npm ci
Pop-Location
if (-not (Test-Path -LiteralPath '.\config.toml')) {
    Copy-Item -LiteralPath '.\config.example.toml' -Destination '.\config.toml'
}
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_creator.ps1
```

这条路径复用便携版已安装的主程序依赖，只补装增强版 Python 依赖。旧便携版若缺少主程序依赖或版本不兼容，请使用方式一创建独立环境。启动器不安装依赖，默认使用 8501；被其他服务占用时会寻找可用端口，并打开实际地址。地址和启动日志保存在 `storage/creator/logs`。

复用原来的 API 设置时，在首次启动前把原项目的私人 `config.toml` 复制到新代码目录；新目录已有配置则先备份并自行合并。不要用 `config.example.toml` 覆盖个人配置。原项目的成片和历史记录不会因为克隆代码自动迁移，迁移 `storage/creator` 前应关闭相关程序并备份。

## 首次配置和模型缓存

在工作台的“基础设置”选择文案模型，填写自己的 API Key、模型和服务地址。选题、文案、AI 标题与浏览器发布使用这里的设置。Edge 标准音色无需 Key，但需要联网；VoxCPM 云端克隆还需要配音设置中的对应 Key 和模型 ID。

Whisper 默认使用 CPU、`int8` 和 `small` 模型。第一次口播提取或自动字幕会下载模型到 `storage/creator/models/faster-whisper`，后续复用缓存。也可以提供完整的 faster-whisper/CTranslate2 格式模型目录 `models/whisper-small` 或 `storage/creator/models/whisper-small`；需要完整配置、权重和词表，单个 OpenAI Whisper `.pt` 文件不能直接替代。下载失败应检查模型源连接和磁盘空间，再重新执行任务。

剪辑会尝试使用已安装的 Edge/Chrome；未找到时 Remotion 可能需要首次下载浏览器。需要明确指定已有浏览器时，在启动前设置 `MPT_RENDER_BROWSER`（剪辑）和 `MPT_BROWSER_PATH`（发布）为浏览器可执行文件的完整路径。发布组件还支持 `MPT_NODE_PATH`。这些设置只在当前终端及其启动的程序中生效。

## Duix 是独立的本机依赖

本仓库不分发 Duix 客户端、模型、Docker 镜像或语音服务。下载 Duix 客户端后，还要分别确认所需引擎和模型可用：

- 数字人口型使用 `duix-avatar-gen-video`、已有 `face2face` 共享数据和 `http://127.0.0.1:8383/easy` 服务。需要能运行其 GPU 容器的 Docker 环境；单独口型生成不需要 ASR/TTS。
- Duix 已有音色的本机克隆配音还需要原有 ASR/TTS 容器，以及已有融影接入服务目录中的 `server.py`。该外部服务不随本仓库提供。没有这一部署时，可先使用 Edge 配音或自行配置 VoxCPM 云端接口。
- 导入样音只保存待创建素材；本机新音色仍需在 Duix 中创建，再刷新音色列表。

现有接入使用 `MPT_FUSION_ROOT` 指定本机接入目录，并读取其 `settings.json`。默认路径来自既有部署，换电脑需要覆盖为自己的实际路径。下面只是字段示例，所有 `E:/...` 路径都需要替换，不能照抄为空目录：

```json
{
  "python": "E:/YourPython/python.exe",
  "ffmpeg": "E:/YourFFmpeg/ffmpeg.exe",
  "hey_db": "E:/YourDuix/biz.db",
  "port": 18600,
  "avatar_root": "E:/YourDuixData/face2face/temp",
  "avatar_url": "http://127.0.0.1:8383/easy"
}
```

`python` 是运行既有融影服务的 Python；`hey_db` 是 Duix 的实际数据库，工作台只读取已有音色和人物。也可以用 `MPT_DUIX_DB` 单独指定数据库。启动工作台前在同一终端设置接入目录：

```powershell
$env:MPT_FUSION_ROOT = 'E:\YourExistingDuixBridge'
```

若已有模型和共享数据，仅缺少口型容器，可使用仓库的脚本；`-DataRoot` 指向已经包含 `face2face` 的真实 Duix 数据目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\creator\setup_avatar.ps1 -DataRoot 'E:\YourExistingDuixData'
```

这一步会拉取固定摘要的镜像并检查服务；脚本不创建空模型目录，不覆盖配置不同的已有容器。可加 `-Registry docker.1ms.run` 选择脚本支持的镜像源。完成后仍需确保 `settings.json` 的 `avatar_root` 对应同一共享目录中的 `temp`。首次部署 Duix 本身应遵循其官方安装说明。

## 账号、数据和验证范围

在发布中心添加账号备注，打开专用浏览器，由自己扫码登录，再点击“检查登录状态”。登录检查不会调用文案模型。创建发布预览仅保存本地文件和文字；逐项点击“确认发布到该账号”后才会启动发布任务，任务文案与观察到的平台页面会交给所配置的模型。没有账号也可以导出包含实际视频、封面和文字的 ZIP，手工上传。

`config.toml` 保存个人设置。稿件、声音样本、成片、任务、账号专用浏览器目录和默认模型缓存位于 `storage/creator`；账号目录为 `storage/creator/publisher_profiles`。可用 `MPT_CREATOR_DATA` 改数据根目录，放到其他位置后也应独立保护和备份。

仓库已经忽略 `config.toml`、`storage/`、`models/`、`.env` 和依赖目录。**不要提交私人配置及其备份、API Key、数据库、账号 profile、Cookie、声音样本、视频或模型。** 更新增强版前备份配置和数据；使用本增强版仓库更新，原项目更新器可能覆盖增强版入口。

已在 Windows、Python 3.11、Node.js 24 和 Edge 上验证配音、数字人口播、Whisper 字幕、音轨合成、封面、步骤交接及 ZIP 文件一致性；发布协议和登录状态也经过本地模拟检查。已验证真实抖音扫码登录复用和官方上传页识别，**没有在验收中上传或提交真实作品，小红书真实账号流程尚未实测**。平台页面、验证码、提交结果和审核仍需实际检查；结果待核查时先查看平台作品列表，避免重复发布。

没有抖音开放平台授权时使用网页搜索和自己的参考库，不提供虚构播放量或实时爆款榜。授权搜索接口目前只通过模拟响应验证。详细验证记录与功能边界见[创作工作台说明](creator-workspace.md)。

# Windows 安装与首次启动

新用户下载 **Windows 联网双击启动包**并完整解压后，双击根目录的 **启动数字人口播.cmd**。不要在压缩包内运行，也不要只复制一个启动文件。可以放在带中文或空格的可写文件夹；首次使用保持网络和启动窗口打开，无需预先安装 Python、Node.js 或原 MoneyPrinterTurbo 客户端。

启动器先检查组件，准备渲染和发布所需浏览器；缺少的运行组件会联网下载到应用自身目录，校验后安装，再打开本机创作页面。它不会安装全局 Python、Node 或浏览器，不会访问发布账号，也不会调用付费模型、生成数字人或自动发布。下载中断后再次双击即可继续检查已下载的完整组件。

本版 Windows 下载包是轻量联网启动包，包含程序、空密钥示例和授权字体；源码 ZIP 也提供同一入口。第一次需要下载独立运行环境和依赖，已准备的完整组件以后直接检查并复用。**首次启动仍需联网；FFmpeg 在用户电脑下载，不作为本项目的 MIT 文件重新分发。** 网络失败会显示提示并保留已有资料。

## 打开软件后

“数字人口播”默认打开四列首页：**01 IP深度学习 → 02 口播制作 → 03 视频处理 → 04 发布制作**。先导入或编辑文案，选择声音与形象，再生成口播和成片，最后核对标题、封面与发布账号。已有口播视频可以直接导入处理；作品库和客户／品牌档案在左下方，原有工具继续保留。

首页“生成成片”可以继续准备作品所需的前置阶段，也可以先逐步生成、试听和预览。“一键创作”工具保留视频目的、客户／品牌资料、模板和声音设置，门店、商品、服务及知识创作共用流程。

主流程仍然是五步：文案 → 声音 → 画面 → 成片 → 发布资料。既可以一键完成，也可以逐步预览和修改。模板里的商品、价格、案例都是示例，不会直接变成你的经营事实。目标时长用于估算口播稿长，真实时长由实际配音决定。

第一次建议用现成文案、标准音色和图文画面试做；商品介绍、素材混剪须上传真实图片或视频。AI 写稿需要在“设置”填入自己的文案模型服务和密钥，费用由所选服务决定。标准音色不需要密钥，但配音时需要联网。浏览器发布还需要文案模型配置、用户登录平台并逐项确认；也可以先导出发布资料包自行上传。

当前发布只接入抖音和小红书，快手、视频号不可选择。定时按钮只下载手动发布清单。数字人需要另外配置可用的Duix服务、自己的形象及相应硬件环境；下载包不带数字人模型、人物视频或声音样本。

## 安装包目录约定

```text
应用目录/
├─ 启动数字人口播.cmd
├─ 检查运行环境.cmd
├─ scripts/start_creator.ps1
├─ scripts/creator/
│  ├─ setup_creator.ps1
│  ├─ portable_entry.py
│  └─ runtime-manifest.json
├─ runtime/
│  ├─ python/python.exe             Python 3.11 独立运行环境
│  ├─ node/node.exe                 Node 22/24，完整发行目录
│  ├─ ffmpeg/ffmpeg.exe             首次启动下载
│  ├─ ffmpeg/ffprobe.exe            与 FFmpeg 配套
│  └─ browser/                     渲染和发布所需的浏览器缓存
├─ creator-hyperframes/node_modules/
├─ creator-browser/node_modules/
├─ resource/fonts/                 Noto Sans SC 及 OFL 许可
├─ models/whisper-small/           可选离线识别模型
├─ config.example.toml             空密钥示例
└─ storage/creator/                使用后创建的客户资料和作品
   └─ vendor/scrapling/            独立依赖目录，不是客户账号数据
```

安装器锁定下载地址与 SHA-256；版本及来源见 `runtime-manifest.json`。Python 使用 [Astral 的独立 CPython 构建](https://github.com/astral-sh/python-build-standalone)，Node 使用 [Node.js 官方发行](https://nodejs.org/dist/)，FFmpeg 使用 [FFmpeg 官方下载页列出的 Windows 构建商](https://ffmpeg.org/download.html#build-windows)。中文字体来自 [Google Fonts 的 Noto Sans SC](https://github.com/google/fonts/tree/main/ofl/notosanssc)，保留 SIL Open Font License。应用不会分发机器上的微软雅黑或黑体文件。

Python/npm 主依赖保留仓库固定版本；Scrapling 单独安装到 `storage/creator/vendor/scrapling`，避免改变已有配音依赖。不需要为公开参考读取安装另一个爬虫浏览器，不使用发布账号 Cookie。来源可在口播参考库中添加，默认关闭自动更新。

普通首次启动使用应用独立环境。离线检查或启动时也可识别父目录中的旧 `lib/python` 和 `lib/ffmpeg`，仅检查和复用完整旧环境，不对旧版共享 Python 执行安装；缺少组件时请联网正常启动。

## 配置、数据与更新

第一次准备完成时，从 `config.example.toml` 创建 `config.toml`；已有配置不会覆盖。安装器不会复制开发者配置、账号 Cookie、私人数据库、声音样本、人物模板或素材。

客户的品牌档案、文案、作品和专用浏览器账号资料保存在 `storage/creator`。更新前关闭软件并备份自己的 `config.toml` 和 `storage/creator`；不要把这些文件上传到 GitHub。新版文件应解压到新目录，再按需迁移自己的数据，避免用整包覆盖现有客户文件。

Python、Node、FFmpeg、FFprobe、渲染浏览器的路径由启动器按当前解压目录设置，包括 `FFMPEG_BINARY`、`MPT_FFPROBE`、`MPT_NODE_PATH`、`HYPERFRAMES_BROWSER_PATH` 和 `PLAYWRIGHT_BROWSERS_PATH`。不需要手工修改系统 PATH。默认只监听 `127.0.0.1`；8501 被其他应用占用时会找空闲端口并打开实际地址。

## 识别模型与数字人

Whisper 默认使用 CPU、int8、small。识别模型不属于基本运行组件；第一次文案提取或自动字幕会下载到 `storage/creator/models/faster-whisper`，以后复用。完整离线模型也可以放在 `models/whisper-small`，需要配置、权重、词表等全部文件；单独的 `.pt` 文件不能替代。

**Duix 是额外配置，不包含在基本启动包中。** 数字人口型需要客户自己的形象素材、Duix 共享目录、可用的口型服务以及相应 Docker/GPU 环境。自己的 Duix 音色还需要实际可运行的语音服务；下载一个客户端或只启动口型容器，不代表这些能力已经就绪。

本包不带商业人物模板、他人声音样本或配音服务密钥。没有 Duix 时可以先使用文字讲解画面、本地素材及标准音色，或者导入已经生成的口播视频。具体接入目录通过 `MPT_FUSION_ROOT` 指定；原有部署里的盘符只是用户机器配置，不能照搬成新用户默认地址。

## 检查与排障

双击 **检查运行环境.cmd** 可检查已准备的组件，不下载模型、不读取密钥、不登录或发布。检查结果保存在 `storage/creator/logs/runtime-health.json`，启动日志位于同一目录；网址保存在 `address.txt`。`-Offline` 只检查或启动已经完整准备的环境，不会补齐缺少的组件；标准配音、AI 写稿和平台发布各自仍需要联网。

需要终端操作时，在应用目录运行：

```powershell
# 仅检查已经准备好的组件
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_creator.ps1 -CheckOnly -Offline

# 完成首次准备并打开；保留当前窗口可查看安装提示
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_creator.ps1

# 已准备好后仅启动本机服务，不打开浏览器
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_creator.ps1 -Offline -NoBrowser
```

无法下载时先检查网络和剩余磁盘空间，再次双击。下载校验失败的文件不会安装。启动器不会删除已有作品、重置登录或替换不完整的旧运行环境目录；遇到目录冲突会明确停止，以便保留现场和数据。

Windows 10/11 64 位是这套双击启动器的目标平台。其他系统、所有平台的真实发布账号和任意 Duix 部署仍需分别验证；运行环境检查成功只表示基本组件可以运行，不等于付费接口、数字人或平台发布已经配置好。

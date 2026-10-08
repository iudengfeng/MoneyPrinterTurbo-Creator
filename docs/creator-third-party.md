# 组件来源与许可

项目代码许可见根目录LICENSE，第三方组件不因本项目使用MIT而改变许可。

Windows 双击启动包用于准备应用专用环境，首次需要联网。包内已有的组件直接复用；缺少的组件按固定清单下载并校验。具体版本、地址和SHA-256见`scripts/creator/runtime-manifest.json`；Python/npm依赖及其许可仍分别适用，下面列出主要组件。

- CPython：[Python Build Standalone](https://github.com/astral-sh/python-build-standalone)，保留Python及构建依赖通知。
- Node.js：[官方发行](https://nodejs.org/dist/)，保留LICENSE。
- FFmpeg／FFprobe：[官方下载页](https://ffmpeg.org/download.html#build-windows) 所列Windows构建商；所选构建含GPL组件，由用户电脑首次下载，不随代码包重新分发。[许可与源码说明](https://ffmpeg.org/legal.html)
- HyperFrames：Apache-2.0；其GSAP依赖使用 [Standard No Charge License](https://gsap.com/community/standard-license/)，不是Apache，商业集成应遵守具体条件。
- Playwright与浏览器：[官方项目](https://github.com/microsoft/playwright)，应用库及浏览器组件分别保留通知。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)／[Whisper](https://github.com/openai/whisper)：模型由使用者首次识别时获取，不包含私人模型和用户录音。
- [Edge-TTS](https://github.com/rany2/edge-tts)：LGPLv3，联网服务条件以供应商为准。
- [Scrapling](https://github.com/D4Vinci/Scrapling)：BSD-3-Clause，安装到隔离目录。
- [Noto Sans SC](https://github.com/google/fonts/tree/main/ofl/notosanssc)：SIL Open Font License，字体与许可一起提供。

不分发微软雅黑、STHeiti、个人音乐、私人Duix数据库、人物视频和样音。Duix属于外部接入，用户须准备适用服务、模型及素材并遵循许可。

运行组件准备好后，不会自动替用户配置收费服务或创建数字人。AI写稿及模型辅助发布使用用户自己的服务配置；标准音色需要联网；Whisper在首次识别时获取模型；Duix口型和自己的声音另需实际可用的服务、形象、音色及相应硬件。安装检查通过仅表示基本组件就绪，不代表所有外部功能已配置或能够离线使用。

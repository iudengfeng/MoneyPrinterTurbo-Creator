"""Prepare/check app-local Windows runtimes without reading customer settings.

Only setup performs downloads. A check is read-only apart from its explicitly
requested JSON report. No model, voice, avatar, account or publication is used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = APP_ROOT / "runtime"
MANIFEST = json.loads(Path(__file__).with_name("runtime-manifest.json").read_text("utf-8"))
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_VERIFIED_BROWSERS = None


def _run(arguments, *, cwd=None, env=None, capture=False, timeout=None):
    process = subprocess.Popen([str(item) for item in arguments], cwd=cwd or APP_ROOT, env=env,
                               creationflags=HIDDEN, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT)
    try:
        output = process.communicate(timeout=timeout)[0] or ""
    except subprocess.TimeoutExpired:
        # Only this command's owned process tree is stopped; an installer must
        # not leave a Chromium downloader alive after its bounded deadline.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           creationflags=HIDDEN, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=15)
        else:
            process.kill()
        process.communicate(timeout=10)
        raise RuntimeError("组件准备超时，请检查网络后重新启动。") from None
    if process.returncode:
        detail = output.strip()[-1200:]
        raise RuntimeError("组件检查或安装失败。" + ("\n" + detail if detail else "请查看安装日志中的提示。"))
    return output.strip()


def _within_app(path):
    if not Path(path).resolve().is_relative_to(APP_ROOT):
        raise RuntimeError("安装目标必须位于应用目录。")
    return Path(path)


def _download_process(arguments, partial, *, env=None):
    """Allow large downloads while terminating a process with no byte progress."""
    process = subprocess.Popen([str(item) for item in arguments], env=env, creationflags=HIDDEN,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    started = updated = time.monotonic()
    previous = -1
    while process.poll() is None:
        size = partial.stat().st_size if partial.exists() else 0
        if size != previous:
            previous, updated = size, time.monotonic()
        if time.monotonic() - updated > 60 or time.monotonic() - started > 1800:
            process.kill()
            process.communicate()
            raise RuntimeError("下载没有进度，请检查网络后重试。")
        time.sleep(.5)
    output = process.communicate()[0].decode("utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError("下载通道未完成：" + output.strip()[-500:])


def _download(item, destination, *, offline=False):
    destination = _within_app(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = item["sha256"].lower()
    if destination.is_file():
        with destination.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() == expected:
                return destination
    if offline:
        raise RuntimeError("离线模式下缺少组件，请联网双击启动一次，或使用已准备完整的离线包。")
    if not item["url"].startswith("https://"):
        raise RuntimeError("安装清单中的下载地址必须使用 HTTPS。")
    partial = destination.with_suffix(destination.suffix + ".part")
    print("正在下载应用组件：" + destination.name, flush=True)
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        try:
            with requests.get(item["url"], timeout=(20, 60), stream=True) as response, partial.open("wb") as output:
                response.raise_for_status()
                for block in response.iter_content(1024 * 1024):
                    output.write(block)
            with partial.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != expected:
                raise RuntimeError("下载校验未通过，文件未安装；请检查网络后重试。")
            partial.replace(destination)
            return destination
        except requests.RequestException:
            print("正在尝试备用下载通道。", flush=True)
    curl = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/curl.exe"
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if powershell.is_file():
        download_env = dict(os.environ, CREATOR_BOOT_URL=item["url"], CREATOR_BOOT_DEST=str(partial.resolve()))
        script = "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;Invoke-WebRequest -Uri $env:CREATOR_BOOT_URL -OutFile $env:CREATOR_BOOT_DEST -UseBasicParsing -TimeoutSec 1800"
        try:
            _download_process([powershell, "-NoProfile", "-NonInteractive", "-Command", script], partial, env=download_env)
            with partial.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != expected:
                raise RuntimeError("下载校验未通过，文件未安装。")
            partial.replace(destination)
            return destination
        except RuntimeError:
            print("正在尝试系统下载通道。", flush=True)
    if curl.is_file():
        try:
            _download_process([curl, "--fail", "--location", "--connect-timeout", "20", "--max-time", "1800",
                  "--speed-limit", "1024", "--speed-time", "30", "--silent", "--show-error",
                  "--output", partial, "--url", item["url"]], partial)
        except (RuntimeError, subprocess.TimeoutExpired):
            # Requests and Windows curl use different TLS implementations.
            # A fallback retains TLS verification and the same pinned hash.
            import requests

            with requests.get(item["url"], timeout=(20, 60), stream=True) as response, partial.open("wb") as output:
                response.raise_for_status()
                for block in response.iter_content(1024 * 1024):
                    output.write(block)
    else:
        with urllib.request.urlopen(item["url"], timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
    with partial.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != expected:
        raise RuntimeError("下载校验未通过，文件未安装；请检查网络后重试。")
    partial.replace(destination)
    return destination


def _unzip(archive, destination):
    destination = _within_app(destination)
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            target = (destination / entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()) or stat.S_ISLNK(entry.external_attr >> 16):
                raise RuntimeError("组件压缩包包含不安全的路径，已停止安装。")
        package.extractall(destination)


def _stage():
    folder = _within_app(RUNTIME / "setup-staging")
    folder.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="install-", dir=folder))


def _copy_new_component(source, destination):
    destination = _within_app(destination)
    if destination.exists():
        if any(destination.iterdir()):
            raise RuntimeError(f"组件目录已存在但不完整：{destination.name}。请保留该目录并联系支持，安装器不会覆盖它。")
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copytree(source, destination)


def _node():
    candidates = [RUNTIME / "node/node.exe"]
    explicit = os.environ.get("MPT_NODE_PATH")
    if explicit:
        candidates.append(Path(explicit))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _media():
    ffmpeg_candidates = [RUNTIME / "ffmpeg/ffmpeg.exe", APP_ROOT.parent / "lib/ffmpeg/ffmpeg-7.0-essentials_build/ffmpeg.exe"]
    ffmpeg = next((path.resolve() for path in ffmpeg_candidates if path.is_file()), None)
    probe_candidates = [RUNTIME / "ffmpeg/ffprobe.exe", APP_ROOT / "creator-hyperframes/bin/ffmpeg/ffprobe.exe"]
    if ffmpeg:
        probe_candidates.append(ffmpeg.with_name("ffprobe.exe"))
    probe = next((path.resolve() for path in probe_candidates if path.is_file()), None)
    return ffmpeg, probe


def _bundled_browser_paths():
    folder = RUNTIME / "browser"
    shell = next(iter(sorted(folder.glob("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"), reverse=True)), None)
    headed = next(iter(sorted(folder.glob("chromium-*/chrome-win64/chrome.exe"), reverse=True)), None)
    direct_shell = folder / "chrome-headless-shell.exe"
    if shell is None and direct_shell.is_file():
        shell = direct_shell
    return shell, headed


def _system_browser_candidates():
    """Official executable locations only; never read a browser user profile."""
    program = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    program86 = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    local = os.environ.get("LOCALAPPDATA")
    candidates = [program86 / "Microsoft/Edge/Application/msedge.exe", program / "Microsoft/Edge/Application/msedge.exe",
                  program / "Google/Chrome/Application/chrome.exe", program86 / "Google/Chrome/Application/chrome.exe"]
    if local:
        candidates.append(Path(local) / "Google/Chrome/Application/chrome.exe")
    return list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))


def _cached_browser_paths():
    """Read executable locations in Playwright's cache, never user profiles."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return [], []
    folder = Path(local) / "ms-playwright"
    shells = sorted((path for path in folder.glob("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe")
                     if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    headed = sorted((path for path in folder.glob("chromium-*/chrome-win64/chrome.exe") if path.is_file()),
                    key=lambda path: path.stat().st_mtime, reverse=True)
    return shells, headed


def _browser_paths():
    if _VERIFIED_BROWSERS is not None:
        render, headed = _VERIFIED_BROWSERS["render"], _VERIFIED_BROWSERS["publish"]
        if render.is_file() and headed.is_file():
            return render, headed
    shell, headed = _bundled_browser_paths()
    cached_shells, cached_headed = _cached_browser_paths()
    system = next(iter(_system_browser_candidates()), None)
    return shell or next(iter(cached_shells), None) or headed or system, headed or system or next(iter(cached_headed), None)


def _hyperframes_browser_probe(browser, env):
    """Ask the unmodified pinned CLI to encode its own tiny alpha composition."""
    component = APP_ROOT / "creator-hyperframes"
    cli = component / "node_modules/hyperframes/bin/hyperframes.mjs"
    gsap = component / "node_modules/gsap/dist/gsap.min.js"
    ffmpeg, ffprobe = _media()
    if not cli.is_file() or not gsap.is_file() or not ffmpeg or not ffprobe:
        raise RuntimeError("HyperFrames 浏览器检查缺少本机渲染或音视频组件。")
    probe_env = dict(env, HYPERFRAMES_BROWSER_PATH=str(browser), PRODUCER_HEADLESS_SHELL_PATH=str(browser),
                     HYPERFRAMES_FFMPEG_PATH=str(ffmpeg), HYPERFRAMES_FFPROBE_PATH=str(ffprobe),
                     HYPERFRAMES_NO_UPDATE_CHECK="1", HYPERFRAMES_NO_TELEMETRY="1", HYPERFRAMES_SKIP_SKILLS="1",
                     DO_NOT_TRACK="1", PRODUCER_ENABLE_STREAMING_ENCODE="true")
    probe_env.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
    with tempfile.TemporaryDirectory(prefix="creator-hyperframes-probe-") as temporary:
        folder = Path(temporary)
        shutil.copy2(gsap, folder / "gsap.min.js")
        (folder / "hyperframes.json").write_text('{"name":"creator-browser-check"}', "utf-8")
        (folder / "index.html").write_text('''<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;background:transparent;width:64px;height:64px}#root{width:64px;height:64px;background:transparent}span{color:white}</style>
</head><body><div id="root" data-composition-id="root" data-start="0" data-duration="0.1" data-width="64" data-height="64" data-fps="30"><span>Creator</span></div>
<script src="gsap.min.js"></script><script>const tl=gsap.timeline({paused:true});tl.to({}, {duration:0.1});window.__timelines={root:tl};window.__renderReady=true;</script></body></html>''', "utf-8")
        output = folder / "probe.webm"
        _run([_node(), cli, "render", folder, "--format", "webm", "--output", output,
              "--fps", "30", "--workers", "1", "--no-browser-gpu", "--no-best-effort",
              "--quality", "standard", "--vp9-cpu-used", "6"], env=probe_env, capture=True, timeout=45)
        if not output.is_file() or output.stat().st_size < 100:
            raise RuntimeError("HyperFrames 浏览器未生成可用透明画面。")
    return True


def _browser_probe(browser, env):
    """Launch an empty isolated context and render pixels, without any network."""
    script = r"""
const {chromium} = require(process.argv[1]);
(async()=>{
  const browser = await chromium.launch({executablePath:process.argv[2],headless:true,chromiumSandbox:true,timeout:25000});
  try {
    const page = await browser.newPage({viewport:{width:320,height:180}});
    await page.setContent('<!doctype html><title>Creator browser check</title><div style="font:20px sans-serif">Creator browser check</div>');
    const pixels = await page.screenshot();
    if(pixels.length < 100)throw new Error('Browser did not render an image');
    console.log(JSON.stringify({version:browser.version(),headless_launch:true,rendered:true}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exit(1);});
"""
    output = _run([_node(), "-e", script, APP_ROOT / "creator-browser/node_modules/playwright", browser],
                  env=env, capture=True, timeout=45)
    if not output.strip():
        raise RuntimeError("浏览器没有返回启动检查结果。")
    report = json.loads(output.splitlines()[-1])
    if not report.get("headless_launch") or not report.get("rendered"):
        raise RuntimeError("浏览器未通过隔离启动和画面检查。")
    try:
        report["hyperframes_rendered"] = _hyperframes_browser_probe(browser, env)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        report["hyperframes_rendered"] = False
        report["hyperframes_error"] = str(exc)
    return report


def _browser_source(path):
    if path.is_relative_to((RUNTIME / "browser").resolve()):
        return "app_chromium"
    cached_shells, cached_headed = _cached_browser_paths()
    if path in {candidate.resolve() for candidate in cached_shells + cached_headed}:
        return "cached_chromium"
    return "system_edge" if path.name.lower() == "msedge.exe" else "system_chrome"


def _select_browsers(env):
    """Select independently verified page and HyperFrames rendering engines."""
    global _VERIFIED_BROWSERS
    shell, bundled_headed = _bundled_browser_paths()
    cached_shells, cached_headed = _cached_browser_paths()
    systems = _system_browser_candidates()
    reports = {}

    def usable(paths, *, render=False):
        for path in dict.fromkeys(value.resolve() for value in paths if value and value.is_file()):
            if path not in reports:
                try:
                    reports[path] = _browser_probe(path, env)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    reports[path] = None
            if reports[path] and (not render or reports[path].get("hyperframes_rendered")):
                return path
        return None

    render = usable([shell, *cached_shells, bundled_headed, *cached_headed, *systems], render=True)
    headed = usable([bundled_headed, *systems, *cached_headed])
    if not render or not headed:
        _VERIFIED_BROWSERS = None
        return None
    _VERIFIED_BROWSERS = {"render": render, "publish": headed, "render_source": _browser_source(render),
                          "publish_source": _browser_source(headed), "versions": {
                              "render": reports[render]["version"], "publish": reports[headed]["version"]}}
    return _VERIFIED_BROWSERS


def _prepare_browsers(env, *, offline=False):
    selected = _select_browsers(env)
    if selected:
        if selected["render_source"].startswith("system_") or selected["publish_source"].startswith("system_"):
            print("已验证本机 Edge／Chrome，使用独立临时窗口，不读取个人浏览器资料。", flush=True)
        if selected["render_source"] == "cached_chromium":
            print("已验证本机缓存中的 Chromium 视频渲染组件，无需重复下载。", flush=True)
        return
    if offline:
        raise RuntimeError("没有可用的视频浏览器。请安装官方 Edge／Chrome，或联网启动下载应用专用浏览器。")
    print("正在下载应用专用浏览器；连接超时会停止，可重新启动重试。", flush=True)
    browser_env = dict(env, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="0", PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT="30000")
    browser_env.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
    _run([_node(), APP_ROOT / "creator-browser/node_modules/playwright/cli.js", "install", "chromium"],
         env=browser_env, timeout=300)
    if not _select_browsers(dict(os.environ, **environment())):
        raise RuntimeError("浏览器下载后未通过启动检查。请重试，或安装官方 Edge／Chrome 后再启动。")


def environment():
    node = _node()
    ffmpeg, probe = _media()
    browser, headed = _browser_paths()
    env = {
        "PYTHONPATH": str(APP_ROOT), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1", "HF_HUB_DISABLE_XET": "1", "HF_HOME": str(APP_ROOT / "storage/creator/models/huggingface"),
        "PIP_CACHE_DIR": str(RUNTIME / "cache/pip"), "npm_config_cache": str(RUNTIME / "cache/npm"),
        "PIP_CONFIG_FILE": os.devnull, "PIP_INDEX_URL": "https://pypi.org/simple", "PIP_EXTRA_INDEX_URL": "",
        "npm_config_registry": "https://registry.npmjs.org", "npm_config_userconfig": str(RUNTIME / "setup.npmrc"),
        "PLAYWRIGHT_BROWSERS_PATH": str(RUNTIME / "browser"), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "HYPERFRAMES_NO_UPDATE_CHECK": "1", "HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1",
    }
    if node:
        env["MPT_NODE_PATH"] = str(node)
    if ffmpeg:
        env["FFMPEG_BINARY"] = env["IMAGEIO_FFMPEG_EXE"] = str(ffmpeg)
    if probe:
        env["MPT_FFPROBE"] = env["HYPERFRAMES_FFPROBE_PATH"] = str(probe)
    if browser:
        env["HYPERFRAMES_BROWSER_PATH"] = env["MPT_RENDER_BROWSER"] = str(browser)
    if headed:
        env["MPT_BROWSER_PATH"] = str(headed)
    font = APP_ROOT / "resource/fonts/NotoSansSC.ttf"
    if font.is_file():
        env["MPT_CREATOR_FONT"] = str(font)
    elif (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc").is_file():
        env["MPT_CREATOR_FONT"] = str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc")
    paths = [str(path.parent) for path in (node, ffmpeg, probe) if path]
    env["PATH"] = os.pathsep.join(paths + [os.environ.get("PATH", "")])
    return env


def _python_dependencies():
    missing = []
    for raw in (APP_ROOT / "requirements.txt").read_text("utf-8").splitlines() + ["yt-dlp==2026.8.19"]:
        line = raw.split("#", 1)[0].strip()
        if not line or ("python_version" in line and sys.version_info < (3, 13)):
            continue
        name, _, expected = line.split(";", 1)[0].strip().partition("==")
        try:
            version = importlib.metadata.version(name)
            if expected and version != expected:
                missing.append(line)
        except importlib.metadata.PackageNotFoundError:
            missing.append(line)
    return missing


def _node_dependencies():
    missing = []
    for component in ("creator-hyperframes", "creator-browser"):
        package = json.loads((APP_ROOT / component / "package.json").read_text("utf-8"))
        for name, version in package["dependencies"].items():
            metadata = APP_ROOT / component / "node_modules" / name / "package.json"
            if not metadata.is_file() or json.loads(metadata.read_text("utf-8"))["version"] != version:
                missing.append(component)
                break
    return missing


def _vendor_ready():
    vendor = APP_ROOT / "storage/creator/vendor/scrapling"
    if not (vendor / "scrapling/__init__.py").is_file():
        return False
    try:
        _run([sys.executable, "-I", "-c", "import sys;sys.path.insert(0,sys.argv[1]);from scrapling.fetchers import Fetcher;print('ok')", vendor], capture=True)
        return True
    except RuntimeError:
        return False


def _initial_config():
    config = APP_ROOT / "config.toml"
    if not config.exists() and not config.is_symlink():
        shutil.copy2(APP_ROOT / "config.example.toml", config)


def prepare(*, offline=False):
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("启动器需要随包提供的 Python 3.11。")
    if not Path(sys.executable).resolve().is_relative_to(RUNTIME.resolve()):
        # Reuse complete old installations, but never run pip with their shared
        # Python, even for a --target install. New installs use private runtime.
        if not inspect()["ready"]:
            raise RuntimeError("旧便携运行环境仅检查和复用，不执行安装；请正常联网启动以准备应用独立环境。")
        _initial_config()
        return
    env = dict(os.environ, **environment())
    if _python_dependencies():
        if offline:
            raise RuntimeError("Python 依赖尚未完整准备，请联网启动完成首次安装。")
        if not Path(sys.executable).resolve().is_relative_to(RUNTIME.resolve()):
            raise RuntimeError("旧便携环境依赖版本不完整；请使用新版便携包，安装器不会改动旧版共享环境。")
        print("正在准备文字、声音与视频组件，首次安装可能需要几分钟。", flush=True)
        try:
            _run([sys.executable, "-m", "pip", "--version"], env=env, capture=True)
        except RuntimeError:
            wheel = _download(MANIFEST["pip"], RUNTIME / "cache/pip-bootstrap.whl", offline=offline)
            _unzip(wheel, Path(sys.executable).parent / "Lib/site-packages")
        _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
              "--only-binary=:all:", "--prefix", "\\\\?\\" + str(Path(sys.executable).parent.resolve()),
              "-r", APP_ROOT / "requirements.txt", "yt-dlp==2026.8.19"], env=env)
    if not _node():
        archive = _download(MANIFEST["node"], RUNTIME / "cache/node.zip", offline=offline)
        stage = _stage()
        _unzip(archive, stage)
        _copy_new_component(stage / MANIFEST["node"]["archive_root"], RUNTIME / "node")
    ffmpeg, probe = _media()
    if not ffmpeg or not probe:
        archive = _download(MANIFEST["ffmpeg"], RUNTIME / "cache/ffmpeg.zip", offline=offline)
        stage = _stage()
        _unzip(archive, stage)
        source = stage / MANIFEST["ffmpeg"]["archive_root"]
        # Preserve the upstream README/licence files alongside downloaded bins.
        _copy_new_component(source, RUNTIME / "ffmpeg-distribution")
        source = RUNTIME / "ffmpeg-distribution"
        binary_dir = RUNTIME / "ffmpeg"
        binary_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("ffmpeg.exe", "ffprobe.exe"):
            target = binary_dir / filename
            if not target.exists():
                shutil.copy2(source / "bin" / filename, target)
    env = dict(os.environ, **environment())
    for component in _node_dependencies():
        if offline:
            raise RuntimeError("字幕或发布组件未完整准备，请联网启动完成首次安装。")
        npm = _node().parent / "node_modules/npm/bin/npm-cli.js"
        if not npm.is_file():
            raise RuntimeError("随包 Node 组件缺少安装工具，请重新解压完整便携包。")
        print("正在准备字幕包装和浏览器组件。", flush=True)
        _run([_node(), npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=APP_ROOT / component, env=env)
    _prepare_browsers(env, offline=offline)
    if not _vendor_ready():
        if offline:
            raise RuntimeError("公开参考读取组件尚未准备，请联网启动完成首次安装。")
        print("正在准备公开参考读取组件。", flush=True)
        vendor_target = _within_app(APP_ROOT / "storage/creator/vendor/scrapling")
        _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
              "--only-binary=:all:", "--target", vendor_target, MANIFEST["scrapling"]], env=env)
    if "font" in MANIFEST:
        font = APP_ROOT / "resource/fonts/NotoSansSC.ttf"
        if not font.is_file():
            _download(MANIFEST["font"], font, offline=offline)
        license_file = APP_ROOT / "resource/fonts/NotoSansSC-OFL.txt"
        if not license_file.is_file():
            _download(MANIFEST["font_license"], license_file, offline=offline)
    _initial_config()


def inspect():
    issues = []
    if sys.version_info[:2] != (3, 11):
        issues.append("需要 Python 3.11")
    if _python_dependencies():
        issues.append("Python 组件未完整准备")
    env = dict(os.environ, **environment())
    node = _node()
    node_version = ""
    if node:
        node_version = _run([node, "--version"], capture=True)
        if int(node_version.lstrip("v").split(".")[0]) not in {22, 24}:
            issues.append("需要 Node 22 或 24")
    else:
        issues.append("缺少应用专用 Node")
    ffmpeg, probe = _media()
    media_versions = {}
    for name, executable in (("ffmpeg", ffmpeg), ("ffprobe", probe)):
        if executable:
            try:
                media_versions[name] = _run([executable, "-version"], env=env, capture=True).splitlines()[0]
            except RuntimeError:
                issues.append(name + " 无法运行，可能缺少同目录 DLL")
        else:
            issues.append("缺少 " + name)
    if ffmpeg and "ffmpeg" in media_versions:
        version_match = re.search(r"ffmpeg version n?(\d+)", media_versions["ffmpeg"])
        if not version_match or int(version_match.group(1)) < 7:
            issues.append("FFmpeg 需要 7 或更新版本")
        try:
            encoders = _run([ffmpeg, "-hide_banner", "-encoders"], env=env, capture=True)
            if any(name not in encoders for name in ("libx264", "aac", "libvpx-vp9", "prores_ks")):
                issues.append("FFmpeg 缺少视频或透明字幕所需的编码器")
        except RuntimeError:
            issues.append("FFmpeg 编码器检查失败")
    missing_node_dependencies = _node_dependencies()
    if missing_node_dependencies:
        issues.append("字幕或发布组件依赖不完整")
    selected_browsers = _select_browsers(env) if node and not missing_node_dependencies else None
    if not selected_browsers:
        issues.append("没有通过隔离启动检查的渲染与发布浏览器")
    env = dict(os.environ, **environment())
    if not _vendor_ready():
        issues.append("公开参考读取组件未就绪")
    if "font" in MANIFEST and not (APP_ROOT / "resource/fonts/NotoSansSC.ttf").is_file():
        issues.append("缺少随应用准备的中文字体")
    if "font_license" in MANIFEST and not (APP_ROOT / "resource/fonts/NotoSansSC-OFL.txt").is_file():
        issues.append("中文字体许可文件缺失")
    if not issues:
        try:
            _run([sys.executable, "-c", "import streamlit, numpy, av, ctranslate2, faster_whisper, PIL, edge_tts;print('ok')"], env=env, capture=True)
            _run([node, APP_ROOT / "creator-browser/worker.mjs", "--check"], env=env, capture=True)
        except RuntimeError as exc:
            issues.append(str(exc))
    return {"schema": 1, "ready": not issues, "issues": issues, "app_root": str(APP_ROOT),
            "python": sys.executable, "python_version": sys.version.split()[0], "node_version": node_version,
            "media_versions": media_versions,
            "headed_browser_bundled": bool(selected_browsers and selected_browsers["publish_source"] == "app_chromium"),
            "browser_source": (selected_browsers["render_source"] if selected_browsers and
                               selected_browsers["render_source"] == selected_browsers["publish_source"] else "mixed" if selected_browsers else "unavailable"),
            "browser_versions": selected_browsers["versions"] if selected_browsers else {}, "environment": environment(),
            "notes": ["Whisper 模型在首次识别时下载，或由离线包单独提供。",
                      "数字人和自己的声音需要另外配置 Duix 服务与可用形象；本包不含人物模板。"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()
    try:
        if options.action == "prepare":
            prepare(offline=options.offline)
        report = inspect()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        report = {"schema": 1, "ready": False, "issues": [str(exc)]}
    if options.report:
        _within_app(options.report)
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    if report["ready"]:
        print("应用组件检查完成。", flush=True)
    else:
        for issue in report["issues"]:
            print("准备未完成：" + issue, file=sys.stderr, flush=True)
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import assert from 'node:assert/strict';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport, getDefaultEnvironment} from '@modelcontextprotocol/sdk/client/stdio.js';
import {ListRootsRequestSchema} from '@modelcontextprotocol/sdk/types.js';
import {PLATFORMS, allowedUrl, validateAction, uploadPath, classifySnapshot, classifyLoginSnapshot, findLoginEntry} from './policy.mjs';

const directory = path.dirname(fileURLToPath(import.meta.url));
let client, config, lastSnapshot = '', flags = {video: false, cover: false, title: false, description: false, submitted: false};

function textResult(result) {
  let text = (result.content ?? []).filter(item => item.type === 'text').map(item => item.text).join('\n');
  // New MCP versions can return generated snapshots as local file links.
  // Only read its own YAML outputs, never arbitrary links or browser profile files.
  const output = path.resolve(config.workspace, 'browser-output');
  for (const match of text.matchAll(/\[Snapshot\]\(([^)]+\.ya?ml)\)/g)) {
    const file = path.resolve(config.workspace, match[1]);
    const relative = path.relative(output, file);
    if (relative.startsWith('..') || path.isAbsolute(relative)) continue;
    try {
      if (fs.statSync(file).size <= 1024 * 1024) text += '\n' + fs.readFileSync(file, 'utf8');
    } catch { /* Incomplete outputs remain visible as diagnostic links. */ }
  }
  return text;
}

async function tool(name, args = {}) {
  const result = await client.callTool({name, arguments: args}, undefined, {timeout: 180000});
  const text = textResult(result);
  if (result.isError) throw new Error(text || `${name} 失败`);
  if (text.includes('[ref=')) lastSnapshot = text;
  const pageUrl = text.match(/Page URL:\s*(https?:\/\/[^\s]+)/)?.[1];
  if (pageUrl && !allowedUrl(pageUrl, config.platform)) {
    await client.callTool({name: 'browser_close', arguments: {}});
    throw new Error('浏览器离开了允许的平台域名，已停止任务');
  }
  return text;
}

async function connect(next) {
  if (!PLATFORMS[next.platform]) throw new Error('不支持的发布平台');
  config = next;
  fs.mkdirSync(config.workspace, {recursive: true});
  fs.mkdirSync(config.profile_path, {recursive: true});
  const args = [path.join(directory, 'node_modules/@playwright/mcp/cli.js'),
    '--user-data-dir', config.profile_path, '--init-page', path.join(directory, 'guard.ts'),
    '--image-responses', 'omit', '--codegen', 'none', '--no-webmcp',
    '--timeout-navigation', '60000', '--timeout-action', '15000',
    '--output-dir', path.join(config.workspace, 'browser-output')];
  if (config.executable_path) args.push('--executable-path', config.executable_path);
  else if (process.platform === 'win32') args.push('--browser', 'msedge');
  if (config.headless) args.push('--headless');
  // Test-only local interception; this switch cannot be enabled by JSONL inputs.
  if (process.argv.includes('--smoke')) args.push('--init-page', path.join(directory, 'test/mock-page.ts'));
  if (process.argv.includes('--login-smoke')) args.push('--init-page', path.join(directory, 'test/mock-login.ts'));
  const transport = new StdioClientTransport({command: process.execPath, args, cwd: config.workspace,
    env: {...getDefaultEnvironment(), MPT_PUBLISH_PLATFORM: config.platform,
      ...(process.argv.includes('--login-smoke') ? {MPT_LOGIN_FIXTURE: config.login_fixture_file} : {})}, stderr: 'pipe'});
  client = new Client({name: 'mpt-publishing-agent', version: '0.1.0'}, {capabilities: {roots: {listChanged: false}}});
  client.setRequestHandler(ListRootsRequestSchema, async () => ({roots: [{uri: pathToFileURL(config.workspace).href, name: 'publish-assets'}]}));
  // MCP diagnostics never share the JSONL protocol stream.
  await client.connect(transport);
  transport.stderr?.on('data', data => process.stderr.write(data));
  const {tools} = await client.listTools();
  const names = new Set(tools.map(item => item.name));
  for (const name of ['browser_navigate', 'browser_snapshot', 'browser_click', 'browser_type', 'browser_file_upload']) {
    if (!names.has(name)) throw new Error(`Playwright MCP 缺少工具：${name}`);
  }
  return tools;
}

async function observe() {
  try { return await tool('browser_snapshot'); }
  catch (error) { return `${lastSnapshot}\n浏览器观察提示：${error.message}`; }
}

async function dispatch(message) {
  if (message.op === 'init') {
    await connect(message.config);
    const snapshot = await tool('browser_navigate', {url: PLATFORMS[config.platform].upload});
    return {snapshot, flags, status: classifySnapshot(snapshot, false)};
  }
  if (message.op === 'close') { await client?.close(); return {closed: true}; }
  if (message.op === 'validate') {
    validateAction(message.action, lastSnapshot, config.task ?? {}, flags);
    return {validated: true, flags};
  }
  if (message.op !== 'action') throw new Error('不支持的协议操作');
  const action = validateAction(message.action, lastSnapshot, config.task ?? {}, flags);
  let result = '';
  switch (action.action) {
    case 'snapshot': result = await observe(); break;
    case 'click': result = await tool('browser_click', {target: action.ref}); break;
    case 'fill': {
      const task = config.task;
      const value = action.field === 'caption' ? [task.title, task.description].filter(Boolean).join('\n') : task[action.field];
      result = await tool('browser_type', {target: action.ref, text: String(value ?? ''), submit: false});
      if (action.field === 'caption') { flags.title = true; flags.description = true; }
      else flags[action.field] = true;
      break;
    }
    case 'upload': {
      const file = uploadPath(config.task, action.kind, config.workspace);
      if (!fs.statSync(file).isFile()) throw new Error('上传文件不存在');
      result = await tool('browser_file_upload', {paths: [file]});
      flags[action.kind] = true;
      break;
    }
    case 'wait': result = await tool('browser_wait_for', {time: 3}); break;
    case 'submit':
      // Mark before the irreversible click: an interrupted/failed call is still uncertain.
      flags.submitted = true;
      try { result = await tool('browser_click', {target: action.ref}); }
      catch (error) { return {snapshot: lastSnapshot, flags, status: 'submission_unknown', error: error.message}; }
      break;
    case 'needs_user': return {snapshot: lastSnapshot, flags, status: 'needs_user', message: String(action.reason ?? '需要在平台页面完成操作').slice(0, 300)};
    case 'done': return {snapshot: lastSnapshot, flags, status: classifySnapshot(lastSnapshot, flags.submitted) ?? 'needs_user'};
  }
  // A file chooser must remain open until the subsequent upload tool call.
  const snapshot = /file chooser/i.test(result) ? `${lastSnapshot}\n${result}` : await observe();
  return {snapshot, flags, status: classifySnapshot(snapshot, flags.submitted)};
}

async function loginMode(file) {
  const login = JSON.parse(fs.readFileSync(file, 'utf8'));
  const statusFile = path.resolve(login.status_file);
  const accountId = String(login.account_id || path.basename(login.profile_path));
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(accountId) || path.dirname(statusFile) !== path.dirname(path.resolve(login.profile_path))
      || path.basename(statusFile) !== `${accountId}-login-status.json`) throw new Error('登录状态路径与专用账号目录不匹配');
  const closeMarker = statusFile.replace(/\.json$/, '.close');
  const checkMarker = statusFile.replace(/\.json$/, '.check');
  let lastVerifiedAt = null;
  let wasVerified = false;
  let terminalError = false;
  let loginEntryOpened = false;
  let loginEntryName = null;
  const update = data => {
    const now = new Date().toISOString();
    if (data.verified) {lastVerifiedAt = now; wasVerified = true;}
    const status = {...data, login_entry_opened: loginEntryOpened, login_entry_name: loginEntryName, account_id: accountId, platform: login.platform, pid: process.pid,
      updated_at: now, verified_at: data.verified ? now : null, was_verified: wasVerified, last_verified_at: lastVerifiedAt};
    fs.mkdirSync(path.dirname(statusFile), {recursive: true});
    fs.writeFileSync(statusFile + '.tmp', JSON.stringify(status));
    fs.renameSync(statusFile + '.tmp', statusFile);
  };
  const inspect = async () => {
    // Never reuse lastSnapshot after an observation error to claim login.
    let snapshot = await tool('browser_snapshot');
    if (/Page URL:\s*about:blank/.test(snapshot)) return false;
    const entry = !loginEntryOpened && findLoginEntry(snapshot, login.platform);
    if (entry) {
      // Only this allowlisted public entry can be clicked in login mode.
      // Mark before the click so errors never cause an automatic retry.
      loginEntryOpened = true;
      loginEntryName = entry.name;
      await tool('browser_click', {target: entry.ref});
      snapshot = await tool('browser_snapshot');
    }
    update(classifyLoginSnapshot(snapshot, login.platform));
    return true;
  };
  try {
    update({status: 'starting', verified: false, message: '正在打开专用登录浏览器。'});
    await connect(login);
    await tool('browser_navigate', {url: PLATFORMS[login.platform].upload});
    await inspect();
    // Keep the browser available for interactive login; no automatic credentials or posting.
    const deadline = Date.now() + 10 * 60 * 1000;
    let nextCheck = Date.now() + 5000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 500));
      if (fs.existsSync(closeMarker)) {fs.unlinkSync(closeMarker); break;}
      const requested = fs.existsSync(checkMarker);
      if (requested || Date.now() >= nextCheck) {
        try {
          if (!await inspect()) break;
          nextCheck = Date.now() + 5000;
        } finally {
          if (requested) fs.rmSync(checkMarker, {force: true});
        }
      }
    }
  } catch {
    terminalError = true;
    // Browser errors can include page text or account names. Persist only a
    // fixed actionable message, with no raw snapshot or credentials.
    update({status: 'error', verified: false, message: '登录浏览器未能完成检查。请关闭后重新打开，或检查本机网络与浏览器。'});
  } finally {
    try {await client?.close();} catch { /* A user-closed browser may already have disconnected. */ }
    if (!terminalError) update({status: 'closed', verified: false, message: '登录窗口已关闭。发布前会重新核对登录状态。'});
    fs.rmSync(checkMarker, {force: true});
  }
}

async function loginSmokeMode() {
  const workspace = path.join(directory, '.login-smoke');
  fs.mkdirSync(workspace, {recursive: true});
  const profile = path.join(workspace, 'local-login-fixture');
  const statusFile = path.join(workspace, 'local-login-fixture-login-status.json');
  const fixture = path.join(workspace, 'fixture-state.json');
  const settings = path.join(workspace, 'settings.json');
  for (const suffix of ['.close', '.check']) fs.rmSync(statusFile.replace(/\.json$/, suffix), {force: true});
  fs.writeFileSync(fixture, JSON.stringify({mode: 'public_landing', login_clicks: 0}));
  fs.writeFileSync(settings, JSON.stringify({platform: 'douyin', account_id: 'local-login-fixture', workspace,
    profile_path: profile, status_file: statusFile, headless: true, login_fixture_file: fixture,
    executable_path: process.env.MPT_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'}));
  const wait = async wanted => {
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      try {const row = JSON.parse(fs.readFileSync(statusFile, 'utf8')); if (row.status === wanted) return row;} catch { /* Atomic status not ready. */ }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw new Error(`mock login did not reach ${wanted}`);
  };
  const running = loginMode(settings);
  try {
    const waiting = await wait('waiting_login');
    assert.equal(waiting.verified, false);
    assert.equal(waiting.evidence.login_form, true);
    assert.equal(waiting.login_entry_opened, true);
    assert.equal(waiting.login_entry_name, '登录或注册');
    assert.equal(JSON.parse(fs.readFileSync(fixture, 'utf8')).login_clicks, 1);
    // Returning to the public entry must not cause a second automatic click.
    fs.writeFileSync(fixture, JSON.stringify({mode: 'public_landing', login_clicks: 1}));
    await new Promise(resolve => setTimeout(resolve, 1200));
    fs.writeFileSync(statusFile.replace(/\.json$/, '.check'), 'check');
    const checkDeadline = Date.now() + 10000;
    while (fs.existsSync(statusFile.replace(/\.json$/, '.check')) && Date.now() < checkDeadline) await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(fs.existsSync(statusFile.replace(/\.json$/, '.check')), false);
    assert.equal(JSON.parse(fs.readFileSync(fixture, 'utf8')).login_clicks, 1);
    assert(JSON.parse(fs.readFileSync(statusFile, 'utf8')).evidence.public_controls.some(control => control.name === '登录或注册'));
    fs.writeFileSync(fixture, JSON.stringify({mode: 'logged_in'}));
    await new Promise(resolve => setTimeout(resolve, 1200));
    fs.writeFileSync(statusFile.replace(/\.json$/, '.check'), 'check');
    const logged = await wait('logged_in');
    assert.equal(logged.verified, true);
    assert(logged.verified_at && logged.evidence.creator_page && logged.evidence.upload_control);
    assert.equal(fs.existsSync(statusFile.replace(/\.json$/, '.check')), false);
    fs.writeFileSync(fixture, JSON.stringify({mode: 'needs_user'}));
    await new Promise(resolve => setTimeout(resolve, 1200));
    fs.writeFileSync(statusFile.replace(/\.json$/, '.check'), 'check');
    const challenge = await wait('needs_user');
    assert.equal(challenge.verified, false);
    assert.equal(challenge.evidence.challenge, true);
    assert.equal(challenge.was_verified, true);
  } finally {
    fs.writeFileSync(statusFile.replace(/\.json$/, '.close'), 'close');
    await running;
  }
  const closed = await wait('closed');
  assert.equal(closed.verified, false);
  assert.equal(closed.was_verified, true);
  assert.equal(Object.keys(flags).some(key => flags[key]), false, 'login never uploads, fills, or submits');
  console.log(JSON.stringify({ok: true, mocked: true, checks: ['public-login-entry-opens-once', 'waiting-login', 'check-marker', 'verified-login', 'challenge', 'close-marker', 'no-upload-or-submit']}));
}

async function checkMode() {
  // Listing tools negotiates the real MCP protocol, but never launches a browser.
  const workspace = path.join(directory, '.protocol-check');
  const tools = await connect({platform: 'douyin', workspace, profile_path: path.join(workspace, 'unused-profile'), headless: true});
  console.log(JSON.stringify({ok: true, tools: tools.map(tool => tool.name)}));
  await client.close();
}

async function smokeMode() {
  const workspace = path.join(directory, '.browser-smoke');
  fs.mkdirSync(workspace, {recursive: true});
  const video = path.join(workspace, 'local-fixture.mp4');
  fs.writeFileSync(video, Buffer.from('local mock media: never sent to a real platform'));
  const candidates = [process.env.MPT_BROWSER_PATH,
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe'];
  const executable_path = candidates.find(file => file && fs.existsSync(file));
  try {
    let result = await dispatch({op: 'init', config: {platform: 'douyin', workspace,
      profile_path: path.join(workspace, 'unused-test-profile'), headless: true, executable_path,
      task: {video_path: video, title: '本地发布测试', description: '这是本地测试正文'}}});
    const ref = label => {
      const line = result.snapshot.split('\n').find(line => line.includes(`"${label}"`) && line.includes('[ref='));
      assert.ok(line, `mock page is missing ${label}: ${result.snapshot}`);
      return line.match(/\[ref=(e\d+)\]/)[1];
    };
    result = await dispatch({op: 'action', action: {action: 'click', ref: ref('上传视频')}});
    result = await dispatch({op: 'action', action: {action: 'upload', kind: 'video'}});
    assert.equal(result.flags.video, true);
    assert.match(result.snapshot, /local-fixture\.mp4/);
    result = await dispatch({op: 'action', action: {action: 'fill', ref: ref('作品描述'), field: 'caption'}});
    assert.equal(result.flags.title, true);
    assert.equal(result.flags.description, true);
    assert.match(result.snapshot, /本地发布测试/);
    result = await dispatch({op: 'action', action: {action: 'submit', ref: ref('发布')}});
    assert.equal(result.status, 'submitted');
    assert.match(result.snapshot, /发布成功/);
    console.log(JSON.stringify({ok: true, mocked: true, checks: ['navigate', 'file-chooser', 'upload', 'fill', 'submit', 'verify'], status: result.status}));
  } finally { await client?.close(); }
}

if (process.argv.includes('--login-smoke')) {
  await loginSmokeMode();
} else if (process.argv.includes('--smoke')) {
  await smokeMode();
} else if (process.argv.includes('--check')) {
  await checkMode();
} else if (process.argv.includes('--login')) {
  await loginMode(process.argv[process.argv.indexOf('--login') + 1]);
} else {
  const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  for await (const line of input) {
    if (!line.trim()) continue;
    let request;
    try {
      request = JSON.parse(line);
      const response = await dispatch(request);
      process.stdout.write(JSON.stringify({id: request.id, ok: true, ...response}) + '\n');
      if (request.op === 'close') break;
    } catch (error) {
      process.stdout.write(JSON.stringify({id: request?.id, ok: false, error: error.message, flags}) + '\n');
    }
  }
  await client?.close();
}

import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {allowedUrl, validateAction, uploadPath, classifySnapshot, classifyLoginSnapshot, findLoginEntry} from '../policy.mjs';

const snapshot = '- button "上传视频" [ref=e1]\n- textbox "标题" [ref=e2]\n- button "发布" [ref=e3]\n- button "删除" [ref=e4]';

test('navigation permits only HTTPS platform domains', () => {
  assert.equal(allowedUrl('https://creator.douyin.com/upload', 'douyin'), true);
  for (const url of ['http://creator.douyin.com', 'https://douyin.com.evil.test', 'https://douyin.com@evil.test', 'file:///C:/Users/private', 'https://creator.xiaohongshu.com', 'https://creator.douyin.com:8443/']) {
    assert.equal(allowedUrl(url, 'douyin'), false);
  }
});

test('model has no arbitrary code, paths, selectors, or generic publish click', () => {
  assert.throws(() => validateAction({action: 'evaluate'}, snapshot, {}, {}));
  assert.throws(() => validateAction({action: 'click', ref: 'button'}, snapshot, {}, {}));
  assert.throws(() => validateAction({action: 'click', ref: 'e3'}, snapshot, {}, {}));
  assert.throws(() => validateAction({action: 'click', ref: 'e4'}, snapshot, {}, {}));
  assert.throws(() => validateAction({action: 'fill', ref: 'e2', field: 'password'}, snapshot, {}, {}));
  assert.deepEqual(validateAction({action: 'click', ref: 'e1'}, snapshot, {}, {}), {action: 'click', ref: 'e1'});
});

test('submit requires prepared video, title, body and requested cover', () => {
  const action = {action: 'submit', ref: 'e3'};
  const task = {description: '正文', cover_path: 'cover.png'};
  assert.throws(() => validateAction(action, snapshot, task, {video: true, title: true}));
  const ready = {video: true, title: true, description: true, cover: true};
  assert.deepEqual(validateAction(action, snapshot, task, ready), action);
  assert.throws(() => validateAction(action, snapshot, task, {...ready, submitted: true}));
});

test('upload path cannot escape the fixed asset workspace', () => {
  const root = path.resolve('test-assets');
  assert.equal(uploadPath({video_path: path.join(root, 'video.mp4')}, 'video', root), path.join(root, 'video.mp4'));
  assert.throws(() => uploadPath({video_path: path.resolve('private.mp4')}, 'video', root));
});

test('success is recognized only after submitting, uncertain result remains unknown', () => {
  assert.equal(classifySnapshot('发布成功', false), null);
  assert.equal(classifySnapshot('发布成功', true), 'submitted');
  assert.equal(classifySnapshot('上传处理中', true), 'submission_unknown');
  assert.equal(classifySnapshot('请先登录', false), 'waiting_login');
});

test('opened studio and public headings never prove login', () => {
  for (const snapshot of ['创作中心 上传视频', 'Page URL: https://creator.douyin.com/\n- heading "抖音创作者中心" [ref=e1]',
    'Page URL: https://douyin.com/\n- button "上传视频" [ref=e1]\n- textbox "作品描述" [ref=e2]']) {
    const status = classifyLoginSnapshot(snapshot, 'douyin');
    assert.equal(status.verified, false);
    assert.equal(status.status, 'waiting_login');
  }
});

test('visible login form wins over creator controls in the background', () => {
  const snapshot = 'Page URL: https://creator.douyin.com/creator-micro/content/upload\n- button "上传视频" [ref=e1]\n- textbox "作品描述" [ref=e2]\n- button "扫码登录" [ref=e3]';
  const status = classifyLoginSnapshot(snapshot, 'douyin');
  assert.equal(status.status, 'waiting_login');
  assert.equal(status.verified, false);
  assert.equal(status.evidence.login_form, true);
});

test('verified login requires official creator URL and multiple visible authenticated controls', () => {
  const snapshot = 'Page URL: https://creator.douyin.com/creator-micro/content/upload\n- button "上传视频" [ref=e1]\n- textbox "作品描述" [ref=e2]';
  const status = classifyLoginSnapshot(snapshot, 'douyin');
  assert.equal(status.status, 'logged_in');
  assert.equal(status.verified, true);
  assert.equal(classifyLoginSnapshot(snapshot.replace('creator.douyin.com', 'creator.xiaohongshu.com'), 'douyin').verified, false);
  assert.equal(classifyLoginSnapshot(snapshot.replace('/creator-micro/content/upload', '/login'), 'douyin').verified, false);
});

test('Xiaohongshu authenticated controls and challenge classification are explicit', () => {
  const snapshot = 'Page URL: https://creator.xiaohongshu.com/new/note-manager\n- link "笔记管理" [ref=e1]\n- button "发布笔记" [ref=e2]';
  assert.equal(classifyLoginSnapshot(snapshot, 'xiaohongshu').verified, true);
  const challenged = classifyLoginSnapshot(snapshot + '\n- heading "安全验证" [ref=e3]', 'xiaohongshu');
  assert.equal(challenged.status, 'needs_user');
  assert.equal(challenged.verified, false);
});

test('login can open only an exact public button or link on the official creator page', () => {
  for (const [role, name] of [['button', '登录或注册'], ['link', '登录'], ['button', '登录/注册'], ['button', '扫码登录']]) {
    const snapshot = `Page URL: https://creator.douyin.com/\n- ${role} "${name}" [ref=e1]`;
    assert.deepEqual(findLoginEntry(snapshot, 'douyin'), {ref: 'e1', name});
  }
  for (const name of ['发布', '发布作品', '授权登录', '登录并授权', '账号设置', '去登录发布作品']) {
    assert.equal(findLoginEntry(`Page URL: https://creator.douyin.com/\n- button "${name}" [ref=e1]`, 'douyin'), null);
  }
  for (const snapshot of ['Page URL: https://douyin.com/\n- button "登录" [ref=e1]',
    'Page URL: https://creator.xiaohongshu.com/\n- button "登录" [ref=e1]',
    'Page URL: https://creator.douyin.com/\n- generic "登录" [ref=e1]',
    'Page URL: https://creator.douyin.com/\n- button "登录" [disabled] [ref=e1]',
    'Page URL: https://creator.douyin.com/\n- button "登录" [ref=e1]\n- textbox "手机号" [ref=e2]',
    'Page URL: https://creator.douyin.com/\n- heading "扫码登录" [ref=e2]\n- button "登录" [ref=e1]',
    'Page URL: https://creator.douyin.com/\n- heading "安全验证" [ref=e2]\n- button "登录" [ref=e1]']) assert.equal(findLoginEntry(snapshot, 'douyin'), null);
});

test('login diagnostics include only allowlisted public control names and never private text', () => {
  const names = ['发布作品', '发布', '开始创作', '投稿', '视频投稿', '上传视频', '点击上传', '选择文件'];
  const snapshot = 'Page URL: https://creator.douyin.com/\n' + names.map((name, index) => `- button "${name}" [ref=e${index + 1}]`).join('\n')
    + '\n- textbox "私人账号名称" [ref=e20]: 私人文案\n- link "个人昵称张三" [ref=e21]\n- paragraph: 私人用户资料';
  const result = classifyLoginSnapshot(snapshot, 'douyin');
  assert.deepEqual(result.evidence.public_controls, names.map(name => ({role: 'button', name})));
  assert.equal(JSON.stringify(result).includes('私人'), false);
  assert.equal(JSON.stringify(result).includes('张三'), false);
  assert.equal(result.verified, false, 'additional diagnostics do not loosen authentication proof');
});

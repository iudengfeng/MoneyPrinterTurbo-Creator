import test from 'node:test';
import assert from 'node:assert/strict';
import {validateAction, classifyLoginSnapshot, classifySnapshot} from '../policy.mjs';

test('publishing cannot enter analytics or comment operations before or after submit', () => {
  for (const name of ['数据中心', '数据总览', '数据分析', '作品数据', '数据详情', '经营数据', '评论管理', '评论中心', '互动管理', '粉丝管理']) {
    for (const submitted of [false, true]) {
      const snapshot = `- link "${name}" [ref=e9]`;
      assert.throws(() => validateAction({action: 'click', ref: 'e9'}, snapshot, {}, {submitted}), /不进入表现数据或评论/);
    }
  }
});

test('upload and work-list verification controls remain available', () => {
  for (const name of ['上传视频', '作品管理', '内容管理', '笔记管理', '查看审核状态']) {
    const action = {action: 'click', ref: 'e2'};
    assert.deepEqual(validateAction(action, `- button "${name}" [ref=e2]`, {}, {submitted: name !== '上传视频'}), action);
  }
});

test('necessary snapshots and result observations are still permitted', () => {
  const snapshot = '- link "数据中心" [ref=e1]\n- paragraph: 审核中';
  for (const action of ['snapshot', 'wait', 'done']) {
    assert.deepEqual(validateAction({action}, snapshot, {}, {submitted: true}), {action});
  }
  assert.equal(classifySnapshot(snapshot, true), 'reviewing');
  assert.equal(classifySnapshot('发布成功', false), null);
  assert.equal(classifySnapshot('发布成功', true), 'submitted');
  assert.equal(classifySnapshot('等待平台返回', true), 'submission_unknown');
});

test('observed analytics navigation names can still corroborate authenticated login', () => {
  const snapshot = 'Page URL: https://creator.douyin.com/creator-micro/content/upload\n- link "数据中心" [ref=e1]\n- button "退出登录" [ref=e2]';
  const result = classifyLoginSnapshot(snapshot, 'douyin');
  assert.equal(result.verified, true);
  assert.equal(result.evidence.management_navigation, true);
  assert.equal(result.status, 'logged_in');
});

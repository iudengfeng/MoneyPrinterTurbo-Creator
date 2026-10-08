import path from 'node:path';

export const PLATFORMS = {
  douyin: {name: '抖音', host: 'douyin.com', upload: 'https://creator.douyin.com/creator-micro/content/upload', manage: 'https://creator.douyin.com/creator-micro/content/manage'},
  xiaohongshu: {name: '小红书', host: 'xiaohongshu.com', upload: 'https://creator.xiaohongshu.com/publish/publish', manage: 'https://creator.xiaohongshu.com/new/note-manager'},
};

export function allowedUrl(value, platform) {
  try {
    const url = new URL(value);
    const root = PLATFORMS[platform]?.host;
    return !!root && url.protocol === 'https:' && !url.username && !url.password && (!url.port || url.port === '443') &&
      (url.hostname === root || url.hostname.endsWith(`.${root}`));
  } catch { return false; }
}

export function referenceLine(snapshot, ref) {
  if (!/^e\d+$/.test(ref ?? '')) throw new Error('只允许当前页面的元素引用');
  const line = snapshot.split('\n').find(line => line.includes(`[ref=${ref}]`));
  if (!line) throw new Error('页面引用已失效，请重新观察页面');
  return line;
}

export function validateAction(action, snapshot, task, flags) {
  const allowed = new Set(['snapshot', 'click', 'fill', 'upload', 'wait', 'submit', 'done', 'needs_user']);
  if (!allowed.has(action.action)) throw new Error('不支持的浏览器动作');
  if (['click', 'fill', 'submit'].includes(action.action)) {
    const line = referenceLine(snapshot, action.ref);
    if (action.action === 'click' && /数据中心|数据总览|数据分析|作品数据|数据详情|经营数据|评论管理|评论中心|互动管理|粉丝管理/.test(line)) {
      throw new Error('发布流程不进入表现数据或评论运营页面');
    }
    if (action.action === 'click' && /发布|提交|删除|推广|充值|支付|授权|购买/.test(line)) {
      throw new Error('发布按钮必须使用 submit 动作，其余敏感操作禁止');
    }
    if (action.action === 'fill' && !/textbox|combobox|searchbox/.test(line)) {
      throw new Error('只允许填写文本输入框');
    }
    if (action.action === 'submit') {
      if (!/button/.test(line) || !/"(?:发布|立即发布|确认发布|发布笔记|发布作品|确认提交)"/.test(line)) {
        throw new Error('提交目标不是已识别的发布按钮');
      }
      if (!flags.video || !flags.title || (task.description && !flags.description) || (task.cover_path && !flags.cover)) {
        throw new Error('视频、标题、正文或封面尚未全部准备完成');
      }
      if (flags.submitted) throw new Error('已经提交过，禁止重复提交');
    }
  }
  if (action.action === 'fill' && !['title', 'description', 'caption'].includes(action.field)) {
    throw new Error('只能填写任务中的标题或正文');
  }
  if (action.action === 'upload' && !['video', 'cover'].includes(action.kind)) {
    throw new Error('只能上传本任务的视频或封面');
  }
  return action;
}

export function uploadPath(task, kind, workspace) {
  const file = kind === 'video' ? task.video_path : task.cover_path;
  if (!file) throw new Error('本任务没有该上传文件');
  const resolved = path.resolve(file);
  const relative = path.relative(path.resolve(workspace), resolved);
  if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('上传文件不在发布工作区');
  return resolved;
}

export function classifySnapshot(snapshot, submitted) {
  if (submitted && /(?:发布成功|笔记发布成功|作品发布成功|发布完成)/.test(snapshot)) return 'submitted';
  if (submitted && /审核中|正在审核|等待审核/.test(snapshot)) return 'reviewing';
  if (/(?:扫码登录|扫描二维码登录|请先登录|手机号登录|短信登录)/.test(snapshot)) return 'waiting_login';
  if (/验证码|安全验证|滑块验证|拖动滑块/.test(snapshot)) return 'needs_user';
  return submitted ? 'submission_unknown' : null;
}

const LOGIN_ENTRY_NAMES = new Set(['登录', '登录或注册', '登录注册', '登录/注册', '登录 / 注册', '立即登录', '扫码登录']);
const PUBLIC_CONTROL_NAMES = new Set([...LOGIN_ENTRY_NAMES, '上传视频', '发布视频', '上传素材', '视频发布', '上传图文', '发布笔记',
  '发布作品', '发布', '开始创作', '投稿', '视频投稿', '点击上传', '选择文件', '作品管理', '内容管理', '笔记管理',
  '发布管理', '数据中心', '数据总览', '作品描述', '填写作品描述', '添加作品描述', '标题', '填写标题', '笔记标题', '正文', '填写正文']);

function publicControls(snapshot) {
  const controls = [];
  for (const match of snapshot.matchAll(/^\s*-\s*(button|link|tab|menuitem|textbox|combobox|searchbox)\s+"([^"]+)"[^\n]*$/gm)) {
    const [, role, name] = match;
    if (PUBLIC_CONTROL_NAMES.has(name) && !controls.some(control => control.role === role && control.name === name)) controls.push({role, name});
  }
  return controls;
}

export function findLoginEntry(snapshot, platform) {
  const result = classifyLoginSnapshot(snapshot, platform);
  if (!result.evidence.creator_page || result.verified || result.evidence.challenge) return null;
  // A visible credential field or QR form is already ready for the user;
  // never click a form's submit button or change its authentication method.
  if (/^\s*-\s*(?:textbox|combobox)\s+"(?:手机号码|手机号|请输入手机号|请输入手机号码|密码|请输入密码|验证码|请输入验证码)[^"]*"/m.test(snapshot)
      || /二维码|^\s*-\s*(?:heading|generic|paragraph|text)\s+"扫码登录"/m.test(snapshot)) return null;
  for (const match of snapshot.matchAll(/^\s*-\s*(button|link)\s+"([^"]+)"([^\n]*)\[ref=(e\d+)\][^\n]*$/gm)) {
    if (LOGIN_ENTRY_NAMES.has(match[2]) && !/\[disabled\]/.test(match[0])) return {ref: match[4], name: match[2]};
  }
  return null;
}

export function classifyLoginSnapshot(snapshot, platform) {
  // A brand heading or an opened window proves neither authentication nor
  // access. Require visible authenticated controls on the official studio.
  const rawUrl = snapshot.match(/Page URL:\s*(https?:\/\/[^\s]+)/)?.[1];
  let creatorPage = false;
  let loginPath = false;
  try {
    const url = new URL(rawUrl);
    creatorPage = allowedUrl(rawUrl, platform) && url.hostname === `creator.${PLATFORMS[platform]?.host}`;
    loginPath = /(?:^|\/)login(?:\/|$)|\/passport(?:\/|$)/i.test(url.pathname);
  } catch { /* Missing URL cannot prove an authenticated creator page. */ }
  const challenge = /安全验证|滑块验证|拖动滑块|完成验证|人机验证|二次验证|账号异常|访问受限/.test(snapshot);
  const loginForm = loginPath || /扫码登录|扫描二维码登录|请先登录|手机号登录|短信登录|密码登录|微信扫码|登录二维码/.test(snapshot)
    || /^\s*-\s*(?:button|tab|textbox)\s+"(?:登录|立即登录|手机号码|手机号|请输入手机号|请输入手机号码|密码|请输入密码)"/m.test(snapshot)
    || publicControls(snapshot).some(control => ['button', 'link'].includes(control.role) && LOGIN_ENTRY_NAMES.has(control.name));
  const uploadControl = /^\s*-\s*(?:button|link)\s+"(?:上传视频|发布视频|上传素材|视频发布|上传图文|发布笔记)"/m.test(snapshot);
  const editorControl = /^\s*-\s*(?:textbox|combobox)\s+"(?:作品描述|填写作品描述|添加作品描述|标题|填写标题|笔记标题|正文|填写正文)"/m.test(snapshot);
  const managementNav = /^\s*-\s*(?:button|link|tab|menuitem)\s+"(?:作品管理|内容管理|笔记管理|发布管理|数据中心|数据总览)"/m.test(snapshot);
  const accountControl = /(?:button|link|menuitem)\s+"(?:退出登录|退出账号|个人资料|账号设置)"/.test(snapshot);
  const evidence = {creator_page: creatorPage, login_form: loginForm, challenge, upload_control: uploadControl,
    editor_control: editorControl, management_navigation: managementNav, account_control: accountControl, public_controls: publicControls(snapshot)};
  if (challenge) return {status: 'needs_user', verified: false, evidence, message: '平台要求安全验证，请在登录窗口中完成后再检查登录。'};
  if (loginForm) return {status: 'waiting_login', verified: false, evidence, message: '尚未登录，请在专用浏览器中扫码或按平台要求登录。'};
  const authenticated = creatorPage && ((uploadControl && editorControl) || (managementNav && (uploadControl || accountControl)));
  if (authenticated) return {status: 'logged_in', verified: true, evidence, message: '已核对当前创作者页面的登录状态。关闭窗口后，发布前仍会重新核对。'};
  return {status: 'waiting_login', verified: false, evidence, message: '尚未确认登录。请等待创作者页面加载，完成登录后点击检查登录。'};
}

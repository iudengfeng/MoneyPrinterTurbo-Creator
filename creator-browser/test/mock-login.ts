import fs from 'node:fs';

// Test-only interception. Every request is answered locally or aborted; no
// credentials, account session or real platform is used.
export default async ({ page }) => {
  const fixture = process.env.MPT_LOGIN_FIXTURE;
  if (!fixture) throw new Error('Missing local login fixture');
  await page.context().route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.hostname !== 'creator.douyin.com') return route.abort('blockedbyclient');
    if (url.pathname === '/__local_login_fixture') {
      return route.fulfill({status: 200, contentType: 'application/json', body: fs.readFileSync(fixture, 'utf8')});
    }
    if (url.pathname === '/__local_login_click') {
      const state = JSON.parse(fs.readFileSync(fixture, 'utf8'));
      state.login_clicks = (state.login_clicks || 0) + 1;
      state.mode = 'waiting_login';
      fs.writeFileSync(fixture, JSON.stringify(state));
      return route.fulfill({status: 200, contentType: 'text/plain', body: 'local fixture login opened'});
    }
    if (!request.isNavigationRequest()) return route.abort('blockedbyclient');
    const html = `<!doctype html><html lang="zh"><meta charset="UTF-8"><title>本机登录验证夹具</title>
      <body><main id="view"><h1>抖音创作者中心</h1><a href="/creator-micro/content/manage">作品管理</a><button id="login-entry">登录或注册</button></main>
      <script>
        let current = '';
        const screens = {
          public_landing: '<h1>抖音创作者中心</h1><a href="/creator-micro/content/manage">作品管理</a><button id="login-entry">登录或注册</button>',
          waiting_login: '<h1>扫码登录</h1><button>登录</button>',
          logged_in: '<h1>创作中心</h1><a href="/creator-micro/content/manage">作品管理</a><button>上传视频</button><label>作品描述<textarea aria-label="作品描述"></textarea></label><button>退出登录</button>',
          needs_user: '<h1>安全验证</h1><button>拖动滑块</button>'
        };
        document.getElementById('view').addEventListener('click', async event => {
          if (event.target.id !== 'login-entry') return;
          await fetch('/__local_login_click', {method: 'POST'});
          document.getElementById('view').innerHTML = screens.waiting_login;
          current = 'waiting_login';
        });
        async function refresh() {
          const state = await fetch('/__local_login_fixture').then(response => response.json());
          if (state.mode !== current) { document.getElementById('view').innerHTML = screens[state.mode]; current = state.mode; }
        }
        refresh(); setInterval(refresh, 250);
      </script></body></html>`;
    return route.fulfill({status: 200, contentType: 'text/html; charset=utf-8', body: html});
  });
};

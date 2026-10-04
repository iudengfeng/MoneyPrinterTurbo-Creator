// All requests are intercepted locally: no platform, account or remote upload.
export default async ({ page }) => {
  const html = `<!doctype html><html lang="zh"><meta charset="UTF-8"><title>本地发布测试</title>
  <body><h1>发布视频</h1>
  <button onclick="document.getElementById('video').click()">上传视频</button>
  <input id="video" type="file" hidden onchange="document.getElementById('uploaded').textContent=this.files[0]?.name||''">
  <p id="uploaded"></p>
  <label>作品描述<textarea aria-label="作品描述" id="caption"></textarea></label>
  <button onclick="if(document.getElementById('video').files.length && document.getElementById('caption').value){document.getElementById('result').textContent='发布成功'}">发布</button>
  <p id="result"></p></body></html>`;
  await page.context().route('**/*', async route => {
    if (route.request().isNavigationRequest() && route.request().url().startsWith('https://creator.douyin.com/')) {
      await route.fulfill({status: 200, contentType: 'text/html; charset=utf-8', body: html});
    } else await route.abort('blockedbyclient');
  });
};

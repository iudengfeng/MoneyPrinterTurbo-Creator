// Only navigation is restricted: CDN requests remain available for video uploads.
// This is fixed application code; neither page content nor the LLM may change it.
export default async ({ page }) => {
  const root = process.env.MPT_PUBLISH_PLATFORM === 'douyin' ? 'douyin.com' : 'xiaohongshu.com';
  const allowed = (url: string) => {
    if (url === 'about:blank') return true;
    try {
      const parsed = new URL(url);
      return parsed.protocol === 'https:' && !parsed.username && !parsed.password && (!parsed.port || parsed.port === '443') &&
        (parsed.hostname === root || parsed.hostname.endsWith(`.${root}`));
    } catch { return false; }
  };
  await page.context().route('**/*', async route => {
    const request = route.request();
    if (request.isNavigationRequest() && !allowed(request.url())) await route.abort('blockedbyclient');
    else await route.continue();
  });
};

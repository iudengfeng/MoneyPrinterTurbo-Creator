# MoneyPrinterTurbo 发布浏览器组件

Python 发布 Agent 使用 MoneyPrinterTurbo 已配置的文本模型选择有限动作。
本组件通过官方 MCP SDK 的 stdio Client 连接官方 Playwright MCP，执行导航、观察、点击、填写与上传。
依赖固定在 package-lock.json，可使用 `npm ci` 恢复。

浏览器页面、账号身份、验证码与平台审核规则仍需要首次人工验收。
工具协议检查和本地测试不代表真实抖音、小红书账号已经发布成功。

- 每个账号有独立持久化浏览器目录，仅供本程序使用。
- 初次登录使用可见专用浏览器，用户扫码或按平台要求登录；不会读取现有浏览器的 Cookie。
- 窗口打开不等于已经登录。程序只读观察官方创作者页面，登录表单或安全验证存在时不会判定为已登录；须观察到多项上传、编辑或作品管理控件才核对成功。
- 登录表单未打开时，仅在官方创作者页点击一次文字明确的登录按钮或链接；验证码、凭证输入框和二维码表单留给用户处理。
- “检查登录”通过本机 `.check` 标记请求重新观察；关闭通过 `.close` 标记完成。状态文件只保存 `status`、`verified`、检查时间、证据布尔值及固定公开控件名称和角色，不保存页面原文、账号资料或凭证。
- 关闭窗口后当前 `verified` 为 false，保留 `was_verified` 和 `last_verified_at` 表示历史核对。发布前仍会重新检查，不把历史状态当作当前登录。
- Agent 只得到页面快照与任务元数据，不能运行脚本、读取凭证或选择任意上传文件。
- 顶层和框架导航限于本账号平台的 HTTPS 域名；静态资源和上传 CDN 可正常访问。
- 上传视频、标题、正文和指定封面未完成时，不允许提交。
- 用户点击“确认发布”才会执行发布任务，创建预览不会打开浏览器。
- 同账号串行，提交前写入持久标记，结果未知时不会自动重发。
- 提交成功与审核通过分别记录；不会把创作者后台网址冒充公开作品链接。

可配置 `MPT_NODE_PATH`、`MPT_BROWSER_PATH` 指定 Node 与 Edge/Chrome。
Windows 默认尝试已安装的 Edge/Chrome。

验证：

```text
npm test
npm run protocol-check
npm run browser-smoke
node worker.mjs --login-smoke
```

协议检查只连接 MCP 并列举工具，不启动浏览器，不访问平台，不操作账号。
browser-smoke 使用真正的无界面浏览器与 MCP 工具，但所有页面和上传均在本地拦截模拟，不连接真实平台。
login-smoke 同样只使用本地拦截页面，验证待登录 → 本机检查 → 页面认证成功 → 安全验证 → 关闭的状态变化，不填写、上传或提交任何内容。

官方来源：

- https://github.com/microsoft/playwright-mcp
- https://github.com/modelcontextprotocol/typescript-sdk/tree/v1.x

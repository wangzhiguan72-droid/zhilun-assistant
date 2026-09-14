# 跨端接入指南（微信小程序 / Uni-app）

面向「把智论助手接到微信小程序」或「用 Uni-app 编译多端」的场景。

> **先说结论：后端零改动即可被小程序调用。**
> 已实测确认两件事：
> ① 后端**不使用 cookie 会话**，会话状态全部由服务端 `file_id` 承载
>    （上传返回 `file_id`，后续接口带着它调），因此无 cookie 的小程序天然可用；
> ② `wx.request / wx.uploadFile` **不走浏览器同源策略**，不看 CORS 头、不发预检。
>
> 所以 CORS 那套是给 **Uni-app 编译出的 H5** 和第三方网页准备的，小程序用不上。

---

## 一、阻塞项（必须先解决，代码解决不了）

这三项是**外部条件**，不是代码问题：

| 阻塞项 | 说明 | 由谁解决 |
| --- | --- | --- |
| **AppID** | 需要注册小程序主体（个人/企业），拿到 AppID 才能真机预览与上传 | 你 |
| **ICP 备案 + HTTPS** | 微信要求 `request 合法域名` 必须是**已备案的 https 域名**，不支持 IP、不支持自签名证书、不能带路径前缀 | 你（域名 + 备案通常需数周） |
| **微信开发者工具** | 上传代码、真机预览、提交审核都要用它 | 你（本机未安装，故本仓库无法端到端验证小程序 UI） |

**因此本仓库交付的是「可直接接入的后端 + 小程序源码脚手架 + 契约测试」，
而不是「已上架的小程序」。** 后者需要你补齐上面三项后在微信侧完成。

---

## 二、后端需要配的东西

小程序要调后端，需要：

1. **HTTPS 域名**（见上）。部署方式见 [DEPLOY_H5.md](DEPLOY_H5.md)。
2. **把域名填进微信后台**：

```
微信公众平台 → 开发管理 → 开发设置 → 服务器域名
  request 合法域名     ：https://your.domain.com
  uploadFile 合法域名  ：https://your.domain.com      ← 上传 Excel/PDF 用
  downloadFile 合法域名：https://your.domain.com      ← 导出 Word 用
```

3. **限流阈值按预期并发调**。默认 `RATE_LIMIT_PER_MIN=60` 面向内测；
   小程序上线后同一出口 IP 的用户会共享计数，需要评估是否调大或改用
   `TRUST_PROXY=1` + 真实用户 IP。
4. **若同时有 H5 跨域需求**，再配 `CORS_ALLOW_ORIGINS`（见第四节）。

---

## 三、接口调用（小程序侧）

完整可用接口见 [API.md](API.md)。小程序侧的调用方式：

```js
// 上传（wx.uploadFile 的特殊点：返回的是字符串，要自己 JSON.parse）
wx.uploadFile({
  url: `${BASE}/api/upload`,
  filePath: tempFilePath,
  name: 'file',                       // 必须是 file，后端用 request.files.get("file")
  success(res) {
    const up = JSON.parse(res.data);  // ← 极易漏的一步
    if (!up.ok) { /* 处理错误 */ }
    const fileId = up.file_id;        // 服务端会话句柄，后续都要带
    // ...
  }
});

// 分析
wx.request({
  url: `${BASE}/api/analyze`,
  method: 'POST',
  header: { 'Content-Type': 'application/json' },
  data: { file_id: fileId, method: 'independent_t',
          group_col: 'gender', value_col: 'score' },
  success(res) { /* res.data.summary.p ... */ }
});
```

**为什么不用 cookie/登录态**：本项目后端全内存、不落盘、无账号体系。
`file_id` 就是会话句柄（服务端 `_SESSION` 字典的键）。
好处是小程序、H5、CLI、桌面端**共用同一套调用方式**，无需各自的鉴权适配。

⚠️ 代价要说清：`file_id` 没有鉴权，**拿到它就能读那份数据**。
因此小程序端**不要**把 `file_id` 用于分享/跨用户传递；
后端目前是内测定位，若要做多人隔离需另加鉴权（属于 P3⑨ 协作审阅的范畴）。

另一个已知行为：`_SESSION` 存在**内存**里，所以**服务重启后所有 `file_id` 立即失效**，
用户会收到「会话已过期，请重新上传文件」。这是「数据不落盘」换来的必然结果，
不是缺陷；小程序侧应对 400 且 `error` 含「会话已过期」时自动引导重新上传。

---

## 四、CORS（仅 H5 / 第三方网页需要）

默认**完全关闭**：不设 `CORS_ALLOW_ORIGINS` 时，后端不发任何 CORS 头，
浏览器同源访问行为与以前完全一致。

```bash
# 只允许自己的 H5 站点
CORS_ALLOW_ORIGINS=https://app.example.com,https://demo.example.com

# 允许携带凭据（本项目不用 cookie，通常不需要开）
CORS_ALLOW_CREDENTIALS=1
```

行为约定（已由 `cross_platform_test.py` 锁住）：

| 场景 | 结果 |
| --- | --- |
| 未配白名单 | 不发任何 CORS 头；同源访问不受影响 |
| 来源在白名单 | 回显该来源 + `Vary: Origin` |
| 来源不在白名单 | **不发头**（浏览器侧自然拒绝），但请求本身照常处理 |
| OPTIONS 预检（允许） | `204` + `Access-Control-Max-Age: 600` |
| OPTIONS 预检（拒绝） | `403`（显式拒绝，而不是静默 200） |
| `*` + 凭据 | 回显具体来源而非 `*`（符合浏览器规范），并在启动时**告警** |

两个安全细节：
- **`Vary: Origin` 必带**，否则 CDN/代理可能把 A 站的响应缓存后发给 B 站。
- **预检不计入限流**。浏览器每次跨域真实请求前都先发预检；若预检也计数，
  用户会被自己的预检耗光额度，症状是「跨域调用时好时坏」——极难排查。

---

## 五、Uni-app 路线（若要做多端）

规划里的思路是 Uni-app 一套代码编译到 H5 + 小程序。
**后端不需要任何改动**，Uni-app 侧只需注意：

- 用 `uni.uploadFile` / `uni.request`，API 形状与小程序原生一致；
- `manifest.json` 里配各端 AppID；
- 小程序端仍需走第二节的域名配置；
- H5 端若与后端**不同源**，才需要第四节开 CORS。

> ⚠️ 本仓库**没有**内置 Uni-app 工程。原因是 Uni-app 需要独立工具链与
> 构建产物管理，塞进这个「零构建 Python 项目」会破坏开箱即跑的定位。
> 需要时在独立目录建工程，直接用本后端的 HTTP 接口即可。

---

## 六、验证清单

上线前后按这个顺序检查：

1. `curl https://your.domain.com/health` → 200
2. 微信后台三个域名都填了、且已备案
3. 小程序里上传一个小 CSV → 拿到 `file_id`
4. 带 `file_id` 调 `/api/analyze` → `ok: true` 且 `summary` 有值
5. 结果与网页端/CLI **一致**（同一份数据同一方法，统计量应完全相同）
6. 连点多次确认没有意外 429（若被限，按第二节第 3 条调整）

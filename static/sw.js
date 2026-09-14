/* 智论助手 Service Worker —— 只缓存「外壳」，绝不缓存任何 API 结果
 * =====================================================================
 * 设计底线（很重要）：
 *   1. /api/* 一律**不拦截**（直接透传网络）。统计结果、论文核查、体检报告
 *      都是「随数据变化」的，任何缓存都可能导致用户看到旧结果 → 正确性事故。
 *   2. 只缓存静态外壳（HTML / 图标 / manifest），让「安装成桌面应用」后
 *      能快速起屏；离线时给出明确的离线页，而不是白屏或假结果。
 *   3. 不做后台同步、不缓存上传文件（数据全内存，本就不落盘）。
 */
const VERSION = 'v1';
const SHELL_CACHE = `zhilun-shell-${VERSION}`;
const OFFLINE_URL = '/static/offline.html';

/* 预缓存：仅外壳与图标。index.html 也缓存，作为离线起屏兜底。 */
const SHELL_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/favicon.ico',
  OFFLINE_URL,
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      // 逐个 add，单个失败不影响整体安装（例如某个图标缺失）
      await Promise.all(
        SHELL_ASSETS.map((url) =>
          cache.add(url).catch(() => {
            /* 忽略单个资源失败 */
          })
        )
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // 清理旧版本缓存
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k.startsWith('zhilun-shell-') && k !== SHELL_CACHE)
          .map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

/* 判断是否该由 SW 接管：
 *   - 非 GET（POST 上传数据 / 分析）→ 不接管
 *   - /api/ 开头的任何请求 → 不接管（正确性底线）
 */
function shouldHandle(request, url) {
  if (request.method !== 'GET') return false;
  if (url.pathname.startsWith('/api/')) return false;
  if (url.origin !== self.location.origin) return false;
  return true;
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (!shouldHandle(event.request, url)) return; // 交回浏览器默认行为

  // 导航请求（打开页面）：网络优先，断网时退回缓存的首页 → 离线页
  if (event.request.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(event.request);
          const cache = await caches.open(SHELL_CACHE);
          cache.put('/', fresh.clone()); // 更新外壳
          return fresh;
        } catch (e) {
          const cache = await caches.open(SHELL_CACHE);
          return (
            (await cache.match('/')) ||
            (await cache.match(OFFLINE_URL)) ||
            new Response('离线：请确认智论助手服务已启动。', {
              status: 503,
              headers: { 'Content-Type': 'text/plain; charset=utf-8' },
            })
          );
        }
      })()
    );
    return;
  }

  // 其它静态资源：缓存优先（图标 / manifest 基本不变），后台顺带更新
  event.respondWith(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      const cached = await cache.match(event.request);
      if (cached) {
        // 后台刷新（stale-while-revalidate）
        fetch(event.request)
          .then((resp) => {
            if (resp && resp.ok) cache.put(event.request, resp.clone());
          })
          .catch(() => {});
        return cached;
      }
      try {
        const resp = await fetch(event.request);
        if (resp && resp.ok) cache.put(event.request, resp.clone());
        return resp;
      } catch (e) {
        return new Response('', { status: 504 });
      }
    })()
  );
});

/* 允许页面主动触发更新（前端点「有新版本」时） */
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

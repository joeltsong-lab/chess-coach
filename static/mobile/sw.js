/* 象棋教练 · 手机版 Service Worker
 *
 * 职责:
 *   1) 预缓存「应用外壳」: /mobile 页面 + manifest + 图标 + 离线提示页
 *   2) 棋局/引擎数据(/api/**) 一律走网络, 不缓存 —— 分析结果必须实时
 *   3) 页面导航「网络优先」: 断网时用缓存的外壳, 再退到离线提示页
 *   4) 版本号管理: 改 CACHE_VERSION 即触发更新, 旧缓存自动清掉
 *
 * 注意: 本文件虽然放在 static/mobile/ 下, 但由 app.py 的 /sw.js 路由以
 *      「根作用域」提供 —— 若直接注册 /static/mobile/sw.js, 作用域只有
 *      /static/mobile/, 管不到 /mobile 页面。
 */

const CACHE_VERSION = 'chess-coach-mobile-v1';
const CACHE_NAME = CACHE_VERSION;

// 需要预缓存的资源(应用外壳)。coach.html 由 /mobile 路由渲染, 用页面地址缓存。
const PRECACHE_URLS = [
  '/mobile',
  '/static/mobile/manifest.json',
  '/static/mobile/icon-192.png',
  '/static/mobile/icon-512.png',
  '/static/mobile/offline.html',
];

const OFFLINE_URL = '/static/mobile/offline.html';

/* ---------- 安装: 预缓存外壳, 立刻接管 ---------- */
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    // 逐个 add, 单个资源失败(如临时 404)不影响整体安装
    await Promise.all(PRECACHE_URLS.map(async url => {
      try {
        await cache.add(new Request(url, {cache: 'reload'}));
      } catch (e) {
        // 忽略单个失败
      }
    }));
    await self.skipWaiting();   // 新 SW 不等旧页面关闭, 直接进入等待队列
  })());
});

/* ---------- 激活: 清掉旧版本缓存, 立刻接管所有页面 ---------- */
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

/* ---------- 拦截请求 ---------- */
self.addEventListener('fetch', event => {
  const req = event.request;

  // 只处理 GET; POST(走子/分析/保存)一律直连网络
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // 跨域不插手

  // 1) 棋局 / 引擎数据: 纯网络, 不缓存
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(req));
    return;
  }

  // 2) 页面导航(打开 /mobile): 网络优先 → 缓存外壳 → 离线提示页
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const resp = await fetch(req);
        const cache = await caches.open(CACHE_NAME);
        cache.put('/mobile', resp.clone());
        return resp;
      } catch (e) {
        const cached = await caches.match('/mobile');
        if (cached) return cached;
        const offline = await caches.match(OFFLINE_URL);
        return offline || new Response('需要连接电脑', {
          status: 503,
          headers: {'Content-Type': 'text/plain; charset=utf-8'},
        });
      }
    })());
    return;
  }

  // 3) 其他静态资源: 网络优先(拿到就顺手更新缓存), 失败回退缓存
  event.respondWith((async () => {
    try {
      const resp = await fetch(req);
      if (resp && resp.ok && resp.type === 'basic') {
        const cache = await caches.open(CACHE_NAME);
        cache.put(req, resp.clone());
      }
      return resp;
    } catch (e) {
      const cached = await caches.match(req);
      if (cached) return cached;
      throw e;
    }
  })());
});

/* ---------- 页面可通过 postMessage('SKIP_WAITING') 主动切换新版本 ---------- */
self.addEventListener('message', event => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

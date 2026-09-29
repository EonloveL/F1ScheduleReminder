// Cloudflare Worker - F1 livetiming 归档数据中转（免费额度足够）
// 用途：阿里云等机房 IP 被 livetiming.formula1.com 风控 403 时，经 Cloudflare 边缘节点中转
// 部署：
//   1. https://workers.cloudflare.com 注册免费账号 -> Create Worker
//   2. 粘贴本文件全部内容 -> Deploy
//   3. 得到地址如 https://f1-livetiming-relay.<你的子域>.workers.dev
//   4. 服务器 .env 配置: TELEMETRY_BASE_URL=https://f1-livetiming-relay.<你的子域>.workers.dev/static
// 安全：仅放行 /static/ 路径，防滥用做开放代理

const UPSTREAM = "https://livetiming.formula1.com";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 只允许 /static/ 路径
    if (!url.pathname.startsWith("/static/")) {
      return new Response("Not allowed", { status: 403 });
    }

    const upstreamUrl = UPSTREAM + url.pathname + url.search;

    const resp = await fetch(upstreamUrl, {
      method: "GET",
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*",
      },
      // 大文件流式转发（遥测流约10MB）
    });

    const headers = new Headers(resp.headers);
    headers.set("Access-Control-Allow-Origin", "*");
    headers.set("Cache-Control", "public, max-age=86400");
    return new Response(resp.body, { status: resp.status, headers });
  },
};

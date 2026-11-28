// Terraform supplies the approved HTML at deployment. No request reaches an upstream.
export function familyPublicPages(hostname, documents) {
  const pages = new Map(Object.entries(documents));
  const headers = {
    "Content-Type": "text/html; charset=utf-8",
    // Preserve the visible support address and mailto link. Cloudflare otherwise
    // rewrites them and injects a decoding script into the document.
    "Cache-Control": "no-cache, no-transform",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
  };
  return {
    fetch(request) {
      const url = new URL(request.url);
      if (url.hostname !== hostname || !pages.has(url.pathname)) {
        return new Response(null, { status: 404 });
      }
      if (request.method !== "GET" && request.method !== "HEAD") {
        return new Response(null, { status: 405, headers: { Allow: "GET, HEAD" } });
      }
      return new Response(request.method === "HEAD" ? null : pages.get(url.pathname), { headers });
    },
  };
}

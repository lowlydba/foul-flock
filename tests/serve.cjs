// Dependency-free static file server for Playwright's webServer, so the
// a11y suite doesn't assume python exists on the runner (ubuntu-slim).
// Usage: node tests/serve.cjs [port] [rootDir]
const http = require("http");
const fs = require("fs");
const path = require("path");

const port = Number(process.argv[2] || 8081);
const root = path.resolve(process.argv[3] || "web");
const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".geojson": "application/geo+json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".txt": "text/plain; charset=utf-8",
};

http.createServer((req, res) => {
  const urlPath = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
  let fp = path.normalize(path.join(root, urlPath));
  if (!fp.startsWith(root)) {
    res.writeHead(403);
    return res.end();
  }
  if (fs.statSync(fp, { throwIfNoEntry: false })?.isDirectory()) {
    fp = path.join(fp, "index.html");
  }
  fs.readFile(fp, (err, data) => {
    if (err) {
      res.writeHead(404);
      return res.end("not found");
    }
    res.writeHead(200, {
      "content-type": types[path.extname(fp).toLowerCase()] || "application/octet-stream",
    });
    res.end(data);
  });
}).listen(port, "127.0.0.1");

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(root, "public");
const source = path.join(publicDir, "v9");
const mime = {
  ".png": "image/png",
  ".mp4": "video/mp4",
  ".woff2": "font/woff2",
};
const inlineAsset = (relative) => {
  const absolute = path.join(publicDir, relative.replace(/^\//, ""));
  if (!absolute.startsWith(publicDir + path.sep))
    throw new Error("Asset outside public directory");
  return `data:${mime[path.extname(absolute)]};base64,${fs.readFileSync(absolute).toString("base64")}`;
};
const css = fs
  .readFileSync(path.join(source, "style.css"), "utf8")
  .replace(
    /url\(["'](\/assets\/[^"']+)["']\)/g,
    (_, url) => `url('${inlineAsset(url)}')`,
  );
const js = fs
  .readFileSync(path.join(source, "app.js"), "utf8")
  .replace(/<\/script/gi, "<\\/script");
let html = fs.readFileSync(path.join(source, "index.html"), "utf8");
html = html
  .replace(
    /<link rel="stylesheet" href="\/v9\/style\.css"\s*\/?>/,
    () => `<style>\n${css}\n</style>`,
  )
  .replace(/src="(\/assets\/[^"]+)"/g, (_, url) => `src="${inlineAsset(url)}"`)
  .replace(
    '<script src="/v9/app.js"></script>',
    () => `<script>\n${js}\n</script>`,
  );
for (const name of ["CoreTeams-прототип-v9.html", "CoreTeams-прототип.html"])
  fs.writeFileSync(path.join(root, name), html);
console.log(
  `Built v9 and current standalone: ${(Buffer.byteLength(html) / 1024 / 1024).toFixed(1)} MiB each.`,
);

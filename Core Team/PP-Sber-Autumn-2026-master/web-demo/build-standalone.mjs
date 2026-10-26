import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(root, "public");

let html = fs.readFileSync(path.join(publicDir, "index.html"), "utf8");
const baseCss = fs.readFileSync(path.join(publicDir, "app.css"), "utf8");
const extraCss = fs.readFileSync(path.join(publicDir, "extra.css"), "utf8");
const script = fs.readFileSync(path.join(publicDir, "app.js"), "utf8");
const bookSpread = fs.readFileSync(path.join(publicDir, "assets", "book-spread.png")).toString("base64");

html = html
  .replace(/\s*<link rel="stylesheet" href="\/app\.css[^>]*>/, `\n  <style>\n${baseCss}\n${extraCss}\n  </style>`)
  .replace(/\s*<link rel="stylesheet" href="\/extra\.css[^>]*>/, "")
  .replace("/assets/book-spread.png", `data:image/png;base64,${bookSpread}`)
  .replace(/\s*<script src="\/app\.js[^>]*><\/script>/, `\n  <script>\n${script.replaceAll("</script>", "<\\/script>")}\n  </script>`);

fs.writeFileSync(path.join(root, "CoreTeams-прототип.html"), html);
console.log("Standalone prototype built: CoreTeams-прототип.html");

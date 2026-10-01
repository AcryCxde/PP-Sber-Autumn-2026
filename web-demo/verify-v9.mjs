import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(root, "CoreTeams-прототип-v9.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, "Standalone must contain its application script");
new vm.Script(script);
assert.ok(html.includes("<style>"), "Standalone must contain its styles");
assert.doesNotMatch(html, /(?:src|href)=["']\/(?:assets|v9)\//, "No unresolved local asset URLs");
assert.doesNotMatch(html, /url\(["']?\/assets\//, "Fonts must also work offline");
assert.equal((html.match(/data:font\/woff2;base64/g) || []).length, 4);
assert.ok(html.includes("data:video/mp4;base64,"));
assert.ok(html.includes("data:image/png;base64,"));
assert.equal(html, fs.readFileSync(path.join(root, "CoreTeams-прототип.html"), "utf8"));
console.log("v9 verified: valid inline JavaScript, embedded styles, 4 fonts, images and video; current file matches v9.");

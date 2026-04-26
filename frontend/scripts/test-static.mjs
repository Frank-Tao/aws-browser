import { access, readFile } from "node:fs/promises";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const dist = join(root, "dist");

await access(join(dist, "index.html"));
await access(join(dist, "config.js"));
await access(join(dist, "assets", "app.js"));
await access(join(dist, "assets", "styles.css"));

const index = await readFile(join(dist, "index.html"), "utf8");
if (!index.includes("/assets/app.js") || !index.includes("/config.js")) {
  throw new Error("dist/index.html does not reference expected runtime assets");
}

const config = await readFile(join(dist, "config.js"), "utf8");
if (!config.includes("AWS_BROWSER_CONFIG")) {
  throw new Error("dist/config.js does not define AWS_BROWSER_CONFIG");
}

console.log("frontend static smoke test passed");

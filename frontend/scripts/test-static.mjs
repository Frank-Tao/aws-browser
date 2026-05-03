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

const app = await readFile(join(dist, "assets", "app.js"), "utf8");
if (!app.includes("collectFilesFromHandle(rootHandle, rootHandle.name)")) {
  throw new Error("folder picker must preserve the selected root folder in relative upload paths");
}
if (!app.includes("new WeakMap()") || !app.includes("fileRelativePaths.set(file,")) {
  throw new Error("folder picker paths must be stored outside File objects so JSON upload keeps hierarchy");
}
if (!app.includes("data-delete-prefix") || !app.includes("deletePrefix(")) {
  throw new Error("S3 folder rows must expose a delete-folder action");
}
if (!app.includes("/api/prefix?prefix=") || !app.includes("files will be deleted in batch")) {
  throw new Error("folder delete must preview file count and confirm before batch deletion");
}

console.log("frontend static smoke test passed");

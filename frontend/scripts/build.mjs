import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const dist = join(root, "dist");

const apiBaseUrl = process.env.AWS_BROWSER_API_BASE_URL || "";
const apiToken = process.env.AWS_BROWSER_API_TOKEN || "";

await rm(dist, { recursive: true, force: true });
await mkdir(join(dist, "assets"), { recursive: true });

await cp(join(root, "styles.css"), join(dist, "assets", "styles.css"));
await cp(join(root, "app.js"), join(dist, "assets", "app.js"));

const index = await readFile(join(root, "index.html"), "utf8");
await writeFile(join(dist, "index.html"), index);
await writeFile(
  join(dist, "config.js"),
  `window.AWS_BROWSER_CONFIG = ${JSON.stringify(
    {
      apiBaseUrl,
      apiToken,
    },
    null,
    2,
  )};\n`,
);

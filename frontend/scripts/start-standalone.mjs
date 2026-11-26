/**
 * Runs the production artefact the way the Docker image does.
 *
 * `next.config.ts` uses `output: "standalone"`, and Next does **not** copy
 * `.next/static` or `public` into `.next/standalone` — the Dockerfile copies them
 * itself (Dockerfile:27-29). `next start` refuses to work with standalone output,
 * so E2E must assemble the same tree and boot `server.js`:
 *
 *   node scripts/start-standalone.mjs        # PORT=3100 by default (E2E)
 */
import { spawn } from "node:child_process";
import { cpSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const standalone = path.join(root, ".next", "standalone");
const server = path.join(standalone, "server.js");
const staticDir = path.join(root, ".next", "static");
const publicDir = path.join(root, "public");

if (!existsSync(server)) {
  console.error(
    `[start-standalone] ${server} not found — run \`NEXT_PUBLIC_API_BASE_URL=... next build\` first.`
  );
  process.exit(1);
}

// Same layout as the runner stage of the Dockerfile.
cpSync(staticDir, path.join(standalone, ".next", "static"), { recursive: true, force: true });
if (existsSync(publicDir)) {
  cpSync(publicDir, path.join(standalone, "public"), { recursive: true, force: true });
}

const port = process.env.PORT ?? process.env.E2E_PORT ?? "3100";
const child = spawn(process.execPath, [server], {
  cwd: standalone,
  stdio: "inherit",
  env: {
    ...process.env,
    NODE_ENV: "production",
    PORT: port,
    // Bind all interfaces so both `localhost` (IPv6 first) and 127.0.0.1 work.
    HOSTNAME: process.env.HOSTNAME ?? "0.0.0.0",
  },
});

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}

import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

async function reservePort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}

async function waitUntilReady(url, child) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.startError) throw child.startError;
    if (child.exitCode !== null) throw new Error(`E2E server exited with ${child.exitCode}`);
    try {
      const response = await fetch(url, { redirect: "manual" });
      if (response.status === 200) return;
    } catch (_) {
      // The socket is not accepting connections yet; poll until the deadline.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`E2E server was not ready within 30s: ${url}`);
}

async function stop(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  const exited = new Promise((resolve) => child.once("exit", resolve));
  const forced = new Promise((resolve) => setTimeout(resolve, 5_000, "timeout"));
  if (await Promise.race([exited, forced]) === "timeout" && child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
}

const frontend = new URL("..", import.meta.url);
const root = await mkdtemp(join(tmpdir(), "llm-wiki-playwright-"));
const port = await reservePort();
let mcpPort = await reservePort();
while (mcpPort === port) mcpPort = await reservePort();
const baseURL = `http://127.0.0.1:${port}`;
const environment = {
  ...process.env,
  LLM_WIKI_E2E_ROOT: root,
  LLM_WIKI_E2E_PORT: String(port),
  LLM_WIKI_E2E_MCP_PORT: String(mcpPort),
  PLAYWRIGHT_BASE_URL: baseURL,
};
const server = spawn("uv", ["run", "--project", "..", "python", "e2e/server.py"], {
  cwd: frontend,
  env: environment,
  stdio: ["ignore", "inherit", "inherit"],
});
server.startError = null;
server.on("error", (error) => { server.startError = error; });

let exitCode = 1;
try {
  await waitUntilReady(`${baseURL}/login`, server);
  const runner = spawn(process.execPath, ["node_modules/@playwright/test/cli.js", "test"], {
    cwd: frontend,
    env: environment,
    stdio: "inherit",
  });
  exitCode = await new Promise((resolve, reject) => {
    runner.once("error", reject);
    runner.once("exit", (code, signal) => resolve(code ?? (signal ? 1 : 0)));
  });
} finally {
  await stop(server);
  await rm(root, { recursive: true, force: true });
}
process.exitCode = exitCode;

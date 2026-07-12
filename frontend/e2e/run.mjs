import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  createCleanupCoordinator,
  stop,
  waitForAnnouncement,
  waitForExit,
  waitUntilReady,
} from "./run-support.mjs";

const frontend = new URL("..", import.meta.url);
const root = await mkdtemp(join(tmpdir(), "llm-wiki-playwright-"));
let server = null;
let runner = null;
const cleanupCoordinator = createCleanupCoordinator({
  signalTarget: process,
  stopChildren: () => Promise.all([stop(runner), stop(server)]),
  removeRoot: () => rm(root, { recursive: true, force: true }),
  exitProcess: (code) => process.exit(code),
});
cleanupCoordinator.install();

let exitCode = 1;
try {
  server = spawn("uv", ["run", "--project", "..", "python", "e2e/server.py"], {
    cwd: frontend,
    env: { ...process.env, LLM_WIKI_E2E_ROOT: root },
    stdio: ["ignore", "pipe", "inherit"],
  });
  const { url: baseURL } = await waitForAnnouncement(server);
  await waitUntilReady(`${baseURL}/login`, server);

  runner = spawn(process.execPath, ["node_modules/@playwright/test/cli.js", "test"], {
    cwd: frontend,
    env: { ...process.env, PLAYWRIGHT_BASE_URL: baseURL },
    stdio: "inherit",
  });
  await waitForExit(runner, { rejectOnError: true });
  exitCode = runner.exitCode ?? (runner.signalCode === null ? 0 : 1);
} finally {
  await cleanupCoordinator.finish();
}
process.exitCode = exitCode;

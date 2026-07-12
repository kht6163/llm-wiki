import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";

import { hasExited, stop, waitForAnnouncement, waitForExit } from "./run-support.mjs";

class FakeChild extends EventEmitter {
  constructor({ exitCode = null, signalCode = null } = {}) {
    super();
    this.exitCode = exitCode;
    this.signalCode = signalCode;
    this.stdout = new PassThrough();
    this.kills = [];
  }

  kill(signal) {
    this.kills.push(signal);
    return true;
  }
}

test("signal termination before readiness rejects immediately", async () => {
  const child = new FakeChild({ signalCode: "SIGTERM" });

  await assert.rejects(waitForAnnouncement(child), /SIGTERM/);
  assert.equal(hasExited(child), true);
});

test("stop returns for an already-signaled child without sending another signal", async () => {
  const child = new FakeChild({ signalCode: "SIGTERM" });

  await stop(child);

  assert.deepEqual(child.kills, []);
});

test("stop observes an exit emitted synchronously by SIGTERM", async () => {
  const child = new FakeChild();
  child.kill = (signal) => {
    child.kills.push(signal);
    child.signalCode = signal;
    child.emit("exit", null, signal);
    return true;
  };

  await stop(child);

  assert.deepEqual(child.kills, ["SIGTERM"]);
});

test("stop cancels its grace timer when normal exit wins", async () => {
  const child = new FakeChild();
  const timer = { cleared: false };
  child.kill = (signal) => {
    child.kills.push(signal);
    queueMicrotask(() => {
      child.exitCode = 0;
      child.emit("exit", 0, null);
    });
    return true;
  };

  await stop(child, {
    setTimeoutImpl: () => timer,
    clearTimeoutImpl: (value) => { value.cleared = true; },
  });

  assert.equal(timer.cleared, true);
});

test("runner spawn errors reject instead of waiting for an exit event", async () => {
  const child = new FakeChild();
  const waiting = waitForExit(child, { rejectOnError: true });

  child.emit("error", new Error("spawn failed"));

  await assert.rejects(waiting, /spawn failed/);
});

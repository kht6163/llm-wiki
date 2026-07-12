import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";

import {
  createCleanupCoordinator,
  hasExited,
  stop,
  waitForAnnouncement,
  waitForExit,
  waitUntilReady,
} from "./run-support.mjs";

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));

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

test("stop waits for actual exit after grace timeout sends SIGKILL", async () => {
  const child = new FakeChild();
  let fireTimeout;
  let completed = false;
  const stopping = stop(child, {
    setTimeoutImpl: (callback) => {
      fireTimeout = callback;
      return {};
    },
    clearTimeoutImpl: () => {},
  }).then(() => { completed = true; });

  fireTimeout("timeout");
  await nextTurn();
  assert.deepEqual(child.kills, ["SIGTERM", "SIGKILL"]);
  assert.equal(completed, false);

  child.exitCode = 137;
  child.emit("exit", 137, null);
  await stopping;
  assert.equal(completed, true);
});

test("cleanup keeps signal handlers installed and reentrant signals share one cleanup", async () => {
  const signalTarget = new EventEmitter();
  let releaseChildren;
  let removals = 0;
  const exits = [];
  const coordinator = createCleanupCoordinator({
    signalTarget,
    stopChildren: () => new Promise((resolve) => { releaseChildren = resolve; }),
    removeRoot: async () => { removals += 1; },
    exitProcess: (code) => { exits.push(code); },
  });
  coordinator.install();

  const cleanupOne = coordinator.cleanup();
  const cleanupTwo = coordinator.cleanup();
  const finishing = coordinator.finish();
  await nextTurn();
  assert.strictEqual(cleanupOne, cleanupTwo);
  assert.equal(signalTarget.listenerCount("SIGINT"), 1);
  assert.equal(signalTarget.listenerCount("SIGTERM"), 1);

  signalTarget.emit("SIGINT");
  signalTarget.emit("SIGTERM");
  assert.equal(removals, 0);
  assert.equal(signalTarget.listenerCount("SIGINT"), 1);
  assert.equal(signalTarget.listenerCount("SIGTERM"), 1);
  releaseChildren();
  await finishing;
  await Promise.resolve();

  assert.equal(removals, 1);
  assert.deepEqual(exits, [130]);
  assert.equal(signalTarget.listenerCount("SIGINT"), 0);
  assert.equal(signalTarget.listenerCount("SIGTERM"), 0);
});

test("announcement wait rejects at its injected deadline", async () => {
  const child = new FakeChild();
  const timer = { cleared: false };
  let expire;
  const announced = waitForAnnouncement(child, {
    deadlineMs: 321,
    setTimeoutImpl: (callback, delay) => {
      assert.equal(delay, 321);
      expire = callback;
      return timer;
    },
    clearTimeoutImpl: (value) => { value.cleared = true; },
  });

  expire();

  await assert.rejects(announced, /321ms/);
  assert.equal(timer.cleared, true);
});

test("hung readiness fetch is aborted at the remaining overall deadline", async () => {
  const child = new FakeChild();
  let now = 1_000;
  let capturedSignal;
  let deadlineTimer;
  const ready = waitUntilReady("http://127.0.0.1/login", child, {
    deadlineMs: 500,
    now: () => now,
    fetchImpl: (_url, options) => {
      capturedSignal = options.signal;
      return new Promise((_resolve, reject) => {
        capturedSignal?.addEventListener("abort", () => reject(new Error("aborted")));
      });
    },
    setTimeoutImpl: (callback, delay) => {
      deadlineTimer = { callback, delay };
      return deadlineTimer;
    },
    clearTimeoutImpl: () => {},
  });
  await Promise.resolve();

  assert.ok(capturedSignal instanceof AbortSignal);
  assert.equal(deadlineTimer.delay, 500);
  now += deadlineTimer.delay;
  deadlineTimer.callback();

  await assert.rejects(ready, /500ms/);
  assert.equal(capturedSignal.aborted, true);
});

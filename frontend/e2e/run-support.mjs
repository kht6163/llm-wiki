const READY_PREFIX = "LLM_WIKI_E2E_READY ";

export function hasExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function exitDescription(child, code = child.exitCode, signal = child.signalCode) {
  if (signal !== null) return `signal ${signal}`;
  return `code ${code}`;
}

export function waitForExit(child, { rejectOnError = false } = {}) {
  if (hasExited(child)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      child.removeListener("error", onError);
      child.removeListener("exit", onExit);
    };
    const onExit = () => {
      cleanup();
      resolve();
    };
    const onError = (error) => {
      cleanup();
      if (rejectOnError) reject(error);
      else resolve();
    };
    child.once("error", onError);
    child.once("exit", onExit);
    if (hasExited(child)) {
      onExit();
    }
  });
}

export function waitForAnnouncement(child, {
  deadlineMs = 30_000,
  setTimeoutImpl = setTimeout,
  clearTimeoutImpl = clearTimeout,
} = {}) {
  if (hasExited(child)) {
    return Promise.reject(new Error(`E2E server exited with ${exitDescription(child)} before readiness`));
  }
  if (!child.stdout) return Promise.reject(new Error("E2E server stdout is unavailable"));

  return new Promise((resolve, reject) => {
    let buffer = "";
    let settled = false;
    let timeoutId;
    const cleanup = () => {
      clearTimeoutImpl(timeoutId);
      child.removeListener("error", onError);
      child.removeListener("exit", onExit);
      child.stdout.removeListener("data", onData);
    };
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback(value);
    };
    const onError = (error) => finish(reject, error);
    const onExit = (code, signal) => finish(
      reject,
      new Error(`E2E server exited with ${exitDescription(child, code, signal)} before readiness`),
    );
    const onData = (chunk) => {
      buffer += chunk.toString();
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline).trimEnd();
        buffer = buffer.slice(newline + 1);
        if (!line.startsWith(READY_PREFIX)) continue;
        try {
          const announcement = JSON.parse(line.slice(READY_PREFIX.length));
          if (typeof announcement.url !== "string") throw new Error("missing readiness URL");
          finish(resolve, announcement);
        } catch (error) {
          finish(reject, new Error(`Invalid E2E readiness announcement: ${error.message}`));
        }
        return;
      }
    };

    child.once("error", onError);
    child.once("exit", onExit);
    child.stdout.on("data", onData);
    timeoutId = setTimeoutImpl(
      () => finish(reject, new Error(`E2E server did not announce readiness within ${deadlineMs}ms`)),
      deadlineMs,
    );
    if (hasExited(child)) onExit(child.exitCode, child.signalCode);
  });
}

export async function waitUntilReady(url, child, {
  fetchImpl = fetch,
  deadlineMs = 30_000,
  pollMs = 100,
  now = Date.now,
  setTimeoutImpl = setTimeout,
  clearTimeoutImpl = clearTimeout,
} = {}) {
  const deadline = now() + deadlineMs;
  while (now() < deadline) {
    if (hasExited(child)) {
      throw new Error(`E2E server exited with ${exitDescription(child)} before readiness`);
    }
    const remaining = deadline - now();
    const controller = new AbortController();
    let fetchTimeoutId;
    const fetchTimeout = new Promise((_, reject) => {
      fetchTimeoutId = setTimeoutImpl(() => {
        controller.abort();
        reject(new Error("readiness fetch deadline exceeded"));
      }, remaining);
    });
    try {
      const response = await Promise.race([
        fetchImpl(url, { redirect: "manual", signal: controller.signal }),
        fetchTimeout,
      ]);
      if (response.status === 200) return;
    } catch (_) {
      // The owned socket is not accepting HTTP yet; poll the readiness condition.
    } finally {
      clearTimeoutImpl(fetchTimeoutId);
    }
    if (now() >= deadline) break;
    const delay = Math.min(pollMs, deadline - now());
    await new Promise((resolve) => {
      const timeoutId = setTimeoutImpl(resolve, delay);
      if (delay <= 0) {
        clearTimeoutImpl(timeoutId);
        resolve();
      }
    });
  }
  throw new Error(`E2E server was not ready within ${deadlineMs}ms: ${url}`);
}

export async function stop(child, {
  graceMs = 5_000,
  setTimeoutImpl = setTimeout,
  clearTimeoutImpl = clearTimeout,
} = {}) {
  if (!child || hasExited(child)) return;

  const exited = waitForExit(child);
  if (hasExited(child)) return;
  child.kill("SIGTERM");
  if (hasExited(child)) return;

  let timeoutId;
  const timeout = new Promise((resolve) => {
    timeoutId = setTimeoutImpl(resolve, graceMs, "timeout");
  });
  try {
    if (await Promise.race([exited, timeout]) === "timeout" && !hasExited(child)) {
      child.kill("SIGKILL");
      await exited;
    }
  } finally {
    clearTimeoutImpl(timeoutId);
  }
}

export function createCleanupCoordinator({
  signalTarget,
  stopChildren,
  removeRoot,
  exitProcess,
}) {
  let cleanupPromise = null;
  let signalExitPromise = null;
  let installed = false;
  const handlers = new Map();

  const cleanup = () => {
    if (!cleanupPromise) {
      cleanupPromise = Promise.resolve()
        .then(stopChildren)
        .finally(removeRoot);
    }
    return cleanupPromise;
  };

  const uninstall = () => {
    if (!installed) return;
    for (const [signal, handler] of handlers) signalTarget.removeListener(signal, handler);
    installed = false;
  };

  const install = () => {
    if (installed) return;
    for (const [signal, exitCode] of [["SIGINT", 130], ["SIGTERM", 143]]) {
      const handler = () => {
        if (!signalExitPromise) {
          signalExitPromise = cleanup().then(
            () => exitProcess(exitCode),
            () => exitProcess(exitCode),
          );
        }
      };
      handlers.set(signal, handler);
      signalTarget.on(signal, handler);
    }
    installed = true;
  };

  const finish = async () => {
    try {
      await cleanup();
    } finally {
      uninstall();
    }
  };

  return { cleanup, finish, install };
}

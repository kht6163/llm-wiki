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

export function waitForAnnouncement(child) {
  if (hasExited(child)) {
    return Promise.reject(new Error(`E2E server exited with ${exitDescription(child)} before readiness`));
  }
  if (!child.stdout) return Promise.reject(new Error("E2E server stdout is unavailable"));

  return new Promise((resolve, reject) => {
    let buffer = "";
    const cleanup = () => {
      child.removeListener("error", onError);
      child.removeListener("exit", onExit);
      child.stdout.removeListener("data", onData);
    };
    const finish = (callback, value) => {
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
    if (hasExited(child)) onExit(child.exitCode, child.signalCode);
  });
}

export async function waitUntilReady(url, child, {
  fetchImpl = fetch,
  deadlineMs = 30_000,
  pollMs = 100,
} = {}) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    if (hasExited(child)) {
      throw new Error(`E2E server exited with ${exitDescription(child)} before readiness`);
    }
    try {
      const response = await fetchImpl(url, { redirect: "manual" });
      if (response.status === 200) return;
    } catch (_) {
      // The owned socket is not accepting HTTP yet; poll the readiness condition.
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
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
    }
  } finally {
    clearTimeoutImpl(timeoutId);
  }
}

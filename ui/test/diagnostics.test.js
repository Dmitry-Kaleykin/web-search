import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createSessionLog } from "../src/diagnostics.js";

test("keeps complete output beyond the screen buffer, in a private session file", async (t) => {
  const projectDir = await mkdtemp(path.join(os.tmpdir(), "ws-log-"));
  t.after(() => rm(projectDir, { recursive: true, force: true }));
  const log = createSessionLog(projectDir);
  const output = "Fatal Python error\n" + "context\n".repeat(600) + "last line\n";
  log.append(output.slice(0, 12));
  log.append(output.slice(12));
  assert.equal(await readFile(log.path, "utf8"), output);
  assert.equal((await stat(log.path)).mode & 0o777, 0o600);
});

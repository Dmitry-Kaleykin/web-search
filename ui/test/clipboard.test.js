import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { copyText } from "../src/clipboard.js";

test("copies Unicode and newlines through native stdin, without a shell", async () => {
  const child = new EventEmitter();
  child.stdin = new EventEmitter();
  const text = "Ошибка Python\nexact details: `$(private)`\n";
  child.stdin.end = (value, encoding) => {
    assert.equal(value, text);
    assert.equal(encoding, "utf8");
    queueMicrotask(() => child.emit("close", 0));
  };
  const ok = await copyText(text, { platform: "darwin", spawnProcess: (command, args, options) => {
    assert.equal(command, "/usr/bin/pbcopy");
    assert.deepEqual(args, []);
    assert.equal(options.shell, undefined);
    assert.equal(options.env.LC_ALL, "en_US.UTF-8");
    return child;
  } });
  assert.equal(ok, true);
});

for (const failure of ["exit", "spawn", "stdin"]) {
  test(`does not report copied after a ${failure} failure`, async () => {
    const child = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.end = () => queueMicrotask(() => {
      if (failure === "exit") child.emit("close", 1);
      else if (failure === "spawn") child.emit("error", new Error("cannot run pbcopy"));
      else child.stdin.emit("error", new Error("EPIPE"));
    });
    assert.equal(await copyText("text", { platform: "darwin", spawnProcess: () => child }), false);
  });
}

test("does not claim success on an unsupported platform", async () => {
  assert.equal(await copyText("text", { platform: "test", spawnProcess: () => assert.fail() }), false);
});

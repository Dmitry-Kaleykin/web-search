import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

export function createSessionLog(projectDir) {
  const directory = path.join(projectDir, ".web-search-data/diagnostics");
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const file = path.join(directory, `console-${stamp}-${process.pid}.log`);
  writeFileSync(file, "", { flag: "wx", mode: 0o600 });
  return {
    path: file,
    append(text) { appendFileSync(file, text); },
  };
}

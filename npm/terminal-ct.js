#!/usr/bin/env node
// npm launcher: runs the Python tool shipped beside it (standard library only), with this process's arguments,
// streams and exit code. Python 3.9 or later is needed (python3, python, or py -3 on Windows).
"use strict";
const { spawnSync } = require("child_process");
const path = require("path");
const script = path.join(__dirname, "..", "terminal_ct.py");
const tries = process.platform === "win32" ? [["py", ["-3"]], ["python", []], ["python3", []]]
                                           : [["python3", []], ["python", []]];
for (const [exe, pre] of tries) {
  const ok = spawnSync(exe, [...pre, "-c", "import sys; sys.exit(sys.version_info < (3, 9))"], { stdio: "ignore" });
  if (ok.status !== 0) continue;
  const r = spawnSync(exe, [...pre, script, ...process.argv.slice(2)], { stdio: "inherit" });
  process.exit(r.status === null ? 1 : r.status);
}
console.error("terminal-ct needs Python 3.9 or later: install it (https://www.python.org, or your package manager)");
process.exit(1);

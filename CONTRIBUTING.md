# Contributing

Thanks for helping. terminal-ct stays small on purpose; these rules keep it that way.

## Ground rules

- **One file, standard library only.** `terminal_ct.py` runs on Python 3.9+ with nothing to install. A change that
  needs a dependency will not be merged; a few lines of standard library usually do the job.
- **The engine's own features first.** If Podman or Docker already does it, call it instead of reimplementing it.
- **Same shape as terminal-vm.** Commands, MCP tools and JSON replies keep the names and arguments terminal-vm uses
  for the same thing.
- **Errors say what to do.** A failure names the cause and the command that fixes it, as `doctor` does.
- **Nothing personal in the repository**: no absolute paths, user or host names, tokens.

## Before a pull request

```sh
python3 -m unittest test_terminal_ct                     # simulated engine, must pass everywhere
TERMINAL_CT_REAL=1 python3 -m unittest test_terminal_ct  # against the installed Podman and Docker, when you have them
```

- New behavior comes with a test in `test_terminal_ct.py`; a bug fix with the test that failed before it.
- Update the README (command table and the matching section) and the agent skill
  (`skills/terminal-ct/SKILL.md`) when a command or MCP tool changes.
- One change per pull request, with a message that says what changes for the user and why.

CI runs the unit tests on Linux, Windows and macOS with Python 3.9 and 3.12, and the real tests on Ubuntu.

## Reporting

Bugs: an issue with `terminal-ct version`, the OS and engine, the steps, and `terminal-ct doctor --report` if it
helps. Security problems: see [SECURITY.md](SECURITY.md), never a public issue.

By contributing you agree that your work is released under the [MIT license](LICENSE).

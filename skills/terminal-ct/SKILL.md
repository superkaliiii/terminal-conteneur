---
name: terminal-ct
description: Run work in separate Linux containers with the terminal-ct MCP tools or CLI - a shell (exec) and, when a GUI is needed, a desktop driven by screenshots, click, type and key; build sandboxes, stacks with replicas, isolated networks. Use when a task needs an isolated Linux environment or desktop that is not the user's own machine, and a full VM (terminal-vm) is more than it needs.
---

# Using terminal-ct

terminal-ct runs Docker or Podman containers made for agents: a shell through `exec`, and optionally a desktop
(Xvnc + Openbox + xterm) driven like a screen. Your actions never touch the user's own desktop, files or sessions.
For Windows or macOS desktops, or a full kernel, use terminal-vm instead.

## Start and end of a session

First call `project_status`: your project's containers and networks (with `want`, what each should be doing), the
notes earlier sessions left, and their latest actions and errors. Pick up from there instead of rebuilding. Before
you stop, `notes_write` what you set up, what works and what is left. A server started with `--project` shows you
only that project (and its sub-projects, `lab/web`).

## Shell first

`exec` (`sh -c COMMAND`) returns the exit code, stdout and stderr. `exec` and the screen tools act on the
container the server was started for (`terminal-ct mcp NAME`); a server started without one needs
`container: NAME` on every call. Prefer `exec` over the screen for anything a command
can do (install, build, run tests, read files). Read the exit code before going on. `ct_logs` shows what the
container's main process printed.

## Screen loop (desktop containers): observe -> act -> verify

1. `status`; if it is not running, `ct_start` it (one you created) or ask the user.
2. `screenshot` and read it before every decision. Coordinates are pixels of the latest screenshot. Never guess.
3. One meaningful action, then check: action tools take `screenshot: true` to return the screen once it changed
   (up to 1.5 s); if the effect is still missing, `wait` (it returns a screenshot once the screen settles).
4. `type` sends any text (no keyboard layout involved); `key` takes chords: `ctrl+a`, `alt+tab`, `return`,
   `escape`, `super`, `f5`. The screen tools take `container` to act on another container you manage.
5. Desktops need a Linux host or WSL: on a Windows host without WSL, work through `exec` only.

## What you may run

`ct_list` marks with `managed: true` what you run without asking: the containers you created, and sandboxes
(offline ones: a sandbox the user put online is theirs). The
user's other containers, internet for a container, a network the user made, loading a snapshot of theirs, or
`autostart` need their approval: the client may ask them, otherwise the error names the command for them to run.
Never try to work around a refusal.

## Building things

- `ct_create` (sandboxed by default: not root, no capabilities, process limit, `/data` in memory, fresh at each
  start, offline; `sandbox: false` for a normal container whose `/data` volume persists), `desktop: true` for a
  screen, then `ct_start`. `ct_clone` a prepared container instead of setting one up again (seconds).
- `ct_snapshot` save before a risky step, load to go back (stopped container), not for sandboxes.
- Stacks: `ct_apply` a desired state (`services` with `image`, `replicas`, `cpus`, `memory_gb`, `restart`,
  `health: {exec, every, retries}`, `ttl`); the daemon keeps it so (restarts, health probes, replicas). `ct_scale`
  changes the replicas, `ct_stack` shows or removes it, `ct_events` tells what happened.
- Nodes (Linux VMs running Docker, added by the user with `terminal-ct node add`): `ct_list` marks a container in
  one with `node`; every tool on it works the same (screen tools included, ~1.5 s per action). In a stack,
  `"spread": "nodes"` spreads a service's replicas over the running nodes and moves them when one goes down
  (`failover` events, `/data` not kept); `"node": "N"` keeps them on N. A service on a network joined by nodes
  (`node join`) with `"ports"` is found by name (`web`, `web.STACK`) from that network's containers.
- Other PCs (added by the user with `terminal-ct host add`) are nodes too (`ct_list` shows `node`). Your stack runs
  there only after the user approves that PC for it (`ct_apply` asks once per PC); without approval keep it on this
  PC. No desktops, networks or ports on another PC yet. Their quota is their own (a share of someone's computer).
- `net_create` + `net_attach` (stopped containers): an isolated network where containers reach each other by name.
- Stay within `quota`; `ct_resources` asks for the CPUs and memory a task needs, give them back after; `top` and
  `dashboard` show where things stand.

## Clean up

Remove what you created when the task is done (`ct_stop`, `ct_remove`, `ct_stack` rm, `net_remove`) unless the
user wants to keep it; say so in `notes_write`. `ct_remove` deletes `/data`: never on a container you did not
create unless the user asks.

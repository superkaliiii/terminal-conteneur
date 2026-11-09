# terminal-ct

Containers with a desktop and a shell, for people and computer-use agents. The container counterpart of
[terminal-vm](https://github.com/superkaliiii/terminal-mac-inspection/tree/unified/terminal-vm): same command and
MCP shape, independent of it (each installs and works alone), and able to show its VMs when both are installed.

Status: **phase 8** of the plan: container lifecycle, desktops (any Debian/Ubuntu, Fedora, Alpine or Arch
image), screen tools, terminal viewer, quota and live resources, isolated networks, sandboxes, an MCP server that
manages containers under agent-authority rules, snapshots, clones and file copies, stacks kept in their desired
state by a daemon (replicas, restarts, health probes, ttl, events), a terminal dashboard, a web dashboard
(containers and terminal-vm's VMs), an agent skill, measurements and CI.

## Install

Python >= 3.9 (standard library only) and Podman (preferred: rootless, no daemon) or Docker.
Debian/Ubuntu: `sudo apt install podman`.

```sh
sh install.sh                      # ~/.local/bin/terminal-ct (--prefix DIR elsewhere), then runs doctor
terminal-ct skills install         # the agent's skill into ~/.claude/skills/terminal-ct
claude mcp add terminal-ct -- ~/.local/bin/terminal-ct mcp   # MCP server for Claude Code (absolute path: the
                                                               # client may not have ~/.local/bin on its PATH)
```

`mcp NAME` gives the screen tools and `exec` a default container; `--project P` fences the server to a project.

On a Windows host, put this folder on the PATH (`terminal-ct.cmd`) with Docker Desktop or Podman for Windows, or
install it inside WSL as above (desktops need Linux or WSL). A one-command, all-in-one installer comes in a later
phase.

## Agent skill

[`skills/terminal-ct/SKILL.md`](skills/terminal-ct/SKILL.md) tells an agent how to work with terminal-ct: start
with `project_status`, prefer `exec` to the screen, the observe → act → verify loop on desktops, what it may run
without asking, building sandboxes, stacks and networks, cleaning up. `terminal-ct skills get|path|install`.

## Commands

| Command | Does |
|---|---|
| `create NAME [--image IMAGE] [--cpus 1] [--memory 1] [--offline] [--net NET ...] [-- CMD...]` | create a container (default image `debian:stable-slim`, pulled if missing; memory in GiB, no swap beyond it); a named volume keeps `/data` |
| `create NAME --sandbox ...` | a sandbox: see below |
| `create NAME --desktop [--image IMAGE] [--width 1280 --height 800]` | the same image with a desktop: Xvnc + Openbox + xterm added on top (built once per base, cached as `tct-desktop:<base>`) |
| `action NAME screenshot\|click\|move\|drag\|scroll\|type\|key\|wait\|status ...` | drive the desktop, same arguments as terminal-vm (`screenshot --output`, `click X Y [--button] [--double]`, `drag X Y X2 Y2`, `scroll --dy N`, `type TEXT`, `key CHORD`, `wait --stable --timeout`) |
| `view NAME [--read-only]` | the desktop inside the terminal, keyboard and mouse: Kitty graphics (Kitty, Ghostty, WezTerm) or Sixel (Windows Terminal, also from WSL; foot, mlterm); Ctrl+] exits, Ctrl+O opens the native window |
| `show NAME` | native window: TigerVNC's viewer on the desktop's unix socket |
| `mcp [NAME]` | stdio MCP server, see [Agents](#agents) |
| `start NAME`, `stop NAME [--timeout 10]`, `rm NAME` | lifecycle; `start` checks the quota; `rm` also deletes the `/data` volume |
| `ls`, `status NAME` | JSON state of terminal-ct's containers (ceilings, internet, networks, sandbox) |
| `exec NAME [--timeout 60] CMD...` | run a command: JSON with exit code, stdout, stderr (the CLI exits with the command's code) |
| `resources NAME [--cpus N] [--memory GiB]` | show, or change at once, even while it runs (`update`) |
| `top` | real CPU % (of one core), memory and processes of each running container next to its ceiling, and the quota |
| `quota [--cpus N] [--memory GiB] [--disk GiB] [--containers N]` | show the host quota and its use, or set it |
| `net create NET`, `net rm NET`, `net ls`, `net attach NAME NET`, `net detach NAME NET` | isolated networks: no LAN, no internet; containers on one reach each other by name |
| `internet NAME [on\|off]` | show or change internet access (stopped container) |
| `snapshot NAME save\|load\|rm TAG`, `snapshot NAME ls` | file system and `/data` (load: stopped container), see [Snapshots and clones](#snapshots-and-clones) |
| `clone SRC NEW [--project P]` | NEW from SRC as it is: its files and a copy of its `/data` |
| `push NAME LOCAL PATH`, `pull NAME PATH LOCAL` | copy files in or out (`/data` included), running or stopped |
| `sandbox NAME on\|off` | make a stopped container a sandbox, or a normal one again (recreated from its image) |
| `image ls`, `image pull REF`, `image build NAME DIR`, `image rm NAME` | images (Docker Hub short names are completed for Podman) |
| `bench [--runs 10] [--desktop]` | measure terminal-ct's operations on this engine, see [Measurements](#measurements) |
| `skills get\|path\|install [--dir D]` | the agent's skill, see [Agent skill](#agent-skill) |
| `doctor`, `version`, `help` | checks |
| `node add NAME [--image debian\|alpine] [--cpus 2] [--memory 2] [--disk 20]`, `node ls`, `node rm NAME [--force]` | nodes: Linux VMs of terminal-vm running Docker, see [Nodes](#nodes) |

`--engine docker|podman` (or `TERMINAL_CT_ENGINE`) picks the engine; by default Podman if installed, else Docker.
`--node NAME` runs the command in a node instead.

Safety: every container terminal-ct makes is named `tct-NAME` and labelled `terminal-ct.*`; containers without
those labels are never listed or touched. An image whose default command is a bare shell is kept running with
`tail -f /dev/null` (under `--init`) so it can be used through `exec`.

## Nodes

A node is a Linux VM made by [terminal-vm](https://github.com/superkaliiii/terminal-mac-inspection/tree/unified/terminal-vm)
(installed alongside: on the PATH, or `TERMINAL_VM="python3 /path/terminal_vm.py"`) that runs Docker: the isolation
of a VM, the lightness of containers inside it.
- `node add NAME [--image debian|alpine]` creates it from an official cloud image (`terminal-vm create --image
  --node`), starts it, has terminal-vm restart it if it falls, and waits for its setup (WSL with KVM: Alpine ready
  in about 60 s, Debian in about 120 s; it needs the internet the first time).
- `--node NAME create ...` puts a container there; terminal-ct remembers it (`STORE/nodes.json`), so `start`,
  `stop`, `exec`, `logs`, `status`, `rm` of that container go to its node without `--node`. `--node NAME ls`, `top`,
  `quota` show the node's own containers and capacity.
- terminal-ct stays on the host and decides: each engine call is `docker ...` run in the node by its guest agent,
  no network port opened, nothing listening. terminal-vm is loaded into terminal-ct's own process when it is a Python
  file (`TERMINAL_VM`, or install.sh's launcher), else called by its command line; loaded, `bench` in an Alpine node
  (WSL, host CPU half busy, `bench/results.json`): create 4.5 s, start 2.6 s, exec 0.9 s, ls 0.8 s, stop 1.2 s, rm
  2 s; by the command line each call also started a Python (0.3 s, 1-2 s from a Windows folder): create took 25 s.
- `node rm NAME` refuses while containers live there; `--force` removes them first (their `/data` with them).
- `ls`, `stack show`, the dashboards and the MCP `ct_list` show the nodes' containers too (field `node`); MCP and
  web actions on one go to its node.
- Quota in a node: the node's own size (all its CPUs and memory); the host's quota file does not apply there,
  terminal-vm's quota already fences the node's VM.
- Stacks: a service with `"node": "N"` keeps all its replicas there (none made elsewhere while N is down: `pending`);
  `"spread": "nodes"` spreads them over the running nodes, the least loaded first, and moves a node's replicas to
  another when it is down (`failover` events; their `/data` does not follow). When the node is back, the copies left
  there are removed. Real run (WSL, two Alpine nodes, 3 replicas): node killed, its 2 replicas made again and running
  on the other in 23 s; old copies gone at its return. Not yet with isolated networks (phase 10).
- Desktops in a node (`--node N create NAME --desktop`, or a stack service): the desktop image is built in the node
  (its context sent there), and the screen tools (`action`, MCP screenshot/click/type/key/wait, dashboard
  thumbnails) are run by a copy of terminal-ct the host puts in the node, next to the desktop's socket (copied again
  when it changes). About 1.5 s per action from the CLI. `view` and `show` stay for this host's desktops.
- `push`/`pull`, image builds, snapshots: files and folders cross by the guest agent through a temporary folder of
  the node (folders as tar archives), removed after.

## Desktops

- The desktop listens only on a **unix socket** in a private host folder (`~/.local/share/terminal-ct/NAME/run/`):
  no TCP port, nothing on the network, no password to keep. It runs as **your user** (Podman: `--userns=keep-id`;
  Docker: `--user`), so the socket is yours alone; `/data` is its home (`HOME=/data`, `LANG=C.UTF-8`).
- `start` returns once the desktop shows a window, so the first click lands on it.
- The terminal (xterm) opens maximized and stays so when the screen size changes (TigerVNC's window resizes the
  desktop to fit, by default).
- `type` sends X keysyms: any text, accents and other scripts included, whatever the keyboard layout.
- Measured in WSL Debian (Podman 5.4 rootless, Docker 26.1): desktop ready 0.4-1.3 s after `start`, stop ~1 s
  (exit code 0); first `--desktop` build 23 s (Alpine) to 84 s (Ubuntu), then cached.
- Desktop containers need a Linux host or WSL (the socket does not cross into Windows). Windows containers have no
  desktop, and there are no macOS containers: for those desktops use terminal-vm.

## Quota and resources

- Every container gets a ceiling (default 1 CPU, 1 GiB, set with `--cpus`/`--memory`, changed live with
  `resources`). The **quota** caps the sum over running containers: CPUs, memory, number of containers; and the
  disk space terminal-ct holds (containers' writable layers, all its volumes, its desktop and kept images). Podman
  volumes are measured with `du` when their folders are on this machine, else (a Podman machine on Windows or
  macOS) from Podman's own table (`system df -v`).
- Defaults come from the engine's host (on Windows: the WSL or Docker Desktop VM): cores - 1, 75% of memory, half of
  the space containers can have, 16 running containers. `quota --cpus ...` sets them (stored in `quota.json`).
- Checked under a lock at `start`, `resources` (increase of a running container) and `create` (disk: refused once
  reached; per-container disk limits exist only with some storage drivers, so use is measured, not capped).
- A container made before 0.3 without limits gets the defaults at its next `start`; one already running counts as
  the whole host until then.

## Networks, internet, sandboxes

- Every container sits on the engine's default bridge (internet) or is unplugged from it (`--offline`,
  `internet NAME off`). The user's containers start online; sandboxes and agents' containers start offline.
- `net create NET` makes an engine `--internal` network: no LAN, no internet; its containers reach each other by
  name. Network and internet changes apply to stopped containers.
- A **sandbox** (`--sandbox`, `sandbox NAME on`): `--cap-drop ALL`, `no-new-privileges`, 256 processes at most,
  runs as your user (not root, even inside), `/data` in memory, no host mount (a desktop keeps its screen socket),
  offline, and a fresh file system at each start (recreated from its image; `start` says `"fresh": true`).
- Podman in WSL: netavark's nftables rules fail on WSL's kernel, so terminal-ct points its own Podman calls at the
  iptables driver (`CONTAINERS_CONF_OVERRIDE`, unless you set it). Podman: a container on an internal network and
  online resolves only container names (IP access to the internet still works).
- Containers made by 0.2: Podman fixes a container's network mode (pasta) at creation and cannot change it. The
  first network, internet, sandbox or project change brings such a container up to date by itself: its file
  system is committed to an image (`tct-keep-NAME`, removed with it), it is remade on the bridge with the same
  `/data`, ceiling and owner, and the old one is only renamed until then (put back if anything fails). Measured:
  5.4 s, files in `/etc` and `/data` kept, desktop working.

## Dashboard

`terminal-ct dash [--project P]`: a live table of the containers (state, health, stack/service/replica, CPU % and
memory used next to the ceiling, age, restarts, flags: D desktop, S sandbox, I internet, A made by an agent), the
daemon, the quota and the latest events, refreshed every 2 s (the quota every 10 s: it measures the disk). Keys on
the selected container: up/down, `s` start, `t` stop, `r` remove (then `y`), `+`/`-` replicas of its service,
`l` its logs, `v` its desktop in the terminal viewer, `q` or Ctrl+] quit. Works in WSL and Linux terminals and in
Windows Terminal (keys through msvcrt there). `logs NAME [-n 100]` prints a container's output.

Agents get the same view as text with the MCP tool `dashboard` (only their project with `--project`), and a
container's output with `ct_logs` (one of the user's containers needs approval).

### Web dashboard

`terminal-ct web [--project P] [--no-open]` starts the daemon if needed and opens its dashboard in the browser:
containers (state, health, stack, CPU and memory with a short history, age, restarts; start, stop, restart, logs,
remove), screen thumbnails of running desktops and VMs (click: a larger, refreshed view), terminal-vm's VMs (start,
stop, force stop), stacks (running / wanted per service, -/+ replicas, an editor that applies the JSON, remove),
the quota and the latest events. From WSL it opens the Windows browser (WSL forwards 127.0.0.1 ports).

- Served by the daemon on 127.0.0.1 only, port `TERMINAL_CT_WEB_PORT` (default 7171; another free port if taken).
- Every API call needs the token of `STORE/web.token` (owner only, kept across restarts): `web` puts it in the URL
  fragment, the page keeps it for the tab and removes it from the address bar. Other Host names are refused.
- VMs come from terminal-vm's JSON CLI (`ls`, `top`, `events`, `action NAME screenshot`, `start`, `stop`): on the
  PATH, or `TERMINAL_VM="python3 /path/terminal_vm.py"` in the daemon's environment. Without it, the VM section is
  empty. Their events (restarts, idle pauses) appear with the containers'.
- The quota is fetched apart every 15 s (measuring the disk takes seconds with Podman); the table every 2 s.
- The same dashboard comes with terminal-vm (`terminal-vm web`, port 7172): the page and its server are one block
  of code, identical in both tools (each one's tests check it); each tool shows its own objects directly and the
  other's through that tool's command line. `terminal-ct dash --json [--project P]` is what terminal-vm reads.

## Snapshots and clones

- `snapshot NAME save TAG` commits the file system to an image `tct-snap-NAME:TAG` and archives `/data` next to it
  (`STORE/NAME/snaps/TAG.tar`, owners kept); `load` remakes the container from that image (the old one is put
  back if anything fails) and restores `/data` as it was; `rm` refuses the snapshot the container runs from. A
  sandbox has none: it starts from its image every time.
- `clone SRC NEW`: NEW gets SRC's file system (`tct-keep-NEW`, removed with NEW) and a copy of its `/data`, same
  ceiling and project, no networks; a clone of a sandbox is a new sandbox of the same image.
- The disk quota counts what each snapshot's commit added and its `/data` archive, not the base image again.
- Measured (WSL): snapshot of a desktop 0.2-0.5 s; clone of a desktop 4 s with Docker, 27 s with Podman (rootless
  `keep-id` makes a copy of each new image with shifted owners, once); load + start of a desktop: ready in 0.6 s.
- Agents: `ct_snapshot` and `ct_clone`; loading or removing a snapshot of a container the agent did not create
  needs approval, and so does cloning one of the user's containers (the clone starts offline). `push`/`pull` are
  CLI only: an agent tool would read or write any host file.

## Stacks (desired state)

```json
{"stack": "lab", "project": "lab",
 "services": {
   "web": {"image": "debian:stable-slim", "replicas": 2, "cpus": 1, "memory_gb": 1, "networks": ["lab"],
           "restart": "always", "health": {"exec": "test -f /data/ok", "every": 10, "retries": 3}},
   "job": {"image": "alpine:3.20", "cmd": ["sh", "-c", "work"], "restart": "on-failure", "ttl": "30m"}}}
```

- `apply stack.json` stores it (`STORE/stacks/`), reconciles once and starts the daemon. Replicas are named
  `STACK-SERVICE-N` and labelled with their stack, service, replica and a hash of their configuration. Service keys:
  `image`, `cmd`, `replicas` (0-100), `cpus`, `memory_gb`, `desktop` (+ `width`, `height`), `sandbox`, `internet`,
  `networks` (created if missing), `restart`, `health`, `ttl`.
- Reconciliation (`daemon`, every 3 s): creates the missing replicas; removes the surplus (`scale STACK SERVICE N`,
  or a service taken out of the file) **with its `/data`**; changes `cpus` / `memory_gb` live; remakes a replica
  whose image, command, desktop, sandbox, internet or networks changed (re-apply the file), keeping its `/data`;
  starts again what stopped by the restart policy (`always`; `on-failure`: exit code not 0; `never`) with an
  exponential backoff up to 300 s (back to zero after a minute up), but never a replica the user stopped with
  `terminal-ct stop` (until they start it); removes what outlived its `ttl`, counted from its first creation, and
  does not recreate it until the stack is applied again. A start the quota refuses is `pending`, tried again later.
- Health: `health.exec` runs in each running replica every `every` seconds after `grace` (default 30 s); after
  `retries` failures in a row the replica is stopped and started again (a sandbox fresh), unless its policy is
  `never`.
- `events [--stack S] [-n 50] [--follow]`: created, started, restarted (with exit code), unhealthy, expired, pending,
  removed, recreated... (`STORE/events.log`, the latest half kept past 4 MB). `stack ls|show|rm`; `stack rm` removes
  the containers too (networks stay).
- `daemon start|stop|status|run`: one per store, seen through a heartbeat file and stopped through a file (the same
  on Windows); `resume` (and so `autostart`) starts it again when stacks exist. `prune` removes kept and snapshot
  images whose container is gone.
- Measured (WSL, alpine): a killed replica is back in 4-6 s, a failed probe restarts it in 5-9 s; Docker creates
  and starts a replica in ~1 s, Podman in ~5 s (each Podman call costs 0.3-0.7 s).
- Agents: `ct_apply` (services are sandboxes unless the stack says otherwise; same approvals as `ct_create`: an
  image to download or build, internet, a network of the user's), `ct_scale`, `ct_stack` (ls, show, rm),
  `ct_events`. Its own stacks freely; changing one of the user's needs approval, and it cannot apply over it. A
  network of the user's made after the approval is not used: apply again to be asked.

## Agents

`mcp [NAME] [--project P]` serves, over stdio:

- screen tools and `exec` (`screenshot`, `click`, `move`, `drag`, `scroll`, `type`, `key`, `wait`, `status`, `exec`;
  input tools take `screenshot: true`) on NAME, or on another container with `container`;
- `ct_list`, `ct_create`, `ct_start`, `ct_stop`, `ct_remove`, `ct_resources`, `ct_internet`, `ct_snapshot`,
  `ct_clone`, `ct_apply`, `ct_scale`, `ct_stack`, `ct_events`, `net_list`,
  `net_create`, `net_remove`, `net_attach`, `net_detach`, `top`, `quota`;
- `dashboard` (the `dash` view as text), `ct_logs`;
- `autostart` (restart after a PC restart; changing it asks the user);
- `project_status` (where things stand: containers and networks with what each should be doing, notes, latest
  actions and errors) and `notes_write`; the server tells the agent at connection to start with `project_status`
  and to leave notes, so the next session picks up from there.

Authority: an agent runs sandboxes and the containers it created (`terminal-ct.created_by=agent`; its creations are
sandboxes by default). It must ask the user (MCP elicitation; without it, the call fails naming the CLI command to
run) to: touch one of the user's other containers (screen, shell or lifecycle), delete a sandbox it did not
create, download or build an image, give internet access, use a network the user created. It never changes the
quota. Management actions are logged to the project's `agent.log`, a sandbox's screen actions to its `actions.log`.

## Projects

- A project groups containers and networks (`create --project P`, `net create NET --project P`; otherwise
  `TERMINAL_CT_PROJECT`, else `default`). Sub-projects are paths: `lab/web`, up to 4 levels; whoever has `lab`
  also has `lab/web`. Inside a VM of terminal-vm's project `lab`, set `TERMINAL_CT_PROJECT=lab/<vm>` so its
  containers sit under that project with their notes and agent log (`STORE/projects/P/`). `project ls`, `project show P` (the same view
  as `project_status`), `project notes P [TEXT]`, `project move NAME P` (stopped container, kept as is).
- `mcp --project P`: the agent sees and reaches only P's containers and networks (and its sub-projects'), and
  creates in P or below. Outside P nothing is listed and nothing can be approved: give each agent its own project
  to keep them apart. Its `quota` shows the room left and its project's own use, not the other projects'; a name
  taken elsewhere is just "not available".

## Restarts

- `start` and `stop` record what each container should be doing (`STORE/NAME/want`). After the PC (or WSL)
  restarts, `resume` starts again what was running (within the quota; a sandbox fresh) and leaves alone what was
  stopped on purpose.
- `autostart on` runs it at each logon: on Windows (also from WSL) a scheduled task `terminal-ct-resume`
  (from WSL: `conhost --headless wsl.exe ... resume --stay`, which keeps WSL up while containers run); on Linux a
  systemd user service (at login; `loginctl enable-linger` for boot without login). `autostart off` removes it,
  `autostart` alone tells whether it is on. To turn it on:

  ```sh
  # from Windows, terminal-ct in WSL (Claude Code: prefix with ! to run it in the session)
  wsl -d Debian -- python3 /path/to/terminal_ct.py autostart on
  # Linux, or inside WSL
  python3 terminal_ct.py autostart on
  ```
- Agents: the MCP tool `autostart` shows the setting, and turns it on or off only with the user's approval.

## Measurements

`terminal-ct bench [--runs 10] [--desktop]`: median, min and max of terminal-ct's own operations on the current
engine (containers named `bench-*`, removed after). Results kept in [`bench/results.json`](bench/results.json).
WSL 2 Debian on Windows 11, 10 runs (3 for desktops), `alpine:3.20`, 2026-11-09:

| Operation (median) | Podman 5.4 | Docker 26.1 |
|---|---:|---:|
| create | 3.16 s | 0.94 s |
| start | 1.61 s | 0.75 s |
| exec `true` | 0.87 s | 0.27 s |
| stop | 1.33 s | 0.47 s |
| rm (with its volume) | 1.78 s | 0.50 s |
| ls | 0.69 s | 0.19 s |
| dashboard data | 1.03 s | 1.81 s |
| desktop ready after start | 0.4 s | 0.5 s |
| screenshot (1280x800 PNG) | 0.37 s | 0.11 s |
| desktop container memory | 19 MiB | 22 MiB |

Most of this is the cost of starting the engine's command-line tool, several times per operation: listing the
containers takes 458 ms through `podman` (126 ms through `docker`) and 1.4 ms (0.4 ms) through the same engine's
API on its unix socket. Talking to that API instead is planned for the optimisation phase. Docker's dashboard data
is slower because `docker stats` samples for about a second.

## Tests

```sh
python3 -m unittest test_terminal_ct                     # simulated engine
TERMINAL_CT_REAL=1 python3 -m unittest test_terminal_ct  # also against the installed Podman and Docker
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): unit tests on Linux, Windows and macOS with Python 3.9
and 3.12; the real tests, a short bench and the install script on Ubuntu with its Docker and Podman. WSL is not
available on hosted runners: the WSL runs are made by hand (results above).

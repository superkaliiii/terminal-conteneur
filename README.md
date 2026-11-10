<div align="center">

# terminal-ct

**Containers with a desktop and a shell, for people and computer-use agents.**

[![CI](https://github.com/superkaliiii/terminal-conteneur/actions/workflows/ci.yml/badge.svg)](https://github.com/superkaliiii/terminal-conteneur/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-3776ab?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20WSL%20%7C%20Windows%20%7C%20macOS-lightgrey)

[Install](#install) · [Commands](#commands) · [Agents](#agents) · [Stacks](#stacks-desired-state) ·
[Nodes](#nodes) · [Other PCs](#other-pcs) · [Dashboard](#dashboard) · [Measurements](#measurements)

</div>

The container counterpart of
[terminal-vm](https://github.com/superkaliiii/terminal-mac-inspection/tree/unified/terminal-vm): same command and
MCP shape, independent of it (each installs and works alone), and able to show its VMs when both are installed.
One Python file, standard library only, on Podman (preferred) or Docker.

## Highlights

- **A desktop in any container**: Debian/Ubuntu, Fedora, Alpine or Arch images get Xvnc + Openbox + xterm on a
  unix socket (no TCP port), driven by `screenshot`, `click`, `type`, `key`; watched in the terminal or a window.
- **Built for agents**: an MCP server with authority rules (what an agent may run without asking), sandboxes,
  projects with notes and logs, an [infra map](#infra-map-and-descriptions) read in one call, a one-line
  description on every object, and an [agent skill](#agent-skill).
- **Fenced resources**: a ceiling per container, a quota for the whole PC, isolated networks, internet on or off.
- **Desired state**: stacks with replicas, restarts, health probes and ttl, kept by a small daemon.
- **Beyond one engine**: nodes (Linux VMs of terminal-vm) with networks and DNS between them, and other PCs driven
  from a central one, with failover.
- **Dashboards**: a live terminal table and a web page (containers and terminal-vm's VMs), shareable with read-only
  or full users.

## Quick start

```console
$ curl -fsSL https://raw.githubusercontent.com/superkaliiii/terminal-conteneur/main/install.sh | sh
$ terminal-ct create box --desktop               # debian:stable-slim + a desktop
$ terminal-ct start box
$ terminal-ct exec box uname -sr
{"exit": 0, "stdout": "Linux 6.6.87.2-microsoft-standard-WSL2\n", "stderr": ""}
$ terminal-ct view box                           # the desktop inside the terminal (Ctrl+] exits)
$ terminal-ct web                                # the web dashboard
```

## Install

Linux, macOS and WSL:

```sh
curl -fsSL https://raw.githubusercontent.com/superkaliiii/terminal-conteneur/main/install.sh | sh
```

Windows (terminal-ct in its own WSL distribution, Alpine with Podman, apart from yours):

```powershell
irm https://raw.githubusercontent.com/superkaliiii/terminal-conteneur/main/install.ps1 | iex
```

One command does it all: what is missing (Python 3.9+, Podman, openssl; WSL on Windows) after one confirmation,
`terminal-ct` on the PATH, and, when Claude Code is installed, its MCP server and the [agent skill](#agent-skill).
Options: `--yes` (no question), `--no-deps`, `--no-mcp`, `--update`, `--uninstall` (the data stays); on Windows
`-Yes`, `-NoDeps`, ... or `TERMINAL_CT_INSTALL="-Yes"` before `irm`. From a checkout: `sh install.sh`.

Other ways, without the engine: `pipx install terminal-ct` or `uvx terminal-ct`, `npm i -g terminal-ct` or
`npx terminal-ct`, `.deb` and `.rpm` packages (`packaging/build.sh`), Homebrew (from the first public release).
Any other MCP client: the server is `terminal-ct mcp` (`mcp NAME`: a default container for the screen tools and
`exec`; `--project P` fences it to a project).

`terminal-ct doctor` names every known problem with the exact command that fixes it (engine, groups, Podman under
WSL, sleep, disk...); `doctor --report` writes a report to attach to an issue, with secrets, the home folder, user
and host names masked. Fedora under WSL cannot run Podman's networks (its netavark has no iptables driver): use
Debian, Ubuntu or Alpine there.

## Agent skill

[`skills/terminal-ct/SKILL.md`](skills/terminal-ct/SKILL.md) tells an agent how to work with terminal-ct: start
with `infra` then `project_status`, describe what it creates, prefer `exec` to the screen, the observe → act → verify loop on desktops, what it may run
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
| `infra [--project P] [--json]` | the map: PCs and nodes with their quota, what runs where, networks and names, VMs, projects; see [Infra map and descriptions](#infra-map-and-descriptions) |
| `describe container\|net\|node\|host\|project NAME [TEXT\|--clear]` | one line on what it is for (no TEXT: show it); `--description TEXT` on `create`, `clone`, `net create`, `node add`, `host add` |
| `push NAME LOCAL PATH`, `pull NAME PATH LOCAL` | copy files in or out (`/data` included), running or stopped |
| `sandbox NAME on\|off` | make a stopped container a sandbox, or a normal one again (recreated from its image) |
| `image ls`, `image pull REF`, `image build NAME DIR`, `image rm NAME` | images (Docker Hub short names are completed for Podman) |
| `bench [--runs 10] [--desktop]` | measure terminal-ct's operations on this engine, see [Measurements](#measurements) |
| `skills get\|path\|install [--dir D]` | the agent's skill, see [Agent skill](#agent-skill) |
| `doctor [--json] [--report]`, `version`, `help` | known problems with their fix; a masked report for an issue |
| `node add NAME [--image debian\|alpine] [--cpus 2] [--memory 2] [--disk 20]`, `node ls`, `node rm NAME [--force]` | nodes: Linux VMs of terminal-vm running Docker, see [Nodes](#nodes) |
| `agent [--listen IP] [--port 7173] [--rotate]` | serve this PC to a central PC, see [Other PCs](#other-pcs) |
| `host add NAME https://IP:PORT --fingerprint SHA256 [--with podman\|docker]`, `host add NAME ssh://[user@]host[:port]`, `host ls`, `host rm NAME [--force]` | other PCs as nodes |
| `--host PC vm ARGS [-- CMD]` | terminal-vm on another PC |
| `web user add NAME [--role read\|full]`, `web user ls`, `web user rm NAME`; `web --listen IP\|off [--port N]` | share the web dashboard, see [Shared session](#shared-session) |

`--engine docker|podman` (or `TERMINAL_CT_ENGINE`) picks the engine; by default Podman if installed, else Docker.
`--node NAME` runs the command in a node instead; `--host NAME` is the same option, read better for another PC.

Safety: every container terminal-ct makes is named `tct-NAME` and labelled `terminal-ct.*`; containers without
those labels are never listed or touched. An image whose default command is a bare shell is kept running with
`tail -f /dev/null` (under `--init`) so it can be used through `exec`.

## Nodes

A node is a Linux VM made by [terminal-vm](https://github.com/superkaliiii/terminal-mac-inspection/tree/unified/terminal-vm)
(installed alongside: on the PATH, or `TERMINAL_VM="python3 /path/terminal_vm.py"`) that runs Docker: the isolation
of a VM, the lightness of containers inside it.
- `node add NAME [--image debian|alpine]` creates it from an official cloud image (`terminal-vm create --image
  --node`), starts it, has terminal-vm restart it if it falls, and waits for its setup. Nodes are overlays on
  terminal-vm's node template (built once per kind, with the internet: about 2 min for Alpine, 4 for Debian), so
  `node add` takes about 19 s (WSL with nested KVM; 61 s for Alpine and ~120 s for Debian before the template).
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
  on the other in 23 s; old copies gone at its return. Again on template nodes (two Alpine nodes of 1 CPU / 1 GiB):
  spread 1 + 1 in 27 s, node stopped, its replica running on the other 16 s later, the copy removed 6 s after its
  return. With networks: see below.
- Desktops in a node (`--node N create NAME --desktop`, or a stack service): the desktop image is built in the node
  (its context sent there), and the screen tools (`action`, MCP screenshot/click/type/key/wait, dashboard
  thumbnails) are run by a copy of terminal-ct the host puts in the node, next to the desktop's socket (copied again
  when it changes). About 1.5 s per action from the CLI. `view` and `show` stay for this host's desktops.
- `push`/`pull`, image builds, snapshots: files and folders cross by the guest agent through a temporary folder of
  the node (folders as tar archives), removed after.

### Networks between nodes

- `node join NODE NET` plugs a node into network NET: a terminal-vm network `tct-NET` (a local switch: no LAN, no
  internet) with a fixed address per node (10.77.N.x); the node restarts once (network cards change while it is
  off). In the node: a Docker network for NET, its firewall rules, NET's DNS.
- A service on NET with `"ports": [80]` (`spread` or `node`) publishes them on its node's NET address only, one
  replica per node (same port everywhere; extra replicas wait for a free node). Containers of NET find it by name:
  `web` or `web.lab` (SERVICE.STACK) answers the addresses of the nodes running it, kept by the daemon (a failover
  shows within seconds). `--node N create NAME --net NET --ports 80` publishes a single container the same way
  (reached at the node's address; names are for stack services).
- Each network's DNS is authoritative only (never forwards: not an open resolver), answers its own network's
  containers only (bound to that bridge, other sources ignored), REFUSED for any other name, runs as `nobody`, reads
  a table only terminal-ct writes, rate-limits each source, ignores malformed queries. Containers with internet also
  get the node's own resolver after it.
- Firewall in each node (made again by the daemon within 30 s of a restart): containers of NET reach their bridge
  and NET's 10.77.N.0/24 only; nothing else on the node but their DNS and the node's NET address; containers with
  internet never reach the networks between nodes through the internet bridge. Real run (WSL, two Alpine nodes):
  a client on n1 reaches `web` on both nodes by name; a container of another network gets neither the name nor the
  port; with n2 down, `web` answers from n1 at once.

## Other PCs

One central PC drives the others: it keeps all the state (stacks, placement, quotas, approvals); another PC only
runs what it is sent (engine calls, file copies, terminal-vm). Another PC is a node of `kind: host`: everything of
[Nodes](#nodes) applies (`--host PC create`, routing of later commands, `ls`, dashboards, MCP, stacks with `node` or
`spread`) except desktops, networks and ports, which come with networks across PCs (phase 11 bis).

- **Agent** (on the other PC, Linux or macOS; on Windows inside WSL): `terminal-ct agent --listen 192.168.1.20`
  serves it over HTTPS (port 7173, `TERMINAL_CT_AGENT_PORT`) with a certificate made once (openssl, kept in
  `STORE/agent/`) and a token (owner only). It prints the URL, the certificate's SHA-256 and the token, and the
  `host add` line to type on the central PC. Only an IP is accepted for `--listen` (default 127.0.0.1: nothing on the
  network). It runs commands as its user (argv, no shell), 4 connections at once per address (64 in all), and logs each one to
  `STORE/agent/agent.log` (bounded). `--rotate` makes a new token: restart a running agent for it to take effect,
  then `host rm` + `host add` on the central PC. The token is printed: when the agent runs as a service, its log
  holds it.
- **SSH** instead: `host add NAME ssh://user@pc` uses your `ssh` and `scp` (keys and `known_hosts` as usual; no agent
  needed there). Not tried here against a real sshd yet. Podman inside WSL reached by SSH lacks the agent's fix for
  WSL's kernel (iptables instead of nftables): use the agent there.
- **`host add NAME https://IP:PORT --fingerprint SHA256`** takes the token from `TERMINAL_CT_AGENT_TOKEN`, else typed
  (hidden) or piped in, never as an argument (`ps`, shell history); the central PC checks the certificate's
  fingerprint before sending the token. It checks the PC answers, runs Linux or macOS, and has a working engine:
  Podman 4 or later first, else Docker (`--with` picks one). Kept in `STORE/hosts.json` (owner only). `host ls` shows
  them with whether they answer; `host rm NAME` refuses while containers live there, `--force` removes them first
  (a PC that does not answer keeps its containers running: remove them there).
- **Desktops on another PC**: `--host PC create NAME --desktop` works there; the screen tools (`action`, MCP)
  run through terminal-ct on that PC, next to the desktop's socket: the agent itself, or over SSH a terminal-ct
  installed there (`TERMINAL_CT`, the login PATH or `~/.local/bin`), never copied onto someone's computer; without
  it the error says so. The socket lives in that user's `~/.local/share/terminal-ct/central/NAME/run` (removed with
  the container). `view`/`show` stay for this PC's desktops. Real run (WSL, an agent with Docker): create, start,
  wait, screenshot, type, key, rm all right.
- **Networks and ports on another PC**: its networks are its own engine's (`terminal-ct --host PC net create NET`,
  `net ls`, `net rm`); its containers on them reach each other by name, as here. `--ports N` publishes on the
  address the central PC reaches that PC by (its agent's, or the SSH host's), never on all of its interfaces. A
  stack service pinned there (`"node": "PC"`) takes networks too. Real run (WSL, over SSH, Docker there): a client
  reached `web` by name on that PC's network, and its published port answered on that address only. Containers of
  different PCs reach each other through the relay:
- **Relay**: `terminal-ct relay add SERVICE --net NET [--on PC]` makes SERVICE (a container with `--ports` on another
  PC added by its agent) reachable by its name on network NET of PC (this PC by default): a small relay container
  there (`python:3.12-alpine`, stdlib code, listening on its address on NET under the service's name) carries each
  TCP connection to that PC's agent over TLS (pinned certificate) with a token the central PC asked for, good for
  that service's ports only (kept hashed in `STORE/agent/relays.json`); the agent joins it to the published port.
  The other PCs never get the central PC's token, and never learn each other but through the relay. `relay rm
  SERVICE [--on PC]` removes the relay and takes the token back. TCP only; the service's PC needs its agent (SSH has
  no door in). Real run (WSL; central Docker, the other PC an agent with Podman): a client on `lab` read `web`'s
  answer by its name; after `relay rm`, nothing.
- **Finding agents**: `host scan [--cidr 192.168.1.0/24] [--port 7173]` lists the terminal-ct agents answering on a
  network (default: this PC's /24) with their address, fingerprint and the `host add` line to run. It only opens a
  TLS handshake to each address (no token, no request: nothing in their logs); adding stays explicit, with the
  token, after comparing the fingerprint with the one the agent shows. Real run (WSL): a /24 in 3 s, the agent found.
- **Quota on another PC**: someone's computer, as this one is: by default cores - 1 and 75% of its memory;
  `terminal-ct --host PC quota --cpus N --memory GiB` sets it (`STORE/quota-PC.json`), never this PC's quota.
- **Failover between PCs**: a stack service with `"spread": "nodes"` uses the other PCs that answer like VM nodes.
  A PC that stops answering (Wi-Fi, sleep, an agent restarting) is given 3 daemon passes before its replicas are made
  again elsewhere; its copies are removed when it is back. Real run (WSL; central Podman, pc1 an agent with Docker,
  pc2 an agent with Podman): spread 1 + 1, pc2's agent killed, its replica running on pc1 10 to 37 s later (as `ls`
  shows it, two runs); the copy left on pc2 removed 16 s after its agent is back.
- **Agents** (MCP) use another PC only with the user's approval, once per PC for each stack (`approved_hosts`,
  asked by `ct_apply`); `host rm` forgets it. A stack an agent placed on another PC before that approval existed
  (made before commit 2fb955b) is taken off that PC at the next pass (its `/data` there removed with it).
- **VMs of other PCs**: when terminal-vm is installed there (on its login PATH, `~/.local/bin`, or `TERMINAL_VM` in
  the agent's environment), the web dashboard lists its VMs as `VM@PC` (start, stop, force stop, screenshots) and
  `terminal-ct --host PC vm ls` (any terminal-vm command, `-- CMD` for `exec`) runs it there; its output shows at the
  end (not interactive); `vm action VM screenshot --output FILE` brings the picture to this PC. terminal-vm's MCP
  server reaches them as `vm: "VM@PC"` (screen tools, `exec`, start, stop; the user's VMs there after their
  approval). Real run (WSL): 5 VMs listed in 0.7 s, a screenshot in 0.45 s; an MCP session over an agent (screenshot,
  status, click, exec, stop, start) all right.
- Measured (WSL, central Podman, agent Docker): `host add` 0.5 s, `create` 1.9 s, `exec` 0.7 s, `ls` 1.2 s; a
  stopped agent shows as not answering and `ls` goes on without it.
- **SSH**: `host add NAME ssh://[user@]host[:port]` runs plain `ssh`/`scp` (key login, `BatchMode`): nothing of
  terminal-ct runs there. Real run (WSL, an sshd on port 2222, Docker there): host add, create, start, exec, push,
  pull (a file and a folder), logs, infra, stop, rm all right; sshd stopped: the PC shows as not answering at once.
  A WSL with Podman reached over SSH gets Podman's iptables firewall driver (WSL's kernel lacks nftables pieces):
  `host add` writes it in that user's `~/.config/terminal-ct/podman-wsl.conf` and each call names it, their own
  `podman` unchanged.
- Antivirus: some (Avast) re-sign HTTPS even on 127.0.0.1 on Windows; the pinned fingerprint then refuses the
  connection on purpose, and the error says so. Run the central PC in WSL, or exclude the agent's port.

### Shared session

The web dashboard can be opened by other people or agents, each with its own token and role:
- `web user add NAME --role read|full` prints NAME's token once (only its SHA-256 is kept, `STORE/web.users.json`,
  owner only); `read` sees everything (containers, VMs, logs, screens, events, stacks) and acts on nothing (403),
  `full` acts too. `web user ls`, `web user rm NAME` (its open tabs are refused from their next call).
- `web --listen IP [--port N]` shares the dashboard on that address of this PC too, over HTTPS (port 7181 by default:
  the local port + 10; terminal-vm 7182), with a certificate for that IP whose fingerprint `web user add` prints with
  the user's URL. 127.0.0.1 keeps serving the owner over HTTP. `web --listen off` stops sharing. Never 0.0.0.0.
- The page shows who is there (each user's tab reports itself every 30 s: "with bob (read)"), and every action, refusal
  and failure goes to `STORE/.web/actions.log` (who, from where, what). Bad tokens from the network are not logged.
- Two `web user` commands run at the same moment may lose one of the two changes: run them one after the other.

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
- `infra` (the map, below), `describe`, and a `description` argument on `ct_create` and `net_create`;
- `project_status` (where things stand: containers and networks with what each should be doing, notes, latest
  actions and errors) and `notes_write`; the server tells the agent at connection to start with `infra`, then
  `project_status`, to describe what it creates and to leave notes, so the next session picks up from there.

Authority: an agent runs sandboxes and the containers it created (`terminal-ct.created_by=agent`; its creations are
sandboxes by default). It must ask the user (MCP elicitation; without it, the call fails naming the CLI command to
run) to: touch one of the user's other containers (screen, shell or lifecycle), delete a sandbox it did not
create, download or build an image, give internet access, use a network the user created. It never changes the
quota. Management actions are logged to the project's `agent.log`, a sandbox's screen actions to its `actions.log`.

## Infra map and descriptions

An agent arriving in the infrastructure learns who does what in one call:

- `infra` (CLI, `--json`; MCP tool `infra`): each PC and node, reachable or not, with its quota (limit, used,
  free), its description and the agent's stacks approved on it; every container with where it runs, its ceiling
  and real use, networks, ports, description, project, creator and `managed` (what the agent runs without
  asking); each network with the names that reach its members (a stack's service on nodes' networks also answers
  as `SERVICE` and `SERVICE.NET`); terminal-vm's VMs (of other PCs too, `VM@PC`); the projects with their
  descriptions. With `--project P` (or an MCP server's project), only P's objects; PCs and nodes stay listed.
- A description is one line (at most 200 printable characters) on a container, network, node, other PC or
  project: `describe KIND NAME TEXT`, `--description` at creation, MCP `describe`; a stack's service takes a
  `description` field (changing it recreates nothing). They live in `STORE/descriptions.json`, so they are right
  for containers on nodes and other PCs, can change at any time, and go when the object goes. They show in `ls`,
  `status`, `stack show`, `net ls`, `node ls`, `host ls`, `project show`, `dash` and the web dashboard.
- Through MCP an agent describes its own containers and networks (and its server's project) freely; the user's
  objects, nodes and PCs need their approval; each change is in the project's `agent.log`.

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
- `autostart on` runs it at each logon: on Windows (also from WSL) the user's Run key `terminal-ct-resume` (no
  administrator needed; from WSL: `conhost --headless wsl.exe ... resume --stay`, which keeps WSL up while
  containers run); on Linux a systemd user service (at login; `loginctl enable-linger` for boot without login).
  `autostart off` removes it, `autostart` alone tells whether it is on.
- `agent --listen IP [--port N] --autostart on` does the same for this PC's agent, kept running (restarted when
  it ends: systemd `Restart=on-failure`, launchd `KeepAlive` on macOS) and started now too; it prints the
  `host add` line for the central PC. `--autostart off` removes it and stops the agent; `--autostart status`.
  Real run (WSL): the agent answered 1 s after `on`, `host add` from a central store, `off` stopped it.
  To turn `autostart` on:

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

## License

MIT, see [LICENSE](LICENSE).

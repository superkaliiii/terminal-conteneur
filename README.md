# terminal-ct

Containers with a desktop and a shell, for people and computer-use agents. The container counterpart of
[terminal-vm](https://github.com/superkaliiii/terminal-mac-inspection/tree/unified/terminal-vm): same command and
MCP shape, independent of it (each installs and works alone), and able to show its VMs when both are installed.

Status: **phase 3** of 8: container lifecycle, desktops (any Debian/Ubuntu, Fedora, Alpine or Arch image),
screen tools, terminal viewer, quota and live resources, isolated networks, sandboxes, and an MCP server that
manages containers under agent-authority rules. Snapshots, orchestration (desired state, restarts, replicas) and
dashboards (terminal and web) come next.

## Requirements

- Python >= 3.9, standard library only.
- Podman (preferred: rootless, no daemon) or Docker. Debian/Ubuntu: `sudo apt install podman`.

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
| `sandbox NAME on\|off` | make a stopped container a sandbox, or a normal one again (recreated from its image) |
| `image ls`, `image pull REF`, `image build NAME DIR`, `image rm NAME` | images (Docker Hub short names are completed for Podman) |
| `doctor`, `version`, `help` | checks |

`--engine docker|podman` (or `TERMINAL_CT_ENGINE`) picks the engine; by default Podman if installed, else Docker.

Safety: every container terminal-ct makes is named `tct-NAME` and labelled `terminal-ct.*`; containers without
those labels are never listed or touched. An image whose default command is a bare shell is kept running with
`tail -f /dev/null` (under `--init`) so it can be used through `exec`.

## Desktops

- The desktop listens only on a **unix socket** in a private host folder (`~/.local/share/terminal-ct/NAME/run/`):
  no TCP port, nothing on the network, no password to keep. It runs as **your user** (Podman: `--userns=keep-id`;
  Docker: `--user`), so the socket is yours alone; `/data` is its home (`HOME=/data`, `LANG=C.UTF-8`).
- `start` returns once the desktop shows a window, so the first click lands on it.
- `type` sends X keysyms: any text, accents and other scripts included, whatever the keyboard layout.
- Measured in WSL Debian (Podman 5.4 rootless, Docker 26.1): desktop ready 0.4-1.3 s after `start`, stop ~1 s
  (exit code 0); first `--desktop` build 23 s (Alpine) to 84 s (Ubuntu), then cached.
- Desktop containers need a Linux host or WSL (the socket does not cross into Windows). Windows containers have no
  desktop, and there are no macOS containers: for those desktops use terminal-vm.

## Quota and resources

- Every container gets a ceiling (default 1 CPU, 1 GiB, set with `--cpus`/`--memory`, changed live with
  `resources`). The **quota** caps the sum over running containers: CPUs, memory, number of containers; and the
  disk space terminal-ct holds (containers' writable layers, their volumes, the desktop images).
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
  online resolves only container names (IP access to the internet still works). Containers made by 0.2 on
  Podman (network mode pasta) are online and cannot change networks: recreate them.

## Agents

`mcp [NAME]` serves, over stdio:

- screen tools and `exec` (`screenshot`, `click`, `move`, `drag`, `scroll`, `type`, `key`, `wait`, `status`, `exec`;
  input tools take `screenshot: true`) on NAME, or on another container with `container`;
- `ct_list`, `ct_create`, `ct_start`, `ct_stop`, `ct_remove`, `ct_resources`, `ct_internet`, `net_list`,
  `net_create`, `net_remove`, `net_attach`, `net_detach`, `top`, `quota`.

Authority: an agent runs sandboxes and the containers it created (`terminal-ct.created_by=agent`; its creations are
sandboxes by default). It must ask the user (MCP elicitation; without it, the call fails naming the CLI command to
run) to: touch one of the user's other containers (screen, shell or lifecycle), delete a sandbox it did not
create, download or build an image, give internet access, use a network the user created. It never changes the
quota. Management actions are logged to `agent.log`, a sandbox's screen actions to its `actions.log`.

## Tests

```sh
python3 -m unittest test_terminal_ct                     # simulated engine
TERMINAL_CT_REAL=1 python3 -m unittest test_terminal_ct  # also against the installed Podman and Docker
```

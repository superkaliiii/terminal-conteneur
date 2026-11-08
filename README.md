# terminal-ct

Containers with a desktop and a shell, for people and computer-use agents. The container counterpart of
[terminal-vm](https://github.com/superkaliiii/terminal-mac-inspection/tree/unified/terminal-vm): same command and
MCP shape, independent of it (each installs and works alone), and able to show its VMs when both are installed.

Status: **phase 2** of 8: container lifecycle, desktops (any Debian/Ubuntu, Fedora, Alpine or Arch image),
screen tools, terminal viewer and a first MCP server. Quota, networks, sandbox, orchestration (desired state,
restarts, replicas) and dashboards (terminal and web) come next.

## Requirements

- Python >= 3.9, standard library only.
- Podman (preferred: rootless, no daemon) or Docker. Debian/Ubuntu: `sudo apt install podman`.

## Commands

| Command | Does |
|---|---|
| `create NAME [--image IMAGE] [--cpus N] [--memory GiB] [-- CMD...]` | create a container (default image `debian:stable-slim`, pulled if missing); a named volume keeps `/data` |
| `create NAME --desktop [--image IMAGE] [--width 1280 --height 800]` | the same image with a desktop: Xvnc + Openbox + xterm added on top (built once per base, cached as `tct-desktop:<base>`) |
| `action NAME screenshot\|click\|move\|drag\|scroll\|type\|key\|wait\|status ...` | drive the desktop, same arguments as terminal-vm (`screenshot --output`, `click X Y [--button] [--double]`, `drag X Y X2 Y2`, `scroll --dy N`, `type TEXT`, `key CHORD`, `wait --stable --timeout`) |
| `view NAME [--read-only]` | the desktop inside the terminal, keyboard and mouse: Kitty graphics (Kitty, Ghostty, WezTerm) or Sixel (Windows Terminal, also from WSL; foot, mlterm); Ctrl+] exits, Ctrl+O opens the native window |
| `show NAME` | native window: TigerVNC's viewer on the desktop's unix socket |
| `mcp NAME` | stdio MCP server for one container: `screenshot`, `click`, `move`, `drag`, `scroll`, `type`, `key`, `wait`, `status`, `exec` (input tools take `screenshot: true`) |
| `start NAME`, `stop NAME [--timeout 10]`, `rm NAME` | lifecycle; `rm` also deletes the `/data` volume |
| `ls`, `status NAME` | JSON state of terminal-ct's containers |
| `exec NAME [--timeout 60] CMD...` | run a command: JSON with exit code, stdout, stderr (the CLI exits with the command's code) |
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

## Tests

```sh
python3 -m unittest test_terminal_ct                     # simulated engine
TERMINAL_CT_REAL=1 python3 -m unittest test_terminal_ct  # also against the installed Podman and Docker
```

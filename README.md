# terminal-ct

Containers with a desktop and a shell, for people and computer-use agents. The container counterpart of
[terminal-vm](https://github.com/superkaliiii/terminal-mac-inspection/tree/unified/terminal-vm): same command and
MCP shape, independent of it (each installs and works alone), and able to show its VMs when both are installed.

Status: **phase 1** of 8: engine layer and container lifecycle. Desktop, MCP, quota, networks, sandbox,
orchestration (desired state, restarts, replicas) and dashboards (terminal and web) come next.

## Requirements

- Python >= 3.9, standard library only.
- Podman (preferred: rootless, no daemon) or Docker. Debian/Ubuntu: `sudo apt install podman`.

## Commands

| Command | Does |
|---|---|
| `create NAME [--image IMAGE] [--cpus N] [--memory GiB] [-- CMD...]` | create a container (default image `debian:stable-slim`, pulled if missing); a named volume keeps `/data` |
| `start NAME`, `stop NAME [--timeout 10]`, `rm NAME` | lifecycle; `rm` also deletes the `/data` volume |
| `ls`, `status NAME` | JSON state of terminal-ct's containers |
| `exec NAME [--timeout 60] CMD...` | run a command: JSON with exit code, stdout, stderr (the CLI exits with the command's code) |
| `image ls`, `image pull REF`, `image build NAME DIR`, `image rm NAME` | images (Docker Hub short names are completed for Podman) |
| `doctor`, `version`, `help` | checks |

`--engine docker|podman` (or `TERMINAL_CT_ENGINE`) picks the engine; by default Podman if installed, else Docker.

Safety: every container terminal-ct makes is named `tct-NAME` and labelled `terminal-ct.*`; containers without
those labels are never listed or touched. An image whose default command is a bare shell is kept running with
`tail -f /dev/null` (under `--init`) so it can be used through `exec`.

## Tests

```sh
python3 -m unittest test_terminal_ct                     # simulated engine
TERMINAL_CT_REAL=1 python3 -m unittest test_terminal_ct  # also against the installed Podman and Docker
```

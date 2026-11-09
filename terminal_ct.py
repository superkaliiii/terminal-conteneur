#!/usr/bin/env python3
"""terminal-ct: containers with a desktop and a shell for people and computer-use agents.

The container counterpart of terminal-vm: same command and MCP shape, Docker or Podman underneath
(whichever is installed), standard library only. Every container it makes carries `terminal-ct.*`
labels; containers without them are never touched.

  create NAME [--image IMAGE] [--desktop [--width 1280 --height 800]] [--cpus 1] [--memory 1 (GiB)]
              [--sandbox] [--offline] [--net NET ...] [-- CMD...]
              default image debian:stable-slim; --desktop adds Xvnc + Openbox to any Debian/Ubuntu, Fedora,
              Alpine or Arch image (built once, cached as tct-desktop:<base>); --sandbox: not root, no
              capabilities, process limit, /data in memory, fresh file system at each start, offline
  ls | status NAME | top | doctor | help | version
  start NAME | stop NAME [--timeout 10] | rm NAME     rm also deletes the container's /data volume
  exec NAME [--timeout 60] CMD...                     run a command, JSON reply (exit code, stdout, stderr)
  resources NAME [--cpus N] [--memory GiB]            show, or change at once (even running), within the quota
  quota [--cpus N] [--memory GiB] [--disk GiB] [--containers N]   show, or set, the host quota
  net create|rm NET | net ls | net attach|detach NAME NET   isolated networks (containers reach each other by name)
  internet NAME [on|off]  |  sandbox NAME on|off      stopped containers (one made by 0.2 is upgraded, kept as is)
  action NAME screenshot|click|move|drag|scroll|type|key|wait|status ...   desktop containers
  mcp [NAME] [--project P]   stdio MCP server: screen tools + exec (on NAME, or `container`), container and
                      network management under the agent-authority rules (sandboxes and its own; the rest asks the
                      user); with --project, only that project, and project_status / notes_write to resume
  create ... --project P  |  project ls | project show [P] | project notes [P [TEXT]] | project move NAME P
  resume [--stay]  |  autostart [on|off]     start again what was running before a restart; at each logon
  view NAME [--read-only]   terminal viewer: Kitty graphics or Sixel (Windows Terminal, foot); Ctrl+] exits
  show NAME                 native window (TigerVNC viewer, through the desktop's unix socket)
  image ls | image pull REF | image build NAME DIR | image rm NAME
Global: --engine docker|podman (default: TERMINAL_CT_ENGINE, else podman if installed, else docker).
"""
import argparse, base64, collections, contextlib, io, json, os, queue, re, shlex, shutil, socket, struct, \
    subprocess, sys, threading, time, zlib

VERSION = "0.3.0"
WINDOWS = os.name == "nt"
if not WINDOWS:
    import fcntl, termios, tty  # terminal viewer only
_DATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~")) if WINDOWS else \
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
STORE = os.environ.get("TERMINAL_CT_HOME") or os.path.join(_DATA, "terminal-ct")
LABEL = "terminal-ct"
PREFIX = "tct-"  # engine-side names: never collide with the user's own containers
DEFAULT_IMAGE = "debian:stable-slim"
KEEP_ALIVE = ["tail", "-f", "/dev/null"]  # for images whose default command is a shell that would exit at once
SHELLS = {"sh", "bash", "ash", "zsh", "/bin/sh", "/bin/bash", "/bin/ash", "/bin/zsh"}


def check_name(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,47}", name or ""):
        raise SystemExit(f"invalid name: {name!r} (lowercase letters, digits, _ . -)")
    return name


def pick_engine(wanted=None):
    wanted = wanted or os.environ.get("TERMINAL_CT_ENGINE")
    for e in ([wanted] if wanted else ["podman", "docker"]):  # podman first: rootless, no daemon
        if e not in ("podman", "docker"):
            raise SystemExit(f"unknown engine {e!r}: docker or podman")
        if shutil.which(e):
            return e
    raise SystemExit(f"{wanted or 'docker or podman'} not found: install one (Debian: sudo apt install podman)")


def in_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


class Node:
    """Where containers run. v1: the local host; later a VM node (through its guest agent) or a remote machine,
    so every engine call goes through a node."""
    def __init__(self, engine=None):
        self.engine = engine or pick_engine()
        if self.engine == "podman" and in_wsl() and "CONTAINERS_CONF_OVERRIDE" not in os.environ:
            # WSL's kernel lacks nftables pieces netavark needs: a bridge container failed to start ("nft did not
            # return successfully"). Its iptables driver works; set for terminal-ct's own calls only.
            os.makedirs(STORE, exist_ok=True)
            conf = os.path.join(STORE, "podman-wsl.conf")
            with open(conf, "w") as f:
                f.write('[network]\nfirewall_driver = "iptables"\n')
            os.environ["CONTAINERS_CONF_OVERRIDE"] = conf

    def host(self):
        """(CPUs, memory GiB, storage folder) of the machine the engine runs on: on Windows and macOS, the WSL,
        Docker Desktop or Podman machine VM. On Linux that is this machine: read here (an engine call costs ~0.5 s)."""
        if not hasattr(self, "_host"):
            if sys.platform.startswith("linux"):  # shortcut: assumes the engine's storage shares STORE's file system
                mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                self._host = os.cpu_count(), mem / 2 ** 30, STORE if os.path.isdir(STORE) else os.path.dirname(STORE)
            else:
                fmt = "{{.Host.CPUs}} {{.Host.MemTotal}} {{.Store.GraphRoot}}" if self.engine == "podman" else \
                    "{{.NCPU}} {{.MemTotal}} {{.DockerRootDir}}"
                c, m, root = self.run(["info", "--format", fmt]).stdout.split(None, 2)
                self._host = int(c), int(m) / 2 ** 30, root.strip()
        return self._host

    def run(self, args, check=True, timeout=None, capture=True):
        # uncaptured output (pull, build progress) goes to stderr: stdout is the JSON reply, or the MCP channel
        r = subprocess.run([self.engine, *args], text=True, timeout=timeout, encoding="utf-8", errors="replace",
                           **({"capture_output": True} if capture else {"stdout": sys.stderr}))
        if check and r.returncode:
            raise SystemExit((r.stderr or r.stdout or f"{self.engine} {args[0]} failed").strip())
        return r

    def json(self, args):
        out = self.run(args).stdout.strip()
        return json.loads(out) if out else []


NODE = None


def node():
    global NODE
    if NODE is None:
        NODE = Node()
    return NODE


def normalize(c):
    """One container from `inspect`, the same shape for Docker and Podman."""
    labels = (c.get("Config") or {}).get("Labels") or {}
    st = c.get("State") or {}
    cpus, mem = allotted(c, None)
    nets = networks_of(c)
    return {"name": labels.get(f"{LABEL}.name") or c.get("Name", "").lstrip("/").removeprefix(PREFIX),
            "status": st.get("Status", "unknown"), "image": (c.get("Config") or {}).get("Image", ""),
            "created_by": labels.get(f"{LABEL}.created_by", "user"), "id": c.get("Id", "")[:12],
            "project": labels.get(f"{LABEL}.project") or DEFAULT_PROJECT,
            "cpus": cpus or "unlimited", "memory_gb": round(mem, 2) if mem else "unlimited",
            "internet": online(c),
            "networks": sorted(n[len(NET_PREFIX):] for n in nets if n.startswith(NET_PREFIX)),
            **({"sandbox": True} if labels.get(f"{LABEL}.sandbox") == "1" else {}),
            **({"desktop": True} if labels.get(f"{LABEL}.desktop") == "1" else {}),
            **({"exit_code": st.get("ExitCode")} if st.get("Status") == "exited" else {})}


BRIDGE = {"docker": "bridge", "podman": "podman"}  # each engine's default network, the way to the internet
NET_PREFIX = PREFIX + "net-"


def networks_of(c):
    return list(((c.get("NetworkSettings") or {}).get("Networks") or {}))


def is_bridged(c):
    """False for containers made before 0.3 on Podman's default (pasta): online, and their networks cannot change."""
    mode = (c.get("HostConfig") or {}).get("NetworkMode") or "bridge"
    return mode in ("bridge", "default") or mode.startswith(PREFIX) or mode in BRIDGE.values()


def online(c):
    if is_bridged(c):
        return BRIDGE[node().engine] in networks_of(c)
    return (c.get("HostConfig") or {}).get("NetworkMode") != "none"


def allotted(c, unlimited="host"):
    """(CPUs, GiB) a container may use. 0 means no limit: counted as the whole host (containers made before 0.3),
    or returned as 0 when unlimited is None."""
    hc = c.get("HostConfig") or {}
    cpus, mem = (hc.get("NanoCpus") or 0) / 1e9, (hc.get("Memory") or 0) / 2 ** 30
    if unlimited is None:
        return cpus, mem
    host = node().host()
    return cpus or host[0], mem or host[1]


def raw_containers(size=False):
    ids = node().run(["ps", "-a", "-q", "--filter", f"label={LABEL}.name"]).stdout.split()
    infos = node().json(["inspect", "--type", "container", *(["--size"] if size else []), *ids]) if ids else []
    return [c for c in infos if not c.get("Name", "").endswith("-OLD")]  # the old one during a remake


def containers():
    return [normalize(c) for c in raw_containers()]


def labels_of(c):
    return (c.get("Config") or {}).get("Labels") or {}


def running(c):
    return (c.get("State") or {}).get("Status") in ("running", "paused", "restarting", "stopping")


def recover(name):
    """Put back a container left renamed tct-NAME-OLD by a remake cut short (power off, killed process)."""
    old = node().run(["inspect", "--type", "container", PREFIX + name + "-OLD"], check=False)
    if old.returncode == 0 and node().run(["inspect", "--type", "container", PREFIX + name], check=False).returncode:
        with contextlib.suppress(ValueError, IndexError):
            if labels_of(json.loads(old.stdout)[0]).get(f"{LABEL}.name") == name:
                node().run(["rename", PREFIX + name + "-OLD", PREFIX + name])


def find(name):
    check_name(name)
    r = node().run(["inspect", "--type", "container", PREFIX + name], check=False)
    if r.returncode:
        recover(name)
        r = node().run(["inspect", "--type", "container", PREFIX + name], check=False)
    try:
        info = json.loads(r.stdout or "[]")
    except ValueError:
        info = []
    if r.returncode or not info or ((info[0].get("Config") or {}).get("Labels") or {}).get(f"{LABEL}.name") != name:
        raise SystemExit(f"no container named {name!r}; create it with: terminal-ct create {name}")
    return info[0]


def qualified(ref):
    """Docker Hub short names in full: Podman refuses to guess a registry without a terminal to ask on."""
    first = ref.split("/")[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return ref  # names its registry
    return "docker.io/" + ("" if "/" in ref else "library/") + ref


def pull(ref):
    node().run(["pull", qualified(ref)], capture=False)


def image_cmd(image):
    """The image's default command, pulling the image if it is not here yet."""
    r = node().run(["image", "inspect", image], check=False)
    if r.returncode:
        pull(image)
        image = qualified(image)
        r = node().run(["image", "inspect", image])
    cfg = json.loads(r.stdout)[0].get("Config") or {}
    return (cfg.get("Entrypoint") or []), (cfg.get("Cmd") or [])


def ct_dir(name):
    """Host folder of a container (private): the desktop's VNC socket lives in its run/ subfolder."""
    d = os.path.join(STORE, check_name(name))
    os.makedirs(os.path.join(d, "run"), mode=0o700, exist_ok=True)
    return d


# where new containers go without --project: TERMINAL_CT_PROJECT lets a VM put its containers under its own
# project (e.g. lab/vm1, a sub-project of the VM's project lab)
DEFAULT_PROJECT = os.environ.get("TERMINAL_CT_PROJECT") or "default"
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}(/[a-z0-9][a-z0-9_.-]{0,47}){0,3}")


def check_project(p):
    """A project name, or a sub-project path: lab, lab/web (an agent given lab also reaches lab/web)."""
    if not isinstance(p, str) or not _PROJECT.fullmatch(p):
        raise SystemExit(f"invalid project: {p!r} (lowercase names, sub-projects as lab/web)")
    return p


def under(p, project):
    """p is project itself or one of its sub-projects; no project means everything."""
    return not project or p == project or p.startswith(project + "/")
DEFAULT_CPUS, DEFAULT_MEMORY = 1, 1  # each container gets a ceiling, so the quota can count it
SANDBOX_PIDS = 256


def create(name, image=DEFAULT_IMAGE, cpus=None, memory=None, cmd=None, created_by="user",
           desktop=False, width=1280, height=800, sandbox=False, internet=None, nets=(), project=None):
    """Internet: on by default for the user's containers; sandboxes and agents' containers start offline."""
    check_name(name)
    project = check_project(project or DEFAULT_PROJECT)
    recover(name)  # an interrupted remake's container is still NAME's: never create over it
    if node().run(["inspect", "--type", "container", PREFIX + name], check=False).returncode == 0:
        raise SystemExit(f"{name!r} already exists (engine name {PREFIX}{name})")
    if desktop:
        if WINDOWS:  # shortcut: the VNC unix socket does not cross into Windows; run terminal-ct inside WSL there
            raise SystemExit("desktop containers need a Linux host or WSL (the screen is reached through a unix socket)")
        if not (640 <= width <= 7680 and 480 <= height <= 4320):
            raise SystemExit("invalid --width/--height")
        if cmd:
            raise SystemExit("a desktop container runs its desktop: start programs from it, or with exec")
    cpus, memory = cpus or DEFAULT_CPUS, memory or DEFAULT_MEMORY
    if cpus <= 0 or memory < 0.0625:
        raise SystemExit("--cpus must be > 0 and --memory at least 0.0625 GiB (64 MiB)")
    known = networks()
    for n in nets:
        if n not in known:
            raise SystemExit(f"no network {n!r}; create it with: terminal-ct net create {n}")
    if not sandbox and node().run(["volume", "inspect", f"{PREFIX}{name}-data"], check=False).returncode == 0:
        # never hand an earlier container's data to a new owner (an agent could then delete it unasked)
        raise SystemExit(f"a volume {PREFIX}{name}-data is left from an earlier {name}: keep it under another name, "
                         f"or remove it: {node().engine} volume rm {PREFIX}{name}-data")
    quota_check({"disk_gb": 0})  # what a container will write is unknown: refuse once the disk quota is reached
    internet = not sandbox and created_by == "user" if internet is None else internet
    spec = {"image": image, "cmd": cmd, "desktop": desktop, "width": width, "height": height, "sandbox": sandbox}
    out = _make(name, spec, created_by, cpus, memory, internet, nets, project, fresh=True)
    want(name, "stopped")
    print(json.dumps(out))


def ensure_image(spec):
    """Pull or build what the spec runs on (slow: outside the quota lock)."""
    if spec.get("desktop") and not spec.get("prebuilt"):
        desktop_image(spec["image"])
    else:
        image_cmd(spec["image"])


def _make(name, spec, created_by, cpus, memory, internet, nets, project, fresh=False):
    """Create the engine container from its spec (kept in a label: a sandbox is recreated from it at each start)."""
    volume, extra = f"{PREFIX}{name}-data", []  # /data is kept when the container is recreated, removed by rm
    image, cmd, sandbox, podman = spec["image"], spec.get("cmd"), spec.get("sandbox"), node().engine == "podman"
    if spec.get("desktop"):
        image = image if spec.get("prebuilt") else desktop_image(image)  # prebuilt: a committed desktop
        # LANG: without UTF-8, xterm wrote é as Latin-1
        extra += ["-e", "LANG=C.UTF-8", "-e", f"TCT_WIDTH={spec['width']}", "-e", f"TCT_HEIGHT={spec['height']}",
                  "-v", f"{os.path.join(ct_dir(name), 'run')}:/run/tct"]
        cmd = [DESKTOP_ENTRY]
    else:
        entry, default = image_cmd(image)
        # a bare shell as the main process exits at once without a terminal: keep such containers alive instead
        if not cmd and not entry and (not default or default[0] in SHELLS):
            cmd = KEEP_ALIVE
    # desktops and sandboxes run as the host user (podman: same uid inside): the VNC socket is the user's alone, and
    # a sandbox is not root even inside; /data is their home
    as_user = spec.get("desktop") or sandbox
    uid, gid = (1000, 1000) if WINDOWS else (os.getuid(), os.getgid())  # shortcut: no uid on Windows, typical one
    if as_user:
        extra += [*(["--userns=keep-id"] if podman else []), "--user", f"{uid}:{gid}", "-e", "HOME=/data"]
    made_volume = False
    if sandbox:  # /data in memory, emptied at each stop; no host mount but the screen's socket
        data = ["--mount", "type=tmpfs,destination=/data,tmpfs-mode=0700,U=true"] if podman else \
            ["--tmpfs", f"/data:rw,uid={uid},gid={gid},mode=700"]
        extra += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(SANDBOX_PIDS)]
    elif as_user and podman:
        data = ["-v", f"{volume}:/data:U"]  # :U hands the volume to that user
    else:
        if as_user:  # docker: hand the volume to the user before the container runs as them
            made_volume = node().run(["volume", "inspect", volume], check=False).returncode != 0
            node().run(["volume", "create", volume])
            node().run(["run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "chown",
                        "-v", f"{volume}:/data", image, f"{uid}:{gid}", "/data"])
        data = ["-v", f"{volume}:/data"]
    args = ["create", "--name", PREFIX + name, "--hostname", name, "--init", "--network", BRIDGE[node().engine],
            # every label set, 0 included: a committed image carries the old container's labels into the new one
            "--label", f"{LABEL}.name={name}", "--label", f"{LABEL}.created_by={created_by}",
            "--label", f"{LABEL}.project={project}", "--label", f"{LABEL}.sandbox={int(bool(sandbox))}",
            "--label", f"{LABEL}.desktop={int(bool(spec.get('desktop')))}",
            "--label", f"{LABEL}.spec={json.dumps(spec, separators=(',', ':'))}", *data, *extra,
            "--cpus", f"{cpus:g}", "--memory", str(int(memory * 2 ** 30)), "--memory-swap", str(int(memory * 2 ** 30)),
            image, *(cmd or [])]  # memory-swap = memory: no swap, so the quota's memory is the real ceiling
    try:
        cid = node().run(args).stdout.strip()
        # every container sits on the default bridge, so networks can change later; offline = unplugged from it
        try:
            if not internet:
                node().run(["network", "disconnect", BRIDGE[node().engine], PREFIX + name])
            for n in nets:
                node().run(["network", "connect", "--alias", name, NET_PREFIX + n, PREFIX + name])
        except SystemExit:
            node().run(["rm", "-f", PREFIX + name], check=False)  # fail closed: never left online by mistake
            raise
    except SystemExit:
        if fresh and made_volume:
            node().run(["volume", "rm", volume], check=False)  # else left behind with no container to rm it
        raise
    return {"created": name, "image": image, "id": cid[:12], "project": project, "cpus": cpus, "memory_gb": memory,
            "internet": internet,
            **({"networks": list(nets)} if nets else {}), **({"sandbox": True} if sandbox else {}),
            **({"desktop": f"{spec['width']}x{spec['height']}"} if spec.get("desktop") else {})}


KEEP = "tct-keep-"  # images holding a container's committed file system (remake keep=True), removed with it


def legacy_spec(info):
    """The spec of a container made before 0.3 (none in its labels), read from its configuration."""
    env = dict(e.split("=", 1) for e in (info.get("Config") or {}).get("Env") or [] if "=" in e)
    return {"image": (info.get("Config") or {}).get("Image", ""), "cmd": None,
            "desktop": labels_of(info).get(f"{LABEL}.desktop") == "1", "sandbox": False,
            "width": int(env.get("TCT_WIDTH", 1280)), "height": int(env.get("TCT_HEIGHT", 800))}


def needs_upgrade(info):
    """Made before 0.3: no spec to recreate it from, or (Podman) network mode pasta, which cannot change networks."""
    return not labels_of(info).get(f"{LABEL}.spec") or not is_bridged(info)


def remake(name, info, keep=False, **change):
    """The container again, same resources, networks and owner, with `change` (sandbox, internet, project) applied.
    keep=False: fresh from its spec's image (a sandbox at each start, sandbox on/off). keep=True: its file system
    as it is, committed to an image first (moving it to another project; a container made before 0.3 brought up to
    date, which Podman cannot do in place: its network mode is fixed at creation). /data's volume is kept. The old
    container is only renamed until the new one exists, and put back if anything fails."""
    if running(info):
        raise SystemExit(f"stop {name} first (terminal-ct stop {name})")
    lab = labels_of(info)
    raw = lab.get(f"{LABEL}.spec")
    keep = keep or needs_upgrade(info)
    spec = json.loads(raw) if raw else legacy_spec(info)
    project = change.pop("project", None) or lab.get(f"{LABEL}.project") or DEFAULT_PROJECT
    internet = change.pop("internet", online(info))
    spec.update(change)
    if keep:
        ref = f"{KEEP}{name}:latest"
        layer = (node().json(["inspect", "--type", "container", "--size", PREFIX + name]) or [{}])[0].get("SizeRw") or 0
        node().run(["commit", PREFIX + name, ref])
        # what the commit added: its lower layers are the base image's (counted on their own, or not terminal-ct's)
        with open(os.path.join(ct_dir(name), "keep_bytes"), "w") as f:
            f.write(str(int(keep_bytes(name) + layer)))
        spec.update(image=ref, prebuilt=bool(spec.get("desktop")))  # a desktop's layer is in the committed image
    ensure_image(spec)  # before touching the container: a missing image must not leave the user with none
    cpus, mem = allotted(info, None)
    nets = [n[len(NET_PREFIX):] for n in networks_of(info) if n.startswith(NET_PREFIX)]
    old = PREFIX + name + "-OLD"  # uppercase: no terminal-ct name can be that
    node().run(["rm", "-f", old], check=False)  # left by an interrupted remake
    node().run(["rename", PREFIX + name, old])
    try:
        _make(name, spec, lab.get(f"{LABEL}.created_by", "user"), cpus or DEFAULT_CPUS, mem or DEFAULT_MEMORY,
              internet, nets, project)
    except BaseException:
        node().run(["rm", "-f", PREFIX + name], check=False)
        node().run(["rename", old, PREFIX + name], check=False)
        raise
    node().run(["rm", "-f", old])
    return find(name)


# ---------- desktop image ----------
DESKTOP_ENTRY = "/usr/local/bin/tct-desktop"
# Xvnc listens only on a unix socket in a host folder only the user can open: no TCP port, no password to keep.
ENTRY_SCRIPT = """#!/bin/sh
rm -f /run/tct/vnc.sock
Xvnc :0 -geometry "${TCT_WIDTH:-1280}x${TCT_HEIGHT:-800}" -depth 24 -rfbport -1 -rfbunixpath /run/tct/vnc.sock \\
     -SecurityTypes None -AlwaysShared -desktop "$(hostname)" &
export DISPLAY=:0
for i in 1 2 3 4 5 6 7 8 9 10; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.2; done
openbox --startup xterm &  # started together, xterm's window stayed unmanaged (not drawn) until a click
trap 'trap "" TERM INT; kill 0 2>/dev/null; wait; exit 0' TERM INT  # a requested stop exits 0, not as a crash
wait
"""
DESKTOP_PACKAGES = {  # family: install command (Xvnc, Openbox, a terminal, a font)
    "debian": "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
              "tigervnc-standalone-server openbox xterm fonts-dejavu-core && rm -rf /var/lib/apt/lists/*",
    "fedora": "dnf install -y tigervnc-server-minimal openbox xterm dejavu-sans-mono-fonts && dnf clean all",
    "alpine": "apk add --no-cache tigervnc openbox xterm font-dejavu",
    "arch": "pacman -Sy --noconfirm tigervnc openbox xterm ttf-dejavu && pacman -Scc --noconfirm",
}


def os_family(image):
    """debian, fedora, alpine or arch, from the image's /etc/os-release."""
    r = node().run(["run", "--rm", "--entrypoint", "cat", image, "/etc/os-release"], check=False)
    ids = " ".join(re.findall(r'^(?:ID|ID_LIKE)="?([^"\n]*)"?', r.stdout, re.M)).split()
    for fam, names in (("debian", {"debian", "ubuntu"}), ("fedora", {"fedora", "rhel", "centos"}),
                       ("alpine", {"alpine"}), ("arch", {"arch", "archlinux"})):
        if names & set(ids):
            return fam
    raise SystemExit(f"--desktop supports Debian/Ubuntu, Fedora, Alpine and Arch images; {image} is {' '.join(ids) or 'unknown'}")


def desktop_tag(base):
    return "tct-desktop:" + re.sub(r"[^a-z0-9_.-]", "-", base.lower())[-120:]


def desktop_image(base):
    """The desktop layer on top of BASE, built once and cached as tct-desktop:<base>."""
    tag = desktop_tag(base)
    if node().run(["image", "inspect", tag], check=False).returncode == 0:
        return tag
    image_cmd(base)  # pulls it if needed
    # FROM the name the image was found under locally (BuildKit refuses a bare image id)
    base_ref = qualified(base) if node().run(["image", "inspect", base], check=False).returncode else base
    ctx = os.path.join(STORE, "build", tag.split(":")[1])
    os.makedirs(ctx, exist_ok=True)
    with open(os.path.join(ctx, "tct-desktop"), "w", newline="\n") as f:
        f.write(ENTRY_SCRIPT)
    with open(os.path.join(ctx, "Dockerfile"), "w", newline="\n") as f:
        f.write(f"FROM {base_ref}\nUSER 0\nRUN {DESKTOP_PACKAGES[os_family(base)]}\n"
                f"COPY tct-desktop {DESKTOP_ENTRY}\nRUN chmod 755 {DESKTOP_ENTRY}\n")
    node().run(["build", "-t", tag, ctx], capture=False)
    return tag


def start(name):
    info = find(name)
    out = {"started": name}
    if not running(info):
        if labels_of(info).get(f"{LABEL}.sandbox") == "1" and labels_of(info).get(f"{LABEL}.spec"):
            ensure_image(json.loads(labels_of(info)[f"{LABEL}.spec"]))  # a rebuild takes minutes: not under the lock
        with quota_lock():  # check and start together: two commands cannot both pass and then both start
            c0, m0 = allotted(info, None)
            if not (c0 and m0):  # made before 0.3 without limits: give it the defaults, else it counts as the host
                resources(name, None if c0 else DEFAULT_CPUS, None if m0 else DEFAULT_MEMORY, lock=False)
                info, out["limits_set"] = find(name), True
            cpus, mem = allotted(info)
            quota_check({"cpus": cpus, "memory_gb": mem, "containers": 1})
            if labels_of(info).get(f"{LABEL}.sandbox") == "1" and (info.get("State") or {}).get("Status") != "created":
                info = remake(name, info)  # a sandbox starts from its image every time
                out["fresh"] = True
            node().run(["start", PREFIX + name])
    want(name, "running")
    if labels_of(info).get(f"{LABEL}.desktop") == "1":
        # an empty screen looks "stable" too: return once the desktop shows a window (1% of the screen not
        # black; the pointer alone is drawn into the image), so input lands on it
        t0, end = time.time(), time.time() + 10
        while time.time() < end:
            try:
                fb = _pixels(name)
                if len(fb) - fb.count(0) > len(fb) // 100:
                    out["desktop_ready"] = round(time.time() - t0, 1)
                    break
            except RuntimeError:  # socket not there yet
                pass
            time.sleep(0.25)
    print(json.dumps(out))


def stop(name, timeout=10):
    find(name)
    want(name, "stopped")  # first: stopped by request, resume leaves it so
    node().run(["stop", "-t", str(timeout), PREFIX + name])
    print(json.dumps({"stopped": name}))


def rm(name):
    find(name)
    node().run(["rm", "-f", PREFIX + name])
    node().run(["rm", "-f", PREFIX + name + "-OLD"], check=False)  # it would keep the volume in use
    node().run(["volume", "rm", f"{PREFIX}{name}-data"], check=False)  # absent if never created
    node().run(["image", "rm", f"{KEEP}{name}:latest"], check=False)  # its committed file system, if any
    shutil.rmtree(os.path.join(STORE, name), ignore_errors=True)
    print(json.dumps({"removed": name}))


def status(name):
    print(json.dumps(normalize(find(name))))


def ls():
    print(json.dumps(containers(), indent=1))


def exec_(name, cmd, timeout=60):
    if not cmd:
        raise SystemExit("exec needs a command: terminal-ct exec NAME CMD...")
    if find(name).get("State", {}).get("Status") != "running":
        raise SystemExit(f"{name} is not running; start it: terminal-ct start {name}")
    try:
        r = node().run(["exec", PREFIX + name, *cmd], check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"command still running after {timeout} s (raise --timeout)")
    out = {"exit": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    print(json.dumps(out))
    return out


# ---------- quota and resources ----------
# One ceiling for all of terminal-ct's containers, set by the user (`quota`), by default from the engine's host.
# Checked under a lock at create (disk), start and resources, so no sequence of commands, human or agent, gets past
# it. CPUs and memory count each running container's ceiling (--cpus, --memory), not its momentary use.
QUOTA_KEYS = ("cpus", "memory_gb", "disk_gb", "containers")
_UNITS = {"": 1, "b": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12, "ki": 2 ** 10, "mi": 2 ** 20, "gi": 2 ** 30, "ti": 2 ** 40}


def parse_size(s):
    """Bytes from an engine's human size: 3MB, 498 MB, 352KiB, 0B."""
    m = re.match(r"\s*([\d.]+)\s*([kKmMgGtT]?i?)[bB]?", s or "")
    return float(m.group(1)) * _UNITS[m.group(2).lower()] if m else 0


def _quota_file():
    try:
        with open(os.path.join(STORE, "quota.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def disk_bytes(infos):
    """Space terminal-ct holds: its containers' writable layers, its volumes, its desktop images. Real use, not a
    limit: per-container disk limits exist only with some storage drivers."""
    total = sum(c.get("SizeRw") or 0 for c in infos)
    if node().engine == "podman":  # every tct- volume, also one left without its container
        rows = [r.split("\t") for r in node().run(["volume", "ls", "--format", "{{.Name}}\t{{.Mountpoint}}"]).stdout.splitlines()]
        vols = {r[0]: r[-1] for r in rows if r[0].startswith(PREFIX)}
        if vols and not WINDOWS and all(os.path.isdir(p) for p in vols.values()):
            # rootless volumes are folders of ours: du takes 4 ms where `system df -v` took 0.7 s. shortcut: files
            # of other uids inside the container may be unreadable and left out (`podman unshare du` if that matters)
            r = subprocess.run(["du", "-skc", *vols.values()], capture_output=True, text=True)
            total += int((r.stdout.strip().splitlines() or ["0"])[-1].split()[0]) * 1024
        elif vols:  # a Podman machine (Windows, macOS): the folders are in its VM, ask Podman
            sizes = volume_sizes()
            total += sum(sizes.get(n, 0) for n in vols)
    else:
        total += sum(v for n, v in volume_sizes().items() if n.startswith(PREFIX))
    for line in node().run(["image", "ls", "--format", "{{.Repository}} {{.Size}}"]).stdout.splitlines():
        repo, _, size = line.partition(" ")
        if repo.split("/")[-1] == "tct-desktop":
            total += parse_size(size)
    # a kept image counts only the layers its commits added (its Size includes the base image's, already counted)
    total += sum(keep_bytes(labels_of(c).get(f"{LABEL}.name")) for c in infos)
    return total


def keep_bytes(name):
    try:
        with open(os.path.join(STORE, name, "keep_bytes")) as f:
            return int(f.read())
    except (OSError, ValueError, TypeError):
        return 0


def volume_sizes():
    """{volume: bytes} from the engine, for volumes whose folders are not on this machine (a Podman machine's VM on
    Windows and macOS, Docker's root-owned store)."""
    if node().engine == "docker":
        vols = json.loads(node().run(["system", "df", "-v", "--format", "{{json .Volumes}}"]).stdout or "[]") or []
        return {v.get("Name", ""): parse_size(v.get("Size", "")) for v in vols}
    out = node().run(["system", "df", "-v"]).stdout  # podman refuses --format with -v: read its table
    rows = out[out.find("VOLUME NAME"):].splitlines()[1:] if "VOLUME NAME" in out else []
    return {p[0]: parse_size(" ".join(p[2:])) for p in (r.split() for r in rows) if len(p) >= 3}


def quota_usage(disk=True):
    infos = raw_containers(size=disk)
    use = dict.fromkeys(QUOTA_KEYS, 0)
    for c in infos:
        if running(c):
            cpus, mem = allotted(c)
            use["cpus"] += cpus; use["memory_gb"] += mem; use["containers"] += 1
    if disk:
        use["disk_gb"] = disk_bytes(infos) / 2 ** 30
    return use


def host_free_bytes(path):
    """Free space containers can grow into. Under WSL the engine's store sits in a virtual disk that df reports at
    its maximum size while it really grows inside a Windows drive: count that drive too."""
    free = shutil.disk_usage(path).free
    if in_wsl() and os.path.isdir("/mnt/c"):  # shortcut: assumes the distribution's disk is on C:, the default
        free = min(free, shutil.disk_usage("/mnt/c").free)
    return free


def quota_limits(use):
    """The user's quota; defaults from the engine's host for what was never set: cores - 1, 75% of its memory, half
    of the space containers can have (free now plus what they hold), 16 running containers."""
    set_ = _quota_file()
    cpus, mem, root = node().host()
    try:
        free = host_free_bytes(root)
    except OSError:  # the engine's store is in another machine (Docker Desktop's VM): use ours
        free = host_free_bytes(STORE if os.path.isdir(STORE) else os.path.dirname(STORE))
    default = {"cpus": max(1, cpus - 1), "memory_gb": max(1, int(mem * 3 // 4)),
               "disk_gb": int((free / 2 ** 30 + use.get("disk_gb", 0)) // 2), "containers": 16}
    return {k: set_.get(k, default[k]) for k in QUOTA_KEYS}, {k: k in set_ for k in QUOTA_KEYS}


class quota_lock:
    """Cross-process lock (an atomic mkdir), so two commands cannot both pass the check and then both start."""
    def __enter__(self):
        os.makedirs(STORE, exist_ok=True)
        self.path = os.path.join(STORE, ".quota.lock")
        for _ in range(300):
            try:
                os.mkdir(self.path); return self
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - os.path.getmtime(self.path) > 120:  # left by a crashed command
                        # rename first: of two waiters seeing it stale, only one claims it (rmdir alone let the
                        # second remove the first's fresh lock)
                        stale = f"{self.path}.{os.getpid()}"
                        os.rename(self.path, stale)
                        os.rmdir(stale)
                time.sleep(0.1)
        raise SystemExit("another terminal-ct command holds the quota lock; try again")

    def __exit__(self, *exc):
        with contextlib.suppress(OSError):
            os.rmdir(self.path)


def quota_check(add):
    """Refuse when adding `add` to current use would pass the quota (disk: when it is already reached)."""
    use = quota_usage(disk="disk_gb" in add)
    lim = quota_limits(use)[0]
    over = [f"{k}: {use[k]:.4g} used + {add[k]:g} > {lim[k]}" for k in add
            if use[k] + add[k] > lim[k] or k == "disk_gb" and use[k] >= lim[k]]
    if over:
        raise SystemExit("quota exceeded (" + "; ".join(over) + "). Free resources (stop or shrink containers: "
                         "a container without limits counts as the whole host, set them with terminal-ct resources), "
                         "or the user raises the quota: terminal-ct quota --cpus/--memory/--disk/--containers")


def quota(set_=None):
    """Show the quota and its use, or (user only) change it."""
    if set_ and any(v is not None for v in set_.values()):
        if any(v is not None and v < 1 for v in set_.values()):
            raise SystemExit("quota values must be at least 1")
        with quota_lock():
            stored = _quota_file()
            stored.update({k: v for k, v in set_.items() if v is not None})
            with open(os.path.join(STORE, "quota.json"), "w") as f:
                json.dump(stored, f)
    use = quota_usage()
    lim, chosen = quota_limits(use)
    return {k: {"limit": lim[k], "used": round(use[k], 2), "free": round(lim[k] - use[k], 2),
                "source": "user" if chosen[k] else "host default"} for k in QUOTA_KEYS}


def resources(name, cpus=None, memory=None, lock=True):
    """Show or change a container's CPUs and memory (GiB), at once even while it runs, within the quota."""
    info = find(name)
    if cpus is not None or memory is not None:
        if cpus is not None and cpus <= 0 or memory is not None and memory < 0.0625:
            raise SystemExit("cpus must be > 0 and memory at least 0.0625 GiB (64 MiB)")
        with quota_lock() if lock else contextlib.nullcontext():
            if running(info):
                cur = allotted(info)
                quota_check({"cpus": max(0, (cpus or cur[0]) - cur[0]), "memory_gb": max(0, (memory or cur[1]) - cur[1])})
            mem = [] if memory is None else ["--memory", str(int(memory * 2 ** 30)), "--memory-swap", str(int(memory * 2 ** 30))]
            node().run(["update", *(["--cpus", f"{cpus:g}"] if cpus else []), *mem, PREFIX + name])
        info = find(name)
    c = normalize(info)
    return {"name": name, "status": c["status"], "cpus": c["cpus"], "memory_gb": c["memory_gb"]}


def top():
    """Real use of each running container (CPU % of one core, memory, processes) next to its ceiling, and the quota."""
    infos = [c for c in raw_containers() if running(c)]
    stats = {}
    if infos:
        out = node().run(["stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}",
                          *(c.get("Name", "").lstrip("/") for c in infos)]).stdout
        for line in out.splitlines():
            p = line.split("|")
            if len(p) == 4:
                stats[p[0].strip()] = {"cpu_percent": float(p[1].strip().rstrip("%") or 0),
                                       "used_memory_gb": round(parse_size(p[2].split("/")[0]) / 2 ** 30, 3),
                                       "pids": int(p[3]) if p[3].strip().isdigit() else None}
    rows = []
    for c in infos:
        n = normalize(c)
        rows.append({"name": n["name"], "cpus": n["cpus"], "memory_gb": n["memory_gb"],
                     **stats.get(c.get("Name", "").lstrip("/"), {}), **({"sandbox": True} if n.get("sandbox") else {})})
    return {"containers": rows, "quota": quota()}


# ---------- networks, internet, sandbox ----------
# Networks are the engine's internal networks (no LAN, no internet; containers on one reach each other by name).
# Internet is the engine's default bridge: a container is plugged into it or not. Changes apply to stopped
# containers (Podman's DNS inside a running one kept the old setup).

def networks():
    """terminal-ct's networks: {name: {"created_by", "members"}}."""
    ids = node().run(["network", "ls", "-q", "--filter", f"label={LABEL}.net"]).stdout.split()
    out = {}
    for n in node().json(["network", "inspect", *ids]) if ids else []:
        lab = n.get("Labels") or n.get("labels") or {}  # docker, podman
        if lab.get(f"{LABEL}.net"):
            out[lab[f"{LABEL}.net"]] = {"created_by": lab.get(f"{LABEL}.created_by", "user"),
                                        "project": lab.get(f"{LABEL}.project") or DEFAULT_PROJECT, "members": []}
    if out:
        for c in raw_containers():
            for k in networks_of(c):
                if k.startswith(NET_PREFIX) and k[len(NET_PREFIX):] in out:
                    out[k[len(NET_PREFIX):]]["members"].append(labels_of(c).get(f"{LABEL}.name"))
    return out


def net_create(net, created_by="user", project=None):
    check_name(net)
    project = check_project(project or DEFAULT_PROJECT)
    if net in networks():
        raise SystemExit(f"network {net!r} already exists")
    node().run(["network", "create", "--internal", "--label", f"{LABEL}.net={net}",
                "--label", f"{LABEL}.created_by={created_by}", "--label", f"{LABEL}.project={project}", NET_PREFIX + net])
    return {"created": net, "internal": True, "project": project}


def net_rm(net):
    n = networks().get(check_name(net))
    if n is None:
        raise SystemExit(f"no network {net!r}")
    if n["members"]:
        raise SystemExit(f"network {net!r} still has containers: {', '.join(n['members'])} (net detach them first)")
    node().run(["network", "rm", NET_PREFIX + net])
    return {"removed": net}


def stopped(name, what):
    """A stopped container whose networks can change: one made before 0.3 is brought up to date first, keeping
    its file system and /data (remake keep=True)."""
    info = find(name)
    if running(info):
        raise SystemExit(f"stop {name} first: {what} changes apply to a stopped container (terminal-ct stop {name})")
    if not is_bridged(info):
        info = remake(name, info, keep=True)
    return info


def net_attach(name, net, attach=True):
    info = stopped(name, "network")
    n = networks().get(check_name(net))
    if n is None:
        raise SystemExit(f"no network {net!r}; create it with: terminal-ct net create {net}")
    on = NET_PREFIX + net in networks_of(info)
    if attach and not on:
        node().run(["network", "connect", "--alias", name, NET_PREFIX + net, PREFIX + name])
    elif not attach and on:
        node().run(["network", "disconnect", NET_PREFIX + net, PREFIX + name])
    return {"name": name, "networks": normalize(find(name))["networks"]}


def internet(name, on=None):
    info = find(name) if on is None else stopped(name, "internet")
    if on is not None and on != online(info):
        node().run(["network", "connect" if on else "disconnect", BRIDGE[node().engine], PREFIX + name])
        info = find(name)
    return {"name": name, "internet": online(info)}


def sandbox(name, on):
    """Sandbox on: no capabilities, no new privileges, a process limit, not root, /data in memory, a fresh file
    system at each start, offline. Off: the same container back as a normal one (still offline, /data's volume
    back). Both recreate the container from its image."""
    info = stopped(name, "sandbox")
    if (labels_of(info).get(f"{LABEL}.sandbox") == "1") != on:
        info = remake(name, info, sandbox=on, **({"internet": False} if on else {}))
    c = normalize(info)
    return {"name": name, "sandbox": bool(c.get("sandbox")), "internet": c["internet"]}


# ---------- resume after a restart ----------
# What each container should be doing is kept on the host (STORE/NAME/want: running or stopped, written by start
# and stop), so after the PC (or WSL) restarts, `resume` starts again what was running and leaves alone what the
# user or an agent stopped. `autostart on` runs it at each logon.

def want(name, state):
    with open(os.path.join(ct_dir(name), "want"), "w") as f:
        f.write(state)


def wanted(name):
    try:
        with open(os.path.join(STORE, name, "want")) as f:
            return f.read().strip()
    except OSError:
        return "stopped"


def each_engine():
    """The engine chosen (--engine, TERMINAL_CT_ENGINE), else each installed one in turn, as the current node."""
    global NODE
    if NODE is not None or os.environ.get("TERMINAL_CT_ENGINE"):
        yield node()
        return
    for e in ("podman", "docker"):
        if shutil.which(e):
            NODE = Node(e)
            yield NODE
    NODE = None


def resume(stay=False, wait=60):
    """Start every container that was running when the machine stopped, on each engine. stay: then keep this
    process alive while any terminal-ct container runs (WSL shuts its VM down once no Windows process uses it)."""
    out = []
    for n_ in each_engine():
        for _ in range(wait):  # the engine may still be starting (dockerd at WSL boot)
            if n_.run(["ps", "-q"], check=False).returncode == 0:
                break
            time.sleep(1)
        for c in raw_containers():
            n = labels_of(c).get(f"{LABEL}.name")
            if wanted(n) == "running" and not running(c):
                try:
                    out.append(dict(quiet(start, n), name=n, engine=n_.engine))
                except (SystemExit, Exception) as e:  # one refusal (quota) must not keep the others down
                    out.append({"name": n, "engine": n_.engine, "error": str(e)})
    print(json.dumps({"resumed": out}))
    while stay and any(running(c) for _ in each_engine() for c in raw_containers()):
        time.sleep(60)


AUTOSTART = "terminal-ct-resume"


def autostart(on=None):
    """Run `resume` at each logon: a Windows scheduled task (for WSL: wsl.exe running it inside the distribution,
    kept alive while containers run), else a systemd user service on Linux."""
    me = [sys.executable, os.path.abspath(__file__)]
    unit = os.path.expanduser(f"~/.config/systemd/user/{AUTOSTART}.service")
    env = {k: os.environ[k] for k in ("TERMINAL_CT_HOME", "XDG_DATA_HOME") if os.environ.get(k)}  # where STORE is
    if on:  # containers running now, from before `want` was kept (0.2): they are to come back too
        for _ in each_engine():
            for c in raw_containers():
                n = labels_of(c).get(f"{LABEL}.name")
                if running(c) and not os.path.isfile(os.path.join(STORE, n, "want")):
                    want(n, "running")
    if WINDOWS or in_wsl():
        schtasks = "schtasks.exe" if in_wsl() else "schtasks"
        if on is None:
            return {"autostart": subprocess.run([schtasks, "/Query", "/TN", AUTOSTART], capture_output=True).returncode == 0}
        if not on:
            subprocess.run([schtasks, "/Delete", "/TN", AUTOSTART, "/F"], capture_output=True)
            return {"autostart": False}
        if in_wsl():  # conhost --headless: no console window at logon; wsl.exe reads a Windows command line
            cmd = "conhost.exe --headless " + subprocess.list2cmdline(
                ["wsl.exe", "-d", os.environ.get("WSL_DISTRO_NAME", "Debian"), "--exec", "env",
                 *(f"{k}={v}" for k, v in env.items()), *me, "resume", "--stay"])
        else:
            if env:  # shortcut: a task's command line sets no variables; set them as user variables to use this
                raise SystemExit(f"autostart on Windows uses the default store; unset {', '.join(env)} first")
            cmd = subprocess.list2cmdline(me + ["resume"])
        if len(cmd) > 261:  # schtasks /TR limit
            raise SystemExit(f"the task's command line is {len(cmd)} characters, schtasks takes 261: {cmd}")
        r = subprocess.run([schtasks, "/Create", "/TN", AUTOSTART, "/SC", "ONLOGON", "/TR", cmd, "/F"],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit((r.stderr or r.stdout).strip())
        return {"autostart": True, "task": AUTOSTART, "runs": cmd}
    if on is None:
        return {"autostart": os.path.isfile(unit)}
    if not on:
        subprocess.run(["systemctl", "--user", "disable", f"{AUTOSTART}.service"], capture_output=True)
        with contextlib.suppress(OSError):
            os.remove(unit)
        return {"autostart": False}
    os.makedirs(os.path.dirname(unit), exist_ok=True)
    with open(unit, "w") as f:
        # no After=default.target: WantedBy already orders it, and both made an ordering cycle
        f.write(f"[Unit]\nDescription=terminal-ct: start again the containers that were running\n\n"
                f"[Service]\nType=oneshot\n" + "".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in env.items())
                + f"ExecStart={shlex.join(me + ['resume'])}\n\n[Install]\nWantedBy=default.target\n")
    r = subprocess.run(["systemctl", "--user", "enable", f"{AUTOSTART}.service"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit((r.stderr or r.stdout).strip())
    return {"autostart": True, "unit": unit,
            "note": "runs at your login; for boot without login: loginctl enable-linger"}


# ---------- projects ----------
# A project groups containers and networks (label terminal-ct.project; "default" when none was given) with what
# an agent needs to pick up where it left off: its notes and the log of what was done. An MCP server started with
# --project sees and manages only that project, so each agent works on its own and not on the others'.

def project_dir(project):
    # one flat folder per project ("+" for "/"): a sub-project's folder never lands among its parent's files
    d = os.path.join(STORE, "projects", check_project(project or DEFAULT_PROJECT).replace("/", "+"))
    os.makedirs(d, exist_ok=True)
    return d


def projects():
    names = {DEFAULT_PROJECT} | {labels_of(c).get(f"{LABEL}.project") or DEFAULT_PROJECT for c in raw_containers()}
    if os.path.isdir(os.path.join(STORE, "projects")):
        names |= {d.replace("+", "/") for d in os.listdir(os.path.join(STORE, "projects"))}
    cs, ns = containers(), networks()
    return [{"project": p, "containers": sum(c["project"] == p for c in cs),
             "running": sum(c["project"] == p and c["status"] == "running" for c in cs),
             "networks": sum(n["project"] == p for n in ns.values())} for p in sorted(names)]


def project_status(project=None, log_lines=30):
    """Everything an agent needs to resume: the project's containers and networks as they are now, its notes, and
    the latest actions (with errors) from its log."""
    project = project or DEFAULT_PROJECT
    d = project_dir(project)
    notes, log = "", []
    with contextlib.suppress(OSError):
        with open(os.path.join(d, "notes.md"), encoding="utf-8") as f:
            notes = f.read()
    with contextlib.suppress(OSError):
        with open(os.path.join(d, AGENT_LOG), encoding="utf-8") as f:
            log = [json.loads(l) for l in collections.deque(f, log_lines) if l.strip()]
    return {"project": project,
            "containers": [dict(c, want=wanted(c["name"])) for c in containers() if under(c["project"], project)],
            "networks": {k: v for k, v in networks().items() if under(v["project"], project)},
            "sub_projects": sorted(p["project"] for p in projects() if p["project"].startswith(project + "/")),
            "notes": notes, "recent_actions": log}


def notes_write(project, text, mode="append"):
    path = os.path.join(project_dir(project), "notes.md")
    with open(path, "a" if mode == "append" else "w", encoding="utf-8") as f:
        f.write((f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n" if mode == "append" else "") + text.rstrip() + "\n")
    return {"project": project or DEFAULT_PROJECT, "notes": mode, "bytes": os.path.getsize(path)}


def project_move(name, project):
    """Move a stopped container to another project, keeping its file system and /data."""
    info = find(name)
    # a sandbox starts from its image anyway: committing would make whatever is in it now its new image
    remake(name, info, keep=labels_of(info).get(f"{LABEL}.sandbox") != "1", project=check_project(project))
    return {"name": name, "project": project}


def image(cmd, ref=None, path=None):
    if cmd == "ls":
        ids = node().run(["image", "ls", "-q"]).stdout.split()
        imgs = node().json(["image", "inspect", *dict.fromkeys(ids)]) if ids else []
        print(json.dumps([{"tags": i.get("RepoTags") or [], "id": i.get("Id", "").removeprefix("sha256:")[:12],
                           "size_mb": round(i.get("Size", 0) / 2 ** 20)} for i in imgs], indent=1))
    elif cmd == "pull":
        pull(ref)
        print(json.dumps({"pulled": ref}))
    elif cmd == "build":
        check_name(ref)
        if not os.path.isfile(os.path.join(path or "", "Dockerfile")):
            raise SystemExit(f"no Dockerfile in {path!r}")
        node().run(["build", "-t", ref, path], capture=False)
        print(json.dumps({"built": ref}))
    elif cmd == "rm":
        node().run(["image", "rm", ref])
        print(json.dumps({"removed": ref}))


# ---------- screen: RFB (VNC) client over the desktop's unix socket ----------

class RFB:
    """Minimal RFB 3.8 client: security None (the socket is the user's alone), 32-bit true colour, raw encoding.
    Each update request asks for the whole screen: over a local socket that is a few ms, and never waits."""
    def __init__(self, path, timeout=5):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(timeout)
        try:
            self.s.connect(path)
        except OSError as e:
            raise RuntimeError(f"desktop not reachable ({e}); is the container running with --desktop?")
        if not self._read(12).startswith(b"RFB 003."):
            raise RuntimeError("not a VNC server")
        self.s.sendall(b"RFB 003.008\n")
        types = self._read(self._read(1)[0])
        if 1 not in types:
            raise RuntimeError("the desktop asks for a password")
        self.s.sendall(b"\x01")
        if struct.unpack(">I", self._read(4))[0]:
            raise RuntimeError("VNC security handshake failed")
        self.s.sendall(b"\x01")  # shared: viewers and agents side by side
        self.w, self.h = struct.unpack(">HH", self._read(4))
        self._read(16)
        self._read(struct.unpack(">I", self._read(4))[0])  # desktop name
        # 32 bpp, depth 24, little endian, true colour, R<<16 G<<8 B: bytes arrive as B, G, R, X
        self.s.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        self.s.sendall(struct.pack(">BxHii", 2, 2, 0, -223))  # raw, DesktopSize
        self.fb = bytearray(self.w * self.h * 4)
        self.mask = 0

    def _read(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("desktop closed the connection")
            buf += chunk
        return bytes(buf)

    def refresh(self):
        self.s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.w, self.h))
        while True:
            t = self._read(1)[0]
            if t == 0:
                break
            if t == 2:  # bell
                continue
            if t == 3:  # server cut text
                self._read(3); self._read(struct.unpack(">I", self._read(4))[0]); continue
            if t == 1:  # colour map: not used in true colour
                self._read(3); self._read(struct.unpack(">H", self._read(2))[0] * 6); continue
            raise RuntimeError(f"unexpected VNC message {t}")
        self._read(1)
        for _ in range(struct.unpack(">H", self._read(2))[0]):
            x, y, w, h, enc = struct.unpack(">HHHHi", self._read(12))
            if enc == -223:  # screen size changed
                self.w, self.h, self.fb = w, h, bytearray(w * h * 4)
            elif enc == 0:
                data = self._read(w * h * 4)
                for row in range(h):
                    o = ((y + row) * self.w + x) * 4
                    self.fb[o:o + w * 4] = data[row * w * 4:(row + 1) * w * 4]
            else:
                raise RuntimeError(f"unexpected VNC encoding {enc}")
        return self

    def rgb(self):
        out = bytearray(self.w * self.h * 3)
        out[0::3], out[1::3], out[2::3] = self.fb[2::4], self.fb[1::4], self.fb[0::4]
        return bytes(out)

    def pointer(self, x, y, mask=None):
        self.mask = self.mask if mask is None else mask
        x, y = max(0, min(self.w - 1, round(x))), max(0, min(self.h - 1, round(y)))
        self.s.sendall(struct.pack(">BBHH", 5, self.mask, x, y))
        self.x, self.y = x, y

    def key(self, keysym, down):
        self.s.sendall(struct.pack(">BBxxI", 4, int(down), keysym))

    def close(self):
        self.s.close()


def png(rgb, w, h):
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


# X keysyms: Xvnc maps them to keys itself, so text needs no keyboard layout
KEYSYMS = {"return": 0xFF0D, "enter": 0xFF0D, "ret": 0xFF0D, "tab": 0xFF09, "esc": 0xFF1B, "escape": 0xFF1B,
           "backspace": 0xFF08, "delete": 0xFFFF, "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
           "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54, "pgup": 0xFF55, "pageup": 0xFF55,
           "pgdn": 0xFF56, "pagedown": 0xFF56, "space": 0x20, "spc": 0x20, "menu": 0xFF67, "capslock": 0xFFE5,
           "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3, "alt": 0xFFE9, "super": 0xFFEB, "meta": 0xFFEB,
           "cmd": 0xFFEB, "win": 0xFFEB, **{f"f{i}": 0xFFBD + i for i in range(1, 13)}}
BUTTONS = {"left": 1, "middle": 2, "right": 4}
STEP = 0.03  # between pointer move, press and release: back-to-back, the click missed its window (no focus)


def char_keysym(ch):
    if ch == "\n":
        return 0xFF0D
    if ch == "\t":
        return 0xFF09
    cp = ord(ch)
    return cp if 0x20 <= cp <= 0xFF else 0x01000000 + cp  # Latin-1 as is, the rest as Unicode keysyms


def chord(spec):
    keys = []
    for part in spec.lower().split("+") if spec != "+" else ["+"]:
        k = KEYSYMS.get(part) or (char_keysym(part) if len(part) == 1 else None)
        if k is None:
            raise ValueError(f"unknown key {part!r}")
        keys.append(k)
    return keys


def desktop(name):
    info = find(name)
    if ((info.get("Config") or {}).get("Labels") or {}).get(f"{LABEL}.desktop") != "1":
        raise RuntimeError(f"{name} has no desktop (create it with --desktop)")
    if (info.get("State") or {}).get("Status") != "running":
        raise RuntimeError(f"{name} is not running; start it: terminal-ct start {name}")
    path = os.path.join(STORE, name, "run", "vnc.sock")
    for _ in range(50):  # the desktop may still be starting
        if os.path.exists(path):
            break
        time.sleep(0.1)
    return RFB(path)


def act(name, tool, a):
    """Shared by the CLI and MCP. Coordinates are pixels of the latest screenshot."""
    r = desktop(name)
    try:
        if tool == "screenshot":
            r.refresh()
            return png(r.rgb(), r.w, r.h), (r.w, r.h)
        if tool == "status":
            return {"status": "running", "width": r.w, "height": r.h}
        if tool == "wait":  # until the screen is unchanged for `stable` seconds, or timeout
            start, last, since = time.time(), None, time.time()
            while True:
                data = bytes(r.refresh().fb)
                if data != last:
                    last, since = data, time.time()
                elif time.time() - since >= float(a.get("stable", 1)):
                    return {"stable": True, "waited": round(time.time() - start, 1)}
                if time.time() - start >= float(a.get("timeout", 30)):
                    return {"stable": False, "waited": round(time.time() - start, 1)}  # a blinking cursor never settles
                time.sleep(0.25)
        if tool == "click":
            b = BUTTONS.get(a.get("button", "left"))
            if b is None:
                raise ValueError("button must be left, right or middle")
            r.pointer(a["x"], a["y"], 0)
            for _ in range(int(a.get("clicks", 1))):
                time.sleep(STEP); r.pointer(r.x, r.y, b); time.sleep(STEP); r.pointer(r.x, r.y, 0)
        elif tool == "move":
            r.pointer(a["x"], a["y"], 0)
        elif tool == "drag":
            r.pointer(a["x"], a["y"], 0); time.sleep(STEP); r.pointer(a["x"], a["y"], 1)
            for i in range(1, 11):
                r.pointer(a["x"] + (a["x2"] - a["x"]) * i / 10, a["y"] + (a["y2"] - a["y"]) * i / 10, 1)
                time.sleep(0.02)
            time.sleep(STEP); r.pointer(a["x2"], a["y2"], 0)
        elif tool == "scroll":
            x, y = (a["x"], a["y"]) if "x" in a and "y" in a else (r.w / 2, r.h / 2)
            dy = int(a["dy"])  # negative scrolls down, like terminal-vm
            for _ in range(max(1, abs(dy) // 100)):
                b = 8 if dy > 0 else 16  # buttons 4 and 5
                r.pointer(x, y, 0); time.sleep(STEP); r.pointer(x, y, b); time.sleep(STEP); r.pointer(x, y, 0)
        elif tool == "type":
            for ch in a["text"]:
                k = char_keysym(ch)
                r.key(k, True); r.key(k, False)
        elif tool == "key":
            keys = chord(a["key"])
            for k in keys:
                r.key(k, True)
            for k in reversed(keys):
                r.key(k, False)
        else:
            raise ValueError(f"unknown action {tool!r}")
        r.s.settimeout(1)
        r.refresh()  # round trip: the input has reached the server before we return
        return {"ok": True}
    finally:
        r.close()


def settled_screenshot(name, before=None, timeout=1.5):
    """PNG after an action: once the screen differs from `before` (at most `timeout` s), then 0.3 s to finish."""
    end = time.time() + timeout
    while before is not None and time.time() < end and _pixels(name) == before:
        time.sleep(0.1)
    time.sleep(0.3)
    return act(name, "screenshot", {})[0]


def _pixels(name):
    r = desktop(name)
    try:
        return bytes(r.refresh().fb)
    finally:
        r.close()


# ---------- interactive terminal viewer (from terminal-vm) ----------

CSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
            "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
TILDE_KEYS = {"2": "insert", "3": "delete", "5": "pgup", "6": "pgdn", "15": "f5", "17": "f6", "18": "f7",
              "19": "f8", "20": "f9", "21": "f10", "23": "f11", "24": "f12"}
_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CELL = re.compile(r"\x1b\[6;(\d+);(\d+)t")  # reply to CSI 16 t: cell height, width in pixels
_CSI = re.compile(r"\x1b[\[O](\d*)(?:;(\d+))?([A-Za-z~])")


def parse_input(data):
    """Terminal bytes (decoded) -> events: ("text", s) ("key", chord) ("mouse", code, x, y, pressed) ("quit",) ("show",)."""
    ev, i = [], 0
    while i < len(data):
        c = data[i]
        if c == "\x1b":
            m = _MOUSE.match(data, i)
            if m:
                ev.append(("mouse", int(m[1]), int(m[2]), int(m[3]), m[4] == "M")); i = m.end(); continue
            m = _CELL.match(data, i)
            if m:
                ev.append(("cell", int(m[1]), int(m[2]))); i = m.end(); continue
            m = _CSI.match(data, i)
            if m:
                num, mod, final = m.groups()
                name = TILDE_KEYS.get(num) if final == "~" else CSI_KEYS.get(final)
                if name:
                    bits = int(mod) - 1 if mod else 0
                    mods = [k for b, k in ((4, "ctrl"), (2, "alt"), (1, "shift")) if bits & b]
                    ev.append(("key", "+".join(mods + [name])))
                i = m.end(); continue
            if i + 1 < len(data) and data[i + 1].isprintable():
                ev.append(("key", "alt+" + data[i + 1])); i += 2; continue
            ev.append(("key", "esc")); i += 1; continue
        if c == "\x1d":
            ev.append(("quit",))
        elif c == "\x0f":
            ev.append(("show",))
        elif c in "\r\n":
            ev.append(("key", "ret"))
        elif c == "\t":
            ev.append(("key", "tab"))
        elif c in "\x7f\x08":
            ev.append(("key", "backspace"))
        elif ord(c) < 32:
            ev.append(("key", "ctrl+" + chr(ord(c) + 96)))
        elif ev and ev[-1][0] == "text":
            ev[-1] = ("text", ev[-1][1] + c)
        else:
            ev.append(("text", c))
        i += 1
    return ev


def graphics():
    """Inline image protocol of this terminal, from its environment (no query round-trip): kitty, sixel or None.
    WT_SESSION: Windows Terminal (Sixel since 1.22), also passed into WSL."""
    e = os.environ
    if "KITTY_WINDOW_ID" in e or e.get("TERM") == "xterm-kitty" or e.get("TERM_PROGRAM", "").lower() in ("ghostty", "wezterm"):
        return "kitty"
    if "WT_SESSION" in e or e.get("TERM") in ("foot", "foot-extra", "mlterm") or e.get("TERM_PROGRAM") == "mintty":
        return "sixel"
    return None


def term_geometry(cell=(20, 10)):
    """cols, rows, cell width and height in pixels. cell: (height, width) as the terminal answered CSI 16 t,
    used when the tty gives no pixel size (Windows consoles, some terminals)."""
    xp = yp = 0
    try:
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    except (OSError, NameError):  # NameError: no fcntl on Windows
        cols, rows = shutil.get_terminal_size()
    return cols, rows, (xp / cols if xp else cell[1]), (yp / rows if yp else cell[0])


def fit(w, h, cols, rows, cw, ch):
    """Cells used to show a w x h image in cols x (rows-1) cells, keeping its aspect ratio."""
    scale = min(cols * cw / w, (rows - 1) * ch / h)
    return max(1, int(w * scale / cw)), max(1, int(h * scale / ch))


# 15-bit colour key, 5 bits per channel (4 bits banded smooth gradients): r << 10 | g << 5 | b, as two bytes
_HI_R, _HI_G = bytes(v >> 3 << 2 for v in range(256)), bytes(v >> 6 for v in range(256))
_LO_G, _LO_B = bytes((v >> 3 & 7) << 5 for v in range(256)), bytes(v >> 3 for v in range(256))
_IS = [bytes(int(i == c) for i in range(256)) for c in range(256)]
_RLE = re.compile(rb"(.)\1{3,}")


def colour_keys(line):
    """RGB bytes -> one 15-bit colour key per pixel, as a memoryview of ints (byte sums never carry)."""
    n, keys = len(line) // 3, bytearray(2 * len(line) // 3)
    r, g, b = line[0::3], line[1::3], line[2::3]
    keys[1::2] = (int.from_bytes(r.translate(_HI_R), "big") + int.from_bytes(g.translate(_HI_G), "big")).to_bytes(n, "big")
    keys[0::2] = (int.from_bytes(g.translate(_LO_G), "big") + int.from_bytes(b.translate(_LO_B), "big")).to_bytes(n, "big")
    return memoryview(keys).cast("H")  # native order is little-endian on x86/ARM: low byte first


def make_palette(rgb, w, h, size=255):
    """The screen's most frequent colours (15-bit keys) and a key -> register table (255: not mapped yet).
    An adaptive palette keeps interface colours nearly exact, where a fixed cube needed a grainy dither."""
    counts = collections.Counter()
    for y in range(0, h, 2):  # every other row ranks the colours just as well, in half the time
        counts.update(colour_keys(rgb[y * w * 3:(y + 1) * w * 3]))
    keys = [k for k, _ in counts.most_common(size)]
    lut = bytearray(b"\xff" * 32768)
    for i, k in enumerate(keys):
        lut[k] = i
    return keys, lut, {}


def _nearest(palette, k):
    """Nearest register to colour key k. Smoothing small text brings thousands of in-between shades per new
    screen: the palette's components are unpacked once (cells[None]) and the search is a flat min over tuples
    (~45 us), which a per-shade lambda made 20 times slower."""
    keys, _, cells = palette
    if None not in cells:
        cells[None] = [(i, q >> 10, q >> 5 & 31, q & 31) for i, q in enumerate(keys)]
    r, g, b = k >> 10, k >> 5 & 31, k & 31
    return min(((r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2, i) for i, pr, pg, pb in cells[None])[1]


_HALF = bytes(v >> 1 for v in range(256))


def _blend(a, b):
    """Byte-wise average of two equal-length byte strings (halves summed as big integers: no carry)."""
    return (int.from_bytes(a.translate(_HALF), "big") + int.from_bytes(b.translate(_HALF), "big")).to_bytes(len(a), "big")


def sixel_rows(rgb, w, h, W, H, palette, old=None, prev=None):
    """RGB bytes (w x h) -> W x H rows of palette registers. Shrinking averages each pixel with its right and lower
    neighbours first, so 1-pixel strokes of small text soften instead of vanishing with the skipped columns; growing
    is nearest-neighbour. No per-pixel Python loop except map(): bytes.translate and big-integer sums work on whole
    rows. prev: the rows this returned for the frame old (same sizes): rows whose source lines did not change are
    reused, so a moving pointer costs a few lines, not a frame. Colours missing from the palette get their nearest
    register."""
    lut = palette[1]
    xs, rows, cache, row_len = [x * w // W for x in range(W)], [], {}, w * 3
    taps = 2 if H < h else 1  # source lines behind one output line

    def source(y):
        line = rgb[y * row_len:(y + 1) * row_len]
        return _blend(line, line[3:] + line[-3:]) if W < w else line
    for Y, y in enumerate(y * h // H for y in range(H)):
        span = slice(y * row_len, min(y + taps, h) * row_len)
        if prev and old[span] == rgb[span]:
            rows.append(prev[Y]); continue
        if y not in cache:
            line = source(y) if taps == 1 or y + 1 >= h else _blend(source(y), source(y + 1))
            mv = colour_keys(line)
            vals = mv if W == w else map(mv.__getitem__, xs)
            row = bytes(map(lut.__getitem__, vals))
            if 255 in row:  # colours not in the palette yet: map them, then this row again
                vals = mv if W == w else list(map(mv.__getitem__, xs))
                for k in set(vals):
                    if lut[k] == 255:
                        lut[k] = _nearest(palette, k)
                row = bytes(map(lut.__getitem__, vals))
            cache[y] = row
        rows.append(cache[y])
    return rows


def changed(old, new, gap=20):
    """Boxes (x0, y0, x1, y1) around the bytes that differ between two equal-sized row lists: one per run of
    changed rows (runs closer than gap rows merge), so a pointer that jumped redraws two small boxes, not one big."""
    ys = [y for y, (a, b) in enumerate(zip(old, new)) if a != b]
    runs = []
    for y in ys:
        if runs and y - runs[-1][-1] <= gap:
            runs[-1].append(y)
        else:
            runs.append([y])
    boxes, n = [], len(new[0])
    for run in runs:
        x0, x1 = n, 0
        for y in run:  # xor of the rows as big integers: lowest and highest differing byte, no per-byte loop
            d = int.from_bytes(old[y], "big") ^ int.from_bytes(new[y], "big")
            x0, x1 = min(x0, n - 1 - (d.bit_length() - 1) // 8), max(x1, n - ((d & -d).bit_length() - 1) // 8)
        boxes.append((x0, run[0], x1, run[-1] + 1))
    return boxes


def sixel(rows, keys):
    """Rows of palette registers, all the same length -> DEC Sixel image; keys: the palette's 15-bit colours."""
    n = len(rows[0])
    out = [b'\x1bPq"1;1;%d;%d' % (n, len(rows))]
    pct = lambda v: round(v * 100 / 31)
    out += [b"#%d;2;%d;%d;%d" % (i, pct(keys[i] >> 10), pct(keys[i] >> 5 & 31), pct(keys[i] & 31))
            for i in sorted(set(b"".join(rows)))]  # only the registers this image uses
    offset = int.from_bytes(b"?" * n, "big")  # sixel characters start at '?' (63)
    for y in range(0, len(rows), 6):
        band, layers = rows[y:y + 6], []
        for c in set(b"".join(band)):  # colours present in this band
            bits = sum(int.from_bytes(r.translate(_IS[c]), "big") << k for k, r in enumerate(band)) + offset
            layers.append(b"#%d" % c + _RLE.sub(lambda m: b"!%d%c" % (len(m[0]), m[1][0]), bits.to_bytes(n, "big")))
        out.append(b"$".join(layers) + b"-")
    return b"".join(out) + b"\x1b\\"


def raw_terminal():
    """Raw keyboard and mouse input; returns the function that restores the terminal."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    return lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)


def show(name):
    """Native window: TigerVNC's viewer opens the desktop's unix socket directly."""
    viewer = shutil.which("vncviewer") or shutil.which("xtigervncviewer")
    if not viewer:
        raise SystemExit("no VNC viewer: install TigerVNC's (Debian: sudo apt install tigervnc-viewer)")
    desktop(name).close()  # running, with a desktop
    subprocess.Popen([viewer, os.path.join(STORE, name, "run", "vnc.sock")], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print(json.dumps({"shown": name}))


def view(name, fps=30, read_only=False):
    """Interactive terminal viewer (adapted from terminal-vm): Kitty graphics or Sixel, keyboard and mouse.
    Ctrl+] exits, Ctrl+O opens the native window."""
    proto = graphics()
    if not proto:
        print("This terminal shows no inline images (Kitty or Sixel); opening the native window instead.", file=sys.stderr)
        return show(name)
    r, out = desktop(name), sys.stdout.buffer  # its own connection: agents keep working while someone watches
    restore = raw_terminal()
    keys = queue.Queue()

    def reader():
        while True:
            chunk = os.read(sys.stdin.fileno(), 65536)
            keys.put(chunk)
            if not chunk:
                return
    threading.Thread(target=reader, daemon=True).start()
    # alt screen, hide cursor, all mouse motion as SGR (in pixels where the terminal can), ask the cell size in pixels
    out.write(b"\x1b[?1049h\x1b[?25l\x1b[?1003h\x1b[?1006h" + (b"\x1b[?1016h" if proto == "kitty" else b"") + b"\x1b[16t")
    last, geom, cell, shown, prev_rgb = None, None, (20, 10), None, b""
    stale, mapped0 = False, 0
    active = time.monotonic()  # last screen change or input: capture at fps for 2 s after it, at 5 Hz when idle
    try:
        while True:
            try:
                r.refresh()
            except (OSError, RuntimeError):  # desktop restarted or stopped: reconnect while it runs
                r.close()
                try:
                    r = desktop(name)
                except (RuntimeError, SystemExit):
                    return
                continue
            w, h, rgb = r.w, r.h, r.rgb()
            data = png(rgb, w, h) if proto == "kitty" else rgb
            g = term_geometry(cell)
            if data != last or g != geom:
                active = time.monotonic()
                cols, rows, cw, ch = g
                if g != geom:  # a fresh alternate screen: ED 2 leaves Sixel images behind in Windows Terminal
                    out.write(b"\x1b[?1049l\x1b[?1049h\x1b[2J")
                    shown = None
                last, geom = data, g
                if proto == "kitty":
                    cu, ru = fit(w, h, cols, rows, cw, ch)
                    dw, dh = cu * cw, ru * ch
                    ox, oy = (cols - cu) // 2, (rows - 1 - ru) // 2
                    b64 = base64.standard_b64encode(data)
                    out.write(b"\x1b[%d;%dH" % (oy + 1, ox + 1))
                    for i in range(0, len(b64), 4096):
                        head = b"a=T,f=100,i=1,c=%d,r=%d,C=1,q=2," % (cu, ru) if i == 0 else b""
                        out.write(b"\x1b_G%sm=%d;%s\x1b\\" % (head, i + 4096 < len(b64), b64[i:i + 4096]))
                else:  # Sixel has no scaling: scale here to fill the window above the status line
                    # shortcut: at most 2x up, the cost grows with the output; raise it for very large windows
                    scale = min(cols * cw / w, (rows - 1) * ch / h, 2)
                    dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
                    ox, oy = int((cols * cw - dw) / 2 // cw), int(((rows - 1) * ch - dh) / 2 // ch)
                    same = shown is not None and len(prev_rgb) == len(data)
                    if not same:  # palette built at start and on resize only
                        palette = make_palette(data, w, h)
                    frame = sixel_rows(data, w, h, dw, dh, palette, prev_rgb, shown) if same else sixel_rows(data, w, h, dw, dh, palette)
                    prev_rgb = data
                    mapped = len(palette[1]) - palette[1].count(255)
                    if not same:
                        mapped0 = mapped
                    stale = mapped - mapped0 > 400
                    boxes = [(0, 0, dw, dh)] if shown is None or cw % 1 or ch % 1 else changed(shown, frame, int(ch))
                    for box in boxes:  # redraw only the cells that changed
                        cw_, ch_ = int(cw), int(ch)
                        x0, y0 = box[0] // cw_ * cw_, box[1] // ch_ * ch_
                        y1 = min(dh, y0 + -(-(box[3] - y0) // 6) * 6)
                        out.write(b"\x1b[%d;%dH" % (oy + y0 // ch_ + 1, ox + x0 // cw_ + 1))
                        out.write(sixel([row[x0:box[2]] for row in frame[y0:y1]], palette[0]))
                    shown = frame
                out.write(f"\x1b[{rows};1H\x1b[2K\x1b[2m {name} · {w}x{h} · Ctrl+] exit · Ctrl+O window\x1b[0m".encode())
                out.flush()
            try:
                chunk = keys.get(timeout=1 / fps if time.monotonic() - active < 2 else 0.2)
                active = time.monotonic()
                while not keys.empty() and chunk:
                    chunk += keys.get_nowait()
            except queue.Empty:
                if stale and time.monotonic() - active > 2:
                    last = shown = None
                    stale = False
                continue
            if not chunk:
                return
            events = parse_input(chunk.decode("utf-8", "replace"))
            motion = lambda e: e[0] == "mouse" and e[1] & 32 and e[1] & 3 == 3
            for i, e in enumerate(events):
                if e[0] == "quit":
                    return
                if e[0] == "cell":
                    cell = e[1:]
                elif e[0] == "show":
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            show(name)
                    except SystemExit as err:
                        out.write(f"\x1b[{rows};1H\x1b[2K\x1b[33m {err}\x1b[0m".encode()); out.flush()
                elif read_only:
                    continue
                elif e[0] == "text":
                    for c in e[1]:
                        if c.isprintable():
                            k = char_keysym(c); r.key(k, True); r.key(k, False)
                elif e[0] == "key":
                    try:
                        ks = chord(e[1])
                    except ValueError:
                        continue
                    for k in ks:
                        r.key(k, True)
                    for k in reversed(ks):
                        r.key(k, False)
                elif e[0] == "mouse":
                    if motion(e) and i + 1 < len(events) and motion(events[i + 1]):
                        continue  # only the latest of a run of moves
                    code, px, py, pressed = e[1:]
                    if proto == "sixel":  # 1-based cells
                        px, py = (px - 0.5) * cw, (py - 0.5) * ch
                    x, y = (px - ox * cw) * w / dw, (py - oy * ch) * h / dh
                    if not (0 <= x < w and 0 <= y < h):
                        continue
                    if code & 64:  # wheel: buttons 4 (up) and 5 (down)
                        b = 8 if code & 1 == 0 else 16
                        r.pointer(x, y, r.mask | b); r.pointer(x, y, r.mask & ~b)
                    elif code & 32 or code & 3 == 3:
                        r.pointer(x, y)  # motion keeps the buttons held
                    else:
                        b = (1, 2, 4)[code & 3]
                        r.pointer(x, y, (r.mask | b) if pressed else (r.mask & ~b))
    finally:
        try:
            r.pointer(getattr(r, "x", 0), getattr(r, "y", 0), 0)  # release held buttons
        except OSError:
            pass
        r.close()
        out.write((b"\x1b_Ga=d,i=1\x1b\\" if proto == "kitty" else b"") + b"\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?25h\x1b[?1049l")
        out.flush()
        restore()


# ---------- MCP (stdio JSON-RPC) ----------

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_N = {"type": "number"}
_SHOT = {"screenshot": {"type": "boolean", "description": "Return a screenshot once the screen has changed (up to 1.5 s)"}}
TOOLS = [
    ("screenshot", "Capture the container's desktop. Coordinates in other tools are pixels of this image.", _schema({})),
    ("click", "Click at a point.", _schema({"x": _N, "y": _N, "clicks": {"type": "integer", "enum": [1, 2, 3]},
                                             "button": {"type": "string", "enum": ["left", "right", "middle"]}, **_SHOT}, ["x", "y"])),
    ("move", "Move the pointer.", _schema({"x": _N, "y": _N, **_SHOT}, ["x", "y"])),
    ("drag", "Drag with the left button.", _schema({"x": _N, "y": _N, "x2": _N, "y2": _N, **_SHOT}, ["x", "y", "x2", "y2"])),
    ("scroll", "Scroll; negative dy scrolls down (100 per notch).", _schema({"dy": _N, "x": _N, "y": _N, **_SHOT}, ["dy"])),
    ("type", "Type literal text (any Unicode: no keyboard layout involved).", _schema({"text": {"type": "string"}, **_SHOT}, ["text"])),
    ("key", "Press a key chord, e.g. ctrl+a, alt+tab, return, super.", _schema({"key": {"type": "string"}, **_SHOT}, ["key"])),
    ("status", "Container state and screen size.", _schema({})),
    ("wait", "Wait until the screen stops changing, then return a screenshot. stable: seconds without change "
             "(default 1); timeout: default 30. A blinking cursor can keep it from settling.", _schema({"stable": _N, "timeout": _N})),
    ("exec", "Run a shell command in the container (sh -c) and return its exit code, stdout and stderr.",
     _schema({"command": {"type": "string"}, "timeout": _N}, ["command"])),
]
INPUT_TOOLS = {"click", "move", "drag", "scroll", "type", "key"}
_CT = {"container": {"type": "string", "description": "Another container (default: this server's own)"}}
SCREEN_TOOLS = [(n, d, dict(s, properties={**s["properties"], **_CT})) for n, d, s in TOOLS]
_NAME, _NET = {"name": {"type": "string"}}, {"net": {"type": "string"}}
_MANAGED = (" The agent runs sandboxes and the containers it created; any other container of the user's needs "
            "their approval.")
AGENT_TOOLS = [
    ("ct_list", "terminal-ct's containers; managed=true marks the ones this agent runs without asking." + _MANAGED,
     _schema({})),
    ("ct_create", "Create a container, sandboxed by default (not root, no capabilities, process limit, /data in "
                  "memory, a fresh file system at each start, offline). image: default debian:stable-slim; desktop: "
                  "add a screen (Debian/Ubuntu, Fedora, Alpine, Arch bases). Needs the user's approval: an image not "
                  "on this host yet (download or build), internet, a network of the user's.",
     _schema({**_NAME, "image": {"type": "string"}, "desktop": {"type": "boolean"}, "width": {"type": "integer"},
              "height": {"type": "integer"}, "cpus": _N, "memory_gb": _N, "sandbox": {"type": "boolean"},
              "internet": {"type": "boolean"}, "networks": {"type": "array", "items": {"type": "string"}}}, ["name"])),
    ("ct_start", "Start a container, within the host quota (a sandbox starts fresh from its image)." + _MANAGED,
     _schema(_NAME, ["name"])),
    ("ct_stop", "Stop a container.", _schema({**_NAME, "timeout": {"type": "integer"}}, ["name"])),
    ("ct_remove", "Delete a container and its /data volume (one the agent did not create needs approval).",
     _schema(_NAME, ["name"])),
    ("ct_resources", "Show, or change at once (even running), a container's CPUs and memory within the quota.",
     _schema({**_NAME, "cpus": _N, "memory_gb": _N}, ["name"])),
    ("ct_internet", "Show (without on) or change a stopped container's internet access; on needs the user's approval.",
     _schema({**_NAME, "on": {"type": "boolean"}}, ["name"])),
    ("net_list", "terminal-ct's isolated networks (no LAN, no internet) and their containers, which reach each "
                 "other by name.", _schema({})),
    ("net_create", "Create an isolated container-to-container network.", _schema(_NET, ["net"])),
    ("net_remove", "Remove an empty network this agent created.", _schema(_NET, ["net"])),
    ("net_attach", "Plug a stopped container into a network (a network the user created needs their approval).",
     _schema({**_NAME, **_NET}, ["name", "net"])),
    ("net_detach", "Unplug a stopped container from a network.", _schema({**_NAME, **_NET}, ["name", "net"])),
    ("top", "Real CPU, memory and process use of each running container, next to its ceiling, and the quota.",
     _schema({})),
    ("quota", "The host quota (CPUs, memory, disk, running containers) and its use; only the user changes it.",
     _schema({})),
    ("autostart", "Show (without on) or change whether containers that were running start again by themselves "
                  "after the PC restarts (a task at the user's logon). Changing it needs the user's approval.",
     _schema({"on": {"type": "boolean"}})),
    ("project_status", "Start here: the project's containers and networks as they are now (want: what each should "
                       "be doing), the notes left by earlier sessions, and the latest actions with their errors.",
     _schema({"project": {"type": "string", "description": "Only when this server has no project of its own"}})),
    ("notes_write", "Keep notes for the next session (what is set up, what is left to do): appended with the date, "
                    "or mode=replace to rewrite them.",
     _schema({"text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]},
              "project": {"type": "string"}}, ["text"])),
]
INSTRUCTIONS = ("terminal-ct: containers with a desktop and a shell. Call project_status first: it says what this "
                "project already has and what earlier sessions did and noted. Before you stop, record with "
                "notes_write what you set up and what is left, so the next session can pick up from there.")
AGENT_NAMES = {n for n, _, _ in AGENT_TOOLS}
PROJECT_ARG_TOOLS = {"ct_create", "net_create", "project_status", "notes_write"}  # take `project` without one of their own
AGENT_LOG = "agent.log"


def _image(data):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}


def quiet(f, *a, **k):
    """Call a CLI function that prints its JSON result (stdout is the MCP channel) and return that result."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = f(*a, **k)
    if r is not None:
        return r
    out = buf.getvalue().strip()
    return json.loads(out.splitlines()[-1]) if out else {}


# ---------- agent authority ----------
# Through MCP, agents run sandboxes and the containers they created (labelled created_by=agent at creation; only
# recreating changes a label). Anything else, and risky steps, needs the user's approval: approve(message, cli) asks
# through MCP elicitation, or raises naming the CLI command the user can run instead.

def agent_owned(info):
    return labels_of(info).get(f"{LABEL}.created_by") == "agent"


def managed(info):
    """The agent's own, or a user's sandbox the user left offline (one they put online would be a way out)."""
    return agent_owned(info) or labels_of(info).get(f"{LABEL}.sandbox") == "1" and not online(info)


def in_scope(info, project, name=None):
    """An MCP server with a project reaches nothing outside it, approval or not."""
    p = labels_of(info).get(f"{LABEL}.project") or DEFAULT_PROJECT
    if not under(p, project):
        raise SystemExit(f"{name or labels_of(info).get(LABEL + '.name')} is not in this agent's project {project!r}")
    return info


def guard(name, approve, what, cli, destructive=False, project=None):
    info = in_scope(find(name), project, name)
    if not managed(info) or destructive and not agent_owned(info):
        approve(f"Let the agent {what} your container {name}?", cli)
    return info


def local_image(ref):
    return any(node().run(["image", "inspect", r], check=False).returncode == 0 for r in (ref, qualified(ref)))


def agent_call(tool, a, approve, project=None):
    """project: the server's own (--project); then nothing outside it is visible or reachable."""
    name, net = a.get("name"), a.get("net")
    mine = lambda p: under(p, project)
    nets = {k: v for k, v in networks().items() if mine(v["project"])} if tool.startswith("net_") or tool == "ct_create" else {}
    guard_ = lambda what, cli, destructive=False: guard(name, approve, what, cli, destructive, project)
    # the project a creation or a note goes to: the one named if it is this server's or below it, else the server's
    asked = a.get("project")
    if asked is not None and not under(check_project(asked), project):
        raise SystemExit(f"project {asked!r} is not this agent's project {project!r} or below it")
    where = asked or project
    if name and project and tool != "ct_create":  # reads too: another project's containers are not this agent's business
        in_scope(find(name), project, name)
    if tool == "ct_list":
        return [dict(normalize(c), managed=managed(c)) for c in raw_containers()
                if mine(labels_of(c).get(f"{LABEL}.project") or DEFAULT_PROJECT)]
    if tool == "top":
        t = top()
        keep = {c["name"] for c in containers() if mine(c["project"])}
        return dict(t, containers=[r for r in t["containers"] if r["name"] in keep])
    if tool == "quota":
        q = quota()
        if not project:
            return q
        # an agent with a project sees the room left and its own use, not what the other projects take
        use = {"cpus": 0, "memory_gb": 0, "containers": 0}
        for c in raw_containers():
            if running(c) and under(labels_of(c).get(f"{LABEL}.project") or DEFAULT_PROJECT, project):
                cpus, mem = allotted(c)
                use["cpus"] += cpus; use["memory_gb"] += mem; use["containers"] += 1
        return {k: {"limit": v["limit"], "free": v["free"], **({"used_by_project": round(use[k], 2)} if k in use else {})}
                for k, v in q.items()}
    if tool == "autostart":
        if a.get("on") is not None:
            approve(("Start the containers that were running again by themselves after each restart of the PC "
                     "(a task at your logon)?") if a["on"] else "Stop restarting containers after a restart of the PC?",
                    f"terminal-ct autostart {'on' if a['on'] else 'off'}")
        return autostart(a.get("on"))
    if tool == "project_status":
        return project_status(where)
    if tool == "notes_write":
        return notes_write(where, a["text"], a.get("mode", "append"))
    if tool == "net_list":
        return nets
    if tool == "net_create":
        if check_name(net) in networks():
            raise SystemExit(f"the network name {net!r} is not available: choose another")
        return net_create(net, "agent", where)
    if tool in ("net_remove", "net_attach") and check_name(net) not in nets and net in networks():
        raise SystemExit(f"network {net!r} is not in this agent's project {project!r}")
    if tool == "net_remove":
        if nets.get(net, {}).get("created_by") != "agent":
            raise SystemExit(f"network {net!r} is not one this agent created")
        return net_rm(net)
    if tool == "ct_create":
        if node().run(["inspect", "--type", "container", PREFIX + check_name(name)], check=False).returncode == 0:
            raise SystemExit(f"the name {name!r} is not available: choose another")  # same answer, any project
        img, desk = a.get("image") or DEFAULT_IMAGE, bool(a.get("desktop"))
        if not (desk and local_image(desktop_tag(img))):
            if not local_image(img):
                approve(f"Download the image {img} for {name}?", f"terminal-ct image pull {img}")
            if desk:
                approve(f"Build the desktop image for {img} (packages from the internet, a few minutes)?",
                        f"terminal-ct create {name} --desktop --image {img}")
        if a.get("internet"):
            approve(f"Give the new container {name} internet access?", f"terminal-ct internet {name} on")
        for n in a.get("networks") or []:
            if project and n not in nets and n in networks():
                raise SystemExit(f"network {n!r} is not in this agent's project {project!r}")
            if nets.get(n, {}).get("created_by") != "agent":
                approve(f"Plug {name} into your network {n!r} (it would reach your containers on it)?",
                        f"terminal-ct net attach {name} {n}")
        return quiet(create, name, img, a.get("cpus"), a.get("memory_gb"), created_by="agent", desktop=desk,
                     width=int(a.get("width", 1280)), height=int(a.get("height", 800)),
                     sandbox=a.get("sandbox", True), internet=bool(a.get("internet")), nets=a.get("networks") or (),
                     project=where)
    if tool == "ct_start":
        guard_("start", f"terminal-ct start {name}")
        return quiet(start, name)
    if tool == "ct_stop":
        guard_("stop", f"terminal-ct stop {name}")
        return quiet(stop, name, int(a.get("timeout", 10)))
    if tool == "ct_remove":
        guard_("delete (with its /data)", f"terminal-ct rm {name}", destructive=True)
        return quiet(rm, name)
    if tool == "ct_resources":
        if a.get("cpus") is not None or a.get("memory_gb") is not None:
            guard_("resize", f"terminal-ct resources {name} --cpus/--memory")
        return resources(name, a.get("cpus"), a.get("memory_gb"))
    if tool == "ct_internet":
        if a.get("on") is not None:
            guard_("change the network of", f"terminal-ct internet {name} {'on' if a['on'] else 'off'}")
            if a["on"]:
                approve(f"Give {name} internet access?", f"terminal-ct internet {name} on")
        return internet(name, a.get("on"))
    if tool in ("net_attach", "net_detach"):
        guard_("change the networks of", f"terminal-ct net {tool[4:]} {name} {net}")
        if tool == "net_attach" and nets.get(net, {}).get("created_by") != "agent":
            approve(f"Plug {name} into your network {net!r} (it would reach your containers on it)?",
                    f"terminal-ct net attach {name} {net}")
        return net_attach(name, net, tool == "net_attach")
    raise SystemExit(f"unknown tool {tool}")


def mcp(name=None, project=None):
    """Screen tools and exec on this server's container (NAME, given by the user) or, with `container`, on another;
    plus managing containers and networks under the agent-authority rules. project: the agent sees and reaches
    only that project's containers and networks, and its notes and log are that project's."""
    if project:
        check_project(project)
    if name:
        check_name(name)
        if project:
            in_scope(find(name), project, name)
    allowed = {name}  # containers this session may drive: the server's own, and those the user approved
    can_ask, pending, asks = False, collections.deque(), iter(range(1, 1 << 30))

    channel = sys.stdout  # replies only: tool code runs with stdout redirected

    def send(msg):
        channel.write(json.dumps(msg) + "\n"); channel.flush()

    def record(tool, a, target, error=None):  # every infrastructure action, and a sandbox's screen actions
        try:
            lab = {}
            with contextlib.suppress(SystemExit):
                lab = labels_of(find(target)) if target else {}
            # the log of the project the action belongs to: this server's, else the one a creation names, else the
            # container's own; never one an agent merely names (it would forge another project's history)
            p = project or (a.get("project") if tool in PROJECT_ARG_TOOLS else None) or lab.get(f"{LABEL}.project")
            p = p if isinstance(p, str) and _PROJECT.fullmatch(p) and under(p, project) else project or DEFAULT_PROJECT
            mine = under(lab.get(f"{LABEL}.project") or DEFAULT_PROJECT, project)
            path = os.path.join(project_dir(p), AGENT_LOG) if tool in AGENT_NAMES else \
                os.path.join(ct_dir(target), "actions.log") if lab.get(f"{LABEL}.sandbox") == "1" and mine else None
            if path:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "arguments": a,
                                        **({"container": target} if target else {}), **({"error": error} if error else {})}) + "\n")
        except (OSError, TypeError, ValueError, SystemExit):  # a log that cannot be written must not end the server
            pass

    def approve(message, cli):
        """Ask the user through the client (MCP elicitation); raise unless they accept."""
        if not can_ask:
            raise RuntimeError(f"This needs the user's approval and this client cannot ask them. Ask the user to run: {cli}")
        aid = f"ask-{next(asks)}"
        send({"jsonrpc": "2.0", "id": aid, "method": "elicitation/create", "params": {
            "message": message, "requestedSchema": {"type": "object", "properties": {
                "approve": {"type": "boolean", "title": "Approve", "description": message}}, "required": ["approve"]}}})
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("the client closed the connection before the user answered")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != aid or "method" in msg:
                pending.append(line)  # other messages wait until the user has answered
                continue
            r = msg.get("result") or {}
            if r.get("action") == "accept" and (r.get("content") or {}).get("approve"):
                return
            raise RuntimeError(f"The user did not approve. They can still run: {cli}")

    while True:
        line = pending.popleft() if pending else sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method, rid = req.get("method"), req.get("id")
        if rid is None or method is None:  # notifications, stray responses
            continue
        if method == "initialize":
            can_ask = "elicitation" in (req.get("params", {}).get("capabilities") or {})
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "terminal-ct", "version": VERSION},
                "instructions": INSTRUCTIONS + (f" This server's project: {project}." if project else "")}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, d, s in SCREEN_TOOLS + AGENT_TOOLS]}})
        elif method == "tools/call":
            p = req.get("params", {})
            tool, a = p.get("name"), dict(p.get("arguments") or {})
            target = a.get("name") if tool in AGENT_NAMES else a.pop("container", None) or name
            try:
                if tool in AGENT_NAMES:
                    with contextlib.redirect_stdout(io.StringIO()):  # engine output must not reach the MCP channel
                        content = [{"type": "text", "text": json.dumps(agent_call(tool, a, approve, project))}]
                else:
                    if not target:
                        raise ValueError("name the container: this server was started without one (container)")
                    if project:  # every call: a container moved to another project leaves this agent's reach
                        in_scope(find(target), project, target)
                    if target not in allowed:
                        guard(target, approve, "use the screen and shell of", f"terminal-ct mcp {target}",
                              project=project)
                        allowed.add(target)
                    if tool == "exec":
                        out = quiet(exec_, target, ["sh", "-c", a["command"]], float(a.get("timeout", 60)))
                        content = [{"type": "text", "text": json.dumps(out)}]
                    else:
                        before = _pixels(target) if a.get("screenshot") and tool in INPUT_TOOLS else None
                        out = act(target, tool, a)
                        if tool == "screenshot":
                            content = [_image(out[0])]
                        else:
                            content = [{"type": "text", "text": json.dumps(out)}]
                            if a.get("screenshot") or tool == "wait":
                                content.append(_image(settled_screenshot(target, before)))
                record(tool, a, target)
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": content}})
            except (Exception, SystemExit) as e:  # report failures to the agent instead of dying
                record(tool, a, target, str(e))
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


def doctor():
    checks = []
    for e in ("podman", "docker"):
        p = shutil.which(e)
        ver = subprocess.run([e, "--version"], capture_output=True, text=True).stdout.strip() if p else ""
        checks.append({"check": e, "ok": bool(p), "detail": ver or "not installed"})
    try:
        n = node()
        n.run(["ps", "-q"])
        checks.append({"check": "engine", "ok": True, "detail": f"{n.engine} answers"})
    except SystemExit as e:
        checks.append({"check": "engine", "ok": False, "detail": str(e)})
    print(json.dumps(checks, indent=1))
    if not checks[-1]["ok"]:
        sys.exit(1)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd_args = []
    if "--" in argv:  # create NAME ... -- CMD: everything after -- is the container's command
        i = argv.index("--")
        argv, cmd_args = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="terminal-ct", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", choices=["docker", "podman"])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("name"); c.add_argument("--image", default=DEFAULT_IMAGE)
    c.add_argument("--cpus", type=float); c.add_argument("--memory", type=float, help="GiB")
    c.add_argument("--desktop", action="store_true", help="Xvnc + Openbox on top of the image")
    c.add_argument("--width", type=int, default=1280); c.add_argument("--height", type=int, default=800)
    c.add_argument("--sandbox", action="store_true"); c.add_argument("--offline", action="store_true")
    c.add_argument("--net", action="append", default=[], help="an isolated network (repeatable)")
    c.add_argument("--project")
    rs = sub.add_parser("resume"); rs.add_argument("--stay", action="store_true")
    sub.add_parser("autostart").add_argument("state", nargs="?", choices=["on", "off"])
    pj = sub.add_parser("project"); pj.add_argument("project_cmd", choices=["ls", "show", "notes", "move"])
    pj.add_argument("args", nargs="*")
    r = sub.add_parser("resources"); r.add_argument("name")
    r.add_argument("--cpus", type=float); r.add_argument("--memory", type=float, help="GiB")
    q = sub.add_parser("quota"); q.add_argument("--cpus", type=int); q.add_argument("--memory", type=int, help="GiB")
    q.add_argument("--disk", type=int, help="GiB"); q.add_argument("--containers", type=int)
    nt = sub.add_parser("net"); nt.add_argument("net_cmd", choices=["create", "rm", "ls", "attach", "detach"])
    nt.add_argument("args", nargs="*"); nt.add_argument("--project")
    for n in ("internet", "sandbox"):
        t = sub.add_parser(n); t.add_argument("name"); t.add_argument("state", nargs="?" if n == "internet" else None,
                                                                       choices=["on", "off"])
    a = sub.add_parser("action"); a.add_argument("name")
    a.add_argument("tool", choices=["screenshot", "click", "move", "drag", "scroll", "type", "key", "wait", "status"])
    a.add_argument("args", nargs="*"); a.add_argument("--output"); a.add_argument("--button", default="left")
    a.add_argument("--double", action="store_true"); a.add_argument("--dy", type=int)
    a.add_argument("--x", type=float); a.add_argument("--y", type=float)
    a.add_argument("--timeout", type=float, default=30); a.add_argument("--stable", type=float, default=1)
    m = sub.add_parser("mcp"); m.add_argument("name", nargs="?"); m.add_argument("--project")
    for n in ("view", "show"):
        v = sub.add_parser(n); v.add_argument("name"); v.add_argument("--read-only", action="store_true")
    for n in ("start", "rm", "status"):
        sub.add_parser(n).add_argument("name")
    s = sub.add_parser("stop"); s.add_argument("name"); s.add_argument("--timeout", type=int, default=10)
    x = sub.add_parser("exec"); x.add_argument("name"); x.add_argument("--timeout", type=float, default=60)
    x.add_argument("command", nargs=argparse.REMAINDER)
    im = sub.add_parser("image"); im.add_argument("image_cmd", choices=["ls", "pull", "build", "rm"])
    im.add_argument("ref", nargs="?"); im.add_argument("path", nargs="?")
    for n in ("ls", "top", "doctor", "help", "version"):
        sub.add_parser(n)
    o = p.parse_args(argv)
    global NODE
    if o.engine:
        NODE = Node(pick_engine(o.engine))
    if o.cmd == "help": return p.print_help()
    if o.cmd == "version": return print(VERSION)
    if o.cmd == "doctor": return doctor()
    if o.cmd == "ls": return ls()
    if o.cmd == "create":
        return create(o.name, o.image, o.cpus, o.memory, cmd_args or None, desktop=o.desktop, width=o.width,
                      height=o.height, sandbox=o.sandbox, internet=False if o.offline else None, nets=o.net,
                      project=o.project)
    show_json = lambda r: print(json.dumps(r, indent=1))
    if o.cmd == "resume": return resume(o.stay)
    if o.cmd == "autostart": return show_json(autostart(None if o.state is None else o.state == "on"))
    if o.cmd == "project":
        want_n = {"ls": (0, 0), "show": (0, 1), "notes": (0, 2), "move": (2, 2)}[o.project_cmd]
        if not want_n[0] <= len(o.args) <= want_n[1]:
            raise SystemExit({"ls": "project ls", "show": "project show [PROJECT]", "notes": "project notes [PROJECT [TEXT]]",
                              "move": "project move NAME PROJECT"}[o.project_cmd])
        if o.project_cmd == "ls": return show_json(projects())
        if o.project_cmd == "show": return show_json(project_status(*o.args))
        if o.project_cmd == "move": return show_json(project_move(*o.args))
        if len(o.args) == 2: return show_json(notes_write(o.args[0], o.args[1]))
        return print(project_status(*o.args)["notes"], end="")
    if o.cmd == "resources": return show_json(resources(o.name, o.cpus, o.memory))
    if o.cmd == "quota":
        return show_json(quota({"cpus": o.cpus, "memory_gb": o.memory, "disk_gb": o.disk, "containers": o.containers}))
    if o.cmd == "top": return show_json(top())
    if o.cmd == "internet": return show_json(internet(o.name, None if o.state is None else o.state == "on"))
    if o.cmd == "sandbox": return show_json(sandbox(o.name, o.state == "on"))
    if o.cmd == "net":
        want = {"create": 1, "rm": 1, "ls": 0, "attach": 2, "detach": 2}[o.net_cmd]
        if len(o.args) != want:
            raise SystemExit({0: "net ls", 1: f"net {o.net_cmd} NET", 2: f"net {o.net_cmd} NAME NET"}[want])
        if o.net_cmd == "ls": return show_json(networks())
        if o.net_cmd == "create": return show_json(net_create(o.args[0], project=o.project))
        if o.net_cmd == "rm": return show_json(net_rm(o.args[0]))
        return show_json(net_attach(o.args[0], o.args[1], o.net_cmd == "attach"))
    if o.cmd == "mcp": return mcp(o.name, o.project)
    if o.cmd == "show": return show(o.name)
    if o.cmd == "view":
        if WINDOWS:
            raise SystemExit("view runs on a Linux host or in WSL (Windows Terminal shows it there through Sixel)")
        return view(o.name, read_only=o.read_only)
    if o.cmd == "action":
        try:
            nums = [float(v) for v in o.args] if o.tool in ("click", "move", "drag") else []
            args = {"screenshot": {}, "status": {}, "wait": {"timeout": o.timeout, "stable": o.stable},
                    "click": lambda: {"x": nums[0], "y": nums[1], "button": o.button, "clicks": 2 if o.double else 1},
                    "move": lambda: {"x": nums[0], "y": nums[1]},
                    "drag": lambda: dict(zip(("x", "y", "x2", "y2"), nums)),
                    "scroll": lambda: {k: v for k, v in (("dy", o.dy), ("x", o.x), ("y", o.y)) if v is not None},
                    "type": lambda: {"text": " ".join(o.args)}, "key": lambda: {"key": o.args[0]}}[o.tool]
            out = act(o.name, o.tool, args() if callable(args) else args)
            if o.tool == "screenshot":
                data, (w, h) = out
                dest = o.output or os.path.join(ct_dir(o.name), "screen.png")
                with open(dest, "wb") as f:
                    f.write(data)
                out = {"path": os.path.abspath(dest), "width": w, "height": h}
            print(json.dumps(out))
        except (RuntimeError, ValueError, KeyError, IndexError, OSError) as e:
            print(json.dumps({"error": str(e) or type(e).__name__}), file=sys.stderr); sys.exit(1)
        return
    if o.cmd == "start": return start(o.name)
    if o.cmd == "stop": return stop(o.name, o.timeout)
    if o.cmd == "rm": return rm(o.name)
    if o.cmd == "status": return status(o.name)
    if o.cmd == "exec":
        out = exec_(o.name, o.command or cmd_args, o.timeout)
        sys.exit(out["exit"])
    if o.cmd == "image":
        if o.image_cmd in ("pull", "rm") and not o.ref or o.image_cmd == "build" and not (o.ref and o.path):
            raise SystemExit(f"image {o.image_cmd} needs " + ("NAME DIR" if o.image_cmd == "build" else "an image"))
        return image(o.image_cmd, o.ref, o.path)


if __name__ == "__main__":
    main()

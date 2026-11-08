#!/usr/bin/env python3
"""terminal-ct: containers with a desktop and a shell for people and computer-use agents.

The container counterpart of terminal-vm: same command and MCP shape, Docker or Podman underneath
(whichever is installed), standard library only. Every container it makes carries `terminal-ct.*`
labels; containers without them are never touched.

  create NAME [--image IMAGE] [--desktop [--width 1280 --height 800]] [--cpus N] [--memory GiB] [-- CMD...]
              default image debian:stable-slim; --desktop adds Xvnc + Openbox to any Debian/Ubuntu, Fedora,
              Alpine or Arch image (built once, cached as tct-desktop:<base>)
  ls | status NAME | doctor | help | version
  start NAME | stop NAME [--timeout 10] | rm NAME     rm also deletes the container's /data volume
  exec NAME [--timeout 60] CMD...                     run a command, JSON reply (exit code, stdout, stderr)
  action NAME screenshot|click|move|drag|scroll|type|key|wait|status ...   desktop containers
  mcp NAME                                            stdio MCP server (screen tools + exec)
  image ls | image pull REF | image build NAME DIR | image rm NAME
Global: --engine docker|podman (default: TERMINAL_CT_ENGINE, else podman if installed, else docker).
"""
import argparse, base64, contextlib, io, json, os, re, shutil, socket, struct, subprocess, sys, time, zlib

VERSION = "0.2.0"
WINDOWS = os.name == "nt"
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


class Node:
    """Where containers run. v1: the local host; later a VM node (through its guest agent) or a remote machine,
    so every engine call goes through a node."""
    def __init__(self, engine=None):
        self.engine = engine or pick_engine()

    def run(self, args, check=True, timeout=None, capture=True):
        r = subprocess.run([self.engine, *args], capture_output=capture, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
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
    return {"name": labels.get(f"{LABEL}.name") or c.get("Name", "").lstrip("/").removeprefix(PREFIX),
            "status": st.get("Status", "unknown"), "image": (c.get("Config") or {}).get("Image", ""),
            "created_by": labels.get(f"{LABEL}.created_by", "user"), "id": c.get("Id", "")[:12],
            **({"exit_code": st.get("ExitCode")} if st.get("Status") == "exited" else {})}


def containers():
    ids = node().run(["ps", "-a", "-q", "--filter", f"label={LABEL}.name"]).stdout.split()
    return [normalize(c) for c in node().json(["inspect", "--type", "container", *ids])] if ids else []


def find(name):
    check_name(name)
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


def create(name, image=DEFAULT_IMAGE, cpus=None, memory=None, cmd=None, created_by="user",
           desktop=False, width=1280, height=800):
    check_name(name)
    if node().run(["inspect", "--type", "container", PREFIX + name], check=False).returncode == 0:
        raise SystemExit(f"{name!r} already exists (engine name {PREFIX}{name})")
    volume, extra = f"{PREFIX}{name}-data", []  # /data is kept when the container is recreated, removed by rm
    if desktop:
        if WINDOWS:  # shortcut: the VNC unix socket does not cross into Windows; run terminal-ct inside WSL there
            raise SystemExit("desktop containers need a Linux host or WSL (the screen is reached through a unix socket)")
        if not (640 <= width <= 7680 and 480 <= height <= 4320):
            raise SystemExit("invalid --width/--height")
        if cmd:
            raise SystemExit("a desktop container runs its desktop: start programs from it, or with exec")
        image = desktop_image(image)
        # the desktop runs as the host user (podman: same uid inside), so the socket is the user's alone; /data is
        # its home
        uid, gid = os.getuid(), os.getgid()
        if node().engine == "podman":
            extra = ["--userns=keep-id", "--user", f"{uid}:{gid}"]
            vol = f"{volume}:/data:U"  # :U hands the volume to that user
        else:
            node().run(["volume", "create", volume])
            node().run(["run", "--rm", "--user", "0", "--entrypoint", "chown", "-v", f"{volume}:/data", image,
                        f"{uid}:{gid}", "/data"])
            extra, vol = ["--user", f"{uid}:{gid}"], f"{volume}:/data"
        # LANG: without UTF-8, xterm wrote é as Latin-1
        extra += ["-e", "HOME=/data", "-e", "LANG=C.UTF-8", "-e", f"TCT_WIDTH={width}", "-e", f"TCT_HEIGHT={height}",
                  "-v", f"{os.path.join(ct_dir(name), 'run')}:/run/tct", "--label", f"{LABEL}.desktop=1"]
        cmd = [DESKTOP_ENTRY]
    else:
        entry, default = image_cmd(image)
        # a bare shell as the main process exits at once without a terminal: keep such containers alive instead
        if not cmd and not entry and (not default or default[0] in SHELLS):
            cmd = KEEP_ALIVE
        vol = f"{volume}:/data"
    args = ["create", "--name", PREFIX + name, "--hostname", name, "--init",
            "--label", f"{LABEL}.name={name}", "--label", f"{LABEL}.created_by={created_by}", "-v", vol, *extra,
            *(["--cpus", str(cpus)] if cpus else []), *(["--memory", f"{memory}g"] if memory else []),
            image, *(cmd or [])]
    cid = node().run(args).stdout.strip()
    print(json.dumps({"created": name, "image": image, "id": cid[:12], **({"desktop": f"{width}x{height}"} if desktop else {})}))


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


def desktop_image(base):
    """The desktop layer on top of BASE, built once and cached as tct-desktop:<base>."""
    tag = "tct-desktop:" + re.sub(r"[^a-z0-9_.-]", "-", base.lower())[-120:]
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
    node().run(["start", PREFIX + name])
    out = {"started": name}
    if ((info.get("Config") or {}).get("Labels") or {}).get(f"{LABEL}.desktop") == "1":
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
    node().run(["stop", "-t", str(timeout), PREFIX + name])
    print(json.dumps({"stopped": name}))


def rm(name):
    find(name)
    node().run(["rm", "-f", PREFIX + name])
    node().run(["volume", "rm", f"{PREFIX}{name}-data"], check=False)  # absent if never created
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


def _image(data):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}


def mcp(name):
    """One container per server (phase 3 adds managing others under the agent-authority rules)."""
    check_name(name)
    send = lambda msg: (sys.stdout.write(json.dumps(msg) + "\n"), sys.stdout.flush())
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method, rid = req.get("method"), req.get("id")
        if rid is None:  # notifications
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "terminal-ct", "version": VERSION}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}})
        elif method == "tools/call":
            p = req.get("params", {})
            tool, a = p.get("name"), dict(p.get("arguments") or {})
            try:
                if tool == "exec":
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        out = exec_(name, ["sh", "-c", a["command"]], float(a.get("timeout", 60)))
                    content = [{"type": "text", "text": json.dumps(out)}]
                else:
                    before = _pixels(name) if a.get("screenshot") and tool in INPUT_TOOLS else None
                    out = act(name, tool, a)
                    if tool == "screenshot":
                        content = [_image(out[0])]
                    else:
                        content = [{"type": "text", "text": json.dumps(out)}]
                        if a.get("screenshot") or tool == "wait":
                            content.append(_image(settled_screenshot(name, before)))
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": content}})
            except (Exception, SystemExit) as e:  # report failures to the agent instead of dying
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
    a = sub.add_parser("action"); a.add_argument("name")
    a.add_argument("tool", choices=["screenshot", "click", "move", "drag", "scroll", "type", "key", "wait", "status"])
    a.add_argument("args", nargs="*"); a.add_argument("--output"); a.add_argument("--button", default="left")
    a.add_argument("--double", action="store_true"); a.add_argument("--dy", type=int)
    a.add_argument("--x", type=float); a.add_argument("--y", type=float)
    a.add_argument("--timeout", type=float, default=30); a.add_argument("--stable", type=float, default=1)
    sub.add_parser("mcp").add_argument("name")
    for n in ("start", "rm", "status"):
        sub.add_parser(n).add_argument("name")
    s = sub.add_parser("stop"); s.add_argument("name"); s.add_argument("--timeout", type=int, default=10)
    x = sub.add_parser("exec"); x.add_argument("name"); x.add_argument("--timeout", type=float, default=60)
    x.add_argument("command", nargs=argparse.REMAINDER)
    im = sub.add_parser("image"); im.add_argument("image_cmd", choices=["ls", "pull", "build", "rm"])
    im.add_argument("ref", nargs="?"); im.add_argument("path", nargs="?")
    for n in ("ls", "doctor", "help", "version"):
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
        return create(o.name, o.image, o.cpus, o.memory, cmd_args or None, desktop=o.desktop, width=o.width, height=o.height)
    if o.cmd == "mcp": return mcp(o.name)
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

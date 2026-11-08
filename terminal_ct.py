#!/usr/bin/env python3
"""terminal-ct: containers with a desktop and a shell for people and computer-use agents.

The container counterpart of terminal-vm: same command and MCP shape, Docker or Podman underneath
(whichever is installed), standard library only. Every container it makes carries `terminal-ct.*`
labels; containers without them are never touched.

  create NAME [--image IMAGE] [--cpus N] [--memory GiB] [-- CMD...]   (default image: debian:stable-slim)
  ls | status NAME | doctor | help | version
  start NAME | stop NAME [--timeout 10] | rm NAME     rm also deletes the container's /data volume
  exec NAME [--timeout 60] CMD...                     run a command, JSON reply (exit code, stdout, stderr)
  image ls | image pull REF | image build NAME DIR | image rm NAME
Global: --engine docker|podman (default: TERMINAL_CT_ENGINE, else podman if installed, else docker).
"""
import argparse, json, os, re, shutil, subprocess, sys

VERSION = "0.1.0"
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
    return [normalize(c) for c in node().json(["inspect", *ids])] if ids else []


def find(name):
    check_name(name)
    r = node().run(["inspect", PREFIX + name], check=False)
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


def create(name, image=DEFAULT_IMAGE, cpus=None, memory=None, cmd=None, created_by="user"):
    check_name(name)
    if node().run(["inspect", PREFIX + name], check=False).returncode == 0:
        raise SystemExit(f"{name!r} already exists (engine name {PREFIX}{name})")
    entry, default = image_cmd(image)
    # a bare shell as the main process exits at once without a terminal: keep such containers alive instead
    if not cmd and not entry and (not default or default[0] in SHELLS):
        cmd = KEEP_ALIVE
    args = ["create", "--name", PREFIX + name, "--hostname", name, "--init",
            "--label", f"{LABEL}.name={name}", "--label", f"{LABEL}.created_by={created_by}",
            "-v", f"{PREFIX}{name}-data:/data",  # kept when the container is recreated, removed by rm
            *(["--cpus", str(cpus)] if cpus else []), *(["--memory", f"{memory}g"] if memory else []),
            image, *(cmd or [])]
    cid = node().run(args).stdout.strip()
    print(json.dumps({"created": name, "image": image, "id": cid[:12]}))


def start(name):
    find(name)
    node().run(["start", PREFIX + name])
    print(json.dumps({"started": name}))


def stop(name, timeout=10):
    find(name)
    node().run(["stop", "-t", str(timeout), PREFIX + name])
    print(json.dumps({"stopped": name}))


def rm(name):
    find(name)
    node().run(["rm", "-f", PREFIX + name])
    node().run(["volume", "rm", f"{PREFIX}{name}-data"], check=False)  # absent if never created
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
    if o.cmd == "create": return create(o.name, o.image, o.cpus, o.memory, cmd_args or None)
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

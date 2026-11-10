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
  apply FILE [--project P] | scale STACK SERVICE N | stack ls|show|rm [STACK] | events [--stack S] [-n 50] [--follow]
  daemon run|start|stop|status | prune      desired state kept by the daemon (restarts, health, ttl); stale images
  dash [--project P] [--json]  |  logs NAME [-n 100]    live dashboard (keys: s start, t stop, r rm, +/- scale, l logs, v view)
  bench [--runs 10] [--desktop]   median/min/max of create, start, exec, stop, rm, ls (containers bench-*)
  skills get|path|install [--dir D]   the agent's SKILL.md (install: into ~/.claude/skills/terminal-ct)
  web [--project P] [--no-open]   web dashboard (containers, terminal-vm's VMs, screens, stacks), served by the
                      daemon on 127.0.0.1 (TERMINAL_CT_WEB_PORT, default 7171) behind a token; opens the browser
  snapshot NAME save|load|rm TAG | snapshot NAME ls     file system + /data (load: stopped container)
  clone SRC NEW [--project P]                          NEW from SRC as it is (files and a copy of /data)
  push NAME LOCAL PATH | pull NAME PATH LOCAL          copy files in or out
  view NAME [--read-only]   terminal viewer: Kitty graphics or Sixel (Windows Terminal, foot); Ctrl+] exits
  show NAME                 native window (TigerVNC viewer, through the desktop's unix socket)
  image ls | image pull REF | image build NAME DIR | image rm NAME
Global: --engine docker|podman (default: TERMINAL_CT_ENGINE, else podman if installed, else docker).
"""
import argparse, base64, calendar, collections, contextlib, hashlib, http.server, io, json, os, platform, queue, re, secrets, \
    shlex, shutil, socket, socketserver, struct, subprocess, sys, tarfile, tempfile, threading, time, urllib.parse, webbrowser, zlib
import importlib.util, ipaddress, http.client, signal, ssl

VERSION = "0.9.0"
WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}  # the daemon has no console: no flash
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


STORE_OWN = ("projects", "stacks", "build", "agent")  # STORE/NAME is a container's folder: never these


def check_name(name):
    if not re.fullmatch(r"[a-z0-9]([a-z0-9_.-]{0,46}[a-z0-9])?", name or ""):
        raise SystemExit(f"invalid name: {name!r} (lowercase letters, digits, _ . -)")
    if name in STORE_OWN or name.startswith("daemon.") or name.endswith((".json", ".log", ".token", ".conf", ".lock")):
        raise SystemExit(f"{name!r} is reserved for terminal-ct's own files: choose another name")
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


# ---------- the engine's API (K1): the hot calls over its unix socket, the CLI otherwise ----------
# Each CLI call costs a process (0.3-0.7 s with Podman under WSL); the Docker Engine API, which Podman serves too,
# answers the same in milliseconds. Node.run sends the calls below through it when a socket answers, and keeps the
# CLI for everything else and whenever the API cannot be reached (any doubt: the CLI).
API = "/v1.41"  # Docker 20.10+, Podman 3+ (its compatible API)


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path_)


def api_call(path, method, url, timeout=60, body=None):
    """(status, decoded JSON or text) from the engine's API on that socket. OSError when it does not answer."""
    c = _UnixHTTP(path, timeout)
    try:
        c.request(method, url, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"} if body is not None else {})
        r = c.getresponse()
        data = r.read()
        text = data.decode("utf-8", "replace")
        if (r.getheader("Content-Type") or "").startswith("application/json") and data:
            with contextlib.suppress(ValueError):
                return r.status, json.loads(data)
        return r.status, text
    except (http.client.HTTPException, ValueError) as e:
        raise OSError(f"engine API: {e}") from None
    finally:
        c.close()


def _done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _api_error(status, reply, what):
    msg = reply.get("message") if isinstance(reply, dict) else str(reply).strip()
    return _done("", 1, f"Error: {msg or f'{what}: HTTP {status}'}")


class Node:
    """Where containers run. v1: the local host; later a VM node (through its guest agent) or a remote machine,
    so every engine call goes through a node."""
    def __init__(self, engine=None):
        self.engine = engine or pick_engine()
        if self.engine == "podman" and in_wsl() and "CONTAINERS_CONF_OVERRIDE" not in os.environ:
            # WSL's kernel lacks nftables pieces netavark needs: a bridge container failed to start ("nft did not
            # return successfully"). Its iptables driver works; set for terminal-ct's own calls only.
            os.makedirs(STORE, exist_ok=True)
            conf, text = os.path.join(STORE, "podman-wsl.conf"), '[network]\nfirewall_driver = "iptables"\n'
            try:
                with open(conf) as f:
                    same = f.read() == text
            except OSError:
                same = False
            if not same:  # atomic: a podman call of another thread must never read it half written
                _write_atomic(conf, text)
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

    def api(self):
        """The engine's API socket when one answers (cached per Node), else None: then the CLI does everything."""
        if "_api" not in self.__dict__:
            self._api = None
            if not WINDOWS and os.environ.get("TERMINAL_CT_API") != "0":
                for path in self._api_sockets():
                    with contextlib.suppress(OSError):
                        if api_call(path, "GET", "/_ping", timeout=2)[0] == 200:
                            self._api = path
                            break
        return self._api

    def _api_sockets(self):
        """Where this engine's API may be: what the user set, the usual sockets, else (Podman) a service of ours."""
        var = os.environ.get("DOCKER_HOST" if self.engine == "docker" else "CONTAINER_HOST")
        if var:  # theirs: only a unix one is ours to use (tcp, ssh: the CLI knows how)
            return [var[len("unix://"):]] if var.startswith("unix://") else []
        if self.engine == "docker":
            return ["/var/run/docker.sock"]
        run_dir = os.environ.get("XDG_RUNTIME_DIR") or ""
        found = [p for p in (os.path.join(run_dir, "podman", "podman.sock") if run_dir else "",
                             "/run/podman/podman.sock" if os.geteuid() == 0 else "") if p and os.path.exists(p)]
        return found or [p for p in [self._podman_service(run_dir)] if p]

    def _podman_service(self, run_dir):
        """Start `podman system service` on a private socket (it stops itself after 10 idle minutes): rootless
        Podman under WSL has no socket of its own. None when no private place for it."""
        base = run_dir if run_dir and os.access(run_dir, os.W_OK) else STORE
        path = os.path.join(base, f"tct-podman-{os.getuid()}.sock")
        if len(path) > 100:  # a unix socket path's limit
            return None
        if not os.path.exists(path):
            os.makedirs(base, exist_ok=True)
            with contextlib.suppress(OSError):
                subprocess.Popen([self.engine, "system", "service", "--time=600", f"unix://{path}"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            for _ in range(30):
                if os.path.exists(path):
                    break
                time.sleep(0.1)
        return path

    def _via_api(self, args, timeout):
        """The reply of `ENGINE args` through the API, or None when it is not one of the calls handled here."""
        sock = self.api()
        if not sock:
            return None
        t = timeout or 60
        q = urllib.parse.quote
        if args[:4] == ["ps", "-a", "-q", "--filter"] and len(args) == 5 and args[4].startswith("label="):
            filters = q(json.dumps({"label": [args[4][len("label="):]]}))
            st, r = api_call(sock, "GET", f"{API}/containers/json?all=1&filters={filters}", t)
            return _done("".join(c["Id"] + "\n" for c in r)) if st == 200 and isinstance(r, list) else None
        if args[:3] == ["inspect", "--type", "container"]:
            size = "--size" in args
            names = [a for a in args[3:] if a != "--size"]
            out, missing = [], []
            for n in names:
                st, r = api_call(sock, "GET", f"{API}/containers/{q(n, safe='')}/json" + ("?size=1" if size else ""), t)
                if st == 200 and isinstance(r, dict):
                    out.append(r)
                elif st == 404:
                    missing.append(n)
                else:
                    return None
            return _done(json.dumps(out) if out else "[]", 1 if missing else 0,
                         f"Error: no such container {', '.join(missing)}" if missing else "")
        if args[0] in ("start", "stop") and (len(args) == 2 or args[1] == "-t" and len(args) == 4):
            name, wait = args[-1], (args[2] if len(args) == 4 else None)
            url = f"{API}/containers/{q(name, safe='')}/{args[0]}" + (f"?t={int(wait)}" if wait else "")
            st, r = api_call(sock, "POST", url, t + (int(wait) if wait else 30))
            return _done(name + "\n") if st in (204, 304) else _api_error(st, r, args[0]) if st in (404, 409, 500) else None
        if args[:2] == ["rm", "-f"] and len(args) == 3:
            st, r = api_call(sock, "DELETE", f"{API}/containers/{q(args[2], safe='')}?force=1", t)
            return _done(args[2] + "\n") if st == 204 else _api_error(st, r, "rm") if st in (404, 409) else None
        if args[:5] == ["network", "ls", "-q", "--filter", args[4] if len(args) == 5 else None] and \
                args[4].startswith("label="):
            filters = q(json.dumps({"label": [args[4][len("label="):]]}))
            st, r = api_call(sock, "GET", f"{API}/networks?filters={filters}", t)
            return _done("".join(n["Id"] + "\n" for n in r)) if st == 200 and isinstance(r, list) else None
        if args[:2] == ["network", "inspect"] and len(args) > 2:
            out = []
            for n in args[2:]:
                st, r = api_call(sock, "GET", f"{API}/networks/{q(n, safe='')}", t)
                if st != 200 or not isinstance(r, dict):
                    return None if st != 404 else _api_error(st, r, "network inspect")
                out.append(r)
            return _done(json.dumps(out))
        if args == ["volume", "ls", "--format", "{{.Name}}\t{{.Mountpoint}}"]:
            st, r = api_call(sock, "GET", f"{API}/volumes", t)
            vols = (r or {}).get("Volumes") if st == 200 and isinstance(r, dict) else None
            return _done("".join(f"{v['Name']}\t{v.get('Mountpoint', '')}\n" for v in vols or [])) \
                if vols is not None else None
        if args[:4] == ["stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}"]:
            return self._api_stats(sock, args[4:], t)
        if args[:2] in (["image", "inspect"], ["volume", "inspect"]) and len(args) == 3:
            kind = "images" if args[0] == "image" else "volumes"
            st, r = api_call(sock, "GET", f"{API}/{kind}/{q(args[2], safe='')}" + ("/json" if kind == "images" else ""), t)
            if st == 200 and isinstance(r, dict):
                return _done(json.dumps([r]))
            return _api_error(st, r, f"{args[0]} inspect") if st == 404 else None
        return None

    def _api_stats(self, sock, names, timeout):
        """`stats --no-stream` as the CLI prints it, from one-shot samples (no 1-2 s sampling wait): CPU % of one core
        between two calls; a container first seen is sampled twice, 0.3 s apart."""
        cpu = self.__dict__.setdefault("_cpu", {})
        def sample(n):
            st, r = api_call(sock, "GET", f"{API}/containers/{urllib.parse.quote(n, safe='')}/stats?stream=false&one-shot=true",
                             timeout)
            if st == 404:
                return None
            if st != 200 or not isinstance(r, dict):
                raise OSError(f"stats: HTTP {st}")
            return r, time.monotonic()
        got = {n: sample(n) for n in names}
        if any(r and n not in cpu for n, r in got.items()):  # no earlier sample to compare with
            for n, r in got.items():
                if r:
                    cpu[n] = ((r[0].get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage"), r[1]
            time.sleep(0.3)
            got = {n: sample(n) if r else None for n, r in got.items()}
        lines = []
        for n, r in got.items():
            if not r:
                continue
            r, now = r
            total = ((r.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage")
            prev, cpu[n] = cpu.get(n), (total, now)
            pct = (total - prev[0]) / ((now - prev[1]) * 1e9) * 100 \
                if prev and prev[0] is not None and total is not None and now > prev[1] else 0.0
            mem = r.get("memory_stats") or {}
            inner = mem.get("stats") or {}
            used = max(0, (mem.get("usage") or 0) - (inner.get("inactive_file") or inner.get("cache") or 0))
            lines.append(f"{n}|{max(pct, 0):.2f}%|{used}B / {mem.get('limit') or 0}B|"
                         f"{(r.get('pids_stats') or {}).get('current', '')}")
        return _done("".join(l + "\n" for l in lines))

    def run(self, args, check=True, timeout=None, capture=True):
        if capture and type(self) is Node:  # this PC's engine (a node or another PC has its own way in)
            try:
                r = self._via_api(list(args), timeout)
            except OSError:  # the socket went away: the CLI, and no API for this Node from now on
                self._api, r = None, None
            if r is not None:
                if check and r.returncode:
                    raise SystemExit((r.stderr or f"{self.engine} {args[0]} failed").strip())
                return r
        # uncaptured output (pull, build progress) goes to stderr: stdout is the JSON reply, or the MCP channel
        r = subprocess.run([self.engine, *args], text=True, timeout=timeout, encoding="utf-8", errors="replace",
                           **({"capture_output": True} if capture else {"stdout": sys.stderr}), **NO_WINDOW)
        if check and r.returncode:
            raise SystemExit((r.stderr or r.stdout or f"{self.engine} {args[0]} failed").strip())
        return r

    def json(self, args):
        out = self.run(args).stdout.strip()
        return json.loads(out) if out else []

    def free_bytes(self, root):
        try:
            return host_free_bytes(root)
        except OSError:  # the engine's store is in another machine (Docker Desktop's VM): use ours
            return host_free_bytes(STORE if os.path.isdir(STORE) else os.path.dirname(STORE))

    def pipe(self, args, stdin=None, stdout=None):
        """An engine call with binary files as stdin/stdout (tar streams)."""
        with open(stdin, "rb") if stdin else contextlib.nullcontext() as i, \
                open(stdout, "wb") if stdout else contextlib.nullcontext() as o:
            r = subprocess.run([self.engine, *args], stdin=i or subprocess.DEVNULL, stdout=o or subprocess.DEVNULL,
                               stderr=subprocess.PIPE, **NO_WINDOW)
        if r.returncode:
            raise SystemExit(r.stderr.decode("utf-8", "replace").strip() or f"{self.engine} {args[0]} failed")


NODE = None
_LOCAL = threading.local()  # .node: a thread working on one stack, on that stack's engine (not the others')
_NODES = {}


def node_for(engine):
    """One Node per engine (its host facts cached), for the daemon's stacks; None: the default engine."""
    if not engine:
        return None
    if engine not in _NODES:
        _NODES[engine] = Node(engine)
    return _NODES[engine]


# ---------- nodes: Linux VMs of terminal-vm running Docker (phase 9) ----------
# terminal-ct stays on the host and decides (state, quota, stacks); a node only runs `docker` commands, sent through
# its guest agent by terminal-vm's `exec`: no network, no port, nothing to install on the host.


class NodeVM(Node):
    """Where containers run when they run in a node VM: each engine call is `docker ...` in that VM."""
    def __init__(self, name, timeout=None):
        self.name, self.engine, self.timeout = name, "docker", timeout  # timeout: each call's, when looking around

    def _exec(self, argv, timeout):
        """A program in the node, as root, by its guest agent: {exit, stdout, stderr}; RuntimeError when the node
        cannot run it. terminal-vm in this process when it can be loaded (~0.1 s a call), else its command line
        (a new Python each time: 0.3 s, 1-2 s from a Windows folder in WSL)."""
        vm = vm_module()
        if vm is None:
            return vm_call(["exec", self.name, "--timeout", str(timeout), "--", *argv], timeout=timeout + 30)
        try:
            with captured(io.StringIO()):
                return vm.guest_run(self.name, argv, max(1.0, min(float(timeout), 3600)))
        except (SystemExit, ValueError, KeyError, IndexError, OSError) as e:  # what its command line reports as errors
            raise RuntimeError(str(e) or f"terminal-vm exec failed ({type(e).__name__})") from None

    def run(self, args, check=True, timeout=None, capture=True):
        if args[0] == "build" and os.path.isdir(args[-1]):  # a context folder of this host: unpacked in the node
            remote = f"/tmp/tct-{secrets.token_hex(6)}"
            try:
                self.sh(f"mkdir -m 700 {remote} && mkdir {remote}/ctx", timeout=30)  # private: a PC has other users
                with tempfile.TemporaryDirectory() as tmp:
                    _tar(args[-1], ".", os.path.join(tmp, "context.tar"))
                    self.copy("push", os.path.join(tmp, "context.tar"), f"{remote}/ctx.tar")
                out = self.sh(f"tar -xf {remote}/ctx.tar -C {remote}/ctx && {self.engine} "
                              + " ".join(shlex.quote(a) for a in args[:-1]) + f" {remote}/ctx", timeout=3600)
            finally:
                with contextlib.suppress(SystemExit):
                    self.sh(f"rm -rf {remote}", timeout=60)
            if not capture:
                sys.stderr.write(out)
            return subprocess.CompletedProcess([self.engine, *args], 0, out, "")
        t = timeout or self.timeout or (600 if capture else 3600)  # uncaptured: a pull or a build, minutes
        try:
            r = self._exec([self.engine, *args], t)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            raise SystemExit(f"node {self.name}: {e}") from None
        if not capture and r.get("stdout"):
            sys.stderr.write(r["stdout"])  # progress (pull, build): stdout is the JSON reply
        cp = subprocess.CompletedProcess([self.engine, *args], r.get("exit", 1), r.get("stdout", ""), r.get("stderr", ""))
        if check and cp.returncode:
            raise SystemExit((cp.stderr or cp.stdout or f"{self.engine} {args[0]} failed on node {self.name}").strip())
        return cp

    def host(self):
        if not hasattr(self, "_host"):
            fmt = "{{.Host.CPUs}} {{.Host.MemTotal}} {{.Store.GraphRoot}}" if self.engine == "podman" else \
                "{{.NCPU}} {{.MemTotal}} {{.DockerRootDir}}"
            c, m, root = self.run(["info", "--format", fmt]).stdout.split(None, 2)
            self._host = int(c), int(m) / 2 ** 30, root.strip()
        return self._host

    def free_bytes(self, root):
        try:
            r = self._exec(["df", "-Pk", root], 30)
            return int(r["stdout"].splitlines()[-1].split()[3]) * 1024
        except (RuntimeError, subprocess.TimeoutExpired, KeyError, IndexError, ValueError) as e:
            raise SystemExit(f"node {self.name}: cannot read its free disk space ({e})") from None

    def sh(self, command, timeout=600):
        """A shell command in the node, as root; its output, or SystemExit with its error."""
        try:
            r = self._exec(["sh", "-c", command], timeout)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            raise SystemExit(f"node {self.name}: {e}") from None
        if r.get("exit"):
            raise SystemExit((r.get("stderr") or r.get("stdout") or f"node {self.name}: command failed").strip())
        return r.get("stdout", "")

    def copy(self, direction, src, dest):
        """A file into (push) or out of (pull) the node, by its guest agent (any size, binary)."""
        try:
            vm_call([direction, self.name, src, dest], timeout=3600)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            raise SystemExit(f"node {self.name}: copy failed ({e})") from None

    def pipe(self, args, stdin=None, stdout=None):
        """An engine call with binary files as stdin/stdout (tar streams): the files cross by the guest agent."""
        tmp = f"/tmp/tct-{secrets.token_hex(6)}"
        self.sh(f"mkdir -m 700 {tmp}", timeout=30)  # the node's other users never read the stream
        try:
            if stdin:
                self.copy("push", stdin, f"{tmp}/in")
            self.sh(f"{self.engine} " + " ".join(shlex.quote(a) for a in args) + (f" < {tmp}/in" if stdin else " </dev/null")
                    + (f" > {tmp}/out" if stdout else " >/dev/null"), timeout=3600)
            if stdout:
                self.copy("pull", f"{tmp}/out", stdout)
        finally:
            with contextlib.suppress(SystemExit):
                self.sh(f"rm -rf {tmp}", timeout=30)

    def deploy(self):
        """This terminal_ct.py in the node (its screen tools run there, next to the desktop's socket); copied again
        when it changed."""
        if self.name in _DEPLOYED:
            return
        with open(os.path.abspath(__file__), "rb") as f:
            mine = hashlib.sha256(f.read()).hexdigest()
        if mine not in self.sh(f"sha256sum {NODE_CT} 2>/dev/null || true", timeout=30):
            self.sh(f"mkdir -p {os.path.dirname(NODE_CT)}", timeout=30)
            self.copy("push", os.path.abspath(__file__), NODE_CT + ".new")  # run as root there: never half written
            # root's alone (the agent made it 0666): nobody else may change code root runs
            self.sh(f"echo '{mine}  {NODE_CT}.new' | sha256sum -c - >/dev/null && chmod 644 {NODE_CT}.new && "
                    f"chmod 755 {os.path.dirname(NODE_CT)} && mv {NODE_CT}.new {NODE_CT}", timeout=30)
        _DEPLOYED.add(self.name)

    def act(self, name, tool, a):
        """act() run by terminal-ct in the node: same replies (a screenshot comes back as PNG bytes)."""
        self.deploy()
        t = int(float(a.get("timeout", 30))) + 60 if tool in ("wait", "ready") else 60
        try:
            r = self._exec(["env", f"TERMINAL_CT_HOME={STORE}", "TERMINAL_CT_ENGINE=docker", "python3", NODE_CT,
                            "_act", name, tool, json.dumps(a)], t)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"node {self.name}: {e}") from None
        if r.get("exit"):
            raise RuntimeError((r.get("stderr") or "").strip().splitlines()[-1:][0] if (r.get("stderr") or "").strip()
                               else f"node {self.name}: {tool} failed")
        try:
            out = json.loads(r["stdout"])
            return (base64.b64decode(out["png"]), tuple(out["size"])) if tool == "screenshot" else out["result"]
        except (ValueError, KeyError, TypeError) as e:  # cut short (the agent keeps 16 MB of output) or not ours
            raise RuntimeError(f"node {self.name}: unreadable reply to {tool} ({e})") from None

    def transfer(self, direction, src, container, dest):
        """push/pull with a container of the node: through a folder of the node, as `docker cp` would do here
        (a folder crosses as a tar)."""
        q, tmp = shlex.quote, f"/tmp/tct-{secrets.token_hex(6)}"
        self.sh(f"mkdir -m 700 {tmp} && mkdir {tmp}/in", timeout=30)  # everything inside: a PC has other users
        try:
            if direction == "push":
                base = os.path.basename(os.path.normpath(src))
                if os.path.isdir(src):
                    with tempfile.TemporaryDirectory() as t:
                        _tar(src, base, os.path.join(t, "x.tar"))
                        self.copy("push", os.path.join(t, "x.tar"), f"{tmp}/x.tar")  # beside in/: any name is free
                    self.sh(f"tar -xf {tmp}/x.tar -C {tmp}/in", timeout=600)
                else:  # a fixed name across scp (an old scp lets the remote shell read the path), then its own
                    self.copy("push", src, f"{tmp}/f")
                    self.sh(f"mv {tmp}/f {q(f'{tmp}/in/{base}')}", timeout=30)
                self.sh(f"{self.engine} cp -a {q(f'{tmp}/in/{base}')} {q(f'{container}:{dest}')}", timeout=600)
                return
            self.sh(f"{self.engine} cp {q(f'{container}:{src}')} {tmp}/out", timeout=600)
            if os.path.isdir(dest):  # as docker cp: into an existing folder, under the source's name
                dest = os.path.join(dest, os.path.basename(src.rstrip("/")) or "out")
            if self.sh(f"test -d {tmp}/out && echo dir || echo file", timeout=30).strip() == "file":
                return self.copy("pull", f"{tmp}/out", dest)
            self.sh(f"tar -cf {tmp}/x.tar -C {tmp} out", timeout=600)
            with tempfile.TemporaryDirectory() as t:
                self.copy("pull", f"{tmp}/x.tar", os.path.join(t, "x.tar"))
                with tarfile.open(os.path.join(t, "x.tar")) as tar:
                    _extract(tar, t)
                shutil.move(os.path.join(t, "out"), dest)
        finally:
            with contextlib.suppress(SystemExit):
                self.sh(f"rm -rf {tmp}", timeout=60)


# ---------- other PCs (phase 11): one central PC drives the others ----------
# The central PC keeps all state (stacks, placement, quota); another PC only runs what it is sent: engine calls,
# terminal-vm, file copies. Direct: HTTPS to `terminal-ct agent` there (self-signed certificate pinned by its SHA-256
# before the token is sent). SSH: plain ssh/scp, nothing of ours runs there. Either way the token (or the SSH login)
# is a shell as the agent's user there, as `docker` access is anyway.

AGENT_PORT = int(os.environ.get("TERMINAL_CT_AGENT_PORT") or 7173)
AGENT_MAX_JSON = 2 ** 20


def _agent_path(f):
    d = os.path.join(STORE, "agent")
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700)
        if WINDOWS:  # chmod means nothing there: this user alone, whatever folder TERMINAL_CT_HOME names
            subprocess.run(["icacls", d, "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:(OI)(CI)F"],
                           capture_output=True, **NO_WINDOW)
    if not WINDOWS:
        os.chmod(d, 0o700)  # makedirs sets it only when it makes the folder
    return os.path.join(d, f)


def agent_credentials(rotate=False):
    """(certificate SHA-256, token) of this PC's agent: the certificate made once (openssl), a new token on rotate."""
    cert, tok = _agent_path("cert.pem"), _agent_path("token")  # cert.pem holds the certificate and its key
    fp = tls_cert(cert)  # the same maker as the shared dashboard's
    if rotate or not os.path.isfile(tok):
        _web_write(tok, secrets.token_urlsafe(32), 0o600)
    with open(tok) as f:  # what the file says: another start may have written it first
        token = f.read().strip()
    if len(token) < 32:  # cut short: an empty token would let anyone in
        _web_write(tok, secrets.token_urlsafe(32), 0o600)
        with open(tok) as f:
            token = f.read().strip()
    return fp, token


AGENT_LOG_MAX = 5 * 2 ** 20
_REFUSED = {}  # address: time of its last refusal logged


def _agent_log(client, what, refusal=False):
    if refusal:
        if time.time() - _REFUSED.get(client, 0) < 60:  # a flood of bad tokens fills no disk
            return
        if len(_REFUSED) > 10000:
            _REFUSED.clear()
        _REFUSED[client] = time.time()
    path = _agent_path("agent.log")
    with contextlib.suppress(OSError):
        if os.path.getsize(path) > AGENT_LOG_MAX:
            os.replace(path, path + ".1")  # one older log kept
    with contextlib.suppress(OSError):
        with open(path, "a", encoding="utf-8") as f:  # escaped: a client's text forges no line, no terminal escape
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {client} {what.encode('unicode_escape').decode('ascii')}\n")


class _AgentHandler(http.server.BaseHTTPRequestHandler):
    server_version, sys_version = "terminal-ct-agent", ""  # no versions told to whoever asks
    timeout = 15  # seconds a client may stay silent (handshake included) before its thread is freed

    def log_message(self, *a):
        pass

    def setup(self):
        self.request.settimeout(self.timeout)
        self.request.do_handshake()  # here, in this request's thread: a slow client never holds up the others
        super().setup()

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        given = self.headers.get("X-Token", "").encode("utf-8", "replace")
        if given and secrets.compare_digest(given, self.server.token.encode()):
            return True
        _agent_log(self.client_address[0], f"refused {self.command} {self.path[:80]}", refusal=True)
        time.sleep(0.5)  # slows down guessing
        self.close_connection = True
        self._reply(401, {"error": "wrong token (terminal-ct agent on that PC shows it; host add again)"})
        return False

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if not self._authorized():
            return
        if u.path == "/ping":
            return self._reply(200, {"tool": TOOL, "version": VERSION, "os": sys.platform})
        if u.path != "/pull":
            return self._reply(404, {"error": "not found"})
        path = dict(urllib.parse.parse_qsl(u.query)).get("path", "")
        try:
            if not os.path.isfile(path):  # a FIFO or a device would stream forever
                raise OSError("not a regular file")
            f = open(path, "rb")
        except OSError as e:
            return self._reply(404, {"error": f"{path}: {e.strerror or e}"})
        with f:
            size = os.fstat(f.fileno()).st_size
            _agent_log(self.client_address[0], f"pull {path}")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            while size:  # exactly what was announced, even if the file grows meanwhile
                chunk = f.read(min(size, 1 << 20))
                if not chunk:  # it shrank: the client sees a short reply and fails
                    break
                self.wfile.write(chunk)
                size -= len(chunk)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        try:
            n = int(self.headers.get("Content-Length") or "")
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            return self._reply(411, {"error": "Content-Length needed"})
        raw = self.rfile.read(n) if u.path == "/exec" and n <= AGENT_MAX_JSON else None  # read even when refused
        if not self._authorized():
            return
        if u.path == "/push":
            return self._push(dict(urllib.parse.parse_qsl(u.query)).get("path", ""), n)
        if u.path != "/exec":
            return self._reply(404, {"error": "not found"})
        if raw is None:
            self.close_connection = True
            return self._reply(413, {"error": "request too large"})
        try:
            req = json.loads(raw)
            argv, timeout, stdin = req["argv"], float(req.get("timeout", 60)), req.get("stdin")
            if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)) or \
                    not 0 < timeout <= 3600 or not isinstance(stdin, (str, type(None))):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            return self._reply(400, {"error": "exec wants {argv: [strings], timeout: 1-3600 s, stdin?: string}"})
        _agent_log(self.client_address[0], "exec " + " ".join(argv)[:200])
        group = {"start_new_session": True} if hasattr(os, "killpg") else {}  # a timeout ends its children too
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                 errors="replace", **group, **NO_WINDOW)
        except OSError as e:
            return self._reply(200, {"exit": 127, "stdout": "", "stderr": f"{argv[0]}: {e.strerror or e}"})
        try:
            out, err = p.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:  # sh -c "tar ... && podman build": the build must not go on alone
            with contextlib.suppress(OSError):
                os.killpg(p.pid, signal.SIGKILL) if group else p.kill()
            p.communicate()
            return self._reply(504, {"error": f"{argv[0]} timed out ({timeout:g} s)"})
        self._reply(200, {"exit": p.returncode, "stdout": out, "stderr": err})

    def _push(self, path, n):
        tmp = f"{path}.tct-{secrets.token_hex(4)}"  # whole or not at all: a cut connection leaves no half file
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as f:  # owner only, and never through a link planted there
                left = n
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise OSError("connection cut short")
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(tmp, path)
        except OSError as e:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            self.close_connection = True
            return self._reply(400, {"error": f"{path}: {e.strerror or e}"})
        _agent_log(self.client_address[0], f"push {path} ({n} bytes)")
        self._reply(200, {"path": path, "bytes": n})


def agent_serve(listen="127.0.0.1", port=None):
    """This PC's agent, HTTPS on listen:port (a thread serves it; serve_forever() to block): the server."""
    ip = ipaddress.ip_address(listen)  # an address, never a name: what is reachable stays explicit
    fp, token = agent_credentials()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(_agent_path("cert.pem"))
    srv = (_AgentServer6 if ip.version == 6 else _AgentServer)((str(ip), AGENT_PORT if port is None else port),
                                                               _AgentHandler)
    srv.slots = threading.BoundedSemaphore(AGENT_CONNECTIONS)
    srv.per_address = 4  # one host on the network cannot take every slot and starve the central PC's calls
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    srv.token, srv.fingerprint = token, fp
    return srv


def agent(listen="127.0.0.1", port=None, rotate=False):
    if in_wsl() and shutil.which("podman"):
        Node("podman")  # its WSL fix (iptables, not nftables) for the podman calls the agent runs
    if rotate:
        agent_credentials(rotate=True)
    srv = agent_serve(listen, port)
    host = f"[{listen}]" if ":" in listen else listen
    url = f"https://{host}:{srv.server_address[1]}"
    print(json.dumps({"url": url, "fingerprint": srv.fingerprint, "token": srv.token,
                      "on the central PC": f"terminal-ct host add NAME {url} --fingerprint {srv.fingerprint}  "
                                           "(then type or paste the token)"}, indent=1), flush=True)
    srv.serve_forever()


def agent_request(host, method, path, body=None, timeout=60, out=None, length=None):
    """One call to another PC's agent: its JSON reply, or with out its reply body written to that file;
    RuntimeError when the agent cannot be reached, is not the pinned one, or refuses."""
    u = urllib.parse.urlsplit(host["url"])
    headers = {"X-Token": host["token"]}
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if body is not None:
        headers["Content-Length"] = str(len(body) if length is None else length)
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # trust = the pinned fingerprint, checked below
        c = http.client.HTTPSConnection(u.hostname, u.port or AGENT_PORT, timeout=timeout, context=ctx)
        try:
            c.connect()
            got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest()
            if not secrets.compare_digest(got, host["fingerprint"].lower().replace(":", "")):  # before the token
                raise RuntimeError(f"certificate {got} is not the pinned one (another machine, a new agent "
                                   "certificate there: host add again, or an antivirus scanning HTTPS in between: "
                                   "exclude this program, or use ssh://)")
            try:
                c.request(method, path, body=body, headers=headers)
            except (BrokenPipeError, ConnectionResetError):  # refused before all was sent: its answer says why
                pass
            r = c.getresponse()
            if r.status == 200 and out:
                tmp = f"{out}.tct-{secrets.token_hex(4)}"
                try:
                    with open(tmp, "wb") as f:
                        shutil.copyfileobj(r, f)
                    if r.length:  # http.client leaves what was not read: the body was cut short
                        raise OSError("reply cut short")
                    os.replace(tmp, out)
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(tmp)
                return None
            data = r.read()
        finally:
            c.close()
    except (OSError, http.client.HTTPException) as e:  # ssl errors are OSErrors
        raise RuntimeError(f"agent {host['url']}: {e}") from None
    except RuntimeError as e:
        raise RuntimeError(f"agent {host['url']}: {e}") from None
    try:
        reply = json.loads(data)
    except ValueError:
        raise RuntimeError(f"agent {host['url']}: unreadable reply ({r.status})") from None
    if r.status != 200:
        raise RuntimeError(f"agent {host['url']}: {reply.get('error') if isinstance(reply, dict) else r.status}")
    return reply


def ssh_target(url):
    """(urlsplit of an ssh://[user@]host[:port] address, its port or None), or ValueError: nothing in it may read as
    an ssh option (ssh://-oProxyCommand=... would run a command on this PC)."""
    u = urllib.parse.urlsplit(url)
    port = u.port  # ValueError when not a number
    if u.scheme != "ssh" or not u.hostname or u.path not in ("", "/") or u.query or u.password is not None or \
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*|[0-9a-fA-F:.]+", u.hostname) or \
            u.username is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", u.username):
        raise ValueError(f"not an ssh://[user@]host[:port] address: {url!r}")
    return u, port


class NodeRemote(NodeVM):
    """Containers of another PC: each engine call is run there by its agent (https://...) or over SSH (ssh://...).
    Its engine (podman or docker) was found by host add."""
    def __init__(self, name, remote, timeout=None):
        self.name, self.remote, self.timeout = name, remote, timeout
        self.engine = remote.get("engine") if remote.get("engine") in ("podman", "docker") else "docker"
        self.rootless = remote.get("rootless", True)

    def free_bytes(self, root):
        try:
            return super().free_bytes(root)
        except SystemExit:  # shortcut: Docker Desktop (a Mac) keeps its store in its own VM, not on that PC: the
            return super().free_bytes("/")  # PC's own disk then; upgrade: read that VM's space (docker system df)

    def _ssh(self, scp=False):
        try:
            u, port = ssh_target(self.remote["url"])
        except (ValueError, KeyError):  # a hosts.json edited by hand: an unreachable PC, as for NodeVM
            raise RuntimeError(f"{self.name}: bad address in hosts.json (host rm, then host add)") from None
        port = ["-P" if scp else "-p", str(port)] if port else []
        return ["scp" if scp else "ssh", "-q" if scp else "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                *port], (f"{u.username}@{u.hostname}" if u.username else u.hostname)

    def _exec(self, argv, timeout):
        if self.remote["url"].startswith("https://"):
            return agent_request(self.remote, "POST", "/exec", {"argv": argv, "timeout": timeout}, timeout + 30)
        cmd, target = self._ssh()
        try:
            r = subprocess.run([*cmd, "--", target, " ".join(shlex.quote(a) for a in argv)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout + 30,
                               stdin=subprocess.DEVNULL, **NO_WINDOW)
        except OSError as e:
            raise RuntimeError(f"ssh: {e.strerror or e}") from None
        if r.returncode == 255:  # ssh's own failure (the remote command's exit codes are below)
            raise RuntimeError(f"ssh {target}: {r.stderr.strip() or 'connection failed'}")
        return {"exit": r.returncode, "stdout": r.stdout, "stderr": r.stderr}

    def copy(self, direction, src, dest, timeout=3600):
        try:
            if self.remote["url"].startswith("https://"):
                if direction == "push":
                    with open(src, "rb") as f:
                        agent_request(self.remote, "POST", "/push?" + urllib.parse.urlencode({"path": dest}), f,
                                      timeout=timeout, length=os.fstat(f.fileno()).st_size)
                else:
                    agent_request(self.remote, "GET", "/pull?" + urllib.parse.urlencode({"path": src}),
                                  timeout=timeout, out=dest)
                return
            cmd, target = self._ssh(scp=True)
            pair = [src, f"{target}:{dest}"] if direction == "push" else [f"{target}:{src}", dest]
            r = subprocess.run([*cmd, "--", *pair], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, **NO_WINDOW)
            if r.returncode:
                raise RuntimeError(r.stderr.strip() or "scp failed")
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            raise SystemExit(f"host {self.name}: copy failed ({e})") from None


def _tar(src, arcname, path):
    """A tar of src that busybox's tar reads too (GNU long names), or SystemExit."""
    try:
        with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as tar:
            tar.add(src, arcname=arcname)
    except (ValueError, OSError) as e:
        raise SystemExit(f"cannot pack {src}: {e}") from None


def _extract(tar, dest):
    """Extract a tar that came out of a container (not trusted): nothing outside dest, no devices, no setuid."""
    if hasattr(tarfile, "data_filter"):
        return tar.extractall(dest, filter="data")
    for m in tar.getmembers():  # what the "data" filter does, for Pythons without it
        parts = m.name.split("/") + (m.linkname.split("/") if m.issym() or m.islnk() else [])
        if m.isdev() or os.path.isabs(m.name) or ".." in parts or (m.issym() or m.islnk()) and os.path.isabs(m.linkname):
            raise SystemExit(f"refused in the copy: {m.name}")
        m.mode &= 0o777
    tar.extractall(dest)


NODE_CT = "/var/lib/terminal-ct/terminal_ct.py"  # terminal-ct's copy in each node
_DEPLOYED = set()  # nodes this process already checked


def _nodes():
    try:
        with open(os.path.join(STORE, "nodes.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {"nodes": d.get("nodes", {}), "placement": d.get("placement", {})}


def _save_nodes(d):
    os.makedirs(STORE, exist_ok=True)
    _write_atomic(os.path.join(STORE, "nodes.json"), json.dumps(d, indent=1))


def node_named(name):
    if name not in _nodes()["nodes"]:
        raise SystemExit(f"no node named {name!r} (terminal-ct node ls; node add NAME, or host add NAME URL)")
    return node_obj(name)


def is_host(name):
    return _nodes()["nodes"].get(name, {}).get("kind") == "host"


def node_obj(name, timeout=None):
    """Node NAME: a terminal-vm VM, or another PC (host add)."""
    if not is_host(name):
        return NodeVM(name, timeout)
    h = _hosts().get(name)
    if not h:
        raise SystemExit(f"host {name}: its address and token are gone from {os.path.join(STORE, 'hosts.json')}: "
                         f"terminal-ct host rm {name}, then host add again")
    return NodeRemote(name, h, timeout)


def _hosts():
    """{name: {url, fingerprint, token}}: in its own file, owner only (the tokens)."""
    try:
        with open(os.path.join(STORE, "hosts.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_hosts(h):
    os.makedirs(STORE, exist_ok=True)
    _web_write(os.path.join(STORE, "hosts.json"), json.dumps(h, indent=1), 0o600)


def _read_token():
    """The agent's token: TERMINAL_CT_AGENT_TOKEN, else typed or piped in (never an argument: ps and history)."""
    t = os.environ.pop("TERMINAL_CT_AGENT_TOKEN", None)  # not handed down to ssh, docker, terminal-vm
    if t is None:
        if sys.stdin is not None and sys.stdin.isatty():
            import getpass
            t = getpass.getpass("agent token (shown by terminal-ct agent on that PC): ")
        else:
            t = sys.stdin.readline() if sys.stdin is not None else ""
    return t.strip()


def host_add(name, url, fingerprint=None, token=None, engine=None):
    """Another PC as a node: reached by its agent (https://IP:PORT, pinned fingerprint, token) or over SSH
    (ssh://[user@]host[:port]). It must answer, run Linux or macOS, and have Podman (4+) or Docker working."""
    check_name(name)  # as stacks name it (node: NAME)
    if "." in name:
        raise SystemExit(f"invalid host name {name!r} (lowercase letters, digits, _ and -)")
    if name in _nodes()["nodes"]:
        raise SystemExit(f"node {name!r} already exists")
    try:
        if (url or "").startswith("https://"):
            u = urllib.parse.urlsplit(url)
            u.port  # ValueError when not a number
            if not u.hostname or "@" in u.netloc or u.path not in ("", "/") or u.query or u.fragment:
                raise ValueError
            fp = (fingerprint or "").lower().replace(":", "")
            if not re.fullmatch(r"[0-9a-f]{64}", fp):
                raise SystemExit("--fingerprint: the SHA-256 shown by terminal-ct agent on that PC (64 hex digits)")
            token = token if token is not None else _read_token()
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):  # as the agent makes it (never a control character)
                raise SystemExit("token: the one shown by terminal-ct agent on that PC (TERMINAL_CT_AGENT_TOKEN, or "
                                 "typed / piped in)")
            h = {"url": f"https://{u.netloc}", "fingerprint": fp, "token": token}
        elif (url or "").startswith("ssh://"):
            ssh_target(url)
            h = {"url": url}
        else:
            raise ValueError
    except ValueError:
        raise SystemExit(f"host address: https://IP:PORT (terminal-ct agent there) or ssh://[user@]host[:port], "
                         f"not {url!r}") from None
    n = NodeRemote(name, h, 30)
    try:
        r = n._exec(["uname", "-s"], 30)
        kind = r["stdout"].strip()
        if r["exit"] or kind not in ("Linux", "Darwin"):  # its commands are POSIX: sh, tar, /tmp
            raise SystemExit(f"host {name}: Linux or macOS only (found {kind or r['stderr'].strip()[:100]}); on "
                             "Windows run the agent inside WSL")
        tried = []
        for eng in [engine] if engine else ["podman", "docker"]:  # as on this PC: Podman first, the one that works
            # info, not version: it needs the engine itself working (a client alone answers version)
            try:
                v = n._exec([eng, "info", "--format", "{{.Version.Version}} {{.Host.Security.Rootless}}"
                             if eng == "podman" else "{{.ServerVersion}}"], 30)
            except (RuntimeError, subprocess.TimeoutExpired) as e:  # hung (a stopped Podman machine): the next one
                tried.append(f"{eng}: {e}")
                continue
            got = v["stdout"].split() if not v["exit"] else []
            if eng == "podman" and got and not got[0].split(".")[0].isdigit() or \
                    eng == "podman" and got and int(got[0].split(".")[0]) < 4:
                tried.append(f"podman {got[0]}: version 4 or later needed")
                continue
            if got:
                h["engine"], version = eng, got[0]
                if eng == "podman":
                    h["rootless"] = got[1:2] == ["true"]  # keep-id is for a rootless Podman only
                break
            tried.append(f"{eng}: {(v.get('stderr') or 'no answer').strip()[:120]}")
        else:
            raise SystemExit(f"host {name}: no container engine works there ({'; '.join(tried)})")
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"host {name}: {e}") from None
    with quota_lock("nodes"):
        d = _nodes()
        if name in d["nodes"]:  # added meanwhile by another command
            raise SystemExit(f"node {name!r} already exists")
        hosts = _hosts()
        hosts[name] = h
        _save_hosts(hosts)  # first: nodes.json never names a host whose token is not there
        d["nodes"][name] = {"kind": "host", "url": h["url"], "engine": h["engine"],
                            "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save_nodes(d)
    _UP.clear()
    event("host_added", None, detail=f"{name} ({h['url']})")
    return {"host": name, "url": h["url"], "os": kind, "engine": h["engine"], "version": version}


def host_alive(name):
    """Whether another PC answers (its agent's ping, or ssh), within a few seconds."""
    try:
        n = node_obj(name, 5)
        if n.remote["url"].startswith("https://"):
            return agent_request(n.remote, "GET", "/ping", timeout=5).get("tool") == TOOL
        cmd, target = n._ssh()
        cmd = [x if x != "ConnectTimeout=10" else "ConnectTimeout=4" for x in cmd]
        return subprocess.run([*cmd, "--", target, "true"], capture_output=True, timeout=6,
                              stdin=subprocess.DEVNULL, **NO_WINDOW).returncode == 0
    except (RuntimeError, SystemExit, subprocess.TimeoutExpired, OSError, ValueError, KeyError):
        return False


def place(container, node_name):
    """Remember where a container lives, so later commands on it go to its node."""
    with quota_lock("nodes"):
        d = _nodes()
        if node_name:
            d["placement"][container] = node_name
        else:
            d["placement"].pop(container, None)
        _save_nodes(d)


def _vm(args, timeout=60):
    """terminal-vm's command line for the node commands: its failures as terminal-ct's own (no traceback)."""
    try:
        return vm_call(args, timeout=timeout)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"terminal-vm {args[0]}: {e}") from None


def node_add(name, image="debian", cpus=2, memory=2, disk=20, project=None, wait=900):
    """A node: a terminal-vm VM from a cloud image with Docker, supervised (restarted if it falls). Registered as
    soon as its VM exists, so a failure after that is cleaned up by node rm."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", name or ""):  # a container name terminal-vm also takes as a VM
        raise SystemExit(f"invalid node name {name!r} (lowercase letters, digits, _ and -)")
    if name in _nodes()["nodes"]:
        raise SystemExit(f"node {name!r} already exists")
    t0 = time.monotonic()
    made = _vm(["create", name, "--image", image, "--node", "--cpus", str(cpus), "--memory", str(memory),
                "--max-memory", str(memory),  # a node's ceiling is its size: the quota counts no headroom
                "--disk", str(disk), *(["--project", check_project(project)] if project else [])], timeout=1800)
    ready = made.get("ready_file") or "/var/lib/terminal-vm/ready"  # left in the guest once Docker answers
    with quota_lock("nodes"):
        d = _nodes()
        d["nodes"][name] = {"image": image, "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save_nodes(d)
    event("node_added", None, detail=f"{name} ({image})")
    _vm(["start", name], timeout=120)
    _vm(["supervise", name, "--restart", "always"])
    end, said = time.monotonic() + wait, 0
    while time.monotonic() < end:
        with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired):
            if vm_call(["exec", name, "--timeout", "10", "--", "test", "-f", ready], timeout=40)["exit"] == 0:
                return {"node": name, "image": image, "ready_s": round(time.monotonic() - t0, 1)}
        if time.monotonic() - t0 > said + 30:
            said = time.monotonic() - t0
            print(f"node {name}: setting up ({int(said)} s)", file=sys.stderr)
        time.sleep(0.5)  # a node from terminal-vm's template is ready ~15 s after its start: seconds count
    raise SystemExit(f"node {name} is not ready after {wait} s (its setup needs the internet): "
                     f"terminal-vm status {name}; remove it with terminal-ct node rm {name}")


# ---------- networks across nodes (phase 10) ----------
# terminal-ct network NET used by nodes = terminal-vm network "tct-NET" (its local switch: no LAN, no internet) that
# those nodes are plugged into, one fixed address each (10.77.N.x). In each node: a Docker network for NET (a plain
# bridge, so ports can be published), its containers allowed to reach their own bridge and NET's 10.77.N.0/24 only
# (DOCKER-USER rule: no internet, no other network), and NET's DNS on that bridge's gateway (see dns_serve).
NODE_NET = "tct-"  # terminal-vm network of terminal-ct network NET
NODE_DNS_DIR = "/var/lib/terminal-ct/dns"


def node_vm_nets(node):
    """{NET: {"ip", "mac", "subnet"}}: the terminal-vm networks of terminal-ct networks this node is plugged into."""
    out = {}
    for n in _vm(["net", "ls"]):
        if n.get("name", "").startswith(NODE_NET):
            for v in n.get("vms", []):
                if v.get("vm") == node:
                    a = _clean_net(n, v)
                    if a:
                        out[n["name"][len(NODE_NET):]] = a
    return out


def _clean_net(n, v):
    """One network of a node from terminal-vm's reply, only if every value is what it should be (they go into a
    script run as root in the node): a valid name, an IPv4 address inside a 10.77.N.0/24, a MAC."""
    try:
        net = check_name(n["name"][len(NODE_NET):])
        sub, ip = ipaddress.IPv4Network(n["subnet"]), ipaddress.IPv4Address(v["ip"])
        if ip not in sub or not sub.subnet_of(ipaddress.IPv4Network("10.77.0.0/16")) or sub.prefixlen != 24 or \
                not re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", str(v["mac"])):
            return None
    except (SystemExit, KeyError, TypeError, ValueError):
        return None
    return {"ip": str(ip), "mac": v["mac"], "subnet": str(sub), "bridge": f"tctn{str(sub).split('.')[2]}"}


def node_join(node, net, wait=180):
    """Plug a node into network NET (its VM restarts: network cards change on a stopped VM)."""
    node_named(node)
    if is_host(node):
        raise SystemExit(f"{node} is another PC: networks across PCs come in phase 11 bis")
    check_name(net)
    if net in node_vm_nets(node):
        return {"node": node, "network": net, "already": True}
    if not any(n.get("name") == NODE_NET + net for n in _vm(["net", "ls"])):
        _vm(["net", "create", NODE_NET + net])
    _vm(["stop", node], timeout=120)
    _vm(["net", "attach", node, NODE_NET + net])
    _vm(["start", node], timeout=120)
    end = time.monotonic() + wait
    while True:  # its agent back
        with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired, SystemExit):
            if NodeVM(node, 10).sh("docker info >/dev/null && echo up", timeout=10).strip() == "up":
                break
        if time.monotonic() > end:
            raise SystemExit(f"node {node} did not come back within {wait} s after joining {net}")
        time.sleep(3)
    got = ensure_node_nets(node)
    event("node_joined", None, detail=f"{node} on {net} ({got[net]['ip']})")
    return {"node": node, "network": net, **got[net]}


def _node_nets_script(nets):
    """The shell lines that make a node's networks so (idempotent: run at each check, after any restart)."""
    q = shlex.quote
    lines = ["set -e", "umask 022", f"mkdir -p {NODE_DNS_DIR}",
             # the DNS runs as nobody: it reads its table through these; the code stays root's alone
             f"chmod 755 {os.path.dirname(NODE_CT)} {NODE_DNS_DIR}", f"chmod 644 {NODE_CT}",
             'link_for() { for d in /sys/class/net/*; do [ "$(cat "$d/address")" = "$1" ] && basename "$d" && return; '
             "done; return 1; }",
             # first, for every node network at once (tctn*): never out of the networks between nodes, even if a
             # step below fails; and nothing reaches the host through slirp's gateway (the host's loopback)
             "iptables -C DOCKER-USER -i tctn+ ! -o tctn+ -m conntrack --ctstate NEW ! --ctorigdst 10.77.0.0/16 -j DROP "
             "2>/dev/null || iptables -I DOCKER-USER -i tctn+ ! -o tctn+ -m conntrack --ctstate NEW ! --ctorigdst "
             "10.77.0.0/16 -j DROP",
             "iptables -C DOCKER-USER -d 10.0.2.2 -j DROP 2>/dev/null || iptables -I DOCKER-USER -d 10.0.2.2 -j DROP",
             "iptables -C INPUT -i tctn+ -m conntrack --ctstate NEW -j DROP 2>/dev/null || "
             "iptables -A INPUT -i tctn+ -m conntrack --ctstate NEW -j DROP",
             "mkdir -p /run/terminal-ct",
             # containers with internet sit on docker0 too: never through it to the networks between nodes (their
             # addresses here, or a published port behind them: matched on the address first asked, before DNAT)
             "iptables -C DOCKER-USER -i docker0 -m conntrack --ctstate NEW --ctorigdst 10.77.0.0/16 -j DROP 2>/dev/null || "
             "iptables -I DOCKER-USER -i docker0 -m conntrack --ctstate NEW --ctorigdst 10.77.0.0/16 -j DROP",
             "iptables -C INPUT -i docker0 -d 10.77.0.0/16 -m conntrack --ctstate NEW -j DROP 2>/dev/null || "
             "iptables -I INPUT -i docker0 -d 10.77.0.0/16 -m conntrack --ctstate NEW -j DROP",
             "echo resolver $(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf)"]
    for net, a in sorted(nets.items()):
        name, table, pid = NET_PREFIX + net, q(f"{NODE_DNS_DIR}/{net}.json"), q(f"/run/terminal-ct/dns-{net}.pid")
        lines += ["(", "set -e"]  # each network in its own shell: one failing (a card not found) never stops the others
        lines += [
            f"IF=$(link_for {q(a['mac'])})", "ip link set \"$IF\" up",  # busybox ip has no "addr replace"
            f"ip -4 addr show dev \"$IF\" | grep -qF {q(' ' + a['ip'] + '/')} || ip addr add {q(a['ip'])}/24 dev \"$IF\"",
            # no new connection from the network to the node itself (sshd...): published ports are DNAT'd, not here
            "iptables -C INPUT -i \"$IF\" -m conntrack --ctstate NEW -j DROP 2>/dev/null || "
            "iptables -A INPUT -i \"$IF\" -m conntrack --ctstate NEW -j DROP",
            f"docker network inspect {q(name)} >/dev/null 2>&1 || docker network create --label {q(LABEL + '.net=' + net)} "
            f"--label {q(LABEL + '.node_network=1')} -o com.docker.network.bridge.name={a['bridge']} {q(name)} >/dev/null",
            f"BR={a['bridge']}",
            f"set -- $(docker network inspect -f '{{{{(index .IPAM.Config 0).Gateway}}}} {{{{(index .IPAM.Config 0).Subnet}}}}' "
            f"{q(name)}); GW=$1; SUB=$2",
            # its containers: their own bridge and this network's subnet between nodes, nothing else
            f"iptables -C DOCKER-USER -i \"$BR\" ! -o \"$BR\" -m conntrack --ctstate NEW ! --ctorigdst {q(a['subnet'])} -j DROP "
            f"2>/dev/null || iptables -I DOCKER-USER -i \"$BR\" ! -o \"$BR\" -m conntrack --ctstate NEW ! --ctorigdst "
            f"{q(a['subnet'])} -j DROP",
            # and of the node itself (INPUT: its published ports are served here): this network's DNS and the
            # node's address on this network only, never another network's address or the node's own services
            "iptables -C INPUT -i \"$BR\" -m conntrack --ctstate NEW -j DROP 2>/dev/null || iptables -I INPUT -i \"$BR\" -m conntrack --ctstate NEW -j DROP",
            "iptables -C INPUT -i \"$BR\" -d \"$GW\" -p udp --dport 53 -j ACCEPT 2>/dev/null || "
            "iptables -I INPUT -i \"$BR\" -d \"$GW\" -p udp --dport 53 -j ACCEPT",
            f"[ -f {table} ] || echo '{{}}' > {table}", f"chmod 644 {table}",
            # the pid in /run (gone at a reboot), and that pid must still be this network's DNS
            # running only if that pid is still this DNS (pids are reused after a restart)
            f"if ! {{ [ -f {pid} ] && tr '\\0' ' ' < \"/proc/$(cat {pid})/cmdline\" 2>/dev/null | "
            f"grep -qF {q('_dns ')} && tr '\\0' ' ' < \"/proc/$(cat {pid})/cmdline\" | grep -qF {table}; }}; then "
            f"nohup python3 {NODE_CT} _dns \"$GW/${{SUB#*/}}\" \"$BR\" {table} >/dev/null 2>&1 & echo $! > {pid}; fi",
            f"echo {q(net)} \"$GW\" \"$BR\" \"$SUB\"", f") || echo failed {q(net)}"]
    return "\n".join(lines)


def ensure_node_nets(node):
    """Make the node's networks so and return {NET: {ip, gateway, bridge, subnet (Docker's)}}."""
    nets = node_vm_nets(node)
    if not nets:
        return {}
    n = NodeVM(node)
    n.deploy()  # its DNS runs from this terminal_ct.py
    out, resolver = {}, None
    for line in n.sh(_node_nets_script(nets), timeout=120).splitlines():
        parts = line.split()
        if parts[:1] == ["resolver"] and len(parts) == 2:
            resolver = parts[1]  # the node's own DNS: names of the internet, for containers that have it
        elif len(parts) == 4 and parts[0] in nets:
            out[parts[0]] = dict(ip=nets[parts[0]]["ip"], gateway=parts[1], bridge=parts[2], subnet=parts[3])
    for v in out.values():
        v["resolver"] = resolver
    return out


def node_routes(name, info):
    """A container of a node on networks between nodes AND on the internet bridge: its default route goes to the
    internet bridge, which may not reach those networks; so a route to each network's 10.77.N.0/24 through that
    network's own bridge, set from the node in its namespace (it gets no capability for it). Again at each start."""
    if not isinstance(node(), NodeVM):
        return
    on = [k[len(NET_PREFIX):] for k in networks_of(info) if k.startswith(NET_PREFIX)]
    if not on or (len(on) == 1 and BRIDGE["docker"] not in networks_of(info)):
        return  # one network and offline: its default route is that network's bridge already
    joined, subnets = ensure_node_nets(node().name), node_vm_nets(node().name)
    q = shlex.quote
    node().sh(f"PID=$(docker inspect -f '{{{{.State.Pid}}}}' {q(PREFIX + name)}) && " + " && ".join(
        f"nsenter -t \"$PID\" -n ip route add {q(subnets[n]['subnet'])} via {q(joined[n]['gateway'])} 2>/dev/null"
        f" || nsenter -t \"$PID\" -n ip route change {q(subnets[n]['subnet'])} via {q(joined[n]['gateway'])}"
        for n in on if n in joined and n in subnets), timeout=60)


def node_ls():
    d, vms = _nodes(), {}
    up = nodes_up() if any(i.get("kind") == "host" for i in d["nodes"].values()) else {}
    if any(i.get("kind") != "host" for i in d["nodes"].values()):
        with contextlib.suppress(RuntimeError, ValueError, subprocess.TimeoutExpired):
            vms = {v["name"]: v for v in vm_call(["ls"])}
    desc = lambda n, kind: {"description": t} if (t := description(kind, n)) else {}
    return [dict({"node": n, "kind": "host", "url": info.get("url"), "engine": info.get("engine", "docker"),
                  "status": "running" if up.get(n) else "unreachable", **desc(n, "host")} if info.get("kind") == "host"
                 else {"node": n, "kind": "vm", "image": info.get("image"),
                       "status": vms.get(n, {}).get("status", "missing"), **desc(n, "node")},
                 containers=sorted(c for c, where in d["placement"].items() if where == n))
            for n, info in sorted(d["nodes"].items())]


def node_rm(name, force=False):
    """Remove a node and its VM. Its containers: refused, or with force removed first (their data goes too)."""
    node_named(name)
    on = sorted(c for c, where in _nodes()["placement"].items() if where == name)
    if on and not force:
        raise SystemExit(f"node {name} still has containers ({', '.join(on)}): rm them, or node rm {name} --force")
    host = is_host(name)
    if host:  # another PC: nothing of it to remove but its containers here and its entry
        status = "running" if host_alive(name) else "unreachable"
    else:
        status = {v["name"]: v for v in _vm(["ls"])}.get(name, {}).get("status", "missing")
    if on and status == "running":
        before, _LOCAL.node = getattr(_LOCAL, "node", None), node_obj(name)
        try:
            for c in on:
                with contextlib.suppress(SystemExit):
                    quiet(rm, c)
        finally:
            _LOCAL.node = before
    for c in on:  # their state on this host, whether rm reached the node or not: never inherited by a new one
        shutil.rmtree(os.path.join(STORE, c), ignore_errors=True)
        set_description("container", c, "")
    set_description("host" if host else "node", name, "")
    if not host and status not in ("stopped", "missing"):
        _vm(["stop", name, "--force"])
        time.sleep(1)
    if not host and status != "missing":  # its VM already removed by hand: only the registry is left to clean
        _vm(["rm", name], timeout=120)
    with quota_lock("nodes"):
        d = _nodes()
        d["nodes"].pop(name, None)
        d["placement"] = {c: n for c, n in d["placement"].items() if n != name}
        _save_nodes(d)
        if host:  # after: a reader never finds the host without its token (a token left alone is overwritten)
            hosts = _hosts()
            hosts.pop(name, None)
            _save_hosts(hosts)
    if host:  # another machine added later under this name is approved for nothing, and has no quota of this one
        with quota_lock("stacks"):
            for sn in stacks():
                with contextlib.suppress(SystemExit, OSError, ValueError):
                    st_ = load_stack(sn)
                    if name in st_.get("approved_hosts", ()):
                        st_["approved_hosts"] = [h for h in st_["approved_hosts"] if h != name]
                        _save_stack(st_)
        with contextlib.suppress(OSError):
            os.remove(os.path.join(STORE, f"quota-{name}.json"))
    _DEPLOYED.discard(name)  # a new node of that name has no terminal-ct yet, nor these
    _NET_CHECKED.pop(name, None)
    for k in [k for k in _DNS_SENT if k[0] == name]:
        _DNS_SENT.pop(k)
    event("node_removed", None, detail=name)
    return {"removed": name, "containers": on}


def node():
    global NODE
    if getattr(_LOCAL, "node", None):
        return _LOCAL.node
    if NODE is None:
        NODE = Node()
    return NODE


def normalize(c):
    """One container from `inspect`, the same shape for Docker and Podman."""
    labels = (c.get("Config") or {}).get("Labels") or {}
    st = c.get("State") or {}
    cpus, mem = allotted(c, None)
    nets = networks_of(c)
    name = labels.get(f"{LABEL}.name") or c.get("Name", "").lstrip("/").removeprefix(PREFIX)
    desc = description("container", name) or service_description(labels.get(f"{LABEL}.stack"),
                                                                  labels.get(f"{LABEL}.service"))
    return {"name": name, **({"description": desc} if desc else {}),
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
        return any(b in networks_of(c) for b in BRIDGE.values())  # listed here, it may be another engine's
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
    infos = []
    if ids:  # one removed between ps and inspect (a stack scaled down, a ttl): left out, the others listed
        r = node().run(["inspect", "--type", "container", *(["--size"] if size else []), *ids], check=False)
        try:
            infos = json.loads(r.stdout.strip() or "[]")
        except ValueError:
            infos = None
        gone = "no such" in (r.stderr or "").lower()
        if not isinstance(infos, list) or r.returncode and not infos and not gone:  # the engine failing, not one gone
            raise SystemExit((r.stderr or r.stdout or f"{node().engine} inspect failed").strip())
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
           desktop=False, width=1280, height=800, sandbox=False, internet=None, nets=(), project=None, labels=None,
           own_volume=False, ports=()):
    """Internet: on by default for the user's containers; sandboxes and agents' containers start offline.
    labels: more terminal-ct.* labels (a stack's service and replica)."""
    check_name(name)
    project = check_project(project or DEFAULT_PROJECT)
    where = _nodes()["placement"].get(name)  # one NAME across host and nodes: they share STORE/NAME
    if where and not (isinstance(node(), NodeVM) and node().name == where):
        raise SystemExit(f"{name!r} already exists (in node {where})")
    recover(name)  # an interrupted remake's container is still NAME's: never create over it
    if node().run(["inspect", "--type", "container", PREFIX + name], check=False).returncode == 0:
        raise SystemExit(f"{name!r} already exists (engine name {PREFIX}{name})")
    if isinstance(node(), NodeRemote) and (desktop or nets or ports):
        raise SystemExit(f"a container on another PC ({node().name}): no desktop, networks or ports yet "
                         "(phase 11 bis)")
    if desktop:
        # shortcut: the VNC unix socket does not cross into Windows, and a node's paths would be Windows ones;
        # run terminal-ct inside WSL there
        if WINDOWS:
            raise SystemExit("desktop containers need a Linux host or WSL (the screen is reached through a unix socket)")
        if not (640 <= width <= 7680 and 480 <= height <= 4320):
            raise SystemExit("invalid --width/--height")
        if cmd:
            raise SystemExit("a desktop container runs its desktop: start programs from it, or with exec")
    cpus, memory = cpus or DEFAULT_CPUS, memory or DEFAULT_MEMORY
    if cpus <= 0 or memory < 0.0625:
        raise SystemExit("--cpus must be > 0 and --memory at least 0.0625 GiB (64 MiB)")
    ports = [int(p) for p in ports or ()]
    if ports and (not isinstance(node(), NodeVM) or not nets or not all(1 <= p <= 65535 for p in ports)):
        raise SystemExit("ports (1-65535) are for a container on a network of nodes: published on that node's "
                         "address of the network (--node N --net NET)")
    if nets and isinstance(node(), NodeVM):  # the node's networks made so (address, firewall, DNS) first
        joined = ensure_node_nets(node().name)
        missing = [n for n in nets if n not in joined]
        if missing:
            raise SystemExit(f"node {node().name} is not on {', '.join(missing)}: terminal-ct node join {node().name} NET")
    known = networks()
    for n in nets:
        if n not in known:
            raise SystemExit(f"no network {n!r}; create it with: terminal-ct net create {n}")
    # own_volume: a stack's replica remade (its configuration changed) takes back its own /data
    if not sandbox and not own_volume and node().run(["volume", "inspect", f"{PREFIX}{name}-data"], check=False).returncode == 0:
        # never hand an earlier container's data to a new owner (an agent could then delete it unasked)
        raise SystemExit(f"a volume {PREFIX}{name}-data is left from an earlier {name}: keep it under another name, "
                         f"or remove it: {node().engine} volume rm {PREFIX}{name}-data")
    quota_check({"disk_gb": 0})  # what a container will write is unknown: refuse once the disk quota is reached
    internet = not sandbox and created_by == "user" if internet is None else internet
    spec = {"image": image, "cmd": cmd, "desktop": desktop, "width": width, "height": height, "sandbox": sandbox,
            **({"ports": ports} if ports else {})}
    out = _make(name, spec, created_by, cpus, memory, internet, nets, project, fresh=True, labels=labels)
    want(name, "stopped")
    print(json.dumps(out))


def ensure_image(spec):
    """Pull or build what the spec runs on (slow: outside the quota lock)."""
    if spec.get("desktop") and not spec.get("prebuilt"):
        desktop_image(spec["image"])
    else:
        image_cmd(spec["image"])


def _make(name, spec, created_by, cpus, memory, internet, nets, project, fresh=False, labels=None):
    """Create the engine container from its spec (kept in a label: a sandbox is recreated from it at each start)."""
    volume, extra = f"{PREFIX}{name}-data", []  # /data is kept when the container is recreated, removed by rm
    image, cmd, sandbox, podman = spec["image"], spec.get("cmd"), spec.get("sandbox"), node().engine == "podman"
    if spec.get("desktop"):
        image = image if spec.get("prebuilt") else desktop_image(image)  # prebuilt: a committed desktop
        # LANG: without UTF-8, xterm wrote é as Latin-1
        extra += ["-e", "LANG=C.UTF-8", "-e", f"TCT_WIDTH={spec['width']}", "-e", f"TCT_HEIGHT={spec['height']}",
                  "-v", f"{os.path.join(ct_dir(name), 'run')}:/run/tct"]
        if isinstance(node(), NodeVM):  # there, the engine would make it as root: the desktop's user could not write
            uid, gid = (1000, 1000) if WINDOWS else (os.getuid(), os.getgid())
            run = shlex.quote(os.path.join(ct_dir(name), "run"))
            node().sh(f"mkdir -p {run} && chown {uid}:{gid} {run} && chmod 700 {run}", timeout=30)
        cmd = ["sh", "-c", DESKTOP_RUN]
    else:  # (below) a container on networks of nodes: their DNS, and its ports on the node's address of each
        entry, default = image_cmd(image)
        # a bare shell as the main process exits at once without a terminal: keep such containers alive instead
        if not cmd and not entry and (not default or default[0] in SHELLS):
            cmd = KEEP_ALIVE
    # desktops and sandboxes run as the host user (podman: same uid inside): the VNC socket is the user's alone, and
    # a sandbox is not root even inside; /data is their home
    as_user = spec.get("desktop") or sandbox
    uid, gid = (1000, 1000) if WINDOWS else (os.getuid(), os.getgid())  # shortcut: no uid on Windows, typical one
    if as_user:
        keep_id = podman and getattr(node(), "rootless", True)  # a rootful Podman refuses keep-id
        extra += [*(["--userns=keep-id"] if keep_id else []), "--user", f"{uid}:{gid}", "-e", "HOME=/data"]
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
    if nets and isinstance(node(), NodeVM):
        joined = ensure_node_nets(node().name)
        for n in nets:
            extra += ["--dns", joined[n]["gateway"], *(x for p in spec.get("ports") or () for x in
                                                     ("-p", f"{joined[n]['ip']}:{p}:{p}"))]
        if internet and joined[nets[0]].get("resolver"):  # after the networks' names: the internet's
            extra += ["--dns", joined[nets[0]]["resolver"]]
    args = ["create", "--name", PREFIX + name, "--hostname", name, "--init", "--network", BRIDGE[node().engine],
            # every label set, 0 included: a committed image carries the old container's labels into the new one
            "--label", f"{LABEL}.name={name}", "--label", f"{LABEL}.created_by={created_by}",
            "--label", f"{LABEL}.project={project}", "--label", f"{LABEL}.sandbox={int(bool(sandbox))}",
            "--label", f"{LABEL}.desktop={int(bool(spec.get('desktop')))}",
            "--label", f"{LABEL}.spec={json.dumps(spec, separators=(',', ':'))}",
            *(x for k, v in (labels or {}).items() for x in ("--label", f"{LABEL}.{k}={v}")), *data, *extra,
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
    keep = (keep or needs_upgrade(info)) and "image" not in change  # a given image (snapshot load) replaces it
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
              internet, nets, project, labels={k: lab[f"{LABEL}.{k}"] for k in STACK_LABELS if f"{LABEL}.{k}" in lab})
    except BaseException:
        node().run(["rm", "-f", PREFIX + name], check=False)
        node().run(["rename", old, PREFIX + name], check=False)
        raise
    node().run(["rm", "-f", old])
    return find(name)


# ---------- desktop image ----------
DESKTOP_ENTRY = "/usr/local/bin/tct-desktop"
# Xvnc listens only on a unix socket in a host folder only the user can open: no TCP port, no password to keep.
# The desktop: given to each container as its command (sh -c), so a change here reaches new and remade containers
# without rebuilding the cached images (whose /usr/local/bin/tct-desktop holds the first version).
DESKTOP_RUN = """rm -f /run/tct/vnc.sock /tmp/.X0-lock /tmp/.X11-unix/X0
Xvnc :0 -geometry "${TCT_WIDTH:-1280}x${TCT_HEIGHT:-800}" -depth 24 -rfbport -1 -rfbunixpath /run/tct/vnc.sock \\
     -SecurityTypes None -AlwaysShared -desktop "$(hostname)" &
export DISPLAY=:0
for i in 1 2 3 4 5 6 7 8 9 10; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.2; done
openbox --startup 'xterm -maximized' &  # filling the screen, also after the viewer resizes it
trap 'trap "" TERM INT; kill 0 2>/dev/null; wait; exit 0' TERM INT  # a requested stop exits 0, not as a crash
wait
"""
# (X locks removed first: a file system committed while the desktop ran (snapshot, clone) kept them, their PID was
# alive again in the new container, and Xvnc refused to start. openbox and xterm started together left xterm's
# window unmanaged, not drawn until a click: hence --startup.)
ENTRY_SCRIPT = "#!/bin/sh\n" + DESKTOP_RUN
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
            if isinstance(node(), NodeVM) and any(k.startswith(NET_PREFIX) for k in networks_of(info)):
                ensure_node_nets(node().name)  # after a node restart its rules are gone: back before it runs
            node().run(["start", PREFIX + name])
            node_routes(name, info)
    want(name, "running")
    if labels_of(info).get(f"{LABEL}.desktop") == "1":
        with contextlib.suppress(RuntimeError):
            ready = act(name, "ready", {})
            if ready is not None:
                out["desktop_ready"] = ready
    print(json.dumps(out))


def desktop_ready(name, timeout=10):
    """Seconds until the desktop shows a window (1% of the screen not black; the pointer alone is drawn into the
    image), so input lands on it: an empty screen looks "stable" too. None if not within timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            fb = _pixels(name)
            if len(fb) - fb.count(0) > len(fb) // 100:
                return round(time.time() - t0, 1)
        except RuntimeError:  # socket not there yet
            pass
        time.sleep(0.25)
    return None


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
    for tag in snapshots(name):
        node().run(["image", "rm", f"{SNAP}{name}:{tag}"], check=False)
    shutil.rmtree(os.path.join(STORE, name), ignore_errors=True)
    if isinstance(node(), NodeVM):
        place(name, None)
    set_description("container", name, "")  # a new container of this name says nothing it did not say itself
    print(json.dumps({"removed": name}))


def status(name):
    print(json.dumps(normalize(find(name))))


def everywhere():
    """[(node or None, raw containers there)]: this host's engine, then each running node (one not answering is
    left out of this view)."""
    out = [(None, raw_containers())]
    for n in sorted(n for n, ok in nodes_up().items() if ok):
        with contextlib.suppress(SystemExit), on_node(n, LOOK_TIMEOUT):
            out.append((n, raw_containers()))
    return out


def all_containers():
    """containers() of this host and of the nodes, each node's marked with "node"."""
    return [dict(normalize(c), **({"node": where} if where else {})) for where, cs in everywhere() for c in cs]


def ls():
    print(json.dumps(all_containers(), indent=1))


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


def _quota_path():
    """This PC's quota.json; another PC's quota-NAME.json (terminal-ct --host NAME quota ...)."""
    return os.path.join(STORE, f"quota-{node().name}.json" if isinstance(node(), NodeRemote) else "quota.json")


def _quota_file():
    try:
        with open(_quota_path()) as f:
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
        if vols and not WINDOWS and not isinstance(node(), NodeVM) and all(os.path.isdir(p) for p in vols.values()):
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
    total += sum(keep_bytes(labels_of(c).get(f"{LABEL}.name")) + sum(snapshots(labels_of(c).get(f"{LABEL}.name")).values())
                 for c in infos)
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


def quota_usage(disk=True, infos=None):
    infos = raw_containers(size=disk) if infos is None else infos
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
    of the space containers can have (free now plus what they hold), 16 running containers. In a node: its whole
    size, and never the host's quota file (terminal-vm's quota already fences its VM)."""
    cpus, mem, root = node().host()
    free = node().free_bytes(root)
    own = type(node()) is NodeVM  # a VM node is all theirs; another PC is someone's computer, as this one is
    set_ = {} if own else _quota_file()  # another PC: its own quota (quota-NAME.json), never this PC's
    default = {"cpus": cpus if own else max(1, cpus - 1),
               "memory_gb": max(1, round(mem)) if own else max(1, int(mem * 3 // 4)),  # a 1 GiB VM shows ~0.95
               "disk_gb": int((free / 2 ** 30 + use.get("disk_gb", 0)) // 2), "containers": 16}
    return {k: set_.get(k, default[k]) for k in QUOTA_KEYS}, {k: k in set_ for k in QUOTA_KEYS}


class quota_lock:
    """Cross-process lock (an atomic mkdir), so two commands cannot both pass the check and then both start.
    what: another lock of the same kind (reconcile)."""
    def __init__(self, what="quota"):
        self.what = what

    def __enter__(self):
        os.makedirs(STORE, exist_ok=True)
        self.path = os.path.join(STORE, f".{self.what}.lock")
        for _ in range(300):
            try:
                os.mkdir(self.path)
                self.token = os.path.join(self.path, f"{os.getpid()}-{secrets.token_hex(4)}")
                open(self.token, "w").close()
                self.held = threading.Event()

                def fresh():  # held for long (an image build under it): never "stale" while its holder lives
                    while not self.held.wait(30):
                        with contextlib.suppress(OSError):
                            os.utime(self.path)
                threading.Thread(target=fresh, daemon=True).start()
                return self
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - os.path.getmtime(self.path) > 120:  # left by a crashed command
                        # rename first: of two waiters seeing it stale, only one claims it (rmdir alone let the
                        # second remove the first's fresh lock)
                        stale = f"{self.path}.{os.getpid()}"
                        os.rename(self.path, stale)
                        shutil.rmtree(stale)
                time.sleep(0.1)
        raise SystemExit(f"another terminal-ct command holds the {self.what} lock; try again")

    def __exit__(self, *exc):
        self.held.set()
        with contextlib.suppress(OSError):  # only our own: if it was broken and taken, the token is not there
            os.remove(self.token)
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


def quota(set_=None, infos=None):
    """Show the quota and its use, or (user only) change it. infos: raw_containers(size=True), already read."""
    if set_ and any(v is not None for v in set_.values()):
        if type(node()) is NodeVM:
            raise SystemExit("a VM node's quota is its size (terminal-vm); set the quota of this PC, or of another "
                             "PC with --host")
        if any(v is not None and v < 1 for v in set_.values()):
            raise SystemExit("quota values must be at least 1")
        with quota_lock():
            stored = _quota_file()
            stored.update({k: v for k, v in set_.items() if v is not None})
            with open(_quota_path(), "w") as f:
                json.dump(stored, f)
    use = quota_usage(infos=infos)
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


def _stats(infos):
    """{engine name: cpu_percent, used_memory_gb, pids} of running containers, from one `stats` call."""
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
    return stats


def top():
    """Real use of each running container (CPU % of one core, memory, processes) next to its ceiling, and the quota."""
    infos = [c for c in raw_containers() if running(c)]
    stats = _stats(infos)
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

def networks(members=True):
    """terminal-ct's networks: {name: {"created_by", "members"}}. members=False: not listed (one engine call less)."""
    ids = node().run(["network", "ls", "-q", "--filter", f"label={LABEL}.net"]).stdout.split()
    out = {}
    for n in node().json(["network", "inspect", *ids]) if ids else []:
        lab = n.get("Labels") or n.get("labels") or {}  # docker, podman
        if lab.get(f"{LABEL}.net"):
            desc = description("net", lab[f"{LABEL}.net"])
            out[lab[f"{LABEL}.net"]] = {"created_by": lab.get(f"{LABEL}.created_by", "user"),
                                        "project": lab.get(f"{LABEL}.project") or DEFAULT_PROJECT, "members": [],
                                        **({"description": desc} if desc else {})}
    if out and members:
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
    if not known_net(net):  # still on nodes through a stack: keep what it says
        set_description("net", net, "")
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
            if wanted(n) == "running" and not running(c) and not labels_of(c).get(f"{LABEL}.stack"):
                try:
                    out.append(dict(quiet(start, n), name=n, engine=n_.engine))
                except (SystemExit, Exception) as e:  # one refusal (quota) must not keep the others down
                    out.append({"name": n, "engine": n_.engine, "error": str(e)})
    if stacks():  # the daemon brings the stacks' containers back to their desired state
        out.append({"daemon": daemon("start")})
    print(json.dumps({"resumed": out}))
    while stay and (daemon_alive() or any(running(c) for _ in each_engine() for c in raw_containers())):
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
                # resume exits at once: without these, systemd would kill what it started (same cgroup: podman's
                # conmon) as the unit ends
                f"[Service]\nType=oneshot\nRemainAfterExit=yes\nKillMode=process\n" +"".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in env.items())
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
    mine = [c for c in containers() if under(c["project"], project)]
    own = {c["name"] for c in mine}
    return {"project": project, **({"description": t} if (t := description("project", project)) else {}),
            "containers": [dict(c, want=wanted(c["name"])) for c in mine],
            "networks": {k: dict(v, members=[m for m in v["members"] if m in own])
                         for k, v in networks().items() if under(v["project"], project)},
            "sub_projects": sorted(p["project"] for p in projects() if p["project"].startswith(project + "/")),
            "notes": notes, "recent_actions": log}


def notes_write(project, text, mode="append"):
    path = os.path.join(project_dir(project), "notes.md")
    with open(path, "a" if mode == "append" else "w", encoding="utf-8") as f:
        f.write((f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n" if mode == "append" else "") + text.rstrip() + "\n")
    return {"project": project or DEFAULT_PROJECT, "notes": mode, "bytes": os.path.getsize(path)}


# ---------- descriptions (phase 12) ----------
# One line on each object (what it is for, who uses it), so an agent arriving in the infra knows who does what.
# Kept in one central file, so it is right for containers on nodes and other PCs too, and can change at any time
# (a label could not without recreating the container). A stack's services carry theirs in the stack file.
DESC_MAX = 200
DESC_KINDS = ("container", "net", "node", "host", "project")
_DESC = {}  # (path, mtime, size) -> its content: read once per change, not once per container listed


def check_description(text):
    if not isinstance(text, str) or len(text) > DESC_MAX or not text.isprintable():
        raise SystemExit(f"a description is one line of at most {DESC_MAX} printable characters")
    return text.strip()


def descriptions():
    """{"KIND:NAME": text}."""
    path = os.path.join(STORE, "descriptions.json")
    try:
        st = os.stat(path)
        key = (path, st.st_ino, st.st_mtime_ns, st.st_size)
        d = _DESC.get(key)
        if d is None:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            d = d if isinstance(d, dict) else {}
            _DESC.clear()  # one file: the last read only (threads of the web dashboard may race here, harmlessly)
            _DESC[key] = d
        return d
    except (OSError, ValueError):
        return {}


def description(kind, name):
    return descriptions().get(f"{kind}:{name}", "")


def set_description(kind, name, text):
    """Set NAME's description; "" clears it. No check that NAME exists (describe does): rm uses it to forget."""
    text = check_description(text)
    with quota_lock("descriptions"):
        _DESC.clear()  # under the lock, from the file itself: never merged into a stale copy
        d = dict(descriptions())
        if text:
            d[f"{kind}:{name}"] = text
        elif d.pop(f"{kind}:{name}", None) is None:
            return {kind: name, "description": ""}
        os.makedirs(STORE, exist_ok=True)
        _write_atomic(os.path.join(STORE, "descriptions.json"), json.dumps(d, indent=1, ensure_ascii=False))
        _DESC.clear()  # same size within one clock tick would look unchanged
    return {kind: name, "description": text}


def service_description(stack, svc):
    """A replica's: its service's, from the stack file (changing it recreates nothing)."""
    if not (stack and svc):
        return ""
    with contextlib.suppress(SystemExit, OSError, ValueError, KeyError, TypeError):
        return load_stack(stack)["services"][svc].get("description") or ""
    return ""


def stack_net_owner(net, project=None):
    """Who made the stacks (of project, None: all) that put NET on nodes: "agent" when only agents' stacks use it,
    "user" when one of the user's does, None when none does."""
    owners = set()
    for s in stacks():
        with contextlib.suppress(SystemExit, OSError, ValueError):
            st = load_stack(s)
            if under(st.get("project") or DEFAULT_PROJECT, project) and \
                    any(net in (c.get("networks") or []) for c in st["services"].values()):
                owners.add(st.get("created_by", "user"))
    return None if not owners else "agent" if owners == {"agent"} else "user"


def known_net(net):
    """A network of this engine, or one a stack puts on nodes (it lives in each node's engine)."""
    return net in networks() or stack_net_owner(net) is not None


def describe(kind, name, text):
    """describe KIND NAME TEXT: the object must exist (a project only needs a valid name)."""
    if kind not in DESC_KINDS:
        raise SystemExit(f"describe one of: {', '.join(DESC_KINDS)}")
    if kind == "project":
        check_project(name)
    elif kind in ("node", "host"):
        if name not in _nodes()["nodes"] or is_host(name) != (kind == "host"):
            raise SystemExit(f"no {kind} named {name!r} (terminal-ct {kind} ls)")
    elif kind == "net":
        if not known_net(check_name(name)):
            raise SystemExit(f"no network {name!r} (terminal-ct net ls)")
    else:
        where = _nodes()["placement"].get(check_name(name))
        with on_node(where) if where else contextlib.nullcontext():
            stack = labels_of(find(name)).get(f"{LABEL}.stack")
        if stack:  # the daemon remakes replicas: the description goes with the service
            raise SystemExit(f"{name} is a replica of stack {stack}: put a description on its service in the stack "
                             "file, then apply it")
    return set_description(kind, name, text)


def project_move(name, project):
    """Move a stopped container to another project, keeping its file system and /data."""
    info = find(name)
    # a sandbox starts from its image anyway: committing would make whatever is in it now its new image
    remake(name, info, keep=labels_of(info).get(f"{LABEL}.sandbox") != "1", project=check_project(project))
    return {"name": name, "project": project}


# ---------- orchestration: stacks, reconciliation, health, events ----------
# A stack is a desired state (STORE/stacks/NAME.json, from `apply FILE`): services, each with its replicas
# (containers NAME-SERVICE-1..N, labelled stack, service, replica and a hash of their configuration). `reconcile`
# makes the engine match it: creates what is missing, removes the surplus, recreates what drifted, starts again
# what stopped (restart policy, exponential backoff), removes what outlived its ttl; the daemon does that every
# few seconds and runs the health probes. Everything it does goes to the event log.
STACK_LABELS = ("stack", "service", "replica", "hash")
SERVICE_KEYS = {"image", "cmd", "replicas", "cpus", "memory_gb", "desktop", "width", "height", "sandbox", "internet",
                "networks", "restart", "health", "ttl", "node", "spread", "ports", "description"}
RECREATE_KEYS = ("image", "cmd", "desktop", "width", "height", "sandbox", "internet", "networks", "ports")  # cpus, memory: live
RESTARTS = ("always", "on-failure", "never")
TICK = 3  # seconds between the daemon's passes
PASS = [None]  # when the daemon's current pass began (None outside the daemon: each reconcile is its own pass)
BACKOFF_MAX = 300


def stacks_dir():
    d = os.path.join(STORE, "stacks")
    os.makedirs(d, exist_ok=True)
    return d


def stacks():
    return sorted(f[:-5] for f in os.listdir(stacks_dir()) if f.endswith(".json") and not f.startswith("."))


def load_stack(name):
    try:
        with open(os.path.join(stacks_dir(), check_name(name) + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"no stack named {name!r}; apply one: terminal-ct apply FILE") from None


def _save_stack(s):
    path = os.path.join(stacks_dir(), s["stack"] + ".json")
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    os.replace(path + ".new", path)  # never a half-written desired state for the daemon to read


def parse_ttl(v):
    """Seconds from 90, "90s", "30m", "2h", "1d"; None for no ttl."""
    if v in (None, "", 0):
        return None
    m = re.fullmatch(r"(\d+)([smhd]?)", str(v).strip())
    if not m or int(m.group(1)) <= 0:
        raise SystemExit(f"invalid ttl {v!r}: a number of seconds, or 30m, 2h, 1d")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def check_stack(s):
    """The stack file, validated and with defaults filled in. Errors name the field."""
    if not isinstance(s, dict) or not isinstance(s.get("services"), dict) or not s["services"]:
        raise SystemExit('a stack is {"stack": NAME, "services": {SERVICE: {...}, ...}}')
    name = check_name(s.get("stack"))
    out = {"stack": name, "services": {}}
    if s.get("project"):
        out["project"] = check_project(s["project"])
    for svc, c in s["services"].items():
        check_name(svc)
        if not isinstance(c, dict) or set(c) - SERVICE_KEYS:
            raise SystemExit(f"service {svc}: unknown keys {sorted(set(c) - SERVICE_KEYS) if isinstance(c, dict) else c}"
                             f" (allowed: {', '.join(sorted(SERVICE_KEYS))})")
        c = dict(c)
        c["replicas"] = c.get("replicas", 1)
        if not isinstance(c["replicas"], int) or not 0 <= c["replicas"] <= 100:
            raise SystemExit(f"service {svc}: replicas must be 0 to 100")
        check_name(f"{name}-{svc}-{max(c['replicas'], 1)}")  # the longest replica name must be a valid name
        c["restart"] = c.get("restart", "always")
        if c["restart"] not in RESTARTS:
            raise SystemExit(f"service {svc}: restart is one of {', '.join(RESTARTS)}")
        parse_ttl(c.get("ttl"))
        if c.get("description") is not None:
            c["description"] = check_description(c["description"])
        if c.get("cmd") is not None and not (isinstance(c["cmd"], list) and all(isinstance(x, str) for x in c["cmd"])):
            raise SystemExit(f"service {svc}: cmd is a list of strings")
        for n in c.get("networks") or []:
            check_name(n)
        if c.get("node") is not None:
            check_name(c["node"])
            if is_host(c["node"]) and (c.get("desktop") or c.get("networks")):
                raise SystemExit(f"service {svc}: {c['node']} is another PC: no desktop or networks there yet "
                                 "(phase 11 bis)")
        if c.get("spread") not in (None, "nodes"):
            raise SystemExit(f'service {svc}: spread is "nodes" (its replicas across the nodes)')
        if c.get("node") and c.get("spread"):
            raise SystemExit(f"service {svc}: node (all replicas there) or spread, not both")
        if c.get("ports") is not None:
            if not (isinstance(c["ports"], list) and c["ports"] and
                    all(isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 for p in c["ports"])):
                raise SystemExit(f"service {svc}: ports is a list of port numbers")
            if not ((c.get("node") or c.get("spread")) and c.get("networks")):
                raise SystemExit(f"service {svc}: ports are for a service on networks of nodes (node or spread, "
                                 "and networks)")
            if c.get("node") and c["replicas"] > 1:
                raise SystemExit(f"service {svc}: with ports, one replica per node: spread it, or 1 replica")
        h = c.get("health")
        if h is not None:
            if not isinstance(h, dict) or not isinstance(h.get("exec"), str):
                raise SystemExit(f'service {svc}: health is {{"exec": "COMMAND", "every": 10, "retries": 3, "grace": 30}}')
            h = {"exec": h["exec"], "every": max(2, int(h.get("every", 10))), "retries": max(1, int(h.get("retries", 3))),
                 "grace": max(0, int(h.get("grace", 30)))}  # no probe in the first seconds: a desktop takes a while
            c["health"] = h
        out["services"][svc] = c
    return out


def service_hash(c):
    """What a replica is made from: a change here recreates it (replicas, restart, health, ttl do not)."""
    return hashlib.sha256(json.dumps({k: c.get(k) for k in RECREATE_KEYS}, sort_keys=True).encode()).hexdigest()[:12]


def _replace(tmp, path):
    """os.replace, retried: on Windows it fails while another process has the file open for a moment."""
    for i in range(20):
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def _write_atomic(path, text):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.new"  # one per writer (threads of the daemon too)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    _replace(tmp, path)


def event(kind, stack=None, service=None, container=None, detail=None):
    """One line of the event log (STORE/events.log): what the reconciler, the daemon or a probe did or saw."""
    e = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": kind,
         **{k: v for k, v in (("stack", stack), ("service", service), ("container", container), ("detail", detail)) if v}}
    path = os.path.join(STORE, "events.log")
    with contextlib.suppress(OSError):
        os.makedirs(STORE, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")
        if os.path.getsize(path) > 4 * 2 ** 20:  # a daemon runs for months: keep the latest lines
            with open(path, encoding="utf-8") as f:
                keep = f.readlines()[-10000:]
            _write_atomic(path, "".join(keep))
    return e


def events(stack=None, n=50, project=None):
    try:
        with open(os.path.join(STORE, "events.log"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []
    own = None if project is None else {s for s in stacks() if under(load_stack(s).get("project") or DEFAULT_PROJECT, project)}
    rows = [e for e in rows if (stack is None or e.get("stack") == stack) and (own is None or e.get("stack") in own)]
    return rows[-n:]


def follow_events(stack=None):
    """Print new events as they come; a rotation of the log starts over at its beginning."""
    path, pos = os.path.join(STORE, "events.log"), 0
    while True:
        with contextlib.suppress(OSError):
            if os.path.getsize(path) < pos:
                pos = 0
            with open(path, encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    if line.endswith("\n"):
                        e = json.loads(line)
                        if stack is None or e.get("stack") == stack:
                            print(json.dumps(e), flush=True)
                        pos += len(line.encode("utf-8"))
        time.sleep(1)


def _state():
    """The reconciler's memory per container (first creation, restarts, backoff, health, expired), across passes.
    Read and written under the reconcile lock only."""
    try:
        with open(os.path.join(stacks_dir(), ".state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    _write_atomic(os.path.join(stacks_dir(), ".state.json"), json.dumps(st))


def _time(t):
    """Seconds since the epoch from an engine's time (Docker: UTC, Z; Podman: local time and its offset)."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?", t or "")
    if not m or m.group(1) == "0001":
        return None
    utc = calendar.timegm(tuple(int(x) for x in m.groups()[:6]) + (0, 0, 0))
    off = (m.group(7) or "Z").replace(":", "")
    return utc - (0 if off == "Z" else (1 if off[0] == "+" else -1) * (int(off[1:3]) * 3600 + int(off[3:5]) * 60))


def _started(c):
    """When the container last started (None if never)."""
    return _time((c.get("State") or {}).get("StartedAt"))


@contextlib.contextmanager
def on_node(name, timeout=None):
    """Engine calls inside go to that node (None: the stack's own engine); the previous engine after.
    timeout: per call, for listings (a frozen node answers nothing: give up quickly)."""
    before = getattr(_LOCAL, "node", None)
    if name:
        _LOCAL.node = node_obj(name, timeout)
    try:
        yield
    finally:
        _LOCAL.node = before


LOOK_TIMEOUT = 15  # seconds for a listing call in a node
UP_MAX_AGE = 2  # seconds a nodes_up() answer is reused
_UP = {}


def nodes_up():
    """Registered nodes and whether each one runs: a VM by one terminal-vm ls, another PC by its answer (all at
    once, a few seconds at most); reused for UP_MAX_AGE seconds."""
    names = _nodes()["nodes"]
    if not names:
        return {}
    if time.time() - _UP.get("t", 0) > UP_MAX_AGE or set(_UP.get("up", ())) != set(names):
        hosts = [n for n, i in names.items() if i.get("kind") == "host"]
        alive = {}
        threads = [threading.Thread(target=lambda h=h: alive.__setitem__(h, host_alive(h)), daemon=True)
                   for h in hosts]
        for t in threads:
            t.start()
        vms = {}
        if len(hosts) < len(names):
            try:
                vms = {v["name"]: v.get("status") for v in vm_call(["ls"], timeout=30)}
            except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                pass
        end = time.monotonic() + 8  # all together: a silent PC costs one wait, not one each
        for t in threads:
            t.join(max(0, end - time.monotonic()))
        _UP.update(t=time.time(), up={n: alive.get(n, False) if n in hosts else vms.get(n) == "running"
                                      for n in names})
    return dict(_UP["up"])


def _target(cname, c, up, placement, load, on_nets=None):
    """Where a replica should live: None (this host), a node, or False (nowhere now: its pinned node is down,
    or no node runs). spread keeps a replica where it is while that node runs, else the least loaded node.
    on_nets: {node: its networks}; a service on networks only goes to nodes on all of them, and a service with
    ports has one replica per node (same port everywhere)."""
    if c.get("node"):
        return c["node"] if up.get(c["node"]) is not False and c["node"] in up else False
    if not c.get("spread"):
        return None
    if placement.get(cname) in up and up[placement[cname]] is not False:  # running, or not answering this pass
        return placement[cname]
    siblings = cname.rsplit("-", 1)[0] + "-"  # STACK-SERVICE-: the other replicas
    taken = {n for k, n in placement.items() if k != cname and k.startswith(siblings) and k[len(siblings):].isdigit()}
    alive = sorted(n for n, ok in up.items() if ok and set(c.get("networks") or ()) <= set((on_nets or {}).get(n, ()))
                   and not (c.get("ports") and n in taken) and not (c.get("desktop") and is_host(n)))
    if not alive:
        return False
    best = min(alive, key=lambda n: (load.get(n, 0), n))
    load[best] = load.get(best, 0) + 1
    return best


def nodes_on_nets():
    """{node: [its networks]} from one terminal-vm net ls."""
    out = collections.defaultdict(list)
    with contextlib.suppress(SystemExit):
        for n in _vm(["net", "ls"]):
            if n.get("name", "").startswith(NODE_NET):
                for v in n.get("vms", []):
                    out[v.get("vm")].append(n["name"][len(NODE_NET):])
    return dict(out)


_DNS_SENT = {}  # (node, network): the table last written there
_NET_CHECKED = {}  # node: when its networks were last made so (ensure_node_nets)


def dns_tables():
    """{network: {name: [node addresses]}}: each service of a stack on networks of nodes, as SERVICE and
    SERVICE.STACK, at the addresses (on that network) of the running nodes that hold its replicas."""
    placement, up = _nodes()["placement"], nodes_up()
    addr = {}  # (node, network): its address there
    with contextlib.suppress(SystemExit):
        for n in _vm(["net", "ls"]):
            for v in n.get("vms", []) if n.get("name", "").startswith(NODE_NET) else ():
                addr[(v.get("vm"), n["name"][len(NODE_NET):])] = v["ip"]
    tables = collections.defaultdict(dict)
    for stack in stacks():
        with contextlib.suppress(SystemExit):
            for svc, c in load_stack(stack)["services"].items():
                if not (c.get("node") or c.get("spread")):
                    continue
                for net in c.get("networks") or ():
                    ips = sorted({addr[(n, net)] for k, n in placement.items()
                                  if k.startswith(f"{stack}-{svc}-") and k[len(stack) + len(svc) + 2:].isdigit()
                                  and up.get(n) and (n, net) in addr})
                    tables[net][svc] = tables[net][f"{svc}.{stack}"] = ips
    return dict(tables)


def update_dns():
    """Write each node's DNS tables where they changed (atomic replace; the server re-reads it)."""
    if not _nodes()["nodes"]:
        return []
    written, tables, up = [], dns_tables(), nodes_up()
    for node, nets in nodes_on_nets().items():
        if not up.get(node):
            continue
        if time.time() - _NET_CHECKED.get(node, 0) > 30:  # after a restart its firewall rules and DNS are gone
            with contextlib.suppress(SystemExit):
                ensure_node_nets(node)
                _NET_CHECKED[node] = time.time()
        for net in nets:
            text = json.dumps(tables.get(net, {}), sort_keys=True)
            if _DNS_SENT.get((node, net)) == text:
                continue
            path = f"{NODE_DNS_DIR}/{net}.json"
            b64 = base64.b64encode(text.encode()).decode()
            with contextlib.suppress(SystemExit):
                NodeVM(node, LOOK_TIMEOUT).sh(f"echo {b64} | base64 -d > {path}.new && chmod 644 {path}.new && "
                                              f"mv {path}.new {path}", timeout=30)
                _DNS_SENT[(node, net)] = text
                written.append((node, net))
    return written


def reconcile(name, now=None):
    """One pass over a stack; returns the events it caused. The nodes' DNS tables follow."""
    with quota_lock("reconcile"):
        st, now = _state(), now or time.time()
        try:
            out = _reconcile(load_stack(name), st, now)
        finally:
            _save_state(st)
    with contextlib.suppress(Exception):  # names follow the replicas; a failure here never stops the pass
        update_dns()
    return out


def _rm_copy(name):
    """One copy of a replica on the current engine (left on a node that was down): its container and /data volume,
    not the host's state of the replica (want, snapshots), which the copy kept elsewhere still has."""
    node().run(["rm", "-f", PREFIX + name], check=False)
    node().run(["volume", "rm", f"{PREFIX}{name}-data"], check=False)


def _rm_quiet(name, keep_data=False):
    with captured(io.StringIO()):
        if not keep_data:
            return rm(name)
        node().run(["rm", "-f", PREFIX + name])  # the replica remade: same name, same /data


def _reconcile(s, st, now):
    name, owner, project = s["stack"], s.get("created_by", "user"), s.get("project") or DEFAULT_PROJECT
    done = []
    ev = lambda kind, svc=None, c=None, detail=None: done.append(event(kind, name, svc, c, detail))
    mine = lambda: {labels_of(c).get(f"{LABEL}.name"): c for c in raw_containers()
                    if labels_of(c).get(f"{LABEL}.stack") == name}
    have = collections.defaultdict(list)  # name: [(container, node where it is: None here)], a copy per place
    for n, c in mine().items():
        have[n].append((c, None))
    placement = _nodes()["placement"]
    on_nodes = any(c.get("node") or c.get("spread") for c in s["services"].values()) or \
        any(k.startswith(name + "-") for k in placement)  # it uses nodes, or did
    up = nodes_up() if on_nodes else {}
    fails = st.setdefault("-nodes", {})  # failed passes in a row per node ("-": never a container's name)
    tick = PASS[0] or now  # the daemon's pass: every stack of one pass sees the same failure, counted once

    def miss(n):
        v = fails.get(n)
        count, at = v if isinstance(v, list) else [v or 0, None]
        if at != tick:
            count += 1
        fails[n] = [count, tick]
        return count
    for n, ok in up.items():
        if ok:
            try:
                with on_node(n, LOOK_TIMEOUT):
                    found = mine()
                fails.pop(n, None)
                for k, c in found.items():
                    have[k].append((c, n))
            except SystemExit:  # VM running, its agent or Docker not answering: a hiccup, or frozen
                up[n] = False if miss(n) >= 3 else None  # None: left as it is this pass; down from the third
        elif ok is False and is_host(n):  # another PC not answering: Wi-Fi, sleep, an agent restarting, until the
            up[n] = False if miss(n) >= 3 else None  # third pass in a row (moving a replica starts it with an empty /data)
    hosts = {n for n, i in _nodes()["nodes"].items() if i.get("kind") == "host"}
    may = up if owner != "agent" else {n: v for n, v in up.items()  # an agent: other PCs the user approved only
                                       if n not in hosts or n in s.get("approved_hosts", ())}
    load = collections.Counter(placement.values())
    on_nets = nodes_on_nets() if any(c.get("networks") for c in s["services"].values()) and up else {}
    known = networks()
    for svc, c in s["services"].items():
        h = service_hash(c)
        blocked = None
        if (c.get("node") or c.get("spread")) and c.get("networks") and owner == "agent":
            blocked = c["networks"][0]  # networks between nodes are the user's (node join): not an agent's to use
        for n in [] if c.get("node") or c.get("spread") else c.get("networks") or []:
            if n not in known:
                net_create(n, owner, project)
                known[n] = {"created_by": owner, "project": project, "members": []}
                ev("network_created", svc, detail=n)
            elif owner == "agent" and known[n]["created_by"] != "agent" and n not in s.get("approved_networks", []):
                blocked = n  # the user's network, made since the approval: not the agent's to use
        ttl = parse_ttl(c.get("ttl"))
        for i in range(1, c["replicas"] + 1):
            cname = f"{name}-{svc}-{i}"
            try:
                copies, mem = have.pop(cname, []), st.setdefault(cname, {})
                if mem.get("expired") and not copies:
                    continue  # its ttl is over and nothing is left of it: nowhere to place it
                target = _target(cname, c, may, placement, load, on_nets)
                keep = [w for _, w in copies if w is None or w in may]  # never kept on a PC the agent may not use
                if target is False:
                    if not keep:
                        if not mem.get("pending"):  # said once, not every pass while the node is down
                            ev("pending", svc, cname,
                               f"{c['node']} is another PC not approved for the agent: apply the stack again"
                               if c.get("node") in hosts and c["node"] not in may else
                               f"node {c['node']} is down" if c.get("node") else
                               "no free node on its networks" if c.get("networks") else "no node runs")
                        mem["pending"] = True
                        for _, w in copies:  # a copy where the agent may not be: gone
                            with on_node(w):
                                _rm_copy(cname)
                        continue
                    target = keep[0]  # keep it where it still runs
                mem.pop("pending", None)
                if target and up.get(target) is None:
                    continue  # its node did not answer this pass: left as it is
                info = next((x for x, w in copies if w == target), None)
                for _, w in copies:
                    if w != target:  # placement changed, or a copy left on a node that was down: /data does not follow
                        with on_node(w):
                            _rm_copy(cname)
                        ev("moved" if info is None else "removed", svc, cname,
                           f"from {w or 'this host'} to {target or 'this host'}" if info is None
                           else f"a copy left on {w or 'this host'} (it runs on {target or 'this host'})")
                if info is None and copies:
                    mem = st[cname] = {"born": mem.get("born"), **({"expired": True} if mem.get("expired") else {})}
                if info is None and placement.get(cname) and placement[cname] != target:
                    old = placement.pop(cname)
                    place(cname, None)  # its node is down or gone: made again elsewhere, the old copy removed on return
                    if up.get(old) is not True:
                        ev("failover", svc, cname, f"node {old} is down: made again on {target or 'this host'}")
                with on_node(target):
                    if mem.get("expired"):
                        if info is not None:
                            _rm_quiet(cname)
                        continue
                    if info is not None and labels_of(info).get(f"{LABEL}.hash") != h:
                        _rm_quiet(cname, keep_data=True)
                        ev("recreated", svc, cname, "its configuration changed (its /data kept)")
                        info = None
                        mem = st[cname] = {"born": mem.get("born")}
                    if info is None:
                        if now < mem.get("next", 0):
                            continue  # backing off after a failed creation
                        if blocked:
                            ev("create_failed", svc, cname, f"network {blocked!r} is the user's: apply the stack again "
                                                            "so they can approve it"); continue
                        try:
                            with captured(io.StringIO()):
                                create(cname, c.get("image") or DEFAULT_IMAGE, c.get("cpus"), c.get("memory_gb"), c.get("cmd"),
                                       created_by=owner, desktop=bool(c.get("desktop")), width=int(c.get("width", 1280)),
                                       height=int(c.get("height", 800)), sandbox=bool(c.get("sandbox")),
                                       internet=c.get("internet"), nets=c.get("networks") or (), project=project,
                                       labels={"stack": name, "service": svc, "replica": i, "hash": h}, own_volume=True,
                                       ports=c.get("ports") or ())
                        except SystemExit as e:
                            mem["fails"] = mem.get("fails", 0) + 1
                            mem["next"] = now + min(BACKOFF_MAX, 2 ** mem["fails"])
                            ev("create_failed", svc, cname, str(e)); continue
                        mem["fails"] = 0
                        mem.setdefault("born", None)
                        mem["born"] = mem["born"] or now  # a ttl runs from the first creation (a sandbox is remade at starts)
                        if target:
                            place(cname, target)
                            placement[cname] = target
                        ev("created", svc, cname, f"on node {target}" if target else None)
                        info = find(cname)
                    mem.setdefault("born", _time(info.get("Created")) or now)
                    want_c, want_m = c.get("cpus") or DEFAULT_CPUS, c.get("memory_gb") or DEFAULT_MEMORY
                    have_c, have_m = allotted(info, None)
                    if abs(have_c - want_c) > 1e-6 or abs(have_m - want_m) > 1e-3:  # changed in place, nothing remade
                        try:
                            resources(cname, want_c, want_m)
                            ev("resized", svc, cname, f"{want_c:g} CPUs, {want_m:g} GiB")
                        except SystemExit as e:
                            ev("pending", svc, cname, str(e))
                    if ttl and now - mem["born"] > ttl:
                        _rm_quiet(cname)
                        mem["expired"] = True
                        ev("expired", svc, cname, f"ttl {c['ttl']}")
                        continue
                    started = _started(info)
                    if running(info):
                        if started and now - started > 60:
                            mem["restarts"] = 0  # a minute up: the next failure starts the backoff over
                        continue
                    status, code = (info.get("State") or {}).get("Status"), (info.get("State") or {}).get("ExitCode", 0)
                    first = status == "created" or mem.get("unhealthy", False)  # dropped once it started again
                    if not first and wanted(cname) == "stopped":
                        continue  # stopped by the user (terminal-ct stop): left so until they start it
                    if not first and (c["restart"] == "never" or c["restart"] == "on-failure" and code == 0):
                        continue  # stopped and meant to stay so (a finished job, or a policy of never)
                    if now < mem.get("next", 0):
                        continue  # backing off
                    try:
                        with captured(io.StringIO()):
                            start(cname)
                    except SystemExit as e:
                        mem["fails"] = mem.get("fails", 0) + 1
                        mem["next"] = now + min(BACKOFF_MAX, 2 ** mem["fails"])
                        ev("pending" if "quota" in str(e) else "start_failed", svc, cname, str(e)); continue
                    mem["fails"] = 0
                    mem["started_at"] = now
                    mem.pop("unhealthy", None)
                    if status == "created":
                        ev("started", svc, cname)
                    else:
                        mem["restarts"] = mem.get("restarts", 0) + 1
                        mem["next"] = now + min(BACKOFF_MAX, 2 ** mem["restarts"])
                        ev("restarted", svc, cname, f"exit code {code}, restart {mem['restarts']}")
            except SystemExit as e:  # one replica's engine error must not stop the pass: next pass tries again
                ev("reconcile_failed", svc, cname, str(e))
    for cname, copies in have.items():  # replicas no service asks for any more: removed with their /data
        try:
            for k, (info, where) in enumerate(copies):
                with on_node(where):  # the last copy takes the replica's state on this host with it
                    _rm_quiet(cname) if k == len(copies) - 1 else _rm_copy(cname)
                ev("removed", labels_of(info).get(f"{LABEL}.service"), cname, "not in the stack any more (its /data too)")
            if placement.get(cname):
                place(cname, None)
            st.pop(cname, None)
        except SystemExit as e:
            ev("reconcile_failed", None, cname, str(e))
    return done


def probe(s, now=None):
    """Health probes of a stack's running replicas, each at its service's interval, after its grace time; after
    `retries` failures in a row the replica is stopped and marked, so the next pass starts it again (unless its
    policy is never). Probes run unlocked; their results are merged into the state under the lock."""
    now, out, results = now or time.time(), [], {}
    with quota_lock("reconcile"):
        st = _state()
    probed_on_nodes = any(c.get("health") and (c.get("node") or c.get("spread")) for c in s["services"].values())
    for where in [None] + ([n for n, ok in nodes_up().items() if ok] if probed_on_nodes else []):
        try:
            with on_node(where):
                results.update(_probe(s, st, now, where))
        except SystemExit:  # a node not answering this pass: its replicas wait for the next one
            continue
    with quota_lock("reconcile"):
        st = _state()  # what reconcile wrote meanwhile stays
        for cname, (ok, cfg, svc, where) in results.items():
            mem = st.setdefault(cname, {})
            mem["probed"] = now
            mem["health_fails"] = 0 if ok else mem.get("health_fails", 0) + 1
            if mem["health_fails"] >= cfg["health"]["retries"]:
                mem.update(health_fails=0, unhealthy=cfg["restart"] != "never")
                with on_node(where), contextlib.suppress(SystemExit):
                    node().run(["stop", "-t", "5", PREFIX + cname], check=False)  # not stop(): the user did not ask
                out.append(event("unhealthy", s["stack"], svc, cname,
                                 f"{cfg['health']['retries']} failed probes: {cfg['health']['exec']}"))
        _save_state(st)
    return out


def _probe(s, st, now, where):
    """The probes due on the current engine: {name: (ok, service config, service, where)}."""
    results = {}
    for c in raw_containers():
        lab = labels_of(c)
        cfg = s["services"].get(lab.get(f"{LABEL}.service")) if lab.get(f"{LABEL}.stack") == s["stack"] else None
        h = (cfg or {}).get("health")
        cname = lab.get(f"{LABEL}.name")
        mem = st.get(cname, {})
        up = _started(c) or now
        if not h or not running(c) or now - up < h["grace"] or now - mem.get("probed", 0) < h["every"]:
            continue
        try:
            ok = quiet(exec_, cname, ["sh", "-c", h["exec"]], h["every"])["exit"] == 0
        except SystemExit:
            ok = False
        results[cname] = (ok, cfg, lab.get(f"{LABEL}.service"), where)
    return results


def apply(s, created_by="user", project=None, approved_networks=(), approved_hosts=()):
    """Store a desired state and reconcile it once now; the daemon keeps it so (started if it does not run).
    Applying again also runs again the replicas whose ttl had expired."""
    s = check_stack(s)
    if project:
        s["project"] = check_project(project)
    for svc, c in s["services"].items():  # one name per network between nodes: a stack never takes another's
        if (c.get("node") or c.get("spread")) and c.get("networks"):
            for other in stacks():
                try:
                    o = load_stack(other)["services"].get(svc) if other != s["stack"] else None
                except SystemExit:
                    continue
                if o and (o.get("node") or o.get("spread")) and set(o.get("networks") or ()) & set(c["networks"]):
                    raise SystemExit(f"service {svc!r} is already a service of stack {other} on the same network: "
                                     "its name there would answer for both; rename one")
    
    with quota_lock("stacks"):
        path = os.path.join(stacks_dir(), s["stack"] + ".json")
        if os.path.isfile(path):
            old = load_stack(s["stack"])
            if old.get("created_by", "user") != created_by:
                raise SystemExit(f"stack {s['stack']!r} belongs to the {'agent' if old.get('created_by') == 'agent' else 'user'}")
            s.setdefault("project", old.get("project"))
        s.update(created_by=created_by, engine=node().engine)
        if approved_networks:
            s["approved_networks"] = sorted(set(approved_networks))
        if approved_hosts:
            s["approved_hosts"] = sorted(set(approved_hosts))
        if not s.get("project"):
            s.pop("project", None)
        _save_stack(s)
    event("applied", s["stack"])
    try:
        with quota_lock("reconcile"):
            st = _state()
            for k in [k for k in st if k.startswith(s["stack"] + "-")]:
                st[k].pop("expired", None)
            _save_state(st)
        out = reconcile(s["stack"])
    except SystemExit as e:  # a long pass of the daemon holds the lock: it will reconcile this stack itself
        if "lock" not in str(e):
            raise
        out = [{"event": "queued", "detail": "the daemon is busy; it reconciles this stack at its next pass"}]
    return {"stack": s["stack"], "events": out, "daemon": daemon("start")}


def scale(stack, service, replicas):
    with quota_lock("stacks"):
        s = load_stack(stack)
        if service not in s["services"]:
            raise SystemExit(f"stack {stack} has no service {service!r} ({', '.join(s['services'])})")
        s["services"][service]["replicas"] = replicas
        keep = {k: s[k] for k in ("created_by", "engine", "approved_networks", "approved_hosts") if k in s}
        s = dict(check_stack(s), **keep)
        _save_stack(s)
    event("scaled", stack, service, detail=f"{replicas} replicas")
    try:
        out = reconcile(stack)
    except SystemExit as e:  # saved: the daemon reconciles it at its next pass
        if "lock" not in str(e):
            raise
        out = [{"event": "queued", "detail": "the daemon is busy; it reconciles this stack at its next pass"}]
    return {"stack": stack, "service": service, "replicas": replicas, "events": out}


def stack_show(name):
    s = load_stack(name)
    with quota_lock("reconcile"):
        st = _state()
    reps = [dict(normalize(c), **{k: v for k, v in st.get(labels_of(c).get(f"{LABEL}.name"), {}).items()
                                  if k in ("restarts", "expired", "health_fails")}, **({"node": where} if where else {}))
            for where, cs in everywhere() for c in cs if labels_of(c).get(f"{LABEL}.stack") == name]
    return {"stack": name, "project": s.get("project") or DEFAULT_PROJECT, "created_by": s.get("created_by", "user"),
            "services": {svc: dict(c, running=sum(r["status"] == "running" for r in reps
                                                  if r["name"].startswith(f"{name}-{svc}-")))
                         for svc, c in s["services"].items()},
            "containers": sorted(reps, key=lambda r: r["name"]), "events": events(name, 10)}


def stack_rm(name):
    """Remove a stack: its desired state, then its containers with their /data (networks stay: other stacks may
    use them)."""
    nets = {n for c in load_stack(name)["services"].values() for n in c.get("networks") or []}
    with quota_lock("stacks"), quota_lock("reconcile"):
        st, gone = _state(), []
        for where, cs in everywhere():  # first: an unreachable engine keeps the stack, not orphan containers
            for c in cs:
                if labels_of(c).get(f"{LABEL}.stack") == name:
                    n = labels_of(c).get(f"{LABEL}.name")
                    with on_node(where):
                        _rm_quiet(n)
                    if where:
                        place(n, None)
                    st.pop(n, None)
                    gone.append(n)
        _save_state(st)
        os.remove(os.path.join(stacks_dir(), name + ".json"))
    for n in nets:
        if not known_net(n):
            set_description("net", n, "")
    left = sorted(f"{c} (node {n})" for c, n in _nodes()["placement"].items() if c.startswith(name + "-"))
    event("stack_removed", name, detail=f"{len(gone)} containers" + (f"; left on nodes down: {', '.join(left)}"
                                                                    if left else ""))
    return {"removed": name, "containers": gone, **({"left_on_nodes_down": left} if left else {})}


def prune():
    """Images terminal-ct made for containers that are gone (kept file systems, snapshots)."""
    names = {labels_of(c).get(f"{LABEL}.name") for _ in each_engine() for c in raw_containers()}
    out = []
    for _ in each_engine():
        for line in node().run(["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]).stdout.splitlines():
            repo = line.rsplit(":", 1)[0].split("/")[-1]
            for pre in (KEEP, SNAP):
                if repo.startswith(pre) and repo[len(pre):] not in names:
                    if node().run(["image", "rm", line], check=False).returncode == 0:
                        out.append(line)
    return {"pruned": out}


# the daemon: one per STORE (it holds the daemon lock for its whole life), seen through a heartbeat file and asked
# to stop through a file: no signals, the same on Windows
def _daemon_file(kind):
    return os.path.join(STORE, f"daemon.{kind}")


def daemon_alive():
    try:
        with open(_daemon_file("beat")) as f:
            return time.time() - float(f.read().split()[0]) < 5 * TICK
    except (OSError, ValueError, IndexError):
        return False


def daemon(cmd="status"):
    if cmd == "status":
        return {"daemon": "running" if daemon_alive() else "stopped", "stacks": stacks()}
    if cmd == "stop":
        if daemon_alive():
            open(_daemon_file("stop"), "w").close()
            for _ in range(10 * TICK):
                if not daemon_alive():
                    break
                time.sleep(0.5)
        return {"daemon": "running" if daemon_alive() else "stopped"}
    if cmd == "start":
        if daemon_alive():
            return "running"
        # a daemon asked to stop may still end its pass with the lock held: wait for it, or the new one would sit on
        # the lock without a beat, and look stopped
        for _ in range(120):  # only while one is ending (its stop file there): a crashed one's lock goes stale
            if not (os.path.isdir(os.path.join(STORE, ".daemon.lock")) and os.path.exists(_daemon_file("stop"))):
                break
            time.sleep(0.5)
        with contextlib.suppress(OSError):
            os.remove(_daemon_file("stop"))
        os.makedirs(STORE, exist_ok=True)
        log = open(_daemon_file("log"), "ab")
        args = [sys.executable, os.path.abspath(__file__), "daemon", "run"]
        env = dict(os.environ, TERMINAL_CT_HOME=STORE)  # this store, however it was chosen (else: the default one)
        if WINDOWS:  # out of the parent's job too: else it dies with the terminal or MCP server that started it
            base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            try:
                subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                 creationflags=base | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:  # a job that forbids breaking away
                subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, creationflags=base)
        else:
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
        return "started"
    if cmd != "run":
        raise SystemExit("daemon run|start|stop|status")
    with quota_lock("daemon"):  # a second daemon waits, then gives up: one per store

        def beat():  # its own thread: a long pass (a desktop image build) must not make the daemon look dead
            while True:
                try:
                    _write_atomic(_daemon_file("beat"), f"{time.time()} {os.getpid()}")
                except OSError as e:  # never let the beat die while the daemon works: it would look stopped
                    print(f"{time.strftime('%H:%M:%S')} beat: {e}", file=sys.stderr, flush=True)
                time.sleep(TICK)
        threading.Thread(target=beat, daemon=True).start()
        threading.Thread(target=web_serve, daemon=True).start()
        event("daemon_started", detail=f"pid {os.getpid()}")
        while not os.path.exists(_daemon_file("stop")):
            PASS[0] = time.time()
            for name in stacks():
                try:
                    s = load_stack(name)
                    _LOCAL.node = node_for(s.get("engine"))  # each stack on the engine it was applied to
                    reconcile(name)
                    probe(s)
                except (SystemExit, Exception) as e:  # one broken stack must not stop the others
                    event("reconcile_failed", name, detail=str(e))
            time.sleep(TICK)
        with contextlib.suppress(OSError):
            os.remove(_daemon_file("stop"))
            os.remove(_daemon_file("beat"))
        with contextlib.suppress(OSError):
            os.remove(_daemon_file("web"))
        event("daemon_stopped")


# ---------- dashboards: the terminal one (dash) and its text for agents (MCP dashboard) ----------

def logs(name, n=100):
    """The container's last N lines of output (stdout and stderr)."""
    find(name)
    r = node().run(["logs", "--tail", str(max(1, min(int(n), 10000))), PREFIX + name], check=False)
    return (r.stdout or "") + (r.stderr or "")


def _age(sec):
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 120 else f"{sec // 60}m" if sec < 7200 else f"{sec // 3600}h" if sec < 172800 else f"{sec // 86400}d"


def dashboard_data(project=None, with_quota=True):
    """Everything the dashboards show: each container (state, health, stack and replica, CPU and memory used next
    to its ceiling, age, restarts, flags), the daemon, the quota (an agent's project: its use and the room left),
    the latest events."""
    groups = [(where, [c for c in cs if under(labels_of(c).get(f"{LABEL}.project") or DEFAULT_PROJECT, project)])
              for where, cs in everywhere()]
    infos, stats = groups[0][1], {}  # infos: this host's, for its quota; nodes have their own capacity
    for where, cs in groups:
        with contextlib.suppress(SystemExit), on_node(where):
            stats.update(_stats([c for c in cs if running(c)]))
    st, now = _state(), time.time()  # read only: a snapshot of the reconciler's memory
    services = {}
    for sname in stacks():
        with contextlib.suppress(SystemExit):
            services[sname] = load_stack(sname)["services"]
    rows = []
    for where, c in ((w, c) for w, cs in groups for c in cs):
        n, lab = normalize(c), labels_of(c)
        mem = st.get(n["name"], {})
        svc = services.get(lab.get(f"{LABEL}.stack"), {}).get(lab.get(f"{LABEL}.service")) or {}
        health = ("unhealthy" if mem.get("unhealthy") else f"failing {mem['health_fails']}" if mem.get("health_fails")
                  else "ok" if svc.get("health") and running(c) else "-")
        s_ = stats.get(c.get("Name", "").lstrip("/"), {})
        rows.append({"name": n["name"], "status": n["status"], "health": health, "project": n["project"],
                     "stack": "/".join(lab.get(f"{LABEL}.{k}", "") for k in ("stack", "service", "replica"))
                     if lab.get(f"{LABEL}.stack") else "",
                     "cpu_percent": s_.get("cpu_percent"), "cpus": n["cpus"], "used_memory_gb": s_.get("used_memory_gb"),
                     "memory_gb": n["memory_gb"], "age": now - _started(c) if running(c) and _started(c) else None,
                     "restarts": mem.get("restarts", 0),
                     "flags": "".join(f for f, on in (("D", n.get("desktop")), ("S", n.get("sandbox")),
                                                      ("I", n["internet"]), ("A", n["created_by"] == "agent")) if on),
                     **({"node": where} if where else {}),
                     **({"description": n["description"]} if n.get("description") else {})})
    q = None
    if with_quota:
        q = project_quota(project, infos)
    return {"engine": node().engine, "daemon": daemon_alive(), "project": project, "rows": sorted(rows, key=lambda r: r["name"]),
            "quota": q, "events": events(n=8, project=project)}


def project_quota(project, infos, q=None):
    """The quota (q: already read); for a project (infos: its containers), its own use next to what is left."""
    q = q or quota()
    if project:
        use = {"cpus": 0, "memory_gb": 0, "containers": 0}
        for c in infos:
            if running(c):
                cpus, m = allotted(c)
                use["cpus"] += cpus; use["memory_gb"] += m; use["containers"] += 1
        q = {k: {"limit": v["limit"], "free": v["free"], "used": round(use[k], 1) if k in use else "-"}
             for k, v in q.items()}
    return q


_COLOURS = {"running": "32", "created": "33", "paused": "33", "exited": "31", "unhealthy": "31", "ok": "32"}


def dashboard_text(d, width=120, selected=None, ansi=False, message="", height=None):
    """The dashboard as text: plain for agents, with colours and a selection for the terminal. height: the rows
    shown are cut to fit, scrolled to keep the selection in view."""
    col = (lambda s, c: f"\x1b[{c}m{s}\x1b[0m" if c else s) if ansi else (lambda s, c: s)
    out = []
    q = d.get("quota") or {}
    quota_s = "  ".join(f"{k.replace('_gb', '')} {v['used'] if 'used' in v else '?'}/{v['limit']}" for k, v in q.items())
    top_line = (f"terminal-ct {VERSION}  {d['engine']}  daemon {'running' if d['daemon'] else 'stopped'}"
                + (f"  project {d['project']}" if d.get("project") else "") + (f"  quota {quota_s}" if quota_s else ""))
    out.append(col(top_line[:width], "1"))
    head = f"  {'NAME':<22} {'STATUS':<8} {'HEALTH':<10} {'STACK':<18} {'CPU%':>6} {'CPUS':>5} {'MEM GiB':>11} {'AGE':>5} {'RST':>3} {'FLAGS':<5} PROJECT"
    out.append(col(head[:width], "7" if ansi else None))
    rows = list(enumerate(d["rows"]))
    if height:  # header 2, events title + 8, footer 3
        room = max(3, height - 2 - 2 - len(d["events"]) - (4 if ansi else 0))
        if len(rows) > room:
            first = max(0, min((selected or 0) - room // 2, len(rows) - room))
            rows = rows[first:first + room]
    for i, r in rows:
        mem = f"{r['used_memory_gb'] if r['used_memory_gb'] is not None else '-'}/{r['memory_gb']}"
        cpu = "-" if r["cpu_percent"] is None else f"{r['cpu_percent']:.1f}"
        line = (f"{'>' if i == selected else ' '} {r['name'][:22]:<22} {r['status'][:8]:<8} {r['health'][:10]:<10} "
                f"{r['stack'][:18]:<18} {cpu:>6} {str(r['cpus'])[:5]:>5} {mem[:11]:>11} {_age(r['age']):>5} "
                f"{r['restarts']:>3} {r['flags']:<5} {r['project']}"
                + (f"  {r['description']}" if r.get("description") else ""))[:width]
        if ansi:
            line = line.replace(f" {r['status'][:8]:<8} ", " " + col(f"{r['status'][:8]:<8}", _COLOURS.get(r["status"])) + " ", 1)
            line = col(line, "1") if i == selected else line
        out.append(line)
    if not d["rows"]:
        out.append("  (no containers" + (f" in project {d['project']}" if d.get("project") else "") + ")")
    out.append("")
    out.append(col("events", "1"))
    for e in d["events"]:
        out.append(f"  {e['time'][11:]} {e['event']:<12} {e.get('container', e.get('stack', '')):<22} {e.get('detail', '')}"[:width])
    if ansi:
        out.append("")
        out.append(col("up/down select  s start  t stop  r remove  +/- scale  l logs  v view  q quit", "2"))
        if message:
            out.append(message[:width])
    return "\n".join(out)


def _poll_keys(timeout=0.2):
    """Keyboard events waiting now (as parse_input gives them), polled so the reader can pause while the viewer
    owns the keyboard: POSIX raw input, or msvcrt on Windows."""
    if WINDOWS:
        import msvcrt
        out, end = [], time.time() + timeout
        while time.time() < end and not out:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    k = {"H": "up", "P": "down"}.get(msvcrt.getwch())
                    if k:
                        out.append(("key", k))
                elif ch in ("\x03", "\x1d"):  # Ctrl+C, Ctrl+]
                    out.append(("quit",))
                else:
                    out.append(("text", ch))
            time.sleep(0.02)
        return out
    import select
    fd = sys.stdin.fileno()
    if not select.select([fd], [], [], timeout)[0]:
        return []
    return parse_input(os.read(fd, 1024).decode("utf-8", "replace"))


# ---------- infra: the map an agent reads first (phase 12) ----------
INFRA_RULES = ("managed: true marks what the agent runs without asking (its own containers, sandboxes the user left "
               "offline). Anything else needs the user's approval: the user's containers, networks, nodes and PCs, "
               "internet, an image not here yet, another PC for an agent's stack (approved_for). Containers on a "
               "network reach each other by name; on nodes' networks a stack's service also answers as SERVICE and "
               "SERVICE.NET.")


def infra(project=None):
    """Every PC and node (reachable or not, its quota: limit, used, free), what runs where with its ceiling and
    use, its networks, ports, description, project and creator; networks and the names that reach their members;
    terminal-vm's VMs; projects. project: only that project's objects (PCs and nodes stay listed: where it may go)."""
    mine = lambda p: under(p or DEFAULT_PROJECT, project)
    d, up = _nodes(), nodes_up()
    approved = {}  # PC: the agent's stacks allowed on it
    for sn in stacks():
        with contextlib.suppress(SystemExit, OSError, ValueError):
            s = load_stack(sn)
            if mine(s.get("project")):
                for h in s.get("approved_hosts", ()):
                    approved.setdefault(h, []).append(sn)
    places = [{"name": "this PC", "kind": "local", "engine": node().engine, "reachable": True}]
    for n, info in sorted(d["nodes"].items()):
        host = info.get("kind") == "host"
        places.append({"name": n, "kind": "host" if host else "node", "reachable": bool(up.get(n)),
                       **({"url": info.get("url"), "engine": info.get("engine", "docker")} if host else
                          {"image": info.get("image")}),
                       **({"description": t} if (t := description("host" if host else "node", n)) else {}),
                       **({"approved_for": approved[n]} if n in approved else {})})
    rows, seen = [], {}
    engine = getattr(_LOCAL, "node", None)  # this command's engine (--engine): each thread starts from it

    def look(pl):
        _LOCAL.node = engine
        where = None if pl["kind"] == "local" else pl["name"]
        try:
            with on_node(where, LOOK_TIMEOUT) if where else contextlib.nullcontext():
                every = raw_containers(size=True)  # one listing for the quota and the rows
                cs = [c for c in every if mine(labels_of(c).get(f"{LABEL}.project"))]
                seen[pl["name"]] = (cs, _stats([c for c in cs if running(c)]))
                pl["quota"] = project_quota(project, cs, quota(infos=every))
        except (SystemExit, RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
            pl.update(reachable=False, error=str(e)[:200])

    side = {}

    def aside(key, f):  # this PC's networks and terminal-vm's VMs, read while the places answer
        _LOCAL.node = engine
        with contextlib.suppress(SystemExit, RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            side[key] = f()

    threads = [threading.Thread(target=look, args=(pl,), daemon=True) for pl in places if pl["reachable"]] + \
        [threading.Thread(target=aside, args=("nets", lambda: networks(members=False)), daemon=True),
         threading.Thread(target=aside, args=("vms", lambda: vm_state(project)), daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(LOOK_TIMEOUT * 4)  # shortcut: a place still answering after that is left out, not waited for
    for pl in places:
        if pl["reachable"] and pl["name"] not in seen:
            pl.update(reachable=False, error=pl.get("error") or "no answer in time")
            continue
        if not pl["reachable"]:
            continue
        cs, stats = seen[pl["name"]]
        for c in cs:
            n, lab, ports = normalize(c), labels_of(c), None
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                ports = json.loads(lab.get(f"{LABEL}.spec") or "{}").get("ports")
            rows.append({"name": n["name"], "on": pl["name"], "status": n["status"], "project": n["project"],
                         "created_by": n["created_by"], "managed": managed(c),
                         **({"description": n["description"]} if n.get("description") else {}),
                         "cpus": n["cpus"], "memory_gb": n["memory_gb"],
                         **({"use": stats[k]} if (k := c.get("Name", "").lstrip("/")) in stats else {}),
                         "internet": n["internet"], "networks": n["networks"], **({"ports": ports} if ports else {}),
                         **{k: True for k in ("desktop", "sandbox") if n.get(k)},
                         **({"stack": "/".join(lab.get(f"{LABEL}.{k}", "") for k in ("stack", "service", "replica"))}
                            if lab.get(f"{LABEL}.stack") else {})})
    nets = {k: dict(v, members=[]) for k, v in side.get("nets", {}).items() if mine(v["project"])}  # from rows,
    for r in rows:                                                                         # this project's only
        for net in r["networks"]:
            if net not in nets:  # a network of nodes (a stack's): it lives in each node's engine
                nets[net] = {"project": r["project"], "created_by": "agent" if r["created_by"] == "agent" else "user",
                             "members": [], **({"description": t} if (t := description("net", net)) else {})}
            e = nets[net]
            if r["name"] not in e["members"]:
                e["members"].append(r["name"])
            names = e.setdefault("names", [])
            svc = r.get("stack", "").split("/")[1:2]
            for nm in [r["name"]] + ([svc[0], f"{svc[0]}.{net}"] if svc and r["on"] != "this PC" else []):
                if nm not in names:
                    names.append(nm)
    vms = side.get("vms") or {"vms": [], "note": "terminal-vm did not answer in time"}
    for v in vms.get("vms", []):
        if v.get("name") in d["nodes"]:
            v["node"] = True  # a node of terminal-ct (its containers above)
    names = {DEFAULT_PROJECT} | {r["project"] for r in rows} | {v["project"] for v in nets.values()}
    if os.path.isdir(os.path.join(STORE, "projects")):  # as projects() finds them, without listing everything again
        names |= {d.replace("+", "/") for d in os.listdir(os.path.join(STORE, "projects"))}
    projs = [{"project": p, **({"description": t} if (t := description("project", p)) else {})}
             for p in sorted(names) if mine(p)]
    return {"project": project, "rules": INFRA_RULES, "pcs_and_nodes": places,
            "containers": sorted(rows, key=lambda r: (r["on"], r["name"])), "networks": nets,
            "vms": vms.get("vms", []), **({"vm_note": vms["note"]} if vms.get("note") else {}), "projects": projs}


def infra_text(m):
    """infra for a person: one line per place, container, network and VM."""
    gb = lambda v: v if isinstance(v, str) else f"{v:g}"
    out = [f"terminal-ct {VERSION}" + (f"  project {m['project']}" if m.get("project") else "")]
    for p in m["pcs_and_nodes"]:
        q = p.get("quota") or {}
        res = "  ".join(f"{k.replace('_gb', '')} {gb(v['used'])}/{gb(v['limit'])}" for k, v in q.items())
        out.append(f"{p['kind']:<6} {p['name']:<14} {'up' if p['reachable'] else 'DOWN':<4} {res}"
                   + (f"  — {p['description']}" if p.get("description") else ""))
    out.append("")
    for r in m["containers"]:
        flags = "".join(f for f, on in (("D", r.get("desktop")), ("S", r.get("sandbox")), ("I", r["internet"]),
                                        ("M", r["managed"])) if on)
        out.append(f"  {r['name']:<22} {r['on']:<12} {r['status']:<8} {flags:<4} {r['project']:<12}"
                   + (f" nets {','.join(r['networks'])}" if r["networks"] else "")
                   + (f" ports {','.join(map(str, r['ports']))}" if r.get("ports") else "")
                   + (f"  — {r['description']}" if r.get("description") else ""))
    if not m["containers"]:
        out.append("  (no containers)")
    for k, v in sorted(m["networks"].items()):
        out.append(f"net {k}: {', '.join(v.get('names') or v['members']) or '(empty)'}"
                   + (f"  — {v['description']}" if v.get("description") else ""))
    for v in m["vms"]:
        out.append(f"vm  {v['name']:<22} {v.get('status', '-'):<8} {v.get('project', '-')}"
                   + (" (node)" if v.get("node") else "") + (f"  — {v['description']}" if v.get("description") else ""))
    if m.get("vm_note"):
        out.append(f"vm  {m['vm_note']}")
    out.append("flags: D desktop, S sandbox, I internet, M the agent runs it without asking")
    return "\n".join(out)


def dash(project=None, refresh=2.0):
    """Live dashboard in the terminal; acts on the selected container. q or Ctrl+] quits."""
    if not sys.stdin.isatty():
        raise SystemExit("dash needs a terminal (agents: the MCP tool dashboard)")
    data = {"d": dashboard_data(project), "gen": 0}
    sel, msg, confirm = 0, "", None
    stop_, paused, keys, dirty = threading.Event(), threading.Event(), queue.Queue(), threading.Event()

    def collect():  # quota every 5th refresh: it measures the disk
        k = 0
        while not stop_.wait(refresh):
            gen = data["gen"]
            with contextlib.suppress(Exception, SystemExit):
                d = dashboard_data(project, with_quota=k % 5 == 0)
                if d["quota"] is None:
                    d["quota"] = data["d"].get("quota")
                if gen == data["gen"]:  # an action since this pass began: its fresher state stays
                    data["d"] = d
                    dirty.set()
            k += 1

    def read():
        while not stop_.is_set():
            if paused.is_set():  # the viewer owns the keyboard now
                time.sleep(0.1)
                continue
            try:
                evs = _poll_keys()
            except OSError:  # the terminal went away
                keys.put(("quit",)); dirty.set()
                return
            for e in evs:
                if e[0] == "text":  # one event per character: "ry" typed fast, or + held down
                    for ch in e[1]:
                        keys.put(("text", ch))
                else:
                    keys.put(e)
                dirty.set()

    def act(fn, *a):
        try:
            r = fn(*a)
            r = json.dumps(r if r is not None else {})[:200]
        except (SystemExit, Exception) as e:
            r = f"error: {e}"
        data["gen"] += 1
        with contextlib.suppress(Exception, SystemExit):  # the new state at once, not at the next refresh
            data["d"] = dict(dashboard_data(project, with_quota=False), quota=data["d"].get("quota"))
        return r

    def drain():
        while not keys.empty():
            keys.get_nowait()

    def scale_by(r, step):
        stack, svc, _ = r["stack"].split("/")
        n = load_stack(stack)["services"][svc].get("replicas", 1)
        return scale(stack, svc, max(0, n + step))

    restore = (lambda: None) if WINDOWS else raw_terminal()
    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l"); out.flush()
    threading.Thread(target=collect, daemon=True).start()
    threading.Thread(target=read, daemon=True).start()
    try:
        while True:
            rows = data["d"]["rows"]
            sel = max(0, min(sel, len(rows) - 1))
            size = shutil.get_terminal_size((120, 40))
            out.write("\x1b[H\x1b[2J" + dashboard_text(data["d"], size.columns, sel if rows else None, True,
                                                       msg, size.lines).replace("\n", "\r\n"))
            out.flush()
            dirty.wait(); dirty.clear()
            while not keys.empty():
                e = keys.get()
                r = rows[sel] if rows else None
                if e[0] == "quit" or e == ("text", "q"):
                    return
                if confirm and e == ("text", "y"):
                    msg = act(lambda n: quiet(rm, n), confirm)  # the one asked about, whatever is selected now
                    confirm = None
                    continue
                confirm = None
                if e == ("key", "up"):
                    sel -= 1
                elif e == ("key", "down"):
                    sel += 1
                elif r and e == ("text", "s"):
                    msg = act(lambda n: quiet(start, n), r["name"])
                elif r and e == ("text", "t"):
                    msg = act(lambda n: quiet(stop, n, 5), r["name"])
                elif r and e == ("text", "r"):
                    confirm, msg = r["name"], f"remove {r['name']} and its /data? y to confirm"
                elif r and e in (("text", "+"), ("text", "-")) and r["stack"]:
                    msg = act(scale_by, r, 1 if e[1] == "+" else -1)
                elif r and e == ("text", "l"):
                    try:
                        text = logs(r["name"], max(10, size.lines - 3))
                    except (SystemExit, Exception) as ex:
                        msg = f"error: {ex}"
                        continue
                    drain()  # a key typed with l must not close the page at once
                    out.write("\x1b[H\x1b[2J" + f"logs of {r['name']} (any key)\n{text}".replace("\n", "\r\n")); out.flush()
                    while not stop_.is_set():
                        try:
                            keys.get(timeout=0.5)
                            break
                        except queue.Empty:
                            pass
                elif r and e == ("text", "v") and "D" in r["flags"]:
                    # in a process of its own: its keyboard reader ends with it, nothing stays to eat dash's keys
                    paused.set(); time.sleep(0.25)
                    out.write("\x1b[?25h\x1b[?1049l"); out.flush(); restore()
                    rc = subprocess.run([sys.executable, os.path.abspath(__file__), "--engine", node().engine,
                                         "view", r["name"]]).returncode
                    restore = (lambda: None) if WINDOWS else raw_terminal()
                    out.write("\x1b[?1049h\x1b[?25l"); out.flush()
                    drain(); paused.clear()
                    msg = "" if rc == 0 else f"view ended with code {rc}"
    finally:
        stop_.set()
        out.write("\x1b[?25h\x1b[?1049l"); out.flush()
        restore()


# ---------- web dashboard: terminal-ct's side (containers here, terminal-vm's VMs through its command line) ----------
TOOL = "terminal-ct"
WEB_PORT = int(os.environ.get("TERMINAL_CT_WEB_PORT") or 7171)
_VM_NAME = re.compile(r"\w[\w.-]{0,63}")


def vm_cli():
    return peer_cli("TERMINAL_VM", "terminal-vm")


_VM_MODULE, _VM_LOCK = {}, threading.Lock()  # (path, mtime): module or None


def vm_module():
    """terminal-vm itself, loaded in this process when its command is plainly a Python file (TERMINAL_VM="python3
    /path/terminal_vm.py", or install.sh's launcher that runs one); loaded again when that file changes (a daemon
    outlives a reinstall). None otherwise (sudo, env, wsl or ssh in front; a Mac's terminal-mac; a binary): then
    its command line is used, which knows what those mean."""
    cli, path = vm_cli() or [], None
    if len(cli) == 2 and os.path.basename(cli[0]).lower().startswith("python") and cli[1].endswith(".py"):
        path = cli[1]
    elif len(cli) == 1 and os.path.isfile(cli[0]) and os.path.getsize(cli[0]) < 4096:
        with contextlib.suppress(OSError, UnicodeDecodeError):
            with open(cli[0], encoding="utf-8") as f:
                found = re.search(r'^exec python3 "([^"]+terminal_vm\.py)" "\$@"$', f.read(), re.M)  # install.sh's
            path = found and found[1]
    try:
        key = (path, os.path.getmtime(path)) if path else None
    except OSError:
        key = None
    if key is None:
        return None
    with _VM_LOCK:
        if key not in _VM_MODULE:
            mod = None
            try:
                spec = importlib.util.spec_from_file_location("terminal_vm_peer", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not hasattr(mod, "guest_run"):  # an older terminal-vm: its command line then
                    mod = None
            except Exception:  # any trouble loading it: the command line still works
                mod = None
            _VM_MODULE.clear()
            _VM_MODULE[key] = mod
        return _VM_MODULE[key]


def vm_call(args, timeout=60, host=None, **k):
    """terminal-vm here, or on another PC (host)."""
    if host:
        return host_vm_call(host, args, timeout, **k)
    cli = vm_cli()
    if not cli:
        raise RuntimeError("terminal-vm not found: put it on the PATH, or set TERMINAL_VM")
    return cli_call(cli, args, timeout, **k)


# TERMINAL_VM there as here ("python3 /path/terminal_vm.py": no spaces), else the login PATH, else install.sh's
# ~/.local/bin (a Mac's zsh profile is not read by sh -l)
VM_FIND = ('c=${TERMINAL_VM:-}; [ -n "$c" ] || c=$(command -v terminal-vm) || c="$HOME/.local/bin/terminal-vm"; '
           'set -f; ')
VM_ON_PC = ["sh", "-lc", VM_FIND + 'exec $c "$@"', "terminal-vm"]


def vm_missing(r):
    """Whether a reply is terminal-vm itself not found there (not a 127 of a guest's command)."""
    return r.get("exit") == 127 and "terminal-vm" in (r.get("stderr") or "") and "not found" in (r.get("stderr") or "")


def host_vm_call(host, args, timeout=60, lines=False):
    """terminal-vm on another PC, run there by its agent or over SSH: its JSON reply; RuntimeError as vm_call."""
    try:
        n = node_obj(host, timeout)
        if not isinstance(n, NodeRemote):
            raise RuntimeError(f"{host} is not another PC (terminal-ct host ls)")
        r = n._exec([*VM_ON_PC, *args], timeout)
    except SystemExit as e:
        raise RuntimeError(str(e)) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{host}: terminal-vm {args[0]} timed out") from None
    out = (r.get("stdout") or "").strip()
    if r.get("exit"):
        msg = ((r.get("stderr") or "") or out).strip()
        with contextlib.suppress(ValueError, AttributeError, IndexError, TypeError):
            msg = json.loads(msg.splitlines()[-1])["error"]
        if vm_missing(r):
            msg = "terminal-vm not found there (install it on that PC: install.sh, or TERMINAL_VM)"
        raise RuntimeError(f"{host}: {msg or f'terminal-vm {args[0]} failed'}")
    if lines:
        return [json.loads(x) for x in out.splitlines() if x.strip()]
    try:
        return json.loads(out) if out else {}
    except ValueError:  # progress lines first: the reply is the last one
        return json.loads(out.splitlines()[-1])


def vm_where(name):
    """A dashboard VM name: "VM" (here) or "VM@PC" (another PC) -> (VM, PC or None); ValueError otherwise."""
    vm, at, host = (name or "").partition("@")
    if not _VM_NAME.fullmatch(vm) or at and not (host and is_host(host)):
        raise ValueError(f"invalid VM name {name!r}")
    return vm, host or None


_VM_CACHE = {}  # project: (time, reply): several tabs, one terminal-vm call every few seconds


def vm_state(project=None, max_age=3):
    hit = _VM_CACHE.get(project)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    out = _vm_state(project)
    _VM_CACHE[project] = (time.time(), out)
    return out


def _vm_one(host, project):
    """(VMs, events) of terminal-vm here (None) or on another PC; another PC's VMs named VM@PC."""
    t = {"timeout": LOOK_TIMEOUT} if host else {}  # another PC: a hung one costs a listing's wait, not minutes
    vms = vm_call(["ls"], host=host, **t)
    if not isinstance(vms, list) or not all(isinstance(v, dict) and isinstance(v.get("name"), str) for v in vms):
        raise ValueError("unreadable VM list")
    vms = [v for v in vms if under(v.get("project") or DEFAULT_PROJECT, project)]
    if any(v.get("status") not in (None, "stopped") for v in vms):
        with contextlib.suppress(RuntimeError, ValueError, AttributeError, subprocess.TimeoutExpired):
            use = {v["name"]: v for v in vm_call(["top"], host=host, **t).get("vms", [])}
            for v in vms:
                v.update({k: use.get(v["name"], {}).get(k) for k in ("cpu_percent", "host_memory_gb")})
    evs = []
    with contextlib.suppress(RuntimeError, ValueError, subprocess.TimeoutExpired):  # an older terminal-vm: none
        evs = [e for e in vm_call(["events", "-n", "8"], host=host, lines=True, **t) if isinstance(e, dict)]
    mine = {v["name"] for v in vms}
    evs = [e for e in evs if not e.get("vm") or e["vm"] in mine]
    if host:  # one name per VM across PCs; the dashboard's actions send it back (vm_where splits it)
        for v in vms:
            v.update(name=f"{v['name']}@{host}", host=host)
        for e in evs:
            if e.get("vm"):
                e["vm"] = f"{e['vm']}@{host}"
            else:
                e["detail"] = f"{e.get('detail') or ''} (on {host})".strip()
            e["host"] = host
    return vms, evs


_VM_STATE_LOCK = threading.Lock()


def _vm_state(project=None):
    """terminal-vm's VMs (ls) here and on each other PC that answers (at once), with the real use of the running
    ones (top). One PC failing in any way is a note, never an empty panel."""
    with _VM_STATE_LOCK:  # several tabs: one round at a time, the others take its answer from the cache
        hit = _VM_CACHE.get(project)
        if hit and time.time() - hit[0] < 1:
            return hit[1]
        hosts = [n for n, i in _nodes()["nodes"].items() if i.get("kind") == "host"]
        pcs = sorted(n for n, ok in nodes_up().items() if ok and n in hosts) if hosts else []
        got, notes = {}, []

        def one(host):
            try:
                got[host] = _vm_one(host, project)
            except Exception as e:  # another PC is pinned, not trusted to answer well
                got[host] = f"terminal-vm{' on ' + host if host else ''}: {e}"
        threads = [threading.Thread(target=one, args=(h,), daemon=True) for h in ([None] if vm_cli() else []) + pcs]
        for t in threads:
            t.start()
        end = time.monotonic() + LOOK_TIMEOUT + 35  # a hung PC is left out of this round
        for t in threads:
            t.join(max(0, end - time.monotonic()))
        out = {"vms": [], "events": []}
        for host in ([None] if vm_cli() else []) + pcs:
            r = got.get(host, f"terminal-vm on {host}: no answer in time")
            if isinstance(r, str):
                notes.append(r)
            else:
                out["vms"] += r[0]
                out["events"] += r[1]
        if not vm_cli():
            notes.insert(0, "terminal-vm not found (PATH or TERMINAL_VM)")
        if notes:
            out["note"] = "; ".join(notes)
        return out


def vm_screenshot(name):
    vm, host = vm_where(name)
    path = os.path.join(_web_dir(), f"vm-{vm}.{threading.get_ident()}.png")  # one per request: two tabs at once
    try:
        if not host:
            vm_call(["action", vm, "screenshot", "--no-wake", "--output", path], timeout=20)  # looking is no use
        else:  # written in a private folder there, then brought here
            n, tmp = node_obj(host, 30), f"/tmp/tct-{secrets.token_hex(6)}"
            try:
                try:
                    r = n._exec(["sh", "-lc", f"mkdir -m 700 {tmp} && " + VM_FIND + 'exec $c "$@"', "terminal-vm",
                                 "action", vm, "screenshot", "--no-wake", "--output", f"{tmp}/s.png"], 30)
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    raise RuntimeError(f"{host}: {e}") from None
                if r.get("exit"):
                    raise RuntimeError(f"{host}: " + ((r.get("stderr") or r.get("stdout") or "").strip()[-300:]
                                                      or "screenshot failed"))
                n.copy("pull", f"{tmp}/s.png", path, timeout=30)
            finally:
                with contextlib.suppress(SystemExit):
                    n.sh(f"rm -rf {tmp}", timeout=30)
        with open(path, "rb") as f:
            return f.read()
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


@contextlib.contextmanager
def on_stack_engine(stack):
    """Run a stack call on the engine the stack was applied to (not the daemon's default one)."""
    try:
        engine = load_stack(stack).get("engine") if isinstance(stack, str) else None
    except SystemExit:
        engine = None
    _LOCAL.node = node_for(engine)
    try:
        yield
    finally:
        _LOCAL.node = None


def web_api(method, path, q, body=None):
    """One dashboard call, on the engine of the container it names (its node, if it lives in one)."""
    name = q.get("name") or (body.get("name") if isinstance(body, dict) else None)
    with on_node(_nodes()["placement"].get(name) if isinstance(name, str) and q.get("kind") != "vm"
                 and not (isinstance(body, dict) and body.get("kind") == "vm") else None):
        return _web_api(method, path, q, body)


def _web_api(method, path, q, body=None):
    """One dashboard call: (content type, bytes or a JSON-able value). Raises on a bad request."""
    project = q.get("project") or None
    if project:
        check_project(project)
    if method == "GET" and path == "/api/state":
        d = dashboard_data(project, with_quota=False)  # the quota measures the disk (seconds): /api/quota
        d["tool"], d["stacks"] = TOOL, []
        for n in stacks():
            with contextlib.suppress(SystemExit):
                s = load_stack(n)
                if under(s.get("project") or DEFAULT_PROJECT, project):
                    d["stacks"].append({"stack": n, "project": s.get("project") or DEFAULT_PROJECT,
                                        "created_by": s.get("created_by", "user"),
                                        "services": {k: {"replicas": v.get("replicas", 1), "image": v.get("image")}
                                                     for k, v in s["services"].items()}})
        return "application/json", d
    if method == "GET" and path == "/api/quota":
        infos = raw_containers()
        names = {DEFAULT_PROJECT} | {labels_of(c).get(f"{LABEL}.project") or DEFAULT_PROJECT for c in infos}
        if os.path.isdir(os.path.join(STORE, "projects")):
            names |= {d.replace("+", "/") for d in os.listdir(os.path.join(STORE, "projects"))}
        mine = [c for c in infos if under(labels_of(c).get(f"{LABEL}.project") or DEFAULT_PROJECT, project)]
        return "application/json", {"quota": project_quota(project, mine), "projects": sorted(names)}
    if method == "GET" and path == "/api/vms":
        return "application/json", vm_state(project)
    if method == "GET" and path == "/api/shot":
        name = q.get("name", "")
        if q.get("kind") == "vm":
            return "image/png", vm_screenshot(name)
        return "image/png", act(check_name(name), "screenshot", {})[0]
    if method == "GET" and path == "/api/logs":
        return "text/plain; charset=utf-8", logs(check_name(q.get("name", "")), int(q.get("n") or 200)).encode()
    if method == "GET" and path == "/api/stack":
        s = load_stack(q.get("name", ""))
        with on_stack_engine(s["stack"]):
            return "application/json", {"desired": {k: s[k] for k in ("stack", "project", "services") if k in s},
                                        "status": stack_show(s["stack"])}
    if method == "GET" and path == "/api/events":
        return "application/json", events(q.get("stack") or None, int(q.get("n") or 50), project)
    if method == "POST" and path == "/api/do":
        if not isinstance(body, dict):
            raise ValueError("a JSON object")
        op, name = body.get("op"), body.get("name") or ""
        if body.get("kind") == "vm":
            if op not in ("start", "stop", "force_stop"):
                raise ValueError("VMs: start, stop or force_stop, and a VM name")
            vm, host = vm_where(name)
            out = vm_call(["stop", vm, "--force"] if op == "force_stop" else [op, vm], timeout=300, host=host)
            _VM_CACHE.clear()  # the next refresh shows the new state at once (else: a second click on start)
            return "application/json", out
        if op in ("start", "stop", "restart", "rm"):
            check_name(name)
            if op in ("stop", "restart"):
                out = quiet(stop, name)
            if op in ("start", "restart"):
                out = quiet(start, name)
            return "application/json", quiet(rm, name) if op == "rm" else out
        if op in ("scale", "apply", "stack_rm"):
            stack = body.get("stack")
            with on_stack_engine(stack.get("stack") if op == "apply" and isinstance(stack, dict) else stack):
                if op == "scale":
                    return "application/json", scale(stack or "", body.get("service", ""), int(body.get("replicas")))
                if op == "apply":
                    return "application/json", apply(stack, created_by="user", project=body.get("project") or None)
                return "application/json", stack_rm(stack or "")
        raise ValueError(f"unknown op {op!r}")
    raise NoRoute(path)


# ===== shared web dashboard: identical in terminal-ct and terminal-vm (their tests compare it; edit both) =====
# The daemon serves it on 127.0.0.1. GET / is the page (no data in it); every /api/ call needs the X-Token header,
# which a cross-site page cannot set without a CORS preflight this server never grants, and a Host header naming
# this server (DNS rebinding). Each tool defines around it: TOOL, WEB_PORT, STORE, WINDOWS, NO_WINDOW, in_wsl,
# check_project, daemon, daemon_alive, _daemon_file, and web_api(method, path, query, body) -> (content type,
# value): its own objects directly, the other tool's through that tool's JSON command line (cli_call).

class _Capture(threading.local):
    buf = None


_CAPTURE = _Capture()


class _Stdout:
    """sys.stdout once a thread captures: each thread's prints go to its own buffer, the others' to the real one
    (contextlib.redirect_stdout swaps it for every thread: the daemon's work and the web's actions mixed)."""
    def __init__(self, real):
        self.real = real

    def write(self, s):
        return (_CAPTURE.buf or self.real).write(s)

    def __getattr__(self, k):
        return getattr(_CAPTURE.buf or self.real, k)


@contextlib.contextmanager
def captured(buf):
    if not isinstance(sys.stdout, _Stdout):
        sys.stdout = _Stdout(sys.stdout)
    old, _CAPTURE.buf = _CAPTURE.buf, buf
    try:
        yield buf
    finally:
        _CAPTURE.buf = old


def peer_cli(env, exe):
    """The other tool's command: from the environment (e.g. TERMINAL_VM="python3 /path/terminal_vm.py"), else
    found on the PATH, else None."""
    if os.environ.get(env):
        return [a.strip('"') for a in shlex.split(os.environ[env], posix=not WINDOWS)]  # Windows paths keep their backslashes
    found = shutil.which(exe)
    return [found] if found else None


def cli_call(cli, args, timeout=60, lines=False, raw=False):
    """Run a JSON command line: its reply (the last line when progress lines come first; every line with lines;
    the text with raw). A failure raises RuntimeError with the tool's own message."""
    try:
        r = subprocess.run(cli + args, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace", **NO_WINDOW)
    except OSError as e:  # TERMINAL_VM naming a program that is not there
        raise RuntimeError(f"{cli[0]}: {e.strerror or e}") from None
    if r.returncode:
        err = (r.stderr or r.stdout).strip()
        with contextlib.suppress(ValueError, AttributeError, IndexError):
            err = json.loads(err.splitlines()[-1])["error"]
        raise RuntimeError(err or f"{os.path.basename(cli[-1])} {args[0]} failed")
    out = r.stdout if raw else r.stdout.strip()
    if raw:
        return out
    if lines:
        return [json.loads(l) for l in out.splitlines() if l.strip()]
    try:
        return json.loads(out) if out else {}
    except ValueError:  # progress lines first: the reply is the last one
        return json.loads(out.splitlines()[-1])


class NoRoute(Exception):
    pass


def _web_write(path, text, mode=0o644):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    for i in range(20):  # Windows: refused while another process has the file open for a moment
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def web_token():
    """The dashboard's token (STORE/web.token, owner only): kept across daemon restarts, so open tabs go on."""
    path = os.path.join(STORE, "web.token")
    with contextlib.suppress(OSError):
        with open(path) as f:
            t = f.read().strip()
        if len(t) >= 32:
            return t
    # missing, or cut short (a crash while writing): an empty token would let any request in
    os.makedirs(STORE, exist_ok=True)
    t = secrets.token_urlsafe(24)
    _web_write(path, t, 0o600)
    return t


WEB_LAST = [0.0]  # time of the latest API call: a daemon with an open dashboard does not end for being idle
WEB_ROLES = ("read", "full")  # read: look (every GET); full: act too (POST)
WEB_SEEN = {}  # user: (time of its latest call, role): who is on the dashboard now


WEB_DIR = ".web"  # a name no container or VM can take (STORE/NAME is theirs)


def _web_dir():
    d = os.path.join(STORE, WEB_DIR)
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700, exist_ok=True)
        old = os.path.join(STORE, "web")  # where they were: a container or VM named web shared (and rm'd) it
        for f in (os.listdir(old) if os.path.isdir(old) else []):
            if f == "actions.log" or f.startswith("cert-") and f.endswith(".pem"):  # the audit log, pinned certs
                with contextlib.suppress(OSError):
                    os.replace(os.path.join(old, f), os.path.join(d, f))
    return d


def tls_cert(path, ip=None):
    """SHA-256 of the self-signed certificate kept at path (with its key, owner only), made once by openssl.
    ip: named in it too (subjectAltName), so a browser can trust it once, imported, instead of each time."""
    if not os.path.isfile(path):
        exe = shutil.which("openssl")
        if not exe:
            raise SystemExit("HTTPS needs openssl once, to make a certificate (Linux, macOS, WSL and Git for Windows "
                             "have it)")
        new, key = f"{path}.{os.getpid()}.new", f"{path}.{os.getpid()}.key"  # two first starts: each its own files
        argv = [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", f"/CN={TOOL}",
                "-keyout", key, "-out", new]
        r = subprocess.run(argv + (["-addext", f"subjectAltName=IP:{ip}"] if ip else []), capture_output=True,
                           text=True, **NO_WINDOW)
        if r.returncode and ip:  # an openssl without -addext (old LibreSSL): a certificate without the address
            r = subprocess.run(argv, capture_output=True, text=True, **NO_WINDOW)
        try:
            if r.returncode:
                raise SystemExit(f"openssl: {r.stderr.strip()}")
            os.chmod(new, 0o600)
            with open(new, "a") as f, open(key) as k:  # one file, one replace: never a key of another start
                f.write(k.read())
            os.replace(new, path)
        finally:
            for x in (new, key):
                with contextlib.suppress(OSError):
                    os.remove(x)
    with open(path) as f:
        pem = f.read()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem[:pem.index("-----END CERTIFICATE-----") + 25])).hexdigest()


def _web_users():
    try:
        with open(os.path.join(STORE, "web.users.json")) as f:
            u = json.load(f)
    except (OSError, ValueError):
        return {}
    return u if isinstance(u, dict) else {}


def web_user(cmd, name=None, role="full"):
    """Dashboard users, each with its own token (shown once: only its SHA-256 is kept) and role: read or full."""
    users, path = _web_users(), os.path.join(STORE, "web.users.json")
    if cmd == "ls":
        return [{"user": n, "role": u.get("role"), "added": u.get("added")} for n, u in sorted(users.items())]
    if cmd not in ("add", "rm"):
        raise SystemExit("web user add NAME [--role read|full] | web user ls | web user rm NAME")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,31}", name or "") or name == "owner":
        raise SystemExit(f"invalid user name {name!r} (lowercase letters, digits, _ . -; not owner)")
    if cmd == "rm":
        if name not in users:
            raise SystemExit(f"no dashboard user {name!r}")
        users.pop(name)
        _web_write(path, json.dumps(users, indent=1), 0o600)
        return {"removed": name}  # its open tabs are refused from their next call
    if role not in WEB_ROLES:
        raise SystemExit("--role read (look) or full (act too)")
    if name in users:
        raise SystemExit(f"user {name!r} exists: web user rm {name} first (a new token)")
    token = secrets.token_urlsafe(24)
    users[name] = {"sha256": hashlib.sha256(token.encode()).hexdigest(), "role": role,
                   "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
    os.makedirs(STORE, exist_ok=True)
    _web_write(path, json.dumps(users, indent=1), 0o600)
    out = {"user": name, "role": role, "token": token, "note": "shown once: give it to that person or agent"}
    net = _web_net()
    if net:
        out.update(url=f"{net['url']}/#t={token}", fingerprint=net["fingerprint"])
    return out


def _web_who(given):
    """(user, role) of a token, or None. The owner's token (web.token) is full."""
    if not given:
        return None
    if secrets.compare_digest(given, web_token().encode()):
        return "owner", "full"
    h = hashlib.sha256(given).hexdigest()
    for n, u in _web_users().items():
        if isinstance(u, dict) and u.get("role") in WEB_ROLES and secrets.compare_digest(h, str(u.get("sha256", ""))):
            return n, u["role"]
    return None


def _web_act_log(user, addr, path, body, error=None):
    """Who did what, from where (STORE/web/actions.log, one JSON line each, one older file kept)."""
    b = body if isinstance(body, dict) else {}
    clean = lambda v, n=64: re.sub(r"[\x00-\x1f\x7f]", "?", str(v))[:n]  # one line each, short: no forged lines
    rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": user, "from": addr, "path": clean(path),
           **{k: clean(b[k]) for k in ("op", "kind", "name", "service", "replicas") if isinstance(b.get(k), (str, int))}}
    st = b.get("stack")
    if isinstance(st, dict):
        st = st.get("stack")
    if isinstance(st, str):
        rec["stack"] = clean(st)
    if error:
        rec["error"] = clean(error, 200)
    log = os.path.join(_web_dir(), "actions.log")
    with contextlib.suppress(OSError):
        if os.path.exists(log) and os.path.getsize(log) > 2 ** 20:
            os.replace(log, log + ".1")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def web_actions(n=20):
    """The latest actions done through the dashboard, newest first."""
    try:
        with open(os.path.join(_web_dir(), "actions.log"), encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def web_session(who):
    """This caller, who else is on the dashboard (a call in the last 30 s), and who did what."""
    now = time.time()
    return {"user": who[0], "role": who[1], "actions": web_actions(20),
            "online": [{"user": n, "role": r, "seen_s": int(now - t)}
                       for n, (t, r) in sorted(WEB_SEEN.items()) if now - t < 30]}


def _web_listen():
    """The network address the dashboard is shared on ({ip, port}; web --listen), or None."""
    try:
        with open(os.path.join(STORE, "web.listen.json")) as f:
            n = json.load(f)
        return {"ip": str(ipaddress.ip_address(n["ip"])), "port": int(n["port"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _web_net():
    """{url, fingerprint} of the network dashboard the running daemon serves, or None."""
    try:
        with open(_daemon_file("webnet")) as f:
            n = json.load(f)
        return n if _web_listen() and n.get("url") and n.get("fingerprint") else None
    except (OSError, ValueError, AttributeError):
        return None


class _WebServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not WINDOWS  # on Windows it lets a second process bind the same port
    allow_reuse_port = False  # never share the port with another store's daemon
    slots = None  # at most 64 clients at once: idle connections never use up threads or memory

    def handle_error(self, *a):  # a failed handshake, a client gone: just that client
        pass

    per_address = None  # the network side: at most this many at once from one address (one host cannot take all)

    def process_request(self, request, client_address):
        if self.slots is None:  # (the agent's server sets its own)
            self.slots = threading.BoundedSemaphore(64)
        if getattr(self, "lock", None) is None:
            self.by_address, self.lock, self.origin = {}, threading.Lock(), {}  # origin: request id -> address
        with self.lock:
            busy = self.per_address and self.by_address.get(client_address[0], 0) >= self.per_address
            if not busy and self.slots.acquire(blocking=False):
                self.by_address[client_address[0]] = self.by_address.get(client_address[0], 0) + 1
                self.origin[id(request)] = client_address[0]  # (a plain socket takes no attribute)
            else:
                return socketserver.TCPServer.shutdown_request(self, request)  # full: closed, no slot to give back
        super().process_request(request, client_address)  # its thread failing to start: shutdown_request below

    def shutdown_request(self, request):  # end of a request that had a slot
        try:
            super().shutdown_request(request)
        finally:
            with self.lock:
                a = self.origin.pop(id(request), None)
                if self.by_address.get(a, 0) > 1:
                    self.by_address[a] -= 1
                else:
                    self.by_address.pop(a, None)
            self.slots.release()

    def server_bind(self):  # HTTPServer's also looks up this host's FQDN: seconds on some Macs, and unused here
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _WebHandler(http.server.BaseHTTPRequestHandler):
    server_version = "terminal-dashboard"
    timeout = 30  # a client stalling mid-request frees its thread

    def log_message(self, *a):  # quiet: the daemon's log is for its own work
        pass

    def version_string(self):  # no Python version told to whoever asks
        return self.server_version

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):  # the network's HTTPS: its handshake here, in this request's thread
            self.request.settimeout(self.timeout)
            self.request.do_handshake()
        super().setup()

    def _send(self, code, ctype, body):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'unsafe-inline'; "
                                                 "script-src 'unsafe-inline'; frame-ancestors 'none'")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        if self.headers.get("Host") not in self.server.hosts:
            return self._send(403, "application/json", {"error": "unknown host"})
        u = urllib.parse.urlsplit(self.path)
        if method == "GET" and u.path == "/":
            return self._send(200, "text/html; charset=utf-8", WEB_PAGE.encode())
        given = self.headers.get("X-Token", "").encode("utf-8", "replace")
        who = _web_who(given)
        if not who:
            return self._send(401, "application/json", {"error": f"token: open the dashboard with {TOOL} web, or ask "
                                                                 f"for one ({TOOL} web user add NAME)"})
        WEB_LAST[0] = time.time()
        WEB_SEEN[who[0]] = (WEB_LAST[0], who[1])
        addr, body = self.client_address[0], None
        try:
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= 2 ** 20:
                    raise ValueError("body too large")
                raw = self.rfile.read(n)  # read even when refused: unread data makes Windows reset the connection
                if self.headers.get_content_type() != "application/json":  # no cross-site "simple" request
                    raise ValueError("Content-Type must be application/json")
                body = json.loads(raw or b"null")
                if who[1] != "full":
                    _web_act_log(who[0], addr, u.path, body, "refused: read only")
                    return self._send(403, "application/json", {"error": "read only: this token may look, not act"})
            if method == "GET" and u.path == "/api/session":
                return self._send(200, "application/json", web_session(who))
            ctype, out = web_api(method, u.path, dict(urllib.parse.parse_qsl(u.query)), body)
            if method == "POST":
                _web_act_log(who[0], addr, u.path, body)
            self._send(200, ctype, out)
        except NoRoute:
            self._send(404, "application/json", {"error": "not found"})
        except ConnectionError:  # the browser left: no error reply on a closed socket
            raise
        except (SystemExit, Exception) as e:  # a bad request or a failed action: the server goes on
            if method == "POST":
                _web_act_log(who[0], addr, u.path, body, str(e) or type(e).__name__)
            self._send(400, "application/json", {"error": str(e) or type(e).__name__})

    def handle(self):
        with contextlib.suppress(ConnectionError):  # the browser left before the reply (closed or frozen tab)
            super().handle()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def web_serve(port=None):
    """Serve the dashboard until the process ends (a thread of the daemon); its port goes to STORE/daemon.web."""
    try:
        web_token()  # made now: the web command reads it once the port is up
        try:
            srv = _WebServer(("127.0.0.1", WEB_PORT if port is None else port), _WebHandler)
        except OSError:  # taken: any free port (the web command reads it from the file)
            srv = _WebServer(("127.0.0.1", 0), _WebHandler)
        p = srv.server_address[1]
        srv.hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        _web_write(_daemon_file("web"), json.dumps({"port": p, "pid": os.getpid()}))
        with contextlib.suppress(OSError):
            os.remove(_daemon_file("webnet"))  # an earlier daemon's: said again below if this one shares it
        if _web_listen():
            threading.Thread(target=_web_serve_net, args=(_web_listen(),), daemon=True).start()
        srv.serve_forever()
    except Exception as e:  # the dashboard down must not take the daemon's own work with it
        print(f"{time.strftime('%H:%M:%S')} web: {e}", file=sys.stderr, flush=True)


class _WebServer6(_WebServer):
    address_family = socket.AF_INET6


def _web_serve_net(net):
    """The dashboard on the network too (web --listen): HTTPS only, the same tokens, at most 64 clients at once."""
    try:
        cert = os.path.join(_web_dir(), f"cert-{net['ip'].replace(':', '_')}.pem")  # one per address: named in it
        fp = tls_cert(cert, net["ip"])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert)
        ip = ipaddress.ip_address(net["ip"])
        srv = (_WebServer6 if ip.version == 6 else _WebServer)((str(ip), net["port"]), _WebHandler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        name = f"[{ip}]" if ip.version == 6 else str(ip)
        host = f"{name}:{srv.server_address[1]}"
        srv.hosts = {host} | ({name} if srv.server_address[1] == 443 else set())  # a browser names this address
        srv.per_address = 8
        _web_write(_daemon_file("webnet"), json.dumps({"url": f"https://{host}", "fingerprint": fp, "pid": os.getpid()}))
        srv.serve_forever()
    except Exception as e:  # the local dashboard and the daemon's work go on
        print(f"{time.strftime('%H:%M:%S')} web (network): {e}", file=sys.stderr, flush=True)


def web_cli(args, role="full", open_browser=True, project=None, listen=None, port=None):
    """The web command of both tools: web [--listen IP|off] [--port N], or web user add|ls|rm."""
    if not args:
        return web(open_browser, project, listen, port)
    if args[0] != "user" or len(args) > 3:
        raise SystemExit("web [--listen IP|off] [--port N] | web user add NAME [--role read|full] | web user ls | "
                         "web user rm NAME")
    return web_user(*args[1:3], role=role)


def web(open_browser=True, project=None, listen=None, port=None):
    """Start the daemon if needed, then open its dashboard in the browser; returns the URL (token included).
    listen: share it on the network too, at that address of this PC (HTTPS), or "off"."""
    if project:
        check_project(project)
    if listen is not None:
        path = os.path.join(STORE, "web.listen.json")
        if listen == "off":
            with contextlib.suppress(OSError):
                os.remove(path)
        else:
            try:
                ip = ipaddress.ip_address(listen)
            except ValueError:
                raise SystemExit("--listen: an address of this PC on the network (an IP), or off") from None
            if ip.is_unspecified or ip.version == 6 and (ip.ipv4_mapped and ip.ipv4_mapped.is_unspecified
                                                         or "%" in str(ip)):
                raise SystemExit("--listen: one address of this PC (not 0.0.0.0 or ::, which would share it on every "
                                 "network; no %zone)")
            p = WEB_PORT + 10 if port is None else port
            if not 1 <= p <= 65535:
                raise SystemExit("--port: 1-65535")
            tls_cert(os.path.join(_web_dir(), f"cert-{str(ip).replace(':', '_')}.pem"), str(ip))  # no openssl: said now
            os.makedirs(STORE, exist_ok=True)
            _web_write(path, json.dumps({"ip": str(ip), "port": p}))
        if daemon_alive():  # it serves what it read when it started
            daemon("stop")
            end = time.time() + 60
            while daemon_alive() and time.time() < end:
                time.sleep(0.3)
            if daemon_alive():  # it would go on serving what it read before
                raise SystemExit(f"the daemon did not stop: the dashboard may still be shared as before; "
                                 f"{TOOL} daemon stop, then {TOOL} web again")
    if not daemon_alive():
        daemon("start")
    end = time.time() + 20
    while True:
        try:
            with open(_daemon_file("web")) as f:
                w = json.load(f)
            with open(_daemon_file("beat")) as f:
                if int(f.read().split()[1]) != w["pid"]:  # left by an earlier daemon
                    raise ValueError("stale")
            socket.create_connection(("127.0.0.1", w["port"]), 1).close()
            break
        except (OSError, ValueError, KeyError, IndexError):
            if time.time() > end:
                raise SystemExit(f"the daemon serves no dashboard (started by an older version?): "
                                 f"{TOOL} daemon stop, then {TOOL} web") from None
            time.sleep(0.3)
    url = f"http://127.0.0.1:{w['port']}/#t={web_token()}" + (f"&p={urllib.parse.quote(project)}" if project else "")
    net = None
    if _web_listen():  # its network side comes up a moment after
        for _ in range(30):
            net = _web_net()
            if net:
                break
            time.sleep(0.3)
    if open_browser:
        with contextlib.suppress(OSError):
            if in_wsl():  # the Windows browser: WSL forwards its 127.0.0.1 ports to Windows
                subprocess.Popen(["rundll32.exe", "url.dll,FileProtocolHandler", url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(url)
    out = {"url": url, "port": w["port"]}
    if net:
        out["network"] = {"url": net["url"], "fingerprint": net["fingerprint"],
                          "users": f"{TOOL} web user add NAME --role read|full: a token for each person or agent"}
    elif _web_listen():
        out["network"] = {"error": f"not served: see {_daemon_file('log')}"}
    return out


WEB_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal dashboard</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#1d2330;--mute:#6b7385;--line:#e2e5ea;--acc:#2f6fdb;--ok:#1f9d55;--warn:#c98a00;--bad:#d64545;--cpu:#2f6fdb;--mem:#8a4fd8}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--panel:#1a1f27;--fg:#e4e8ef;--mute:#8e97a8;--line:#2a313c;--acc:#6aa0ff;--ok:#3cc47c;--warn:#e0b040;--bad:#ff6b6b;--cpu:#6aa0ff;--mem:#b68cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
header h1{font-size:16px;margin:0}.chip{padding:2px 8px;border:1px solid var(--line);border-radius:10px;color:var(--mute);font-size:12px}
main{padding:12px 16px;display:grid;gap:14px}section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow-x:auto}
h2{font-size:14px;margin:0 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--mute);font-weight:500;font-size:12px}
.running,.ok{color:var(--ok)}.created,.paused,.failing{color:var(--warn)}.exited,.unhealthy,.dead{color:var(--bad)}
button{font:inherit;font-size:12px;padding:2px 8px;border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--fg);cursor:pointer;margin-right:3px}
button:hover{border-color:var(--acc)}button.danger:hover{border-color:var(--bad);color:var(--bad)}button:disabled{opacity:.5;cursor:wait}
.shots{display:flex;flex-wrap:wrap;gap:10px}.shot{width:240px;cursor:pointer}.shot img{width:100%;aspect-ratio:16/10;object-fit:contain;background:#000;border-radius:5px;display:block}
.shot div{font-size:12px;color:var(--mute);margin-top:3px}canvas{vertical-align:middle;margin-right:4px}
#msg{color:var(--mute);font-size:12px;min-height:1em}#msg.err{color:var(--bad)}
textarea{width:100%;min-height:180px;font:12px ui-monospace,monospace;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:6px}
.ev{font:12px ui-monospace,monospace;white-space:pre-wrap;max-height:220px;overflow:auto;margin:0}
dialog{width:min(1100px,96vw);max-height:92vh;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--fg);padding:10px}
dialog img{max-width:100%;max-height:78vh;display:block;margin:auto}dialog pre{max-height:76vh;overflow:auto;font:12px ui-monospace,monospace;white-space:pre-wrap}
select{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px}.mute{color:var(--mute)}
</style></head><body>
<header><h1 id="tool">terminal</h1><span id="engine" class="chip">…</span><span id="daemon" class="chip">daemon …</span><span id="who" class="chip"></span><span id="online" class="mute"></span>
<label class="mute">project <select id="project"><option value="">all</option></select></label><span id="quota" class="mute"></span>
<span id="msg"></span></header>
<main>
<section><h2>Screens</h2><div id="shots" class="shots"><span class="mute">no running desktop</span></div></section>
<section><h2>Containers</h2><table><thead><tr><th>name</th><th>status</th><th>health</th><th>project</th><th>stack</th><th>CPU</th><th>memory</th><th>age</th><th>restarts</th><th>flags</th><th></th></tr></thead><tbody id="cts"></tbody></table><div id="ctnote" class="mute"></div></section>
<section><h2>VMs (terminal-vm)</h2><table><thead><tr><th>name</th><th>os</th><th>status</th><th>project</th><th>CPU</th><th>memory</th><th></th></tr></thead><tbody id="vms"></tbody></table><div id="vmnote" class="mute"></div></section>
<section><h2>Stacks</h2><table><thead><tr><th>stack</th><th>project</th><th>services (running / wanted)</th><th></th></tr></thead><tbody id="stacks"></tbody></table>
<p><button id="new">new stack</button></p><div id="editor" hidden><textarea id="spec" spellcheck="false"></textarea><p><button id="apply">apply</button><button id="close">close</button></p></div></section>
<section><h2>Events</h2><pre id="events" class="ev"></pre></section>
<section><h2>Actions (who did what)</h2><pre id="actions" class="ev"></pre></section>
</main>
<dialog id="dlg"><p><b id="dlgtitle"></b> <button id="dlgclose" style="float:right">close</button></p><div id="dlgbody"></div></dialog>
<script>
"use strict";
const $ = id => document.getElementById(id);
const h = (tag, props, ...kids) => { const e = document.createElement(tag); Object.assign(e, props || {});
  for (const k of kids) if (k != null) e.append(k instanceof Node ? k : String(k)); return e; };
let token = sessionStorage.getItem("tct-token") || "";
const frag = new URLSearchParams(location.hash.slice(1));
if (frag.get("t")) { token = frag.get("t"); sessionStorage.setItem("tct-token", token); }
if (frag.get("p")) sessionStorage.setItem("tct-project", frag.get("p"));
history.replaceState(null, "", location.pathname);  // the token out of the address bar
const hist = {}, HIST = 60; let last = null, shotTimer = 0, quotaBusy = false;
const project = () => $("project").value;
function msg(t, err) { $("msg").textContent = t; $("msg").className = err ? "err" : ""; }
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {"X-Token": token, "Content-Type": "application/json"}}, opts || {}));
  if (!r.ok) { let e = r.status + ""; try { e = (await r.json()).error; } catch (_) {} throw new Error(e); }
  return r;
}
const q = o => new URLSearchParams(Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v != null)));
async function act(body, button, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  if (button) button.disabled = true;
  const what = (body.op || "") + " " + (body.name || (body.stack && typeof body.stack === "object" ? body.stack.stack : body.stack) || "");
  msg(what + " …");
  try { const r = await (await api("/api/do", {method: "POST", body: JSON.stringify(body)})).json();
    msg(what + ": done" + (r && r.desktop_ready != null ? " (desktop ready in " + r.desktop_ready + " s)" : "")); return r; }
  catch (e) { msg(e.message, true); } finally { if (button) button.disabled = false; refresh(); }
}
function spark(values, max, colour) {
  const c = h("canvas", {width: 70, height: 18}), x = c.getContext("2d");
  x.strokeStyle = colour; x.lineWidth = 1.5; x.beginPath();
  values.forEach((v, i) => { const px = i * 70 / (HIST - 1), py = 17 - Math.min(1, (v || 0) / (max || 1)) * 16;
    i ? x.lineTo(px, py) : x.moveTo(px, py); });
  x.stroke(); return c;
}
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const age = s => s == null ? "-" : s < 120 ? Math.round(s) + "s" : s < 7200 ? Math.round(s / 60) + "m" : s < 172800 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d";
function button(label, body, confirmText, cls) {
  const b = h("button", {textContent: label, className: cls || ""}); b.onclick = () => act(body, b, confirmText); return b;
}
// the cells of buttons survive the refresh while their state is the same: rebuilt every 2 s, the button under the
// pointer was replaced, and a click landing on the swap was lost
function kept(tb) { const m = new Map(); for (const tr of tb.children) if (tr.cells_) m.set(tr.dataset.key, tr.cells_); return m; }
function cell(prev, key, sig, make) {
  const old = (prev.get(key) || {})[sig.split("|")[0]];
  if (old && old.sig === sig) return old;
  const td = h("td", null, ...make()); td.sig = sig; return td;
}
function keyed(key, cells, ...tds) { const tr = h("tr", null, ...tds); tr.dataset.key = key; tr.cells_ = cells; return tr; }
function renderCts(d) {
  const tb = $("cts"), prev = kept(tb); tb.replaceChildren();
  for (const r of d.rows) {
    const hh = hist[r.name] = hist[r.name] || {cpu: [], mem: []};
    hh.cpu.push(r.cpu_percent); hh.mem.push(r.used_memory_gb);
    const memMax = typeof r.memory_gb === "number" ? r.memory_gb : Math.max(1, ...hh.mem.filter(v => v != null));
    const cpuMax = 100 * (typeof r.cpus === "number" ? r.cpus : 1);
    if (hh.cpu.length > HIST) { hh.cpu.shift(); hh.mem.shift(); }
    const run = r.status === "running";
    const acts = cell(prev, r.name, "acts|" + r.status + "|" + r.flags + "|" + r.stack, () => [
        run ? button("stop", {op: "stop", name: r.name}) : button("start", {op: "start", name: r.name}),
        button("restart", {op: "restart", name: r.name}), logsButton(r.name),
        r.flags.includes("D") && run ? shotButton("ct", r.name) : null,
        button("rm", {op: "rm", name: r.name}, "Remove " + r.name + " and its /data?" + (r.stack ? " (a stack replica: the daemon makes it again, with an empty /data)" : ""), "danger")]);
    tb.append(keyed(r.name, {acts}, h("td", null, h("b", {textContent: r.name}),
      r.node ? h("span", {className: "mute", textContent: " @" + r.node, title: "in node " + r.node}) : null,
      r.description ? h("div", {className: "mute", textContent: r.description}) : null), h("td", {className: r.status, textContent: r.status}),
      h("td", {className: r.health.split(" ")[0], textContent: r.health}), h("td", {textContent: r.project}), h("td", {textContent: r.stack || "-"}),
      h("td", null, spark(hh.cpu, Math.max(100, cpuMax), css("--cpu")), r.cpu_percent == null ? "-" : r.cpu_percent + "%"),
      h("td", null, spark(hh.mem, memMax, css("--mem")), (r.used_memory_gb == null ? "-" : r.used_memory_gb) + " / " + (r.memory_gb || "-") + " GiB"),
      h("td", {textContent: age(r.age)}), h("td", {textContent: r.restarts}), h("td", {textContent: r.flags, title: "D desktop, S sandbox, I internet, A agent's"}),
      acts));
  }
  for (const k of Object.keys(hist)) if (!d.rows.some(r => r.name === k)) delete hist[k];
  if (!d.rows.length) tb.append(h("tr", null, h("td", {colSpan: 11, className: "mute", textContent: "no container"})));
}
function logsButton(name) {
  const b = h("button", {textContent: "logs"});
  b.onclick = async () => { const pre = h("pre"); openDlg("logs " + name, pre);
    try { pre.textContent = await (await api("/api/logs?" + q({name, n: 300}))).text(); pre.scrollTop = 1e9; } catch (e) { pre.textContent = e.message; } };
  return b;
}
function shotButton(kind, name) { const b = h("button", {textContent: "screen"}); b.onclick = () => bigShot(kind, name); return b; }
let dlgTimer = 0, dlgGen = 0;  // dlgGen: a refresh loop of an earlier view stops by itself
function openDlg(title, body) { dlgGen++; clearTimeout(dlgTimer); $("dlgtitle").textContent = title; $("dlgbody").replaceChildren(body); $("dlg").showModal(); }
$("dlgclose").onclick = () => $("dlg").close();
$("dlg").onclose = () => { dlgGen++; clearTimeout(dlgTimer);
  $("dlgbody").querySelectorAll("img").forEach(i => { if (i.src.startsWith("blob:")) URL.revokeObjectURL(i.src); }); };
async function loadShot(img, kind, name) {
  if (img.busy) return; img.busy = true;  // a slow capture (a VM: seconds): never two at once for one image
  try { const r = await api("/api/shot?" + q({kind, name})), url = URL.createObjectURL(await r.blob());
    const old = img.src; img.src = url; if (old.startsWith("blob:")) URL.revokeObjectURL(old); }
  finally { img.busy = false; }
}
function bigShot(kind, name) {
  const img = h("img", {alt: name}); openDlg((kind === "vm" ? "VM " : "") + name, img);
  const gen = dlgGen;
  const tick = async () => { try { if (!document.hidden) await loadShot(img, kind, name); } catch (e) { msg(e.message, true); }
    if ($("dlg").open && gen === dlgGen) dlgTimer = setTimeout(tick, kind === "vm" ? 2000 : 1000); };
  tick();
}
const shotEls = {};
async function renderShots(d, vms) {
  const want = d.rows.filter(r => r.flags.includes("D") && r.status === "running").map(r => ["ct", r.name])
    .concat(vms.filter(v => v.status === "running").map(v => ["vm", v.name]));  // looking wakes nothing
  const box = $("shots"), keys = want.map(w => w.join(":"));
  for (const k of Object.keys(shotEls)) if (!keys.includes(k)) { const s = shotEls[k].img.src;
    if (s.startsWith("blob:")) URL.revokeObjectURL(s); shotEls[k].remove(); delete shotEls[k]; }
  for (const [kind, name] of want) {
    const k = kind + ":" + name;
    if (!shotEls[k]) { const img = h("img", {alt: name}); shotEls[k] = h("div", {className: "shot", onclick: () => bigShot(kind, name)}, img,
        h("div", {textContent: (kind === "vm" ? "VM " : "") + name})); shotEls[k].img = img; box.append(shotEls[k]); }
  }
  if (!want.length) box.replaceChildren(h("span", {className: "mute", textContent: "no running desktop"}));
  else box.querySelectorAll(":scope > span").forEach(e => e.remove());
  if (Date.now() - shotTimer > 5000 && !document.hidden) { shotTimer = Date.now();
    for (const [kind, name] of want) loadShot(shotEls[kind + ":" + name].img, kind, name).catch(() => {}); }
}
function renderVms(v) {
  const tb = $("vms"), prev = kept(tb); tb.replaceChildren(); $("vmnote").textContent = v.note || "";
  for (const m of v.vms) {
    const run = m.status && m.status !== "stopped";
    const acts = cell(prev, m.name, "acts|" + run + "|" + (m.engine || ""), () => [
      m.engine ? null : run ? button("stop", {kind: "vm", op: "stop", name: m.name}, "Stop VM " + m.name + "?")
        : button("start", {kind: "vm", op: "start", name: m.name}), run ? shotButton("vm", m.name) : null,
      run && !m.engine ? button("force stop", {kind: "vm", op: "force_stop", name: m.name}, "Power off VM " + m.name + " at once (like pulling the plug)?", "danger") : null]);
    tb.append(keyed(m.name, {acts}, h("td", null, h("b", {textContent: m.name}),
      m.description ? h("div", {className: "mute", textContent: m.description}) : null), h("td", {textContent: m.os || "-"}),
      h("td", {className: run ? "running" : "exited", textContent: m.status || "-"}), h("td", {textContent: m.project || "-"}),
      h("td", {textContent: m.cpu_percent == null ? "-" : m.cpu_percent + "%"}),
      h("td", {textContent: (m.host_memory_gb == null ? "-" : m.host_memory_gb) + " / " + (m.memory_gb || "-") + " GiB"}),
      acts));
  }
  if (!v.vms.length) tb.append(h("tr", null, h("td", {colSpan: 7, className: "mute", textContent: "no VM"})));
}
function renderStacks(d) {
  const tb = $("stacks"), prev = kept(tb); tb.replaceChildren();
  for (const s of d.stacks) {
    const counts = Object.entries(s.services).map(([svc, c]) => [svc, c.replicas,
      d.rows.filter(r => r.stack.startsWith(s.stack + "/" + svc + "/") && r.status === "running").length]);
    const svcs = cell(prev, s.stack, "svcs|" + JSON.stringify(counts), () => counts.flatMap(([svc, want, runningN]) => {
      const sc = n => button(n > want ? "+" : "−", {op: "scale", stack: s.stack, service: svc, replicas: n});
      return [h("span", {textContent: svc + " " + runningN + "/" + want + " "}), want > 0 ? sc(want - 1) : null, sc(want + 1), " "];
    }));
    const acts = cell(prev, s.stack, "acts|", () => { const edit = h("button", {textContent: "edit"}); edit.onclick = () => editStack(s.stack);
      return [edit, button("rm", {op: "stack_rm", stack: s.stack}, "Remove stack " + s.stack + ", its containers and their /data?", "danger")]; });
    tb.append(keyed(s.stack, {svcs, acts}, h("td", null, h("b", {textContent: s.stack})), h("td", {textContent: s.project}), svcs, acts));
  }
  if (!d.stacks.length) tb.append(h("tr", null, h("td", {colSpan: 4, className: "mute", textContent: "no stack"})));
}
async function editStack(name) {
  try { const r = await (await api("/api/stack?" + q({name}))).json(); $("spec").value = JSON.stringify(r.desired, null, 1); $("editor").hidden = false; }
  catch (e) { msg(e.message, true); }
}
$("new").onclick = () => { $("spec").value = JSON.stringify({stack: "web", project: project() || undefined,
  services: {app: {image: "nginx", replicas: 2, cpus: 0.5, memory_gb: 0.5, restart: "always"}}}, null, 1); $("editor").hidden = false; };
$("close").onclick = () => { $("editor").hidden = true; };
$("apply").onclick = async () => { let s; try { s = JSON.parse($("spec").value); } catch (e) { return msg("JSON: " + e.message, true); }
  if (await act({op: "apply", stack: s}, $("apply"))) $("editor").hidden = true; };
function renderTop(d, v) {
  if (d.tool) {  // a state reply (the quota refresh passes only the quota)
  $("tool").textContent = d.tool; $("ctnote").textContent = d.note || "";
  $("engine").textContent = d.engine || "no container engine"; $("daemon").textContent = "daemon " + (d.daemon ? "running" : "stopped");
  $("daemon").style.color = d.daemon ? css("--ok") : css("--bad");
  const evs = (d.events || []).concat((v && v.events) || []).sort((a, b) => (a.time < b.time ? 1 : a.time > b.time ? -1 : 0)).slice(0, 12);
  $("events").textContent = evs.map(e => [e.time, e.event, e.stack, e.service, e.container, e.vm && "VM " + e.vm, e.detail].filter(Boolean).join("  ")).join("\n") || "no event";
  }
  if (d.quota) last = d.quota;
  if (last) $("quota").textContent = Object.entries(last).map(([k, v]) => k.replace("_gb", "") + " " + (v.used ?? "?") + "/" + (v.limit ?? "∞")).join(" · ");
  const sel = $("project"), cur = sel.value, names = [...new Set((d.projects || []).concat(cur ? [cur] : []))].sort();
  if (d.projects && names.join("\n") !== [...sel.options].slice(1).map(o => o.value).join("\n")) {  // rebuilt only when it changed: an open list stays open
    sel.replaceChildren(h("option", {value: "", textContent: "all"}), ...names.map(p => h("option", {value: p, textContent: p})));
    sel.value = cur; }
}
let busy = false, again = false;
function renderSession(s) {
  if (!s) return;
  $("who").textContent = s.user + (s.role === "read" ? " (read only)" : "");
  const others = s.online.filter(o => o.user !== s.user).map(o => o.user + (o.role === "read" ? " (read)" : ""));
  $("online").textContent = others.length ? "with " + others.join(", ") : "";
  $("actions").textContent = s.actions.map(a => [a.time, a.user, a.from, a.op || a.path, a.kind, a.stack, a.service,
    a.name, a.replicas, a.error && "failed: " + a.error].filter(x => x !== undefined && x !== "").join("  ")).join("\n")
    || "no action yet";
}
async function refresh() {
  if (busy) { again = true; return; }  // asked meanwhile (an action, another project): once this pass ends
  if (document.hidden) return; busy = true;  // a forgotten tab costs the engine nothing
  try {
    const p = project();
    const [d, v] = await Promise.all([api("/api/state?" + q({project: p})).then(r => r.json()),
      api("/api/vms?" + q({project: p})).then(r => r.json()).catch(e => ({vms: [], note: e.message}))]);
    if (p !== project()) return;  // the project changed meanwhile: the next pass shows it
    renderTop(d, v); renderCts(d); renderVms(v); renderStacks(d); renderShots(d, v.vms);
    renderSession(await api("/api/session").then(r => r.json()).catch(() => null));
    if ($("msg").className === "err" && $("msg").textContent.startsWith("dashboard:")) msg("");
  } catch (e) { msg("dashboard: " + e.message, true); }
  finally { busy = false; if (again) { again = false; refresh(); } }
}
async function refreshQuota() {  // apart: measuring the disk takes seconds, the table must not wait for it
  if (quotaBusy || document.hidden) return; quotaBusy = true;
  try { const p = project(), r = await (await api("/api/quota?" + q({project: p}))).json();
    if (p === project()) renderTop({quota: r.quota, projects: r.projects}); }
  catch (e) { msg("quota: " + e.message, true); } finally { quotaBusy = false; }
}
$("project").onchange = () => { sessionStorage.setItem("tct-project", project()); last = null; $("quota").textContent = ""; refresh(); refreshQuota(); };
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); refreshQuota(); } });
const saved = sessionStorage.getItem("tct-project");
if (saved) $("project").append(h("option", {value: saved, textContent: saved})), $("project").value = saved;
if (!token) msg("no token: open the dashboard with the web command (terminal-ct web, terminal-vm web)", true); else { refresh(); refreshQuota(); setInterval(refresh, 2000); setInterval(refreshQuota, 15000); }
</script></body></html>
"""
# ===== end of shared web dashboard =====


AGENT_CONNECTIONS = 64


class _AgentServer(_WebServer):
    """At most AGENT_CONNECTIONS clients at once (the dashboard server's cap): idle connections (15 s each) never
    use up threads or memory."""


class _AgentServer6(_AgentServer):
    address_family = socket.AF_INET6


# ---------- snapshots, clones, files ----------
# A snapshot is the container's file system committed to an image (tct-snap-NAME:TAG) plus its /data volume as a
# tar file (STORE/NAME/snaps/TAG.tar): load brings both back. A clone starts from the same two. Sandboxes have
# neither: they start from their image at every start anyway.
SNAP = "tct-snap-"


def _data_tar(name, image, path, mode="save"):
    """NAME's /data volume through a throwaway container of `image` (as root, offline, the volume its only mount).
    save: /data to a tar file. stage: unpack a tar file into /data/.tct-restore, leaving /data as it is (a failure
    loses nothing). swap: replace /data's content by the staged one. clear: drop a staged copy."""
    base = ["run", "--rm", "-i", "--network", "none", "--user", "0", "-v", f"{PREFIX}{name}-data:/data",
            "--entrypoint", "sh", image, "-c"]
    staged = "/data/.tct-restore"
    script = {"save": "tar -C /data -cf - .",
              "stage": f"rm -rf {staged} && mkdir {staged} && tar -C {staged} -xf -",
              "swap": f"cd /data && for f in * .[!.]* ..?*; do [ -e \"$f\" ] && [ \"$f\" != .tct-restore ] && rm -rf \"$f\"; done; "
                      f"cd {staged} && for f in * .[!.]* ..?*; do [ -e \"$f\" ] && mv \"$f\" /data/; done; cd / && rmdir {staged}",
              "clear": f"rm -rf {staged}"}[mode]
    node().pipe([*base, script], stdin=path if mode == "stage" else None, stdout=path if mode == "save" else None)


def _snap_dir(name):
    d = os.path.join(ct_dir(name), "snaps")
    os.makedirs(d, exist_ok=True)
    return d


def _check_tag(tag):
    if not re.fullmatch(r"[a-z0-9]([a-z0-9_.-]{0,62}[a-z0-9])?", tag or ""):
        raise SystemExit(f"invalid tag: {tag!r} (lowercase letters, digits, _ . -; ends with a letter or digit)")
    return tag


def _snap_entries(name):
    """{tag: {"bytes": what the commit added, "image": the image that wrote its /data tar}}."""
    try:
        with open(os.path.join(STORE, name, "snaps", "index.json")) as f:
            idx = json.load(f)
    except (OSError, ValueError):
        return {}
    return {t: v if isinstance(v, dict) else {"bytes": v} for t, v in idx.items()}


def snapshots(name):
    """{tag: bytes} of NAME's snapshots (what each commit added, plus its /data tar)."""
    d = os.path.join(STORE, name, "snaps")
    return {t: e.get("bytes", 0) + (os.path.getsize(os.path.join(d, t + ".tar")) if os.path.isfile(os.path.join(d, t + ".tar")) else 0)
            for t, e in _snap_entries(name).items()}


def _snap_index(name, tag, entry=None):
    """Add (entry) or drop (None) one snapshot, under the lock: two saves at once must not lose one."""
    with quota_lock():
        idx = _snap_entries(name)
        if entry is None:
            idx.pop(tag, None)
        else:
            idx[tag] = entry
        with open(os.path.join(_snap_dir(name), "index.json"), "w") as f:
            json.dump(idx, f)


@contextlib.contextmanager
def paused(info):
    """A running container paused: its file system and /data are taken at the same instant."""
    on = (info.get("State") or {}).get("Status") == "running"
    if on:
        node().run(["pause", info["Name"].lstrip("/")])
    try:
        yield
    finally:
        if on:
            node().run(["unpause", info["Name"].lstrip("/")], check=False)


def _layer(info):
    return (node().json(["inspect", "--type", "container", "--size", info["Name"].lstrip("/")]) or [{}])[0].get("SizeRw") or 0


def snapshot(name, cmd, tag=None):
    info = find(name)
    if cmd == "ls":
        return {"name": name, "snapshots": snapshots(name)}
    _check_tag(tag)
    if labels_of(info).get(f"{LABEL}.sandbox") == "1":
        raise SystemExit(f"{name} is a sandbox: it starts from its image every time, a snapshot would never be used")
    ref, tar = f"{SNAP}{name}:{tag}", os.path.join(_snap_dir(name), tag + ".tar")
    if cmd == "save":
        if tag in _snap_entries(name):
            raise SystemExit(f"{name} already has a snapshot {tag!r} (snapshot {name} rm {tag} first)")
        layer = _layer(info)
        quota_check({"disk_gb": layer / 2 ** 30})  # its /data tar comes on top: measured once written
        _snap_index(name, tag, {"bytes": layer, "image": info["Image"]})  # first: a crash leaves it for rm to clean
        try:
            with paused(info):
                node().run(["commit", PREFIX + name, ref])
                _data_tar(name, info["Image"], tar)
        except BaseException:
            node().run(["image", "rm", ref], check=False)
            with contextlib.suppress(OSError):
                os.remove(tar)
            _snap_index(name, tag)
            raise
        return {"name": name, "snapshot": tag, "saved": True, "bytes": snapshots(name)[tag]}
    entry = _snap_entries(name).get(tag)
    if entry is None:
        raise SystemExit(f"{name} has no snapshot {tag!r} (snapshot {name} ls)")
    if cmd == "load":
        if running(info):
            raise SystemExit(f"stop {name} first (terminal-ct stop {name})")
        helper = entry.get("image") or info["Image"]  # the image whose tar wrote it: it has tar
        # /data unpacked aside first, the container remade (put back if that fails), then /data swapped: no step
        # leaves the user with neither the old state nor the snapshot's
        _data_tar(name, helper, tar, "stage")
        try:
            spec = json.loads(labels_of(info).get(f"{LABEL}.spec") or "{}")
            remake(name, info, image=ref, prebuilt=bool(spec.get("desktop") or labels_of(info).get(f"{LABEL}.desktop") == "1"))
        except BaseException:
            _data_tar(name, helper, None, "clear")
            raise
        _data_tar(name, helper, None, "swap")
        return {"name": name, "snapshot": tag, "loaded": True}
    if cmd == "rm":
        spec = json.loads(labels_of(info).get(f"{LABEL}.spec") or "{}")
        if spec.get("image") == ref:
            raise SystemExit(f"{name} runs from snapshot {tag!r}: load another one first, or keep it")
        r = node().run(["image", "rm", ref], check=False)
        if r.returncode and node().run(["image", "inspect", ref], check=False).returncode == 0:
            raise SystemExit(f"snapshot {tag!r} of {name} is still used (a container made from it?): "
                             f"{(r.stderr or r.stdout).strip()}")
        with contextlib.suppress(OSError):
            os.remove(tar)
        _snap_index(name, tag)
        return {"name": name, "snapshot": tag, "removed": True}
    raise SystemExit("snapshot NAME save|load|rm TAG | snapshot NAME ls")


def clone(src, new, created_by="user", project=None, internet=None):
    """NEW from SRC as it is now: its file system (committed to tct-keep-NEW, removed with NEW) and a copy of its
    /data, same ceiling and project. Networks are not copied; internet as SRC unless said otherwise."""
    info = find(src)
    check_name(new)
    recover(new)
    if node().run(["inspect", "--type", "container", PREFIX + new], check=False).returncode == 0:
        raise SystemExit(f"{new!r} already exists")
    if node().run(["volume", "inspect", f"{PREFIX}{new}-data"], check=False).returncode == 0:
        raise SystemExit(f"a volume {PREFIX}{new}-data is left from an earlier {new}: keep it under another name, "
                         f"or remove it: {node().engine} volume rm {PREFIX}{new}-data")
    lab = labels_of(info)
    spec = json.loads(lab.get(f"{LABEL}.spec") or "null") or legacy_spec(info)
    sandbox = lab.get(f"{LABEL}.sandbox") == "1"
    cpus, mem = allotted(info, None)
    project = check_project(project or lab.get(f"{LABEL}.project") or DEFAULT_PROJECT)
    tar = os.path.join(ct_dir(new), "clone.tar")
    try:
        if not sandbox:  # a sandbox clone is a new sandbox of the same image: nothing of SRC's to carry
            ref, layer = f"{KEEP}{new}:latest", _layer(info)
            quota_check({"disk_gb": layer / 2 ** 30})
            with paused(info):
                node().run(["commit", PREFIX + src, ref])
                _data_tar(src, info["Image"], tar)
            spec = dict(spec, image=ref, prebuilt=bool(spec.get("desktop")))
            with open(os.path.join(ct_dir(new), "keep_bytes"), "w") as f:
                f.write(str(int(layer)))
        out = _make(new, spec, created_by, cpus or DEFAULT_CPUS, mem or DEFAULT_MEMORY,
                    online(info) if internet is None else internet, (), project, fresh=True)
        if not sandbox:
            _data_tar(new, info["Image"], tar, "stage")
            _data_tar(new, info["Image"], None, "swap")
    except BaseException:  # no half-made clone: only what is NEW's (it did not exist, nor its volume)
        node().run(["rm", "-f", PREFIX + new], check=False)
        node().run(["volume", "rm", f"{PREFIX}{new}-data"], check=False)
        node().run(["image", "rm", f"{KEEP}{new}:latest"], check=False)
        shutil.rmtree(os.path.join(STORE, new), ignore_errors=True)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.remove(tar)
    want(new, "stopped")
    return dict(out, cloned_from=src)


def transfer(name, direction, src, dest):
    """push NAME LOCAL PATH | pull NAME PATH LOCAL: files in or out, running or stopped. A sandbox's /data is in
    memory: reachable only while it runs, and gone at its stop."""
    find(name)
    if isinstance(node(), NodeVM):  # the engine is in the node: this host's paths cross by its guest agent
        node().transfer(direction, src, PREFIX + name, dest)
        return {direction: name, "from": src, "to": dest}
    args = [src, f"{PREFIX}{name}:{dest}"] if direction == "push" else [f"{PREFIX}{name}:{src}", dest]
    # -a: the host file's owner kept (Docker made it root's, which a desktop's user could not change; Podman's
    # default already)
    node().run(["cp", *(["-a"] if direction == "push" else []), *args])
    return {direction: name, "from": src, "to": dest}


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
    if isinstance(node(), NodeVM):
        raise RuntimeError(f"{name} lives in node {node().name}: its screen is reached through screenshot and the "
                           "other actions (view and show work for this host's desktops)")
    path = os.path.join(STORE, name, "run", "vnc.sock")
    for _ in range(50):  # the desktop may still be starting
        if os.path.exists(path):
            break
        time.sleep(0.1)
    return RFB(path)


def act(name, tool, a):
    """Shared by the CLI and MCP. Coordinates are pixels of the latest screenshot."""
    if isinstance(node(), NodeRemote):  # never terminal-ct copied onto someone's PC
        raise RuntimeError(f"{name} is on another PC ({node().name}): no desktop there yet (phase 11 bis)")
    if isinstance(node(), NodeVM):  # the desktop's socket is in the node: terminal-ct there does it
        return node().act(name, tool, a)
    if tool == "ready":
        return desktop_ready(name, float(a.get("timeout", 10)))
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
    """The screen's content, to see it change: the raw frame here; in a node, its screenshot (same use)."""
    if isinstance(node(), NodeVM):
        return act(name, "screenshot", {})[0]
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
                        with captured(io.StringIO()):
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
_DESC_ARG = {"description": {"type": "string", "description": "what it is for, one line (who uses it, its role)"}}
AGENT_TOOLS = [
    ("ct_list", "terminal-ct's containers; managed=true marks the ones this agent runs without asking." + _MANAGED,
     _schema({})),
    ("ct_create", "Create a container, sandboxed by default (not root, no capabilities, process limit, /data in "
                  "memory, a fresh file system at each start, offline). image: default debian:stable-slim; desktop: "
                  "add a screen (Debian/Ubuntu, Fedora, Alpine, Arch bases). Needs the user's approval: an image not "
                  "on this host yet (download or build), internet, a network of the user's.",
     _schema({**_NAME, "image": {"type": "string"}, "desktop": {"type": "boolean"}, "width": {"type": "integer"},
              "height": {"type": "integer"}, "cpus": _N, "memory_gb": _N, "sandbox": {"type": "boolean"},
              "internet": {"type": "boolean"}, "networks": {"type": "array", "items": {"type": "string"}},
              **_DESC_ARG}, ["name"])),
    ("ct_start", "Start a container, within the host quota (a sandbox starts fresh from its image)." + _MANAGED,
     _schema(_NAME, ["name"])),
    ("ct_stop", "Stop a container.", _schema({**_NAME, "timeout": {"type": "integer"}}, ["name"])),
    ("ct_remove", "Delete a container and its /data volume (one the agent did not create needs approval).",
     _schema(_NAME, ["name"])),
    ("ct_snapshot", "Snapshots of a container (file system and /data): save, load (stopped), rm or ls. Loading or "
                    "removing one in a container the agent did not create needs the user's approval. Not for sandboxes.",
     _schema({**_NAME, "action": {"type": "string", "enum": ["save", "load", "rm", "ls"]}, "tag": {"type": "string"}},
             ["name", "action"])),
    ("ct_clone", "A new container from one as it is now (file system and a copy of /data), in seconds. Cloning one "
                 "of the user's containers needs their approval, and the clone starts offline.",
     _schema({**_NAME, "new": {"type": "string"}, "project": {"type": "string"}, **_DESC_ARG}, ["name", "new"])),
    ("ct_apply", "Apply a desired state: {\"stack\": NAME, \"services\": {SERVICE: {image, replicas, cpus, memory_gb, "
                 "desktop, sandbox (default true), internet, networks, cmd, restart: always|on-failure|never, health: "
                 "{exec, every, retries}, ttl: 30m}}}. Replicas are NAME-SERVICE-1..N; a daemon keeps them so "
                 "(restarts with backoff, health probes, ttl). Same approvals as ct_create.",
     _schema({"stack": {"type": "object"}, "project": {"type": "string"}}, ["stack"])),
    ("ct_scale", "Set the number of replicas of a stack's service.",
     _schema({"stack": {"type": "string"}, "service": {"type": "string"}, "replicas": {"type": "integer"}},
             ["stack", "service", "replicas"])),
    ("ct_stack", "Stacks: ls, show (services, replicas with restarts, latest events) or rm (its containers too).",
     _schema({"action": {"type": "string", "enum": ["ls", "show", "rm"]}, "stack": {"type": "string"}}, ["action"])),
    ("ct_events", "The event log: created, started, restarted, unhealthy, expired, pending (quota)...",
     _schema({"stack": {"type": "string"}, "n": {"type": "integer"}})),
    ("ct_resources", "Show, or change at once (even running), a container's CPUs and memory within the quota.",
     _schema({**_NAME, "cpus": _N, "memory_gb": _N}, ["name"])),
    ("ct_internet", "Show (without on) or change a stopped container's internet access; on needs the user's approval.",
     _schema({**_NAME, "on": {"type": "boolean"}}, ["name"])),
    ("net_list", "terminal-ct's isolated networks (no LAN, no internet) and their containers, which reach each "
                 "other by name.", _schema({})),
    ("net_create", "Create an isolated container-to-container network.", _schema({**_NET, **_DESC_ARG}, ["net"])),
    ("describe", "Set what an object is for, in one line (at most 200 characters; \"\" clears it), so the next agent "
                 "knows who does what: a container (a stack's service: its description field), a network, a node, "
                 "another PC (host) or a project. Free on the agent's own containers and networks (and its own "
                 "project); the user's need their approval.",
     _schema({"kind": {"type": "string", "enum": list(DESC_KINDS)}, "name": {"type": "string"},
              "description": {"type": "string"}}, ["kind", "name", "description"])),
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
    ("dashboard", "The dashboard as text: every container (state, health, stack/replica, CPU and memory used next "
                  "to its ceiling, age, restarts, flags D desktop S sandbox I internet A agent's), the daemon, the "
                  "quota, the latest events. What the user sees with terminal-ct dash.", _schema({})),
    ("ct_logs", "The last lines a container printed (stdout and stderr); n: how many (default 100).",
     _schema({**_NAME, "n": {"type": "integer"}}, ["name"])),
    ("infra", "Start here: the map of the whole infrastructure in one call. Each PC and node (reachable or not, "
              "its quota: limit, used, free), every container with where it runs, its ceiling and use, networks, "
              "ports, description, project, creator and managed (what you may run without asking), each network "
              "with the names that reach its members, terminal-vm's VMs, the projects. Then project_status for "
              "your notes and recent actions.", _schema({})),
    ("project_status", "Then this: the project's containers and networks as they are now (want: what each should "
                       "be doing), the notes left by earlier sessions, and the latest actions with their errors.",
     _schema({"project": {"type": "string", "description": "Only when this server has no project of its own"}})),
    ("notes_write", "Keep notes for the next session (what is set up, what is left to do): appended with the date, "
                    "or mode=replace to rewrite them.",
     _schema({"text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]},
              "project": {"type": "string"}}, ["text"])),
]
INSTRUCTIONS = ("terminal-ct: containers with a desktop and a shell. Call infra first: the map of who runs what "
                "where, with what resources and descriptions. Then project_status: what this project has and what "
                "earlier sessions did and noted. Describe what you create (description, or describe). Before you stop, record with "
                "notes_write what you set up and what is left, so the next session can pick up from there.")
AGENT_NAMES = {n for n, _, _ in AGENT_TOOLS}
PROJECT_ARG_TOOLS = {"ct_create", "ct_clone", "ct_apply", "net_create", "project_status", "notes_write"}  # take `project` without one of their own
AGENT_LOG = "agent.log"


def _image(data):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}


def quiet(f, *a, **k):
    """Call a CLI function that prints its JSON result (stdout is the MCP channel) and return that result."""
    buf = io.StringIO()
    with captured(buf):
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
    if not under(p, project):  # the same answer as for no container: other projects' names stay unseen
        n = name or labels_of(info).get(LABEL + '.name')
        raise SystemExit(f"no container named {n!r}; create it with: terminal-ct create {n}")
    return info


def guard(name, approve, what, cli, destructive=False, project=None):
    info = in_scope(find(name), project, name)
    if not managed(info) or destructive and not agent_owned(info):
        approve(f"Let the agent {what} your container {name}?", cli)
    return info


def local_image(ref):
    return any(node().run(["image", "inspect", r], check=False).returncode == 0 for r in (ref, qualified(ref)))


def creation_approvals(name, img, desk, internet, wanted_nets, approve, nets, project):
    """What a creation by an agent needs the user's approval for: an image not here yet, internet, the user's
    networks. A network in another project does not exist for a scoped agent. Returns the user's networks approved."""
    if not (desk and local_image(desktop_tag(img))):
        if not local_image(img):
            approve(f"Download the image {img} for {name}?", f"terminal-ct image pull {img}")
        if desk:
            approve(f"Build the desktop image for {img} (packages from the internet, a few minutes)?",
                    f"terminal-ct create {name} --desktop --image {img}")
    if internet:
        approve(f"Give {name} internet access?", f"terminal-ct internet {name} on")
    approved = []
    for n in wanted_nets:
        if project and n not in nets and n in networks():
            raise SystemExit(f"no network {n!r}; create it with: terminal-ct net create {n}")
        if n in networks() and nets.get(n, {}).get("created_by") != "agent":  # a new one is created as the agent's
            approve(f"Plug {name} into your network {n!r} (it would reach your containers on it)?",
                    f"terminal-ct net attach {name} {n}")
            approved.append(n)
    return approved


def stack_in_scope(stack, project, approve=None):
    """An agent's own stack in its project; someone else's needs approval (approve) or is refused (no approve)."""
    s = load_stack(stack)
    if not under(s.get("project") or DEFAULT_PROJECT, project):
        raise SystemExit(f"no stack named {stack!r}; apply one: terminal-ct apply FILE")
    if s.get("created_by") != "agent":
        if approve is None:
            raise SystemExit(f"stack {stack!r} belongs to the user")
        pcs = sorted(n for n, i in _nodes()["nodes"].items() if i.get("kind") == "host"
                     and any(c.get("spread") or c.get("node") == n for c in s["services"].values()))
        approve(f"Let the agent change your stack {stack}?" + (f" It runs on your other PCs too ({', '.join(pcs)})."
                                                                if pcs else ""), f"terminal-ct stack ... {stack}")
    return s


def agent_describe(kind, name, text, approve, project=None):
    """describe through MCP: free on the agent's own containers and networks and on its server's project (and
    below); the user's objects, nodes and PCs need their approval. Outside the agent's project: not found."""
    text = check_description(text)
    if not isinstance(name, str) or kind not in DESC_KINDS:
        raise SystemExit(f"kind is one of {', '.join(DESC_KINDS)}, and name a string")
    cli = f"terminal-ct describe {kind} {shlex.quote(name)} " + (shlex.quote(text) if text else "--clear")
    ask = lambda what: approve(f"Let the agent describe {what} as: {text!r}?" if text else
                               f"Let the agent clear the description of {what}?", cli)
    if kind == "container":
        info = in_scope(find(check_name(name)), project, name)
        if not agent_owned(info):
            ask(f"your container {name}")
        return describe(kind, name, text)
    _LOCAL.node = None  # not a container: never routed to the node of a container of the same name
    if kind == "net":
        n = networks().get(check_name(name))
        owner = n["created_by"] if n is not None and under(n["project"], project) else \
            stack_net_owner(name, project) if n is None else None  # another project's: the same as none
        if owner is None:
            raise SystemExit(f"no network {name!r}; create it with: terminal-ct net create {name}")
        if owner != "agent":
            ask(f"your network {name}")
    elif kind == "project":
        if not under(check_project(name), project):
            raise SystemExit(f"project {name!r} is not this agent's project {project!r} or below it")
        if not project:
            ask(f"the project {name}")
    else:
        if name not in _nodes()["nodes"] or is_host(name) != (kind == "host"):
            raise SystemExit(f"no {kind} named {name!r} (terminal-ct {kind} ls)")
        ask(f"your {'other PC' if kind == 'host' else 'node'} {name}")
    return describe(kind, name, text)


def agent_call(tool, a, approve, project=None):
    """project: the server's own (--project); then nothing outside it is visible or reachable."""
    name, net = a.get("name"), a.get("net")
    mine = lambda p: under(p, project)
    nets = {}
    if tool.startswith("net_") or tool in ("ct_create", "ct_apply"):  # its networks, and only its containers on them
        own = {c["name"] for c in containers() if mine(c["project"])}
        nets = {k: dict(v, members=[m for m in v["members"] if m in own])
                for k, v in networks().items() if mine(v["project"])}
    guard_ = lambda what, cli, destructive=False: guard(name, approve, what, cli, destructive, project)
    # the project a creation or a note goes to: the one named if it is this server's or below it, else the server's
    asked = a.get("project")
    if asked is not None and not under(check_project(asked), project):
        raise SystemExit(f"project {asked!r} is not this agent's project {project!r} or below it")
    where = asked or project
    if name and project and tool not in ("ct_create", "describe"):  # reads too: another project's containers are not this agent's business
        in_scope(find(name), project, name)
    if tool == "ct_list":
        return [dict(normalize(c), managed=managed(c), **({"node": where} if where else {}))
                for where, cs in everywhere() for c in cs if mine(labels_of(c).get(f"{LABEL}.project") or DEFAULT_PROJECT)]
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
    if tool == "dashboard":
        return dashboard_text(dashboard_data(project), width=160)
    if tool == "ct_logs":
        guard_("read the output of", f"terminal-ct logs {name}")
        return logs(name, int(a.get("n", 100)))
    if tool == "autostart":
        if a.get("on") is not None:
            approve(("Start the containers that were running again by themselves after each restart of the PC "
                     "(a task at your logon)?") if a["on"] else "Stop restarting containers after a restart of the PC?",
                    f"terminal-ct autostart {'on' if a['on'] else 'off'}")
        return autostart(a.get("on"))
    if tool == "project_status":
        return project_status(where)
    if tool == "infra":
        return infra(project)
    if tool == "notes_write":
        return notes_write(where, a["text"], a.get("mode", "append"))
    if tool == "net_list":
        return nets
    if tool == "describe":
        return agent_describe(a.get("kind"), name, a.get("description", ""), approve, project)
    if tool == "net_create":
        if check_name(net) in networks():
            raise SystemExit(f"the network name {net!r} is not available: choose another")
        desc = check_description(a.get("description") or "")
        out = net_create(net, "agent", where)
        if desc:
            set_description("net", net, desc)
        return out
    if tool in ("net_remove", "net_attach") and check_name(net) not in nets and net in networks():
        raise SystemExit(f"no network {net!r}; create it with: terminal-ct net create {net}")
    if tool == "net_remove":
        if nets.get(net, {}).get("created_by") != "agent":
            raise SystemExit(f"network {net!r} is not one this agent created")
        return net_rm(net)
    if tool == "ct_create":
        if name in _nodes()["placement"]:  # an agent's new container starts on this PC, never through a name's node
            raise SystemExit(f"the name {name!r} is not available: choose another")
        if node().run(["inspect", "--type", "container", PREFIX + check_name(name)], check=False).returncode == 0:
            raise SystemExit(f"the name {name!r} is not available: choose another")  # same answer, any project
        img, desk = a.get("image") or DEFAULT_IMAGE, bool(a.get("desktop"))
        desc = check_description(a.get("description") or "")
        creation_approvals(name, img, desk, a.get("internet"), a.get("networks") or [], approve, nets, project)
        out = quiet(create, name, img, a.get("cpus"), a.get("memory_gb"), created_by="agent", desktop=desk,
                    width=int(a.get("width", 1280)), height=int(a.get("height", 800)),
                    sandbox=a.get("sandbox", True), internet=bool(a.get("internet")), nets=a.get("networks") or (),
                    project=where)
        if desc:
            set_description("container", name, desc)
        return out
    if tool == "ct_apply":
        st = a.get("stack")
        if not isinstance(st, dict):
            raise SystemExit("stack: the desired state as an object")
        st, approved = check_stack(st), []
        for svc, c in st["services"].items():
            c.setdefault("sandbox", True)  # an agent's services are sandboxes unless it says otherwise
            if c.get("internet") and c.get("sandbox"):
                raise SystemExit(f"service {svc}: a sandbox is offline; set sandbox false to give it internet")
            approved += creation_approvals(f"{st['stack']}-{svc}", c.get("image") or DEFAULT_IMAGE, bool(c.get("desktop")),
                                           c.get("internet"), c.get("networks") or [], approve, nets, project)
        if st["stack"] in stacks():
            stack_in_scope(st["stack"], project)
        hosts = [n for n, i in _nodes()["nodes"].items() if i.get("kind") == "host"]
        before = load_stack(st["stack"]).get("approved_hosts", []) if st["stack"] in stacks() else []
        allowed = [h for h in before if h in hosts]
        for h in sorted({c["node"] for c in st["services"].values() if c.get("node") in hosts} |
                        (set(hosts) if any(c.get("spread") for c in st["services"].values()) else set())):
            if h not in allowed:  # someone's PC: the user says so, once per PC for this stack
                approve(f"Let the agent run containers of its stack {st['stack']} on your other PC {h}?",
                        f"terminal-ct apply STACK.json (yourself), or keep the agent's stack on this PC")
                allowed.append(h)
        return apply(st, created_by="agent", project=where, approved_networks=approved, approved_hosts=allowed)
    if tool == "ct_scale":
        stack_in_scope(a["stack"], project, approve)
        return scale(a["stack"], a["service"], int(a["replicas"]))
    if tool == "ct_stack":
        what = a.get("action", "ls")
        if what == "ls":
            return [dict(stack_show(n), events=None) for n in stacks()
                    if under(load_stack(n).get("project") or DEFAULT_PROJECT, project)]
        stack_in_scope(a["stack"], project, approve if what == "rm" else lambda m, c: None)
        return stack_show(a["stack"]) if what == "show" else stack_rm(a["stack"])
    if tool == "ct_events":
        return events(a.get("stack"), int(a.get("n", 50)), project)
    if tool == "ct_snapshot":
        act_ = a.get("action", "ls")
        if act_ in ("load", "rm"):
            guard_(f"{act_} snapshot {a.get('tag')} of", f"terminal-ct snapshot {name} {act_} {a.get('tag')}",
                   destructive=True)
        elif act_ == "save":
            guard_("snapshot", f"terminal-ct snapshot {name} save {a.get('tag')}")
        return snapshot(name, act_, a.get("tag"))
    if tool == "ct_clone":
        info = find(name)
        users = not managed(info)
        if users:  # a copy of their files the agent would own
            approve(f"Clone your container {name} as {a['new']} (a copy of its files the agent manages, offline)?",
                    f"terminal-ct clone {name} {a['new']}")
        if node().run(["inspect", "--type", "container", PREFIX + check_name(a["new"])], check=False).returncode == 0:
            raise SystemExit(f"the name {a['new']!r} is not available: choose another")
        if isinstance(node(), NodeRemote):  # a new container on someone's PC: through an approved stack only
            raise SystemExit(f"{name} is on another PC ({node().name}): an agent clones only on this PC")
        desc = check_description(a.get("description") or "")
        out = clone(name, a["new"], created_by="agent", project=where or labels_of(info).get(f"{LABEL}.project"),
                    internet=False if users else None)
        if desc:
            set_description("container", a["new"], desc)
        return out
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
        with contextlib.suppress(SystemExit):
            if project and not under(labels_of(find(name)).get(f"{LABEL}.project") or DEFAULT_PROJECT, project):
                name = None  # its container is outside the project: serve without one of its own
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
            path = os.path.join(project_dir(p), AGENT_LOG) if tool in AGENT_NAMES or tool == "exec" else \
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
            target = (None if tool == "describe" and a.get("kind") != "container" else a.get("name")) \
                if tool in AGENT_NAMES else a.pop("container", None) or name
            where = _nodes()["placement"].get(target) if isinstance(target, str) else None
            _LOCAL.node = None
            try:
                _LOCAL.node = node_obj(where) if where else None  # a container in a node: every engine call goes there
                if tool in AGENT_NAMES:
                    with captured(io.StringIO()):  # engine output must not reach the MCP channel
                        r = agent_call(tool, a, approve, project)
                        content = [{"type": "text", "text": r if isinstance(r, str) else json.dumps(r)}]
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
                        out = quiet(exec_, target, ["sh", "-c", a["command"]],
                                    max(1.0, min(float(a.get("timeout", 60)), 600)))
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


# ---------- measurements and the agent's skill ----------
BENCH_IMAGE = "alpine:3.20"


def _timed(f, *a, **k):
    t = time.perf_counter()
    quiet(f, *a, **k)
    return (time.perf_counter() - t) * 1000


def _summary(ms):
    ms = sorted(ms)
    return {"median_ms": round(ms[len(ms) // 2] if len(ms) % 2 else (ms[len(ms) // 2 - 1] + ms[len(ms) // 2]) / 2, 1),
            "min_ms": round(ms[0], 1), "max_ms": round(ms[-1], 1), "runs": len(ms)}


def bench(runs=10, desktop=False):
    """Time terminal-ct's own operations on this engine: median, min and max of `runs` cycles of create, start,
    exec, stop, rm on a small image, the listing and the dashboard's data; with desktop, the desktop's readiness,
    a screenshot, its memory. Uses containers named bench-*, removed after; the quota applies as for any start."""
    if desktop and WINDOWS:
        raise SystemExit("bench --desktop runs on a Linux host or in WSL (desktop containers need one)")
    if not local_image(BENCH_IMAGE):
        pull(BENCH_IMAGE)
    times, extra, made = collections.defaultdict(list), {}, set()
    base = f"bench-{os.getpid()}-{secrets.token_hex(2)}"  # never an existing name: only bench's own are removed

    def make(name, **k):
        r = _timed(create, name, BENCH_IMAGE if not k else DEFAULT_IMAGE, **k)
        made.add(name)
        return r
    try:
        name = base + "-ct"
        for _ in range(runs):
            times["create"].append(make(name))
            times["start"].append(_timed(start, name))
            times["exec_true"].append(_timed(exec_, name, ["true"]))
            times["ls"].append(_timed(containers))
            times["dashboard_data"].append(_timed(dashboard_data, None, False))
            times["stop"].append(_timed(stop, name, 1))
            times["rm"].append(_timed(rm, name))
            made.discard(name)
        if desktop:
            name = base + "-desk"
            make(name, desktop=True)  # the first one builds the desktop image: not counted
            for _ in range(max(3, runs // 3)):
                r = quiet(start, name)
                if r.get("desktop_ready") is not None:  # absent: not ready within 10 s
                    times["desktop_ready"].append(r["desktop_ready"] * 1000)
                times["screenshot"].append(_timed(act, name, "screenshot", {}))
                extra["desktop_memory_mb"] = round(
                    _stats([find(name)]).get(PREFIX + name, {}).get("used_memory_gb", 0) * 1024)
                quiet(stop, name, 1)
    finally:
        for name in made:
            with contextlib.suppress(SystemExit):
                quiet(rm, name)
    host = platform.platform() + (" (WSL)" if in_wsl() else "")
    version = node().run(["version", "--format", "{{.Client.Version}}"], check=False).stdout.strip()
    return {"tool": f"terminal-ct {VERSION}", "engine": f"{node().engine} {version}", "host": host,
            "date": time.strftime("%Y-%m-%d"), "image": BENCH_IMAGE,
            "results": {k: _summary(v) for k, v in times.items()}, **extra}


SKILL = next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "terminal-ct", "SKILL.md"),
                          os.path.join(sys.prefix, "share", "terminal-ct", "skills", "terminal-ct", "SKILL.md"),  # pip, pipx, uvx
                          os.path.join(__import__("site").getuserbase(), "share", "terminal-ct", "skills", "terminal-ct", "SKILL.md"))
              if os.path.isfile(p)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "terminal-ct", "SKILL.md"))


def skills(cmd, dest=None):
    """The agent's skill: print it, its path, or copy it where Claude Code reads skills."""
    if cmd == "get":
        with open(SKILL, encoding="utf-8") as f:
            return print(f.read(), end="")
    if cmd == "path":
        return print(SKILL)
    dest = os.path.join(dest or os.path.expanduser("~/.claude/skills"), "terminal-ct")
    os.makedirs(dest, exist_ok=True)
    shutil.copy(SKILL, os.path.join(dest, "SKILL.md"))
    print(json.dumps({"installed": os.path.join(dest, "SKILL.md")}))


SUDO = "" if os.name == "nt" or os.geteuid() == 0 else "sudo "  # in suggested commands: root has none to run


def _pkg(*names):
    """The command that installs these packages on this system (first package manager found), or None."""
    for exe, cmd in (("apt-get", f"{SUDO}apt-get install -y"), ("dnf", f"{SUDO}dnf install -y"), ("zypper", f"{SUDO}zypper install -y"),
                     ("pacman", f"{SUDO}pacman -S --noconfirm"), ("apk", f"{SUDO}apk add"), ("brew", "brew install")):
        if shutil.which(exe):
            return f"{cmd} {' '.join(names)}"
    return None


def _standby_minutes():
    """Minutes before Windows sleeps on mains power (from WSL too), None when unknown or not Windows."""
    exe = "powercfg" if WINDOWS else "powercfg.exe" if in_wsl() and shutil.which("powercfg.exe") else None
    if not exe:
        return None
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        out = subprocess.run([exe, "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"], capture_output=True,
                             text=True, errors="replace", timeout=10, **NO_WINDOW).stdout  # an OEM code page
        m = re.findall(r"0x([0-9a-fA-F]{8})", out)
        return int(m[-2], 16) // 60 if len(m) >= 2 else None  # AC, then DC: seconds
    return None


def doctor_checks():
    """Each known problem: {check, ok, detail, fix (the exact command), required}."""
    out = []
    def check(name, ok, detail, fix=None, required=True):
        out.append({"check": name, "ok": bool(ok), "detail": detail, **({"fix": fix} if fix and not ok else {}),
                    "required": required})
    engines = {e: shutil.which(e) for e in ("podman", "docker")}
    for e, path in engines.items():
        ver = ""
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            ver = subprocess.run([e, "--version"], capture_output=True, text=True, timeout=20,
                                 **NO_WINDOW).stdout.strip() if path else ""
        check(e, path, ver or "not installed", _pkg(e) if e == "podman" else None, required=False)
    if not any(engines.values()):
        check("engine", False, "neither Podman nor Docker is installed",
              _pkg("podman") or ("winget install RedHat.Podman" if WINDOWS else "brew install podman" if MAC
                                 else "install Podman"))
    else:
        try:
            n = node()
            n.run(["ps", "-q"], timeout=60)
            check("engine", True, f"{n.engine} answers")
            sock = n.api() if type(n) is Node else None
            check("engine API", sock, f"through {sock}: calls in milliseconds" if sock else
                  "not reachable: each call starts the CLI (slower)", required=False)
        except (SystemExit, subprocess.TimeoutExpired) as e:
            msg, fix = str(e), None
            low = msg.lower()
            if "permission denied" in low and "docker" in low:
                fix = f"{SUDO}usermod -aG docker $USER   (then log out and in again)"
            elif "cannot connect to the docker daemon" in low or "is the docker daemon running" in low:
                fix = f"{SUDO}systemctl start docker" if os.path.isdir("/run/systemd/system") else f"{SUDO}service docker start"
            elif "newuidmap" in low or "subuid" in low or "subgid" in low:
                fix = f"{SUDO}usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER; podman system migrate"
            elif MAC or WINDOWS:
                fix = "podman machine init; podman machine start   (or start Docker Desktop)"
            check("engine", False, msg[:300], fix)
    if engines["podman"] and sys.platform.startswith("linux") and os.geteuid() != 0:
        try:
            import getpass
            me = getpass.getuser()
            with open("/etc/subuid", encoding="utf-8") as f:
                ok = any(l.split(":")[0] == me for l in f)
        except (OSError, KeyError):
            ok = False
        check("podman rootless", ok, "subordinate ids for your user" if ok else "no line for you in /etc/subuid",
              f"{SUDO}usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER; podman system migrate",
              required=False)
    check("python", sys.version_info >= (3, 9), sys.version.split()[0], _pkg("python3"))
    if in_wsl():
        if engines["podman"]:
            if os.path.exists("/etc/fedora-release") or os.path.exists("/etc/redhat-release"):
                check("wsl distribution", False, "Fedora's Podman networking fails under WSL (its netavark has no "
                      "iptables driver, and WSL's kernel refuses its nftables rules)", "use a Debian, Ubuntu or Alpine "
                      "WSL distribution (terminal-ct's Windows installer makes an Alpine one)")
            ipt = shutil.which("iptables") or os.path.exists("/usr/sbin/iptables") or os.path.exists("/sbin/iptables")
            check("iptables", ipt, "for Podman's firewall driver under WSL" if ipt else
                  "not found: Podman under WSL cannot start a container with a network", _pkg("iptables"))
            check("podman in WSL", True, "terminal-ct sets Podman's iptables firewall driver for its own calls (WSL's "
                  "kernel lacks nftables pieces); for your own podman: add [network] firewall_driver = \"iptables\" "
                  "to ~/.config/containers/containers.conf", required=False)
        check("wsl", False, "WSL stops this distribution when no Windows program uses it, and its containers with it",
              "terminal-ct autostart on   (a logon task keeps it running while containers want to run)",
              required=False)
    standby = _standby_minutes()
    if standby is not None:
        check("sleep", standby == 0, "this PC never sleeps on mains power" if standby == 0 else
              f"this PC sleeps after {standby} min idle on mains power: containers and nodes pause with it",
              "powercfg /change standby-timeout-ac 0", required=False)
    if any(engines.values()):
        with contextlib.suppress(SystemExit, OSError, ValueError, subprocess.SubprocessError):
            free = node().free_bytes(node().host()[2]) / 2 ** 30
            check("disk", free >= 5, f"{free:.0f} GiB free for images and containers",
                  "free some space (terminal-ct prune; podman system prune)", required=False)
    ossl = shutil.which("openssl")
    check("openssl", ossl, ossl or "not found (agent and web --listen make their certificate with it)",
          _pkg("openssl") or ("winget install ShiningLight.OpenSSL.Light" if WINDOWS else None), required=False)
    viewer = shutil.which("vncviewer") or shutil.which("xtigervncviewer")
    check("window viewer", viewer, viewer or "show NAME opens a desktop in a window with TigerVNC's viewer",
          _pkg("tigervnc-viewer") if not (WINDOWS or MAC) else "winget install TigerVNC.TigerVNC" if WINDOWS
          else "brew install --cask tigervnc-viewer", required=False)
    vm = vm_cli()
    check("terminal-vm", vm, " ".join(vm) if vm else "not found: nodes (node add) and VMs on the dashboard need it",
          "install terminal-vm (its install.sh)", required=False)
    claude = shutil.which("claude")
    check("claude", claude, claude or "Claude Code not found: declare the MCP server in your agent's own way",
          required=False)
    return out


_AUTH = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"',}]+")
_SECRET = re.compile(r"(?i)(token|password|passwd|pass|pwd|secret|key|bearer)"
                     r"([\"']?\s*[:=]\s*[\"']?|\s+)(?!<hidden>)[^\s\"',}]+")
_URL_CRED = re.compile(r"(://[^/\s:@]+:)[^@\s/]+@")  # postgres://user:PASSWORD@host


def known_secrets():
    """The secrets this store holds (dashboard and agent tokens, other PCs' tokens): hidden wherever they show."""
    out = []
    for path in ("web.token", os.path.join("agent", "token")):
        with contextlib.suppress(OSError):
            with open(os.path.join(STORE, path), encoding="utf-8") as f:
                out.append(f.read().strip())
    with contextlib.suppress(Exception):
        out += [h.get("token", "") for h in _hosts().values()]
    return [x for x in out if len(x) >= 8]


def mask(text, secrets=()):
    """A report safe to attach to a ticket: known secrets, values after token=/password=..., the home folder, user
    and host names."""
    import getpass
    for x in secrets:
        text = text.replace(x, "<hidden>")
    text = _AUTH.sub(lambda m: m.group(1) + "<hidden>", text)  # the header's scheme word is not its secret
    text = _SECRET.sub(lambda m: m.group(1) + m.group(2) + "<hidden>", text)
    text = _URL_CRED.sub(lambda m: m.group(1) + "<hidden>@", text)
    for real, alias in ((os.path.expanduser("~"), "~"), (platform.node(), "<host>")):
        if real and len(real) > 1:
            text = text.replace(real, alias)
    with contextlib.suppress(Exception):
        me = getpass.getuser()
        if len(me) > 2:
            text = re.sub(rf"\b{re.escape(me)}\b", "<user>", text)
    text = re.sub(r"(?i)(/mnt/[a-z]/Users/|[A-Z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/))[^\\/\s\"']+",
                  lambda m: m.group(1) + "<user>", text)  # a Windows account's folder, from WSL or in JSON too
    return text


def doctor_report():
    """doctor --report: versions, checks, state and the end of the logs, masked; written to a file to attach."""
    parts = [f"terminal-ct {VERSION}", f"python {sys.version.split()[0]} on {platform.platform()}"
             + (" (WSL)" if in_wsl() else ""), "", "== checks", json.dumps(doctor_checks(), indent=1)]
    with contextlib.suppress(Exception, SystemExit):
        parts += ["", "== nodes and PCs", json.dumps([{k: n.get(k) for k in ("node", "kind", "status", "engine")}
                                                      for n in node_ls()], indent=1)]
    with contextlib.suppress(Exception, SystemExit):
        parts += ["", "== quota", json.dumps(quota(), indent=1)]
    with contextlib.suppress(Exception, SystemExit):
        parts += ["", "== daemon", json.dumps({"running": daemon_alive(), "stacks": stacks()})]
    for name in ("events.log", "daemon.log", os.path.join(WEB_DIR, "actions.log")):
        with contextlib.suppress(OSError):
            with open(os.path.join(STORE, name), encoding="utf-8", errors="replace") as f:
                parts += ["", f"== {name} (last 60 lines)", "".join(collections.deque(f, 60)).rstrip()]
    os.makedirs(STORE, exist_ok=True)
    path = os.path.join(STORE, f"report-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the owner's only, masked or not
    with open(fd, "w", encoding="utf-8") as f:
        f.write(mask("\n".join(parts), known_secrets()) + "\n")
    return path


def doctor(as_json=False, report=False):
    if report:
        path = doctor_report()
        return print(f"report written (secrets, home folder, user and host names masked; read it before sending): "
                     f"{path}")
    checks = doctor_checks()
    if as_json:
        print(json.dumps(checks, indent=1))
    else:
        for c in checks:
            mark = "ok " if c["ok"] else "ERR" if c["required"] else " - "
            print(f"[{mark}] {c['check']:<16} {c['detail']}" + (f"\n      fix: {c['fix']}" if c.get("fix") else ""))
    if not all(c["ok"] for c in checks if c["required"]):
        sys.exit(1)


# ---------- the nodes' DNS (phase 10): service names of one isolated network, nothing else ----------
# Run by terminal-ct in a node, one per network, on that Docker network's gateway address only. Authoritative only:
# it never forwards, caches or looks anything up (no open resolver, no amplification); any other name is REFUSED, so
# the container's resolver asks its next server. Its table is a file terminal-ct writes (root's, replaced whole);
# nothing changes it over the network. Strict parsing; malformed packets get no answer; a rate limit per source.

DNS_MAX = 512  # bytes of a query (and of an answer): plain UDP DNS, no EDNS
DNS_TTL = 5  # seconds: a failover shows within moments
DNS_MAX_ADDRS = 8
DNS_RATE = (50, 100)  # queries per second per source, burst
_DNS_LABEL = re.compile(rb"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def dns_table(text):
    """{name: [IPv4, ...]} from the table file's JSON; anything not clean is left out."""
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    out = {}
    for name, addrs in (raw.items() if isinstance(raw, dict) else ()):
        if not (isinstance(name, str) and isinstance(addrs, list) and len(name) <= 253 and
                all(_DNS_LABEL.fullmatch(l.encode()) for l in name.lower().split("."))):
            continue
        ips = []
        for a in addrs[:DNS_MAX_ADDRS]:
            if isinstance(a, str):  # IPv4Address(7) would be 0.0.0.7
                with contextlib.suppress(ValueError):
                    ips.append(ipaddress.IPv4Address(a).packed)
        out[name.lower()] = ips
    return out


def dns_answer(packet, table):
    """The reply to one query (bytes), or None to stay silent (malformed, not a query). Pure: tested alone."""
    if not 12 + 5 <= len(packet) <= DNS_MAX:
        return None
    qid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", packet[:12])
    if flags & 0x8000 or (flags >> 11) & 0xF or qd != 1 or an or ns or ar > 1:  # a response, not QUERY, not 1 question
        return None
    labels, i = [], 12
    while True:
        if i >= len(packet):
            return None
        n = packet[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 or n > 63 or i + 1 + n > len(packet):  # no compression in a question; label bounds
            return None
        # any bytes: a well-formed name we do not hold (_tcp...) gets REFUSED, not silence (the client would wait)
        label = packet[i + 1:i + 1 + n].decode("latin-1").lower()
        labels.append(label if "." not in label else "\0")  # a dot inside a label never matches a two-label name
        i += 1 + n
    if i + 4 > len(packet) or len(".".join(labels)) > 253 or not labels:
        return None
    qtype, qclass = struct.unpack("!HH", packet[i:i + 4])
    question = packet[12:i + 4]
    name = ".".join(labels)
    rd = flags & 0x0100
    if qclass != 1 or name not in table:  # not ours: REFUSED, never looked up elsewhere
        return struct.pack("!HHHHHH", qid, 0x8000 | rd | 5, 1, 0, 0, 0) + question
    addrs = table[name][:DNS_MAX_ADDRS] if qtype == 1 else []  # AAAA and the rest: no data
    answers = b"".join(struct.pack("!HHHIH", 0xC00C, 1, 1, DNS_TTL, 4) + a for a in addrs)
    return struct.pack("!HHHHHH", qid, 0x8000 | 0x0400 | rd, 1, len(addrs), 0, 0) + question + answers


def _dns_read(table_path, seen):
    """(table, key) when the file changed since `seen` (key: inode, time, size: a rename always shows), else None."""
    st = os.stat(table_path)
    key = (st.st_ino, st.st_mtime_ns, st.st_size)
    if key == seen:
        return None
    with open(table_path, encoding="utf-8") as f:
        return dns_table(f.read()), key


def dns_serve(listen, table_path, port=53, user="nobody", device=None):
    """The server for one network. listen: its gateway with the network's prefix (10.88.0.1/24); only sources of
    that subnet are answered (Linux would deliver another network's packets to this address too, and a spoofed
    source would make it reflect at someone else). device: the network's bridge, the socket bound to it alone.
    Root is dropped once port 53 is bound and the table read."""
    iface = ipaddress.IPv4Interface(listen)
    ip = iface.ip
    if ip.is_unspecified or ip.is_multicast or ip == ipaddress.IPv4Address("255.255.255.255") or \
            iface.network.prefixlen < 8:
        raise SystemExit("the DNS listens on one network's gateway address, never on all of them")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if device:  # Linux: SO_BINDTODEVICE (root only, so before the drop)
        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), device.encode())
    sock.bind((str(ip), port))
    table, seen = _dns_read(table_path, None)  # unreadable: fail now, not answer REFUSED forever
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # drop root for good
        import pwd
        pw = pwd.getpwnam(user)
        os.setgroups([])
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
        if os.getuid() == 0 or os.geteuid() == 0 or not os.access(table_path, os.R_OK):
            raise SystemExit(f"the DNS could not drop root, or {user} cannot read {table_path}")
    buckets = {}
    while True:
        try:
            data, (src, sport) = sock.recvfrom(DNS_MAX + 1)
            if ipaddress.IPv4Address(src) not in iface.network:
                continue  # not from this network: no answer, and no reflection elsewhere
            with contextlib.suppress(OSError, ValueError):
                got = _dns_read(table_path, seen)
                if got:
                    table, seen = got
            now = time.monotonic()
            if len(buckets) > 4096:  # bounded by the subnet already; never rebuilt per packet
                buckets.clear()
            tokens, last = buckets.get(src, (DNS_RATE[1], now))
            tokens = min(DNS_RATE[1], tokens + (now - last) * DNS_RATE[0])
            buckets[src] = (tokens - 1 if tokens >= 1 else tokens, now)
            if tokens < 1:
                continue  # over its rate: dropped
            reply = dns_answer(data, table)
            if reply:
                sock.sendto(reply, (src, sport))
        except Exception:  # one bad packet or a send failing must not end the service
            continue


def _act_cli(name, tool, a):
    """Internal (a node's copy, called by the host's terminal-ct): act() as one JSON line."""
    try:
        r = act(name, tool, json.loads(a))
    except (RuntimeError, ValueError, KeyError, OSError) as e:
        print(str(e) or type(e).__name__, file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"png": base64.b64encode(r[0]).decode(), "size": list(r[1])} if tool == "screenshot"
                     else {"result": r}))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_act"] and len(argv) == 4:
        return _act_cli(*argv[1:])
    if argv[:1] == ["_dns"] and len(argv) == 4:  # internal, in a node: GATEWAY/PREFIX BRIDGE TABLE_FILE
        return dns_serve(argv[1], argv[3], device=argv[2])
    cmd_args = []
    if "--" in argv:  # create NAME ... -- CMD: everything after -- is the container's command
        i = argv.index("--")
        argv, cmd_args = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="terminal-ct", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", choices=["docker", "podman"])
    p.add_argument("--node", "--host", help="run in this node: a Linux VM of terminal-vm with Docker (node add), or "
                                            "another PC (host add)")
    sub = p.add_subparsers(dest="cmd", required=True)
    vmp = sub.add_parser("vm", help="terminal-vm on another PC: terminal-ct --host PC vm ARGS")
    vmp.add_argument("vm_args", nargs=argparse.REMAINDER)
    hs = sub.add_parser("host", help="other PCs as nodes (their agent, or SSH)")
    hs.add_argument("host_cmd", choices=["add", "ls", "rm"]); hs.add_argument("host_name", nargs="?")
    hs.add_argument("url", nargs="?"); hs.add_argument("--fingerprint"); hs.add_argument("--force", action="store_true")
    hs.add_argument("--with", dest="host_engine", choices=["podman", "docker"], help="its engine (else Podman, then Docker)")
    hs.add_argument("--description", dest="host_desc", help="what this PC is for (one line)")
    nd = sub.add_parser("node"); nd.add_argument("node_cmd", choices=["add", "ls", "rm", "join"])
    nd.add_argument("node_name", nargs="?"); nd.add_argument("node_net", nargs="?", help="join: the network")
    nd.add_argument("--image", dest="node_image", choices=["debian", "alpine"], default="debian")
    nd.add_argument("--cpus", dest="node_cpus", type=int, default=2)
    nd.add_argument("--memory", dest="node_memory", type=int, default=2, help="GiB")
    nd.add_argument("--disk", dest="node_disk", type=int, default=20, help="GiB")
    nd.add_argument("--project", dest="node_project"); nd.add_argument("--force", action="store_true")
    nd.add_argument("--description", dest="node_desc", help="what this node is for (one line)")
    c = sub.add_parser("create"); c.add_argument("name"); c.add_argument("--image", default=DEFAULT_IMAGE)
    c.add_argument("--cpus", type=float); c.add_argument("--memory", type=float, help="GiB")
    c.add_argument("--desktop", action="store_true", help="Xvnc + Openbox on top of the image")
    c.add_argument("--width", type=int, default=1280); c.add_argument("--height", type=int, default=800)
    c.add_argument("--sandbox", action="store_true"); c.add_argument("--offline", action="store_true")
    c.add_argument("--net", action="append", default=[], help="an isolated network (repeatable)")
    c.add_argument("--ports", type=int, action="append", default=[],
                   help="with --node and --net: publish it on that node's address of the network (repeatable)")
    c.add_argument("--project"); c.add_argument("--description", help="what it is for (one line)")
    rs = sub.add_parser("resume"); rs.add_argument("--stay", action="store_true")
    ap = sub.add_parser("apply"); ap.add_argument("file"); ap.add_argument("--project")
    sk = sub.add_parser("stack"); sk.add_argument("stack_cmd", choices=["ls", "show", "rm"]); sk.add_argument("stack", nargs="?")
    sc = sub.add_parser("scale"); sc.add_argument("stack"); sc.add_argument("service"); sc.add_argument("replicas", type=int)
    ev_ = sub.add_parser("events"); ev_.add_argument("--stack"); ev_.add_argument("-n", type=int, default=50)
    ev_.add_argument("--follow", action="store_true")
    sub.add_parser("daemon").add_argument("daemon_cmd", choices=["run", "start", "stop", "status"])
    sub.add_parser("prune")
    da = sub.add_parser("dash"); da.add_argument("--project"); da.add_argument("--json", action="store_true")
    inf = sub.add_parser("infra", help="the map: PCs and nodes, what runs where, networks and names, VMs, projects")
    inf.add_argument("--project"); inf.add_argument("--json", action="store_true")
    bn = sub.add_parser("bench"); bn.add_argument("--runs", type=int, default=10); bn.add_argument("--desktop", action="store_true")
    sk_ = sub.add_parser("skills"); sk_.add_argument("skill_cmd", choices=["get", "path", "install"]); sk_.add_argument("--dir")
    wb = sub.add_parser("web"); wb.add_argument("--project"); wb.add_argument("--no-open", action="store_true")
    wb.add_argument("web_args", nargs="*", help="user add NAME | user ls | user rm NAME")
    wb.add_argument("--role", choices=["read", "full"], default="full"); wb.add_argument("--listen", help="IP or off")
    wb.add_argument("--port", type=int)
    ag = sub.add_parser("agent", help="serve this PC to a central PC (HTTPS, pinned certificate + token)")
    ag.add_argument("--listen", default="127.0.0.1"); ag.add_argument("--port", type=int); ag.add_argument("--rotate", action="store_true")
    lg = sub.add_parser("logs"); lg.add_argument("name"); lg.add_argument("-n", type=int, default=100)
    sn = sub.add_parser("snapshot"); sn.add_argument("name"); sn.add_argument("snap_cmd", choices=["save", "load", "rm", "ls"])
    sn.add_argument("tag", nargs="?")
    cl = sub.add_parser("clone"); cl.add_argument("src"); cl.add_argument("new"); cl.add_argument("--project")
    cl.add_argument("--description", help="what the new one is for (one line)")
    ds = sub.add_parser("describe", help="KIND NAME [TEXT]: one line on what it is for (no TEXT: show it)")
    ds.add_argument("desc_kind", choices=DESC_KINDS); ds.add_argument("desc_name")
    ds.add_argument("desc_text", nargs="?"); ds.add_argument("--clear", action="store_true")
    for n in ("push", "pull"):
        tr = sub.add_parser(n); tr.add_argument("name"); tr.add_argument("src"); tr.add_argument("dest")
    sub.add_parser("autostart").add_argument("state", nargs="?", choices=["on", "off"])
    pj = sub.add_parser("project"); pj.add_argument("project_cmd", choices=["ls", "show", "notes", "move"])
    pj.add_argument("args", nargs="*")
    r = sub.add_parser("resources"); r.add_argument("name")
    r.add_argument("--cpus", type=float); r.add_argument("--memory", type=float, help="GiB")
    q = sub.add_parser("quota"); q.add_argument("--cpus", type=int); q.add_argument("--memory", type=int, help="GiB")
    q.add_argument("--disk", type=int, help="GiB"); q.add_argument("--containers", type=int)
    nt = sub.add_parser("net"); nt.add_argument("net_cmd", choices=["create", "rm", "ls", "attach", "detach"])
    nt.add_argument("args", nargs="*"); nt.add_argument("--project")
    nt.add_argument("--description", dest="net_desc", help="create: what it is for (one line)")
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
    for n in ("ls", "top", "help", "version"):
        sub.add_parser(n)
    dr = sub.add_parser("doctor", help="known problems and their fix; --report: a masked report to attach to a ticket")
    dr.add_argument("--json", action="store_true"); dr.add_argument("--report", action="store_true")
    o = p.parse_args(argv)
    for k in ("description", "host_desc", "node_desc", "net_desc"):  # refused before anything is made
        if getattr(o, k, None) is not None:
            setattr(o, k, check_description(getattr(o, k)))
    described = lambda kind, name, k: (set_description(kind, name, getattr(o, k)) if getattr(o, k, None) else None)
    global NODE
    if o.engine and o.node:
        raise SystemExit("--engine or --node, not both (a node runs Docker)")
    if o.engine:
        NODE = Node(pick_engine(o.engine))
    elif o.node and o.cmd in ("apply", "stack", "scale"):  # a stack places its replicas itself (node, spread)
        raise SystemExit(f'--node/--host does not apply to {o.cmd}: put "node": "{o.node}" (or "spread": "nodes") '
                         "on the stack's services")
    elif o.node and o.cmd != "host":
        NODE = node_named(o.node)
    elif o.cmd != "node" and isinstance(getattr(o, "name", None), str) and o.name in _nodes()["placement"]:
        NODE = node_obj(_nodes()["placement"][o.name])  # that container lives in a node
    if o.cmd == "vm":
        if not isinstance(NODE, NodeRemote):
            raise SystemExit("vm ARGS runs terminal-vm on another PC: terminal-ct --host PC vm ARGS (on this PC, run "
                             "terminal-vm itself)")
        if not o.vm_args:
            raise SystemExit("terminal-ct --host PC vm ARGS (e.g. vm ls)")
        try:
            r = NODE._exec([*VM_ON_PC, *o.vm_args, *(["--", *cmd_args] if cmd_args else [])], 3600)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            raise SystemExit(f"host {NODE.name}: {e}") from None
        for stream, text in ((sys.stdout, r.get("stdout")), (sys.stderr, r.get("stderr"))):
            stream.flush()  # its bytes as they came: a Windows console code page cannot refuse them
            (stream.buffer.write(text.encode("utf-8")) if hasattr(stream, "buffer") else stream.write(text)) \
                if text else None
            stream.flush()
        if vm_missing(r):
            print(f"terminal-vm not found on {NODE.name} (install it there: install.sh, or TERMINAL_VM)",
                  file=sys.stderr)
        code = r.get("exit") or 0
        sys.exit(128 - code if code < 0 else code)  # killed by a signal there: 128 + its number, as a shell says
    if o.cmd == "host":
        if o.host_cmd != "ls" and not o.host_name or o.host_cmd == "add" and not o.url:
            raise SystemExit({"add": "host add NAME https://IP:PORT --fingerprint SHA256 (token: typed, piped, or "
                                     "TERMINAL_CT_AGENT_TOKEN) | host add NAME ssh://[user@]host[:port]",
                              "rm": "host rm NAME [--force]"}[o.host_cmd])
        if o.host_cmd in ("rm",) and not is_host(o.host_name):
            raise SystemExit(f"no host named {o.host_name!r} (terminal-ct host ls)")
        out = ([n for n in node_ls() if n["kind"] == "host"] if o.host_cmd == "ls" else
               host_add(o.host_name, o.url, o.fingerprint, engine=o.host_engine) if o.host_cmd == "add" else node_rm(o.host_name, o.force))
        if o.host_cmd == "add":
            described("host", o.host_name, "host_desc")
        return print(json.dumps(out, indent=1))
    if o.cmd == "node":
        if o.node_cmd != "ls" and not o.node_name:
            raise SystemExit(f"node {o.node_cmd} NAME")
        if o.node_cmd == "join" and not o.node_net:
            raise SystemExit("node join NODE NET")
        out = (node_ls() if o.node_cmd == "ls" else
               node_add(o.node_name, o.node_image, o.node_cpus, o.node_memory, o.node_disk, o.node_project)
               if o.node_cmd == "add" else node_join(o.node_name, o.node_net) if o.node_cmd == "join"
               else node_rm(o.node_name, o.force))
        if o.node_cmd == "add":
            described("node", o.node_name, "node_desc")
        return print(json.dumps(out, indent=1))
    if o.cmd == "help": return p.print_help()
    if o.cmd == "version": return print(VERSION)
    if o.cmd == "doctor": return doctor(o.json, o.report)
    if o.cmd == "ls": return ls()
    if o.cmd == "create":
        r = create(o.name, o.image, o.cpus, o.memory, cmd_args or None, desktop=o.desktop, width=o.width,
                   height=o.height, sandbox=o.sandbox, internet=False if o.offline else None, nets=o.net,
                   project=o.project, ports=o.ports)
        if isinstance(NODE, NodeVM):
            place(o.name, NODE.name)
        described("container", o.name, "description")
        return r
    show_json = lambda r: print(json.dumps(r, indent=1))
    if o.cmd == "resume": return resume(o.stay)
    if o.cmd == "apply":
        try:
            with open(o.file, encoding="utf-8") as f:
                desired = json.load(f)
        except (OSError, ValueError) as e:
            raise SystemExit(f"{o.file}: {e}")
        return show_json(apply(desired, project=o.project))
    if o.cmd == "stack":
        if o.stack_cmd != "ls" and not o.stack:
            raise SystemExit(f"stack {o.stack_cmd} NAME")
        if o.stack_cmd == "ls":
            return show_json([dict(stack_show(n), events=None) for n in stacks()])
        with contextlib.nullcontext() if o.engine else on_stack_engine(o.stack):  # the engine it was applied to
            return show_json(stack_show(o.stack) if o.stack_cmd == "show" else stack_rm(o.stack))
    if o.cmd == "scale":
        with contextlib.nullcontext() if o.engine else on_stack_engine(o.stack):
            return show_json(scale(o.stack, o.service, o.replicas))
    if o.cmd == "events":
        if o.follow:
            return follow_events(o.stack)
        for e in events(o.stack, o.n):
            print(json.dumps(e))
        return
    if o.cmd == "daemon":
        r = daemon(o.daemon_cmd)
        return None if o.daemon_cmd == "run" else show_json(r if isinstance(r, dict) else {"daemon": r})
    if o.cmd == "prune": return show_json(prune())
    if o.cmd == "dash":
        if o.json:  # what the web dashboard shows (terminal-vm's dashboard reads it)
            return print(json.dumps(web_api("GET", "/api/state", {"project": o.project or ""})[1]))
        return dash(o.project)
    if o.cmd == "infra":
        m = infra(check_project(o.project) if o.project else None)
        if not o.json and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")  # a description in a script the console cannot show
        return show_json(m) if o.json else print(infra_text(m))
    if o.cmd == "web": return show_json(web_cli(o.web_args, o.role, not o.no_open, o.project, o.listen, o.port))
    if o.cmd == "agent": return agent(o.listen, o.port, o.rotate)
    if o.cmd == "bench": return show_json(bench(max(1, o.runs), o.desktop))
    if o.cmd == "skills": return skills(o.skill_cmd, o.dir)
    if o.cmd == "logs": return print(logs(o.name, o.n), end="")
    if o.cmd == "snapshot": return show_json(snapshot(o.name, o.snap_cmd, o.tag))
    if o.cmd == "clone":
        r = clone(o.src, o.new, project=o.project)
        described("container", o.new, "description")
        return show_json(r)
    if o.cmd == "describe":
        if o.desc_text is None and not o.clear:
            return show_json({o.desc_kind: o.desc_name, "description": description(o.desc_kind, o.desc_name)})
        return show_json(describe(o.desc_kind, o.desc_name, "" if o.clear else o.desc_text))
    if o.cmd in ("push", "pull"): return show_json(transfer(o.name, o.cmd, o.src, o.dest))
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
        if o.net_cmd == "create":
            r = net_create(o.args[0], project=o.project)
            described("net", o.args[0], "net_desc")
            return show_json(r)
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
    if o.cmd == "rm": return rm(o.name)  # from its node, rm forgets where it was
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

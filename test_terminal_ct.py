import contextlib, io, json, os, shutil, socket, struct, subprocess, tempfile, threading, unittest, zlib

import terminal_ct as ct


class FakeNode(ct.Node):
    """Records engine calls; answers inspect from a small in-memory engine."""
    os_release = 'NAME="Alpine Linux"\nID=alpine\n'

    def __init__(self, engine="docker"):
        self.engine, self.calls, self.objects, self.images = engine, [], {}, {
            "debian:stable-slim": {"Config": {"Cmd": ["bash"]}},
            "alpine:3.20": {"Config": {"Cmd": ["/bin/sh"]}},
            "nginx": {"Config": {"Entrypoint": ["/docker-entrypoint.sh"], "Cmd": ["nginx", "-g", "daemon off;"]}}}

    def run(self, args, check=True, timeout=None, capture=True):
        self.calls.append(args)
        out, code = "", 0
        if args[:2] == ["image", "inspect"]:
            img = self.images.get(args[2])
            out, code = (json.dumps([img]), 0) if img else ("", 1)
        elif args[0] == "pull":
            self.images[args[1]] = {"Config": {"Cmd": ["sh"]}}
        elif args[0] == "create":
            name = args[args.index("--name") + 1]
            labels = dict(a.split("=", 1) for a in args[args.index("--name"):] if a.startswith("terminal-ct."))
            self.objects[name] = {"Id": "abc123def4567890", "Name": "/" + name,
                                  "State": {"Status": "created"}, "Config": {"Labels": labels, "Image": "img"}}
            out = "abc123def4567890"
        elif args[0] == "inspect":
            assert args[1:3] == ["--type", "container"], args  # untyped, it matched images and volumes too
            found = [self.objects[n] for n in args[3:] if n in self.objects]
            out, code = json.dumps(found), (0 if found else 1)
        elif args[0] == "start":
            self.objects[args[1]]["State"]["Status"] = "running"
        elif args[0] == "ps":
            out = "\n".join(self.objects)
        elif args[0] == "rm":
            self.objects.pop(args[-1], None)
        elif args[0] == "exec":
            out = "hi\n"
        elif args[0] == "run" and "cat" in args:  # os_family
            out = self.os_release
        elif args[0] == "build":
            self.images[args[args.index("-t") + 1]] = {"Config": {}}
        if check and code:
            raise SystemExit("engine error")
        return subprocess.CompletedProcess(args, code, out, "")


def quiet(f, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        f(*a, **k)
    return json.loads(buf.getvalue().splitlines()[-1]) if buf.getvalue().strip() else None


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.n = ct.NODE = FakeNode()

    def test_create_labels_volume_and_keep_alive(self):
        quiet(ct.create, "web", cpus=1, memory=0.5)
        args = next(c for c in self.n.calls if c[0] == "create")
        self.assertEqual(args[args.index("--name") + 1], "tct-web")   # never the user's own names
        self.assertIn("terminal-ct.name=web", args)
        self.assertIn("tct-web-data:/data", args)
        self.assertEqual(args[-3:], ["tail", "-f", "/dev/null"])        # bash default: kept alive
        self.assertIn("0.5g", args)
        with self.assertRaises(SystemExit):
            ct.create("web")                                           # already exists

    def test_service_image_keeps_its_command(self):
        self.n.images["nginx"] = self.n.images["nginx"]
        quiet(ct.create, "proxy", image="nginx")
        self.assertEqual(next(c for c in self.n.calls if c[0] == "create")[-1], "nginx")

    def test_missing_image_is_pulled_fully_qualified(self):
        quiet(ct.create, "a", image="busybox:1.36")
        self.assertIn(["pull", "docker.io/library/busybox:1.36"], self.n.calls)

    def test_lifecycle_and_listing(self):
        quiet(ct.create, "web")
        quiet(ct.start, "web")
        self.assertEqual(quiet(ct.exec_, "web", ["echo", "hi"])["stdout"], "hi\n")
        ls = ct.containers()
        self.assertEqual((ls[0]["name"], ls[0]["status"], ls[0]["created_by"]), ("web", "running", "user"))
        quiet(ct.rm, "web")
        self.assertIn(["volume", "rm", "tct-web-data"], self.n.calls)
        with self.assertRaises(SystemExit):
            ct.find("web")

    def test_foreign_containers_are_invisible(self):
        self.n.objects["tct-x"] = {"Name": "/tct-x", "State": {}, "Config": {"Labels": {}}}  # no terminal-ct label
        with self.assertRaises(SystemExit):
            ct.find("x")

    def test_names(self):
        for bad in ("", "Web", "a b", "-x", "../x", "x" * 49):
            with self.assertRaises(SystemExit):
                ct.check_name(bad)

    def test_qualified(self):
        self.assertEqual(ct.qualified("debian:stable-slim"), "docker.io/library/debian:stable-slim")
        self.assertEqual(ct.qualified("user/app"), "docker.io/user/app")
        for full in ("ghcr.io/o/i:1", "localhost/mine", "localhost:5000/x"):
            self.assertEqual(ct.qualified(full), full)

    def test_normalize_docker_and_podman(self):
        base = {"Id": "0123456789abcdef", "State": {"Status": "exited", "ExitCode": 3},
                "Config": {"Labels": {"terminal-ct.name": "w", "terminal-ct.created_by": "agent"}, "Image": "i"}}
        for name in ("/tct-w", "tct-w"):  # Docker prefixes a slash, Podman does not
            c = ct.normalize(dict(base, Name=name))
            self.assertEqual((c["name"], c["status"], c["exit_code"], c["created_by"]), ("w", "exited", 3, "agent"))


@unittest.skipIf(ct.WINDOWS, "desktop containers need a Linux host or WSL")
class Desktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); ct.STORE = self.tmp

    def test_create_desktop_args(self):
        for engine in ("podman", "docker"):
            with self.subTest(engine=engine):
                n = ct.NODE = FakeNode(engine)
                quiet(ct.create, "d", image="alpine:3.20", desktop=True, width=1024, height=640)
                build = next(c for c in n.calls if c[0] == "build")
                with open(os.path.join(build[-1], "Dockerfile")) as f:
                    self.assertIn("apk add", f.read())                     # Alpine family detected
                args = next(c for c in n.calls if c[0] == "create")
                joined = " ".join(args)
                for want in ("TCT_WIDTH=1024", "TCT_HEIGHT=640", "HOME=/data", "LANG=C.UTF-8",
                             "terminal-ct.desktop=1", f"{os.path.join(self.tmp, 'd', 'run')}:/run/tct"):
                    self.assertIn(want, joined)
                self.assertEqual(args[-2:], ["tct-desktop:alpine-3.20", ct.DESKTOP_ENTRY])
                if engine == "podman":
                    self.assertIn("--userns=keep-id", args); self.assertIn("tct-d-data:/data:U", args)
                else:  # docker: the volume is handed to the user before the desktop runs as them
                    self.assertTrue(any(c[0] == "run" and "chown" in c for c in n.calls))
                self.assertIn(f"{os.getuid()}:{os.getgid()}", args)

    def test_unsupported_family(self):
        n = ct.NODE = FakeNode()
        n.os_release = "ID=gentoo\n"
        with self.assertRaises(SystemExit):
            ct.create("d", image="alpine:3.20", desktop=True)


class Keys(unittest.TestCase):
    def test_keysyms(self):
        self.assertEqual(ct.chord("ctrl+shift+t"), [0xFFE3, 0xFFE1, ord("t")])
        self.assertEqual(ct.chord("Return"), [0xFF0D])
        self.assertEqual(ct.chord("f12"), [0xFFC9])
        self.assertEqual(ct.char_keysym("é"), 0xE9)                       # Latin-1 as is
        self.assertEqual(ct.char_keysym("€"), 0x010020AC)                 # the rest as Unicode keysyms
        self.assertEqual(ct.char_keysym("\n"), 0xFF0D)
        with self.assertRaises(ValueError):
            ct.chord("ctrl+nope")

    def test_viewer_keys_all_map_to_keysyms(self):
        """Every key the terminal viewer can decode has an X keysym (else typing it in view would be lost)."""
        names = set(ct.CSI_KEYS.values()) | set(ct.TILDE_KEYS.values()) | {"ret", "tab", "backspace", "esc"}
        for n in names:
            self.assertTrue(ct.chord(n), n)
        events = ct.parse_input("a\x1b[1;5A\x03\x1b[<0;10;5M\x1d")
        self.assertEqual(events, [("text", "a"), ("key", "ctrl+up"), ("key", "ctrl+c"), ("mouse", 0, 10, 5, True), ("quit",)])
        self.assertEqual(ct.chord("ctrl+up"), [0xFFE3, 0xFF52])

    def test_png(self):
        data = ct.png(bytes([255, 0, 0] * 6), 3, 2)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (3, 2))
        raw = zlib.decompress(data[data.index(b"IDAT") + 4:-16])
        self.assertEqual(raw, (b"\x00" + bytes([255, 0, 0] * 3)) * 2)

    def test_rgb_order(self):
        r = ct.RFB.__new__(ct.RFB)
        r.w, r.h, r.fb = 1, 1, bytearray([3, 2, 1, 0])                   # B, G, R, X on the wire
        self.assertEqual(r.rgb(), bytes([1, 2, 3]))


@unittest.skipUnless(hasattr(socket, "AF_UNIX") and not ct.WINDOWS, "unix sockets")
class FakeVnc(unittest.TestCase):
    """The RFB client against a minimal server: handshake, a raw update, a resize, input messages."""
    def test_protocol(self):
        path = os.path.join(tempfile.mkdtemp(), "vnc.sock")
        srv = socket.socket(socket.AF_UNIX); srv.bind(path); srv.listen(1)
        got = {}

        def serve():
            c, _ = srv.accept()
            rd = lambda n: c.recv(n, socket.MSG_WAITALL)
            c.sendall(b"RFB 003.008\n"); rd(12)
            c.sendall(b"\x01\x01"); rd(1); c.sendall(struct.pack(">I", 0)); rd(1)
            c.sendall(struct.pack(">HH", 2, 1) + bytes(16) + struct.pack(">I", 1) + b"t")
            got["pixfmt"] = rd(20); got["enc"] = rd(12)
            rd(10)  # update request
            c.sendall(struct.pack(">BxH", 0, 2) + struct.pack(">HHHHi", 0, 0, 2, 1, 0) + bytes([1, 2, 3, 0, 4, 5, 6, 0])
                      + struct.pack(">HHHHi", 0, 0, 3, 1, -223))
            got["pointer"] = rd(6); got["key"] = rd(8)
            c.close()
        t = threading.Thread(target=serve); t.start()
        r = ct.RFB(path)
        self.assertEqual((r.w, r.h), (2, 1))
        r.refresh()
        self.assertEqual((r.w, r.h), (3, 1))                              # DesktopSize applied after the pixels
        r.pointer(1, 0, 4); r.key(0xFF0D, True); t.join(5); r.close(); srv.close()
        self.assertEqual(struct.unpack(">BxHii", got["enc"]), (2, 2, 0, -223))
        self.assertEqual(got["pointer"], struct.pack(">BBHH", 5, 4, 1, 0))
        self.assertEqual(got["key"], struct.pack(">BBxxI", 4, 1, 0xFF0D))


@unittest.skipUnless(os.environ.get("TERMINAL_CT_REAL"), "set TERMINAL_CT_REAL=1 to run against real engines")
class RealEngines(unittest.TestCase):
    def test_round_trip(self):
        for engine in ("podman", "docker"):
            if not shutil.which(engine):
                continue
            with self.subTest(engine=engine):
                ct.NODE = ct.Node(engine)
                name = f"t-{engine}"
                subprocess.run([engine, "rm", "-f", "tct-" + name], capture_output=True)
                quiet(ct.create, name, image="alpine:3.20")
                quiet(ct.start, name)
                out = quiet(ct.exec_, name, ["sh", "-c", "echo ok > /data/f; cat /data/f; hostname"])
                self.assertEqual(out["stdout"].split(), ["ok", name])
                self.assertEqual(next(c for c in ct.containers() if c["name"] == name)["status"], "running")
                quiet(ct.stop, name, 1)
                quiet(ct.rm, name)
                self.assertNotIn(name, [c["name"] for c in ct.containers()])


if __name__ == "__main__":
    unittest.main()

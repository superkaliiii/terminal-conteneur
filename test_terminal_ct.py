import contextlib, io, json, os, shutil, subprocess, unittest

import terminal_ct as ct


class FakeNode(ct.Node):
    """Records engine calls; answers inspect from a small in-memory engine."""
    def __init__(self, engine="docker"):
        self.engine, self.calls, self.objects, self.images = engine, [], {}, {
            "debian:stable-slim": {"Config": {"Cmd": ["bash"]}},
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
            found = [self.objects[n] for n in args[1:] if n in self.objects]
            out, code = json.dumps(found), (0 if found else 1)
        elif args[0] == "start":
            self.objects[args[1]]["State"]["Status"] = "running"
        elif args[0] == "ps":
            out = "\n".join(self.objects)
        elif args[0] == "rm":
            self.objects.pop(args[-1], None)
        elif args[0] == "exec":
            out = "hi\n"
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
        quiet(ct.create, "a", image="alpine:3.20")
        self.assertIn(["pull", "docker.io/library/alpine:3.20"], self.n.calls)

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

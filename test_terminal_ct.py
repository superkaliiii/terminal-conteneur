import base64, contextlib, hashlib, io, re, json, os, shutil, socket, ssl, struct, subprocess, sys, tempfile, threading, time, unittest, unittest.mock, zlib

import terminal_ct as ct

# the real tests' engines (TERMINAL_CT_REAL_ENGINES="docker" where Podman cannot set limits, e.g. a CI runner)
REAL_ENGINES = tuple(os.environ.get("TERMINAL_CT_REAL_ENGINES", "podman docker").split())


class FakeNode(ct.Node):
    """Records engine calls; answers inspect from a small in-memory engine."""
    os_release = 'NAME="Alpine Linux"\nID=alpine\n'

    def __init__(self, engine="docker"):
        self.engine, self.calls, self.objects, self.nets, self.images = engine, [], {}, {}, {
            "debian:stable-slim": {"Config": {"Cmd": ["bash"]}},
            "alpine:3.20": {"Config": {"Cmd": ["/bin/sh"]}},
            "nginx": {"Config": {"Entrypoint": ["/docker-entrypoint.sh"], "Cmd": ["nginx", "-g", "daemon off;"]}}}

    def host(self):  # fixed: on Linux the real one reads this machine (a 4-core CI runner: quota of 3 CPUs)
        return 8, 16.0, ct.STORE if os.path.isdir(ct.STORE) else os.path.dirname(ct.STORE)

    def pipe(self, args, stdin=None, stdout=None):
        self.calls.append(["pipe", *args])
        if stdout:
            with open(stdout, "wb") as f:
                f.write(b"tar of /data")

    def run(self, args, check=True, timeout=None, capture=True):
        self.calls.append(args)
        out, code = "", 0
        opt = lambda flag, conv=str: conv(args[args.index(flag) + 1]) if flag in args else None
        if args[:2] == ["image", "inspect"]:
            img = self.images.get(args[2])
            out, code = (json.dumps([img]), 0) if img else ("", 1)
        elif args[0] == "pull":
            self.images[args[1]] = {"Config": {"Cmd": ["sh"]}}
        elif args[0] == "info":
            out = f"8 {16 * 2 ** 30} {ct.STORE}"
        elif args[0] == "create":
            name = args[args.index("--name") + 1]
            labels = dict(a.split("=", 1) for a in args[args.index("--name"):] if a.startswith("terminal-ct."))
            net = opt("--network")
            self.objects[name] = {"Id": "abc123def4567890", "Name": "/" + name, "State": {"Status": "created"},
                                  "Image": "sha256:" + name,
                                  "Config": {"Labels": labels, "Image": "img"},
                                  "HostConfig": {"NanoCpus": int(float(opt("--cpus") or 0) * 1e9),
                                                 "Memory": int(opt("--memory") or 0), "NetworkMode": net},
                                  "NetworkSettings": {"Networks": {net: {}}}}
            out = "abc123def4567890"
        elif args[0] == "inspect":
            assert args[1:3] == ["--type", "container"], args  # untyped, it matched images and volumes too
            found = [self.objects[n] for n in args[3:] if n in self.objects]
            out, code = json.dumps(found), (0 if found else 1)
        elif args[0] == "start":
            self.objects[args[1]]["State"].update(Status="running", StartedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        elif args[0] == "stop":
            self.objects[args[-1]]["State"].update(Status="exited", ExitCode=getattr(self, "exit_code", 0))
        elif args[0] == "update":
            hc = self.objects[args[-1]]["HostConfig"]
            if "--cpus" in args: hc["NanoCpus"] = int(float(opt("--cpus")) * 1e9)
            if "--memory" in args: hc["Memory"] = int(opt("--memory"))
        elif args[0] == "ps":
            out = "\n".join(self.objects)
        elif args[0] == "rm":
            self.objects.pop(args[-1], None)
        elif args[0] == "exec":
            out, code = "hi\n", getattr(self, "exec_code", 0)
        elif args[0] == "logs":
            out = "".join(f"line {i}\n" for i in range(int(args[args.index("--tail") + 1])))
        elif args[0] == "stats":
            out = "\n".join(f"{n}|12.5%|512MiB / 1GiB|7" for n in args[4:])
        elif args[:2] == ["network", "create"]:
            self.nets[args[-1]] = dict(a.split("=", 1) for a in args if a.startswith("terminal-ct."))
        elif args[:2] == ["network", "ls"]:
            out = "\n".join(self.nets)
        elif args[:2] == ["network", "inspect"]:
            out = json.dumps([{"Labels": self.nets[n]} for n in args[2:]])
        elif args[:2] == ["network", "rm"]:
            self.nets.pop(args[2])
        elif args[:2] in (["network", "connect"], ["network", "disconnect"]):
            net, c = args[-2], self.objects[args[-1]]["NetworkSettings"]["Networks"]
            if net not in self.nets and net != ct.BRIDGE[self.engine]:
                code = 1
            elif args[1] == "connect":
                c[net] = {}
            else:
                c.pop(net, None)
        elif args[:2] == ["volume", "inspect"]:
            code = 0 if args[2] in getattr(self, "volumes", ()) else 1
        elif args[:2] == ["volume", "ls"]:
            out = "\n".join(f"{n}\t{p}" for n, p in getattr(self, "podman_volumes", {}).items())
        elif args[:2] == ["system", "df"] and self.engine == "podman":
            out = ("Images space usage:\n\nREPOSITORY TAG\n\nLocal Volumes space usage:\n\nVOLUME NAME  LINKS  SIZE\n"
                   "tct-web-data  1      2 GB\ntheirs  0  9GB\n")
        elif args[:2] == ["system", "df"]:
            out = json.dumps([{"Name": "tct-web-data", "Size": "2GB"}, {"Name": "theirs", "Size": "9GB"}])
        elif args[0] == "commit":
            self.images[args[2]] = {"Config": {"Cmd": ["tail", "-f", "/dev/null"]}}
        elif args[0] == "rename":
            obj = self.objects.pop(args[1]); obj["Name"] = "/" + args[2]; self.objects[args[2]] = obj
        elif args[:2] == ["image", "ls"] and "--format" in args:
            out = "tct-desktop 1GB\ndebian 80MB"
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
        ct.STORE = tempfile.mkdtemp()

    def test_create_labels_volume_and_keep_alive(self):
        quiet(ct.create, "web", cpus=1, memory=0.5)
        args = next(c for c in self.n.calls if c[0] == "create")
        self.assertEqual(args[args.index("--name") + 1], "tct-web")   # never the user's own names
        self.assertIn("terminal-ct.name=web", args)
        self.assertIn("tct-web-data:/data", args)
        self.assertEqual(args[-3:], ["tail", "-f", "/dev/null"])        # bash default: kept alive
        self.assertEqual(args[args.index("--memory") + 1], str(2 ** 29))
        self.assertEqual(args[args.index("--memory-swap") + 1], str(2 ** 29))  # no swap past the ceiling
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


class Phase3(unittest.TestCase):
    """Quota, resources, top, networks, internet, sandbox, agent authority."""
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()

    def created(self, name):
        return [c for c in self.n.calls if c[0] == "create" and c[c.index("--name") + 1] == "tct-" + name][-1]

    def test_defaults_ceiling_bridge_and_internet(self):
        quiet(ct.create, "u")
        args = self.created("u")
        self.assertEqual((args[args.index("--cpus") + 1], args[args.index("--network") + 1]), ("1", "bridge"))
        self.assertEqual(args[args.index("--memory") + 1], str(2 ** 30))
        self.assertNotIn(["network", "disconnect", "bridge", "tct-u"], self.n.calls)  # the user's: online
        quiet(ct.create, "s", sandbox=True)
        args = self.created("s")
        for flag in ("--cap-drop", "no-new-privileges", "--pids-limit", "--tmpfs", "terminal-ct.sandbox=1"):
            self.assertTrue(any(flag in a for a in args), flag)
        self.assertFalse(any(a.endswith(":/data") for a in args))      # no volume: /data in memory
        self.assertIn(["network", "disconnect", "bridge", "tct-s"], self.n.calls)  # sandbox: offline
        quiet(ct.create, "a", created_by="agent")
        self.assertIn(["network", "disconnect", "bridge", "tct-a"], self.n.calls)  # agent's: offline
        self.assertEqual(ct.status.__name__, "status")
        c = ct.normalize(ct.find("s"))
        self.assertEqual((c["cpus"], c["memory_gb"], c["internet"], c["sandbox"]), (1, 1, False, True))

    def test_quota_blocks_start_and_resize(self):
        with open(os.path.join(ct.STORE, "quota.json"), "w") as f:
            json.dump({"cpus": 3, "memory_gb": 4}, f)
        quiet(ct.create, "a", cpus=2); quiet(ct.create, "b", cpus=2)
        quiet(ct.start, "a")
        with self.assertRaisesRegex(SystemExit, "cpus: 2 used \\+ 2 > 3"):
            ct.start("b")
        self.assertEqual(self.n.objects["tct-b"]["State"]["Status"], "created")
        with self.assertRaisesRegex(SystemExit, "memory_gb"):
            ct.resources("a", memory=5)                                   # running: the increase is checked
        self.assertEqual(ct.resources("a", memory=4)["memory_gb"], 4)    # exactly the quota: fits
        self.assertEqual(ct.resources("a", cpus=3)["cpus"], 3)
        self.assertEqual(ct.resources("b", cpus=8)["cpus"], 8)           # stopped: checked at start
        q = ct.quota()
        self.assertEqual((q["cpus"]["used"], q["cpus"]["source"], q["containers"]["source"]), (3, "user", "host default"))
        self.assertAlmostEqual(q["disk_gb"]["used"], (2e9 + 1e9) / 2 ** 30, 2)  # its volume + desktop image, not theirs
        self.assertFalse(os.path.exists(os.path.join(ct.STORE, ".quota.lock")))  # released after a refusal too

    def test_disk_quota_blocks_create(self):
        with open(os.path.join(ct.STORE, "quota.json"), "w") as f:
            json.dump({"disk_gb": 2}, f)
        with self.assertRaisesRegex(SystemExit, "disk_gb"):
            ct.create("x")

    def test_unlimited_old_containers(self):
        quiet(ct.create, "old"); quiet(ct.create, "run")
        for n in ("tct-old", "tct-run"):
            self.n.objects[n]["HostConfig"].update(NanoCpus=0, Memory=0)
        self.n.objects["tct-run"]["State"]["Status"] = "running"         # started before the upgrade
        self.assertEqual(ct.quota_usage(disk=False)["cpus"], 8)          # counts as the whole host
        self.assertEqual(ct.normalize(ct.find("run"))["cpus"], "unlimited")
        quiet(ct.stop, "run")
        self.assertTrue(quiet(ct.start, "old")["limits_set"])            # given the defaults, then started
        self.assertEqual(ct.resources("old")["cpus"], 1)

    def test_top(self):
        quiet(ct.create, "a"); quiet(ct.start, "a")
        row = ct.top()["containers"][0]
        self.assertEqual((row["name"], row["cpu_percent"], row["used_memory_gb"], row["pids"]), ("a", 12.5, 0.5, 7))

    def test_parse_size(self):
        for s, b in (("3MB", 3e6), ("498 MB", 498e6), ("352KiB", 352 * 1024), ("0B", 0), ("1.5GiB", 1.5 * 2 ** 30)):
            self.assertEqual(ct.parse_size(s), b)

    def test_networks_and_internet(self):
        quiet(ct.create, "a")
        self.assertEqual(ct.net_create("lab")["internal"], True)
        self.assertIn("--internal", next(c for c in self.n.calls if c[:2] == ["network", "create"]))
        quiet(ct.start, "a")
        with self.assertRaisesRegex(SystemExit, "stop a first"):
            ct.net_attach("a", "lab")
        quiet(ct.stop, "a")
        self.assertEqual(ct.net_attach("a", "lab")["networks"], ["lab"])
        self.assertIn(["network", "connect", "--alias", "a", "tct-net-lab", "tct-a"], self.n.calls)
        with self.assertRaisesRegex(SystemExit, "still has containers"):
            ct.net_rm("lab")
        self.assertEqual(ct.internet("a", False)["internet"], False)
        self.assertEqual(ct.internet("a")["internet"], False)
        quiet(ct.create, "b", nets=["lab"], internet=False)
        self.assertEqual(sorted(ct.networks()["lab"]["members"]), ["a", "b"])
        with self.assertRaisesRegex(SystemExit, "no network"):
            ct.create("c", nets=["nope"])
        ct.net_attach("a", "lab", False); quiet(ct.rm, "b")
        self.assertEqual(ct.net_rm("lab"), {"removed": "lab"})

    def test_sandbox_starts_fresh_and_toggles(self):
        quiet(ct.create, "s", sandbox=True)
        self.assertNotIn("fresh", quiet(ct.start, "s"))                   # first start: as created
        quiet(ct.stop, "s")
        n_creates = sum(c[0] == "create" for c in self.n.calls)
        self.assertTrue(quiet(ct.start, "s")["fresh"])                    # again: recreated from its spec
        self.assertEqual(sum(c[0] == "create" for c in self.n.calls), n_creates + 1)
        self.assertFalse(ct.normalize(ct.find("s"))["internet"])          # still offline after recreation
        quiet(ct.stop, "s")
        self.assertEqual(ct.sandbox("s", False), {"name": "s", "sandbox": False, "internet": False})
        self.assertIn("tct-s-data:/data", self.created("s"))
        quiet(ct.create, "u")
        self.assertEqual(ct.sandbox("u", True), {"name": "u", "sandbox": True, "internet": False})

    def test_leftover_volume_not_adopted(self):
        self.n.volumes = {"tct-work-data"}
        with self.assertRaisesRegex(SystemExit, "left from an earlier work"):
            ct.create("work")

    def test_network_failure_fails_closed(self):
        self.n.nets["tct-net-gone"] = {"terminal-ct.net": "gone"}
        real = self.n.run

        def run(args, **k):
            if args[:2] == ["network", "connect"]:
                raise SystemExit("network vanished")
            return real(args, **k)
        self.n.run = run
        with self.assertRaisesRegex(SystemExit, "vanished"):
            ct.create("x", internet=False, nets=["gone"])
        self.assertNotIn("tct-x", self.n.objects)                        # not left behind, online or not

    def test_pre_03_podman_container_upgraded_in_place(self):
        """A 0.2 container on Podman's pasta: changing its network commits it and remakes it on the bridge."""
        self.n = ct.NODE = FakeNode("podman")
        quiet(ct.create, "old")
        o = self.n.objects["tct-old"]
        o["HostConfig"]["NetworkMode"] = "pasta"; o["NetworkSettings"]["Networks"] = {"pasta": {}}
        del o["Config"]["Labels"]["terminal-ct.spec"]
        self.assertEqual(ct.internet("old", False), {"name": "old", "internet": False})
        self.assertIn(["commit", "tct-old", "tct-keep-old:latest"], self.n.calls)     # its file system kept
        args = self.created("old")
        self.assertEqual((args[args.index("--network") + 1], args[-1]), ("podman", "tct-keep-old:latest"))  # its own command
        self.assertIn("tct-old-data:/data", args)                                     # and its /data
        self.assertEqual(list(self.n.objects), ["tct-old"])                           # the old one gone
        quiet(ct.rm, "old")
        self.assertIn(["image", "rm", "tct-keep-old:latest"], self.n.calls)

    def test_remake_failure_puts_the_old_container_back(self):
        quiet(ct.create, "s", sandbox=True); quiet(ct.start, "s"); quiet(ct.stop, "s")
        before = self.n.objects["tct-s"]
        real = self.n.run
        self.n.run = lambda args, **k: (_ for _ in ()).throw(SystemExit("engine down")) if args[0] == "create" \
            else real(args, **k)
        with self.assertRaisesRegex(SystemExit, "engine down"):
            ct.start("s")
        self.assertIs(self.n.objects["tct-s"], before)                                # back under its name

    def test_podman_machine_volumes_counted(self):
        self.n = ct.NODE = FakeNode("podman")
        self.n.podman_volumes = {"tct-web-data": "/nonexistent/in/the/vm", "theirs": "/x"}
        self.assertEqual(ct.disk_bytes([]), 2e9 + 1e9)                                # its volume + desktop image


class AgentAuthority(unittest.TestCase):
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()
        self.asked = []

    def approve(self, message, cli):
        self.asked.append(cli)

    def refuse(self, message, cli):
        raise RuntimeError(f"The user did not approve. They can still run: {cli}")

    def test_agent_creates_sandboxes_offline_and_owns_them(self):
        out = ct.agent_call("ct_create", {"name": "job"}, self.refuse)       # image here, sandbox: no question
        self.assertEqual((out["sandbox"], out["internet"]), (True, False))
        self.assertTrue(ct.managed(ct.find("job")) and ct.agent_owned(ct.find("job")))
        ct.agent_call("ct_start", {"name": "job"}, self.refuse)
        ct.agent_call("ct_resources", {"name": "job", "cpus": 2}, self.refuse)
        ct.agent_call("ct_stop", {"name": "job"}, self.refuse)
        ct.agent_call("ct_remove", {"name": "job"}, self.refuse)
        self.assertEqual(self.asked, [])

    def test_risky_steps_ask(self):
        ct.agent_call("ct_create", {"name": "dl", "image": "busybox:1.36", "internet": True}, self.approve)
        self.assertEqual(self.asked, ["terminal-ct image pull busybox:1.36", "terminal-ct internet dl on"])
        quiet(ct.create, "mine")                                           # the user's own
        with self.assertRaisesRegex(RuntimeError, "terminal-ct start mine"):
            ct.agent_call("ct_start", {"name": "mine"}, self.refuse)
        self.assertEqual(self.n.objects["tct-mine"]["State"]["Status"], "created")
        ct.net_create("theirs")
        with self.assertRaisesRegex(RuntimeError, "net attach dl theirs"):
            ct.agent_call("net_attach", {"name": "dl", "net": "theirs"}, self.refuse)
        with self.assertRaisesRegex(SystemExit, "not one this agent created"):
            ct.agent_call("net_remove", {"net": "theirs"}, self.refuse)
        ct.agent_call("net_create", {"net": "lab"}, self.refuse)
        self.assertEqual(ct.agent_call("net_attach", {"name": "dl", "net": "lab"}, self.refuse)["networks"], ["lab"])
        listed = {c["name"]: c["managed"] for c in ct.agent_call("ct_list", {}, self.refuse)}
        self.assertEqual(listed, {"dl": True, "mine": False})

    def test_user_sandbox_removal_asks(self):
        quiet(ct.create, "box", sandbox=True)                              # the user's sandbox: managed...
        ct.agent_call("ct_start", {"name": "box"}, self.refuse)
        with self.assertRaisesRegex(RuntimeError, "terminal-ct rm box"):  # ...but deleting it is theirs to say
            ct.agent_call("ct_remove", {"name": "box"}, self.refuse)
        quiet(ct.stop, "box"); ct.internet("box", True)                   # the user put it online: not the agent's
        with self.assertRaisesRegex(RuntimeError, "terminal-ct start box"):
            ct.agent_call("ct_start", {"name": "box"}, self.refuse)

    def test_mcp_elicitation_round_trip(self):
        quiet(ct.create, "mine")
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {"elicitation": {}}}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ct_start", "arguments": {"name": "mine"}}},
                 {"jsonrpc": "2.0", "id": "ask-1", "result": {"action": "accept", "content": {"approve": True}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "exec", "arguments": {"command": "x"}}}]
        stdin, stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), io.StringIO()
        with unittest.mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(stdout):
            ct.mcp()
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual(replies[1]["method"], "elicitation/create")
        self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"]), {"started": "mine"})
        self.assertTrue(replies[3]["result"]["isError"])                  # no server container: name one
        with open(os.path.join(ct.STORE, "projects", "default", "agent.log")) as f:
            self.assertEqual(json.loads(f.readline())["tool"], "ct_start")


class Orchestration(unittest.TestCase):
    STACK = {"stack": "lab", "services": {
        "web": {"image": "alpine:3.20", "replicas": 2, "restart": "always",
                "health": {"exec": "true", "every": 10, "retries": 2}},
        "job": {"image": "alpine:3.20", "restart": "on-failure", "ttl": "30m"}}}

    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()
        self.p = unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test")  # no background process
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def kinds(self, out):
        return [(e["event"], e.get("container")) for e in out]

    def test_validation(self):
        bad = [{"stack": "x"}, {"stack": "x", "services": {"a": {"nope": 1}}},
               {"stack": "x", "services": {"a": {"restart": "sometimes"}}},
               {"stack": "x", "services": {"a": {"ttl": "soon"}}}, {"stack": "x", "services": {"a": {"replicas": 101}}},
               {"stack": "x" * 40, "services": {"service": {"replicas": 10}}},           # lab-...-10 too long
               {"stack": "x", "services": {"a": {"health": {"every": 3}}}}, {"stack": "x", "services": {"a": {"cmd": "ls"}}}]
        for s in bad:
            with self.assertRaises(SystemExit, msg=s):
                ct.check_stack(s)
        self.assertEqual(ct.check_stack({"stack": "x", "services": {"a": {}}})["services"]["a"],
                         {"replicas": 1, "restart": "always"})
        self.assertEqual([ct.parse_ttl(v) for v in (None, 90, "90s", "30m", "2h", "1d")], [None, 90, 90, 1800, 7200, 86400])

    def test_apply_scale_drift_and_rm(self):
        out = ct.apply(self.STACK)
        self.assertEqual(sorted(c for k, c in self.kinds(out["events"]) if k == "started"),
                         ["lab-job-1", "lab-web-1", "lab-web-2"])
        lab = ct.find("lab-web-2")["Config"]["Labels"]
        self.assertEqual((lab["terminal-ct.stack"], lab["terminal-ct.service"], lab["terminal-ct.replica"]), ("lab", "web", "2"))
        self.assertEqual(ct.reconcile("lab"), [])                                       # converged: nothing to do
        out = ct.scale("lab", "web", 1)
        self.assertEqual(self.kinds(out["events"]), [("removed", "lab-web-2")])
        s = ct.load_stack("lab"); s["services"]["web"]["image"] = "debian:stable-slim"; ct._save_stack(s)
        self.assertIn(("recreated", "lab-web-1"), self.kinds(ct.reconcile("lab")))      # its configuration drifted
        self.assertEqual(sorted(ct.stack_rm("lab")["containers"]), ["lab-job-1", "lab-web-1"])
        self.assertEqual(ct.stacks(), [])

    def test_restart_policies_and_backoff(self):
        ct.apply(self.STACK)
        self.n.exit_code = 1
        for c in ("tct-lab-web-1", "tct-lab-job-1"):
            self.n.run(["stop", c])                                                      # both died with code 1
        out = self.kinds(ct.reconcile("lab"))
        self.assertIn(("restarted", "lab-web-1"), out); self.assertIn(("restarted", "lab-job-1"), out)
        self.n.run(["stop", "tct-lab-web-1"])
        self.assertEqual(ct.reconcile("lab"), [])                                         # backing off (2 s)
        self.assertIn(("restarted", "lab-web-1"), self.kinds(ct.reconcile("lab", now=time.time() + 5)))
        self.n.exit_code = 0
        self.n.run(["stop", "tct-lab-job-1"])                                             # the job finished well
        self.assertNotIn(("restarted", "lab-job-1"), self.kinds(ct.reconcile("lab", now=time.time() + 600)))

    def test_quota_leaves_replicas_pending(self):
        with open(os.path.join(ct.STORE, "quota.json"), "w") as f:
            json.dump({"cpus": 2}, f)
        out = self.kinds(ct.apply(self.STACK)["events"])
        self.assertEqual(sum(k == "started" for k, _ in out), 2)
        self.assertEqual(sum(k == "pending" for k, _ in out), 1)                          # the third waits for room

    def test_ttl_expires_and_is_not_recreated(self):
        ct.apply(self.STACK)
        out = self.kinds(ct.reconcile("lab", now=time.time() + 1801))
        self.assertIn(("expired", "lab-job-1"), out)
        self.assertNotIn("tct-lab-job-1", self.n.objects)
        self.assertNotIn(("created", "lab-job-1"), self.kinds(ct.reconcile("lab", now=time.time() + 1900)))

    def test_health_probe_restarts_after_retries(self):
        ct.apply(self.STACK)
        self.n.exec_code = 1
        s = ct.load_stack("lab")
        now = time.time()
        self.assertEqual(ct.probe(s, now), [])                                            # in its grace time
        self.assertEqual(ct.probe(s, now + 31), [])                                       # first failure
        out = ct.probe(s, now + 42)
        self.assertEqual(self.kinds(out), [("unhealthy", "lab-web-1"), ("unhealthy", "lab-web-2")])
        self.assertEqual(self.n.objects["tct-lab-web-1"]["State"]["Status"], "exited")
        self.assertIn(("restarted", "lab-web-1"), self.kinds(ct.reconcile("lab")))        # back, policy always

    def test_events_and_agent_rules(self):
        ct.apply(self.STACK)
        self.assertTrue(all(e["stack"] == "lab" for e in ct.events("lab")))
        asked = []
        approve = lambda m, c: asked.append(c)
        with self.assertRaisesRegex(SystemExit, "belongs to the user"):
            ct.agent_call("ct_apply", {"stack": dict(self.STACK)}, approve)
        ct.agent_call("ct_scale", {"stack": "lab", "service": "web", "replicas": 3}, approve)
        self.assertEqual(asked, ["terminal-ct stack ... lab"])                            # the user's stack: asked
        mine = {"stack": "bot", "services": {"w": {"image": "alpine:3.20", "internet": True, "sandbox": False}}}
        asked.clear()
        out = ct.agent_call("ct_apply", {"stack": mine}, approve, project="p")
        self.assertEqual(asked, ["terminal-ct internet bot-w on"])
        self.assertEqual((ct.load_stack("bot")["created_by"], ct.load_stack("bot")["project"]), ("agent", "p"))
        self.assertTrue(ct.agent_owned(ct.find("bot-w-1")))
        self.assertEqual([s["stack"] for s in ct.agent_call("ct_stack", {"action": "ls"}, approve, project="p")], ["bot"])
        with self.assertRaisesRegex(SystemExit, "no stack named 'lab'"):
            ct.agent_call("ct_stack", {"action": "show", "stack": "lab"}, approve, project="p")
        self.assertTrue(all(e["stack"] == "bot" for e in ct.agent_call("ct_events", {}, approve, project="p")))
        sandboxed = {"stack": "sb", "services": {"w": {"image": "alpine:3.20"}}}
        ct.agent_call("ct_apply", {"stack": sandboxed}, approve)
        self.assertEqual(ct.find("sb-w-1")["Config"]["Labels"]["terminal-ct.sandbox"], "1")  # sandbox by default

    def test_user_stop_is_kept_and_live_resize_keeps_data(self):
        ct.apply(self.STACK)
        quiet(ct.stop, "lab-web-1")                                                       # the user's own stop
        self.assertNotIn(("restarted", "lab-web-1"), self.kinds(ct.reconcile("lab", now=time.time() + 600)))
        quiet(ct.start, "lab-web-1")
        s = ct.load_stack("lab"); s["services"]["web"]["cpus"] = 2; ct._save_stack(s)
        out = self.kinds(ct.reconcile("lab"))
        self.assertIn(("resized", "lab-web-1"), out); self.assertNotIn(("recreated", "lab-web-1"), out)
        s["services"]["web"]["image"] = "debian:stable-slim"; ct._save_stack(s)
        self.assertIn(("recreated", "lab-web-1"), self.kinds(ct.reconcile("lab")))
        self.assertNotIn(["volume", "rm", "tct-lab-web-1-data"], self.n.calls)          # drift keeps /data
        ct.scale("lab", "web", 1)
        self.assertIn(["volume", "rm", "tct-lab-web-2-data"], self.n.calls)             # scale down drops it

    def test_agent_stack_and_a_user_network_made_later(self):
        bot = {"stack": "bot", "services": {"w": {"image": "alpine:3.20", "networks": ["lab"]}}}
        ct.agent_call("ct_apply", {"stack": bot}, lambda m, c: None)                     # lab created as the agent's
        quiet(ct.rm, "bot-w-1"); ct.net_rm("lab"); ct.net_create("lab")                  # now the user's
        self.assertIn(("create_failed", "bot-w-1"), self.kinds(ct.reconcile("bot")))
        asked = []
        ct.agent_call("ct_apply", {"stack": bot}, lambda m, c: asked.append(c))          # applied again: asked
        self.assertEqual((asked, ct.load_stack("bot")["approved_networks"]), (["terminal-ct net attach bot-w lab"], ["lab"]))
        self.assertTrue(ct.find("bot-w-1"))

    def test_apply_again_reruns_expired_jobs(self):
        ct.apply(self.STACK)
        ct.reconcile("lab", now=time.time() + 1801)
        self.assertNotIn("tct-lab-job-1", self.n.objects)
        ct.apply(self.STACK)
        self.assertIn("tct-lab-job-1", self.n.objects)

    def test_lock_is_only_removed_by_its_holder(self):
        with ct.quota_lock("x") as l:
            shutil.rmtree(l.path); os.mkdir(l.path)                                      # broken and taken by another
        self.assertTrue(os.path.isdir(os.path.join(ct.STORE, ".x.lock")))                 # theirs is left alone

    def test_resume_leaves_stacks_to_the_daemon(self):
        ct.apply(self.STACK)
        self.n.run(["stop", "tct-lab-web-1"])
        out = quiet(ct.resume)
        self.assertEqual([r for r in out["resumed"] if r.get("name")], [])
        self.assertEqual(out["resumed"][-1], {"daemon": "test"})


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()
        self.p = unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_data_and_text(self):
        ct.apply({"stack": "lab", "services": {"web": {"image": "alpine:3.20", "replicas": 2,
                                                       "health": {"exec": "true"}}}})
        quiet(ct.create, "solo", sandbox=True, project="other")
        d = ct.dashboard_data()
        web = next(r for r in d["rows"] if r["name"] == "lab-web-1")
        self.assertEqual((web["status"], web["health"], web["stack"], web["cpu_percent"], web["used_memory_gb"]),
                         ("running", "ok", "lab/web/1", 12.5, 0.5))
        self.assertEqual(next(r for r in d["rows"] if r["name"] == "solo")["flags"], "S")
        text = ct.dashboard_text(d, 160)
        self.assertIn("lab-web-2", text); self.assertIn("events", text); self.assertNotIn("\x1b[", text)
        colour = ct.dashboard_text(d, 160, selected=0, ansi=True, message="done")
        self.assertIn("\x1b[32m", colour); self.assertIn("q quit", colour); self.assertTrue(colour.endswith("done"))
        self.assertTrue(all(len(l) <= 60 for l in ct.dashboard_text(d, 60).splitlines()))   # fits the width
        scoped = ct.dashboard_data("other")
        self.assertEqual([r["name"] for r in scoped["rows"]], ["solo"])
        self.assertEqual((scoped["quota"]["cpus"]["used"], scoped["quota"]["disk_gb"]["used"]), (0, "-"))  # its own

    def test_tall_lists_fit_the_screen(self):
        for i in range(30):
            quiet(ct.create, f"c{i:02}")
        d = ct.dashboard_data()
        text = ct.dashboard_text(d, 150, selected=25, ansi=True, height=24)
        self.assertLessEqual(len(text.splitlines()), 24)
        self.assertIn("c25", text); self.assertNotIn("c00", text)                       # scrolled to the selection

    def test_mcp_dashboard_and_logs(self):
        quiet(ct.create, "mine")
        ct.agent_call("ct_create", {"name": "job"}, lambda m, c: None, project="p")
        text = ct.agent_call("dashboard", {}, lambda m, c: None, project="p")
        self.assertIn("job", text); self.assertNotIn("mine", text)                       # its project only
        self.assertEqual(ct.agent_call("ct_logs", {"name": "job", "n": 3}, None, project="p"), "line 0\nline 1\nline 2\n")
        with self.assertRaisesRegex(RuntimeError, "terminal-ct logs mine"):                # the user's: asked
            ct.agent_call("ct_logs", {"name": "mine"}, lambda m, c: (_ for _ in ()).throw(RuntimeError(c)))
        self.assertEqual(ct._age(None), "-"); self.assertEqual([ct._age(s) for s in (5, 300, 7300, 200000)],
                                                               ["5s", "5m", "2h", "2d"])


class Web(unittest.TestCase):
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct._NODES, ct._VM_CACHE = {"docker": self.n}, {}                                    # stacks name their engine
        ct.STORE = tempfile.mkdtemp()
        self.p = unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_api(self):
        api = lambda m, path, q=None, body=None: ct.web_api(m, path, q or {}, body)[1]
        api("POST", "/api/do", body={"op": "apply", "stack": {"stack": "lab", "services": {"web": {"image": "alpine:3.20"}}}})
        quiet(ct.create, "solo", project="other")
        d = api("GET", "/api/state")
        self.assertEqual([r["name"] for r in d["rows"]], ["lab-web-1", "solo"])
        self.assertEqual(d["stacks"][0]["services"], {"web": {"replicas": 1, "image": "alpine:3.20"}})
        self.assertIsNone(d["quota"])                                                      # apart: it is slow
        qd = api("GET", "/api/quota")
        self.assertIn("other", qd["projects"]); self.assertIn("cpus", qd["quota"])
        self.assertEqual(api("GET", "/api/quota", {"project": "other"})["quota"]["cpus"]["used"], 0)
        self.assertEqual([r["name"] for r in api("GET", "/api/state", {"project": "other"})["rows"]], ["solo"])
        self.assertEqual(api("GET", "/api/state", {"project": "other"})["stacks"], [])      # lab is in default
        self.assertEqual(api("GET", "/api/stack", {"name": "lab"})["desired"],
                         {"stack": "lab", "services": {"web": {"image": "alpine:3.20", "replicas": 1, "restart": "always"}}})
        self.assertEqual(api("GET", "/api/logs", {"name": "solo", "n": "2"}), b"line 0\nline 1\n")
        self.assertEqual(api("POST", "/api/do", body={"op": "start", "name": "solo"}), {"started": "solo"})
        self.assertEqual(api("POST", "/api/do", body={"op": "restart", "name": "solo"}), {"started": "solo"})
        self.assertEqual(self.n.objects["tct-solo"]["State"]["Status"], "running")
        self.assertEqual(api("POST", "/api/do", body={"op": "stop", "name": "solo"}), {"stopped": "solo"})
        self.assertEqual(ct.wanted("solo"), "stopped")                                     # resume leaves it
        api("POST", "/api/do", body={"op": "scale", "stack": "lab", "service": "web", "replicas": 2})
        self.assertIn("tct-lab-web-2", self.n.objects)
        self.assertEqual(api("POST", "/api/do", body={"op": "rm", "name": "solo"}), {"removed": "solo"})
        self.assertEqual(api("POST", "/api/do", body={"op": "stack_rm", "stack": "lab"})["removed"], "lab")
        self.assertEqual(self.n.objects, {})
        for bad in ({"op": "rm", "name": "../x"}, {"op": "format"}, {"kind": "vm", "op": "rm", "name": "wn"},
                    {"kind": "vm", "op": "start", "name": "-x"}):
            with self.assertRaises((SystemExit, ValueError)):
                api("POST", "/api/do", body=bad)
        with self.assertRaises(SystemExit):
            api("GET", "/api/state", {"project": "Bad Name"})
        with self.assertRaises(ct.NoRoute):
            api("GET", "/api/nothing")
        with self.assertRaises(SystemExit):                                                 # project checked
            api("POST", "/api/do", body={"op": "apply", "project": "Bad/../x",
                                         "stack": {"stack": "b", "services": {"s": {"image": "alpine:3.20"}}}})
        with unittest.mock.patch.object(ct, "act", lambda n, t, a: (b"PNG" + n.encode(), (1, 1))):
            self.assertEqual(ct.web_api("GET", "/api/shot", {"name": "lab-web-1"}), ("image/png", b"PNGlab-web-1"))

    def test_vms_through_terminal_vm(self):
        fake = os.path.join(ct.STORE, "fake_vm.py")
        with open(fake, "w") as f:
            f.write("import json, sys\na = sys.argv[1:]\n"
                    "if a[0] == 'ls': print(json.dumps([{'name': 'wn', 'status': 'running', 'project': 'default', "
                    "'memory_gb': 4}, {'name': 'deb', 'status': 'stopped', 'project': 'lab'}], indent=1))\n"
                    "elif a[0] == 'top': print(json.dumps({'vms': [{'name': 'wn', 'cpu_percent': 7, 'host_memory_gb': 3.1}]}))\n"
                    "elif a[0] == 'action': open(a[a.index('--output') + 1], 'wb').write(b'PNGVM'); print('{}')\n"
                    "elif a[0] == 'start': print('booting'); print(json.dumps({'started': a[1]}))\n"
                    "elif a[0] == 'stop' and a[1] == 'wn': print(json.dumps({'stopped': a[1:]}))\n"
                    "else: sys.stderr.write(json.dumps({'error': 'no such VM'}) + '\\n'); sys.exit(1)\n")
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_VM": f'"{sys.executable}" "{fake}"'}):
            v = ct.vm_state()
            self.assertIs(ct.vm_state(), v)                                                  # cached: one call for all tabs
            self.assertEqual([(m["name"], m.get("cpu_percent")) for m in v["vms"]], [("wn", 7), ("deb", None)])
            self.assertEqual([m["name"] for m in ct.vm_state("lab")["vms"]], ["deb"])
            self.assertEqual(ct.web_api("GET", "/api/shot", {"kind": "vm", "name": "wn"}), ("image/png", b"PNGVM"))
            self.assertEqual(os.listdir(os.path.join(ct.STORE, ".web")), [])                 # temporary file removed
            self.assertEqual(ct.web_api("POST", "/api/do", {}, {"kind": "vm", "op": "start", "name": "wn"})[1],
                             {"started": "wn"})                                              # progress lines skipped
            self.assertEqual(ct.web_api("POST", "/api/do", {}, {"kind": "vm", "op": "force_stop", "name": "wn"})[1],
                             {"stopped": ["wn", "--force"]})
            with self.assertRaisesRegex(RuntimeError, "no such VM"):
                ct.web_api("POST", "/api/do", {}, {"kind": "vm", "op": "stop", "name": "zz"})
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_VM": ""}), \
                unittest.mock.patch.object(ct.shutil, "which", lambda e: None):
            ct._VM_CACHE.clear()
            self.assertEqual(ct.vm_state()["vms"], [])                                       # alone: no VM section

    def test_http_server(self):
        threading.Thread(target=ct.web_serve, args=(0,), daemon=True).start()
        for _ in range(200):
            if os.path.exists(ct._daemon_file("web")):
                break
            time.sleep(0.05)
        with open(ct._daemon_file("web")) as f:
            port = json.load(f)["port"]
        token = ct.web_token()
        self.assertEqual(token, ct.web_token())                                              # kept

        def call(path, method="GET", headers=None, body=None):
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read()
        status, page = call("/")
        self.assertEqual(status, 200); self.assertIn(b"terminal-ct", page)
        self.assertNotIn(token.encode(), page)                                               # the page holds no secret
        self.assertEqual(call("/api/state")[0], 401)
        self.assertEqual(call("/api/state", headers={"X-Token": "wrong"})[0], 401)
        self.assertEqual(call("/", headers={"Host": "evil.example:80"})[0], 403)             # DNS rebinding
        status, body = call("/api/state", headers={"X-Token": token})
        self.assertEqual((status, json.loads(body)["engine"]), (200, "docker"))
        status, body = call("/api/do", "POST", {"X-Token": token, "Content-Type": "application/json"},
                            b'{"op": "start", "name": "nobody"}')
        self.assertEqual(status, 400); self.assertIn("nobody", json.loads(body)["error"])   # the server goes on
        status, body = call("/api/do", "POST", {"X-Token": token, "Content-Type": "text/plain"}, b'{"op": "rm"}')
        self.assertIn("Content-Type", json.loads(body)["error"])                             # no simple request
        with open(os.path.join(ct.STORE, "web.token"), "w"):                                 # cut short by a crash
            pass
        self.assertEqual(call("/api/state", headers={"X-Token": ""})[0], 401)                # never "" == ""
        token = ct.web_token()
        self.assertEqual(len(token), 32)
        self.assertEqual(call("/api/state", headers={"X-Token": token})[0], 200)
        self.assertEqual(call("/api/state", headers={"X-Token": "été"})[0], 401)  # not ASCII: refused
        self.assertEqual(call("/api/nothing", headers={"X-Token": token})[0], 404)
        self.assertEqual(call("/api/state", headers={"X-Token": token})[0], 200)

    def test_threads_capture_their_own_output(self):
        out = {}

        def work(i):
            for _ in range(200):
                buf = io.StringIO()
                with ct.captured(buf):
                    print(i)
                    time.sleep(0)
                if buf.getvalue() != f"{i}\n":
                    out[i] = buf.getvalue()
        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(out, {})

    def test_stack_calls_on_the_stacks_engine(self):
        pod = FakeNode("podman")
        ct._NODES["podman"] = pod
        ct._LOCAL.node = pod
        try:
            ct.apply({"stack": "lab", "services": {"web": {"image": "alpine:3.20"}}})        # applied on podman
        finally:
            ct._LOCAL.node = None
        ct.web_api("POST", "/api/do", {}, {"op": "scale", "stack": "lab", "service": "web", "replicas": 2})
        self.assertEqual((sorted(pod.objects), self.n.objects), (["tct-lab-web-1", "tct-lab-web-2"], {}))
        ct.web_api("POST", "/api/do", {}, {"op": "apply", "stack": {"stack": "lab", "services": {"web": {"image": "alpine:3.20"}}}})
        self.assertEqual(ct.load_stack("lab")["engine"], "podman")                           # not moved to docker
        ct.web_api("POST", "/api/do", {}, {"op": "stack_rm", "stack": "lab"})
        self.assertEqual(pod.objects, {})

    def test_reconciler_engine_is_its_threads(self):
        seen = []
        t = threading.Thread(target=lambda: (setattr(ct._LOCAL, "node", FakeNode("podman")), seen.append(ct.node().engine)))
        t.start(); t.join()
        self.assertEqual((seen, ct.node().engine), (["podman"], "docker"))


class Phase8(unittest.TestCase):
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()

    def test_bench(self):
        r = ct.bench(runs=3)
        self.assertEqual(set(r["results"]), {"create", "start", "exec_true", "ls", "dashboard_data", "stop", "rm"})
        self.assertEqual(r["results"]["start"]["runs"], 3)
        self.assertLessEqual(r["results"]["rm"]["min_ms"], r["results"]["rm"]["median_ms"])
        self.assertEqual(self.n.objects, {})                                               # nothing left behind
        self.assertEqual(ct._summary([4, 1, 3, 2])["median_ms"], 2.5)
        quiet(ct.create, "bench-ct")                                                       # the user's own
        ct.bench(runs=1)
        self.assertEqual(list(self.n.objects), ["tct-bench-ct"])                           # left alone

    def test_skill(self):
        with open(ct.SKILL, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: terminal-ct\n"))
        for tool in ("project_status", "notes_write", "exec", "ct_create", "ct_apply", "screenshot"):  # names that exist
            self.assertIn(f"`{tool}`", text)
        self.assertTrue({"project_status", "ct_apply"} <= ct.AGENT_NAMES)
        dest = tempfile.mkdtemp()
        quiet(ct.skills, "install", dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "terminal-ct", "SKILL.md")))


class SharedDashboard(unittest.TestCase):
    """The web dashboard's code is one block, identical in terminal-ct and terminal-vm: change it in both, then set
    SHARED_SHA in both tests to the new value."""
    SHARED_SHA = "1438c234262934d5b591839c36d218bb09d2204f8bc14b6e2a6d0666630ab50f"

    @staticmethod
    def block(path):
        text = open(path, encoding="utf-8").read().replace("\r\n", "\n")
        return text[text.index("# ===== shared web dashboard"):text.index("# ===== end of shared web dashboard =====")]

    def test_block_is_the_shared_one(self):
        import hashlib
        mine = self.block(ct.__file__)
        self.assertEqual(hashlib.sha256(mine.encode()).hexdigest(), self.SHARED_SHA,
                         "the shared dashboard block changed: copy it to the other tool and update SHARED_SHA in both")
        other = os.environ.get("TERMINAL_VM_SRC") or os.path.join(os.path.dirname(os.path.abspath(ct.__file__)), "..", "terminal-mac-src", "terminal-vm", "terminal_vm.py")
        if os.path.isfile(other):  # both checked out side by side: compare them directly
            self.assertEqual(mine, self.block(other))


class Nodes(unittest.TestCase):
    """Phase 9.2: nodes are terminal-vm VMs with Docker; engine calls go there through `terminal-vm exec`."""

    def setUp(self):
        self.local = ct.NODE = FakeNode("podman")
        ct.STORE = tempfile.mkdtemp()
        self.engines, self.vm_calls, self.vms = {}, [], {}
        self.shell, self.files, self.deployed, self.acts = [], {}, {}, []
        self.vmnets, self.netscripts, self.tables = {}, [], {}
        ct._DEPLOYED.clear()

        def vm_call(args, timeout=60, **k):
            self.vm_calls.append(args)
            if args[0] == "exec":
                name, argv = args[1], args[args.index("--") + 1:]
                if name in getattr(self, "frozen", ()):
                    raise RuntimeError("guest command timed out (15 s)")
                if argv[0] == "docker" and argv[1] in getattr(self, "refuse", {}).get(name, ()):
                    return {"exit": 1, "stdout": "", "stderr": "Docker is restarting"}
                if argv[0] == "docker":
                    r = self.engines.setdefault(name, FakeNode("docker")).run(argv[1:], check=False)
                    return {"exit": r.returncode, "stdout": r.stdout, "stderr": "no such object" if r.returncode else ""}
                if argv[0] == "sh" and "link_for()" in argv[2]:                    # the node's networks made so
                    lines = []
                    for net in re.findall(r"^echo (\S+) \"\$GW\"", argv[2], re.M):
                        self.engines.setdefault(name, FakeNode("docker")).nets[ct.NET_PREFIX + net] = {
                            "terminal-ct.net": net}
                        lines.append(f"{net} 172.20.0.1 br-0123456789ab 172.20.0.0/16")
                    self.netscripts.append((name, argv[2]))
                    return {"exit": 0, "stdout": "\n".join(lines) + "\n", "stderr": ""}
                if argv[0] == "sh" and "base64 -d" in argv[2]:                     # a DNS table written
                    b64, path = re.match(r"echo (\S+) \| base64 -d > (\S+)\.new", argv[2]).groups()
                    self.tables[(name, path)] = json.loads(base64.b64decode(b64))
                    return {"exit": 0, "stdout": "", "stderr": ""}
                if argv[0] == "sh":
                    self.shell.append(argv[2])
                    if argv[2].endswith("&& echo up"):                                  # back after a restart
                        return {"exit": 0, "stdout": "up\n", "stderr": ""}
                    if "sha256sum" in argv[2]:
                        return {"exit": 0, "stdout": self.deployed.get(name, ""), "stderr": ""}
                    for out in re.findall(r"> (/tmp/tct-\w+/out)", argv[2]):
                        self.files[out] = b"tar of /data"
                    if argv[2].startswith("test -d"):
                        return {"exit": 0, "stdout": "file\n", "stderr": ""}
                    m = re.match(r"docker cp \S+ (/tmp/tct-\w+/out)$", argv[2])
                    if m:
                        self.files[m.group(1)] = b"from the container"
                    return {"exit": 0, "stdout": "", "stderr": ""}
                if argv[:2] == ["env", f"TERMINAL_CT_HOME={ct.STORE}"]:
                    self.acts.append(argv[-3:])
                    if argv[-2] == "screenshot":
                        out = {"png": base64.b64encode(b"PNG!").decode(), "size": [1280, 800]}
                    else:
                        out = {"result": {"ok": argv[-2]}}
                    return {"exit": 0, "stdout": json.dumps(out), "stderr": ""}
                if argv[0] == "df":
                    return {"exit": 0, "stdout": "Filesystem 1K-blocks Used Available\n/dev/vda1 20000000 1 10485760 1% /\n"}
                return {"exit": 0, "stdout": "", "stderr": ""}                   # test -f ready
            if args[:2] == ["net", "ls"]:
                return [dict(v, vms=list(v["vms"])) for v in self.vmnets.values()]
            if args[:2] == ["net", "create"]:
                self.vmnets[args[2]] = {"name": args[2], "index": len(self.vmnets) + 1, "vms": [],
                                        "subnet": f"10.77.{len(self.vmnets) + 1}.0/24"}
                return self.vmnets[args[2]]
            if args[:2] == ["net", "attach"]:
                n = self.vmnets[args[3]]
                ip = f"10.77.{n['index']}.{10 + len(n['vms'])}"
                n["vms"].append({"vm": args[2], "ip": ip, "mac": f"52:54:00:00:00:{len(n['vms']):02x}"})
                return {}
            if args[0] == "push":
                with open(args[2], "rb") as f:
                    self.files[args[3]] = f.read()
                if args[3] == ct.NODE_CT + ".new":                             # then checked and moved
                    self.deployed[args[1]] = hashlib.sha256(self.files[args[3]]).hexdigest()
                return {}
            if args[0] == "pull":
                with open(args[3], "wb") as f:
                    f.write(self.files[args[2]])
                return {}
            if args[0] == "create":
                self.vms[args[1]] = "stopped"
            elif args[0] == "start":
                if getattr(self, "fail_start", False):
                    raise RuntimeError("quota exceeded")
                self.vms[args[1]] = "running"
            elif args[0] == "stop":
                self.vms[args[1]] = "stopped"
            elif args[0] == "rm":
                self.vms.pop(args[1])
            elif args[0] == "ls":
                return [{"name": n, "status": s} for n, s in self.vms.items()]
            return {}
        self.patch = unittest.mock.patch.object(ct, "vm_call", vm_call)
        self.patch.start()
        self.age = unittest.mock.patch.object(ct, "UP_MAX_AGE", -1)  # the test changes VM states at once
        self.age.start()
        self.inproc = unittest.mock.patch.object(ct, "vm_module", lambda: None)  # through the fake command line
        self.inproc.start()

    def tearDown(self):
        self.patch.stop(); self.age.stop(); self.inproc.stop()
        ct.NODE = None

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ct.main(list(argv))
        ct.NODE = self.local                                                   # main sets it for its own command
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def test_add_create_follow_and_remove(self):
        r = self.cli("node", "add", "n1", "--image", "alpine", "--memory", "1")
        self.assertEqual(r["node"], "n1")
        self.assertIn(["create", "n1", "--image", "alpine", "--node", "--cpus", "2", "--memory", "1", "--max-memory", "1", "--disk", "20"],
                      self.vm_calls)
        self.assertIn(["supervise", "n1", "--restart", "always"], self.vm_calls)          # restarted if it falls
        self.cli("--node", "n1", "create", "web")
        node = self.engines["n1"]
        self.assertIn("tct-web", node.objects)                                       # created in the node...
        self.assertNotIn("tct-web", self.local.objects)                              # ...not on this host
        self.cli("start", "web")                                                     # no --node: it is remembered
        self.assertEqual(node.objects["tct-web"]["State"]["Status"], "running")
        self.assertEqual(self.cli("node", "ls")[0]["containers"], ["web"])
        with self.assertRaisesRegex(SystemExit, "still has containers"):
            self.cli("node", "rm", "n1")
        self.cli("rm", "web")
        self.assertNotIn("tct-web", node.objects)
        self.assertEqual(self.cli("node", "ls")[0]["containers"], [])
        self.assertEqual(self.cli("node", "rm", "n1")["removed"], "n1")
        self.assertEqual((self.cli("node", "ls"), self.vms), ([], {}))

    def test_force_removes_its_containers_first(self):
        self.cli("node", "add", "n1")
        self.cli("--node", "n1", "create", "a")
        self.assertEqual(self.cli("node", "rm", "n1", "--force")["containers"], ["a"])
        self.assertNotIn("tct-a", self.engines["n1"].objects)

    def test_refused(self):
        with self.assertRaisesRegex(SystemExit, "no node named"):
            self.cli("--node", "nope", "ls")
        self.cli("node", "add", "n1")
        with self.assertRaisesRegex(SystemExit, "not both"):
            self.cli("--engine", "docker", "--node", "n1", "ls")
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.cli("node", "add", "n1")

    def test_failures_leave_nothing_stuck(self):
        self.fail_start = True                                                   # VM made, start refused
        with self.assertRaisesRegex(SystemExit, "terminal-vm start: quota exceeded"):
            self.cli("node", "add", "n1")
        self.assertEqual(self.cli("node", "ls")[0]["node"], "n1")                # registered: node rm can clean it
        self.assertEqual(self.cli("node", "rm", "n1")["removed"], "n1")
        self.assertEqual(self.vms, {})
        self.fail_start = False
        self.cli("node", "add", "n2")
        del self.vms["n2"]                                                       # its VM removed by hand
        self.assertEqual(self.cli("node", "ls")[0]["status"], "missing")
        self.assertEqual(self.cli("node", "rm", "n2")["removed"], "n2")          # only the registry to clean
        with self.assertRaisesRegex(SystemExit, "invalid node name"):
            self.cli("node", "add", "a.b")

    def test_one_name_across_host_and_nodes(self):
        self.cli("node", "add", "n1")
        self.cli("--node", "n1", "create", "web")
        with self.assertRaisesRegex(SystemExit, "in node n1"):
            quiet(ct.create, "web")                                              # MCP, web: never a local twin
        self.local.objects["tct-web"] = dict(self.engines["n1"].objects["tct-web"])  # one made before this rule
        with unittest.mock.patch.object(ct, "pick_engine", lambda e: e), \
                unittest.mock.patch.object(ct, "Node", lambda e: self.local):
            self.cli("--engine", "podman", "rm", "web")                          # removes the local one...
        self.assertNotIn("tct-web", self.local.objects)
        self.assertEqual(self.cli("node", "ls")[0]["containers"], ["web"])       # ...the node's stays placed
        os.makedirs(os.path.join(ct.STORE, "web"), exist_ok=True)
        self.vms["n1"] = "stopped"                                               # node down: force still cleans up
        self.cli("node", "rm", "n1", "--force")
        self.assertFalse(os.path.exists(os.path.join(ct.STORE, "web")))

    def test_spread_failover_and_return(self):
        """9.3/9.4: replicas spread over the nodes; a node down -> made again on another; back -> its copies go."""
        with unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test"):
            self.cli("node", "add", "n1"); self.cli("node", "add", "n2")
            ct.apply({"stack": "lab", "services": {"app": {"image": "alpine:3.20", "replicas": 3, "spread": "nodes"}}})
            where = lambda: {r["name"]: r.get("node") for r in ct.all_containers()}
            self.assertEqual(where(), {"lab-app-1": "n1", "lab-app-2": "n2", "lab-app-3": "n1"})   # least loaded first
            self.assertEqual(self.local.objects, {})                                                # none on this host
            self.assertEqual(ct.stack_show("lab")["services"]["app"]["running"], 3)
            self.vms["n1"] = "stopped"                                                              # n1 falls
            out = ct.reconcile("lab")
            self.assertEqual(sorted(e["container"] for e in out if e["event"] == "failover"), ["lab-app-1", "lab-app-3"])
            self.assertEqual(where(), {"lab-app-1": "n2", "lab-app-2": "n2", "lab-app-3": "n2"})
            self.vms["n1"] = "running"                                                              # n1 back, old copies
            self.assertEqual(sorted(self.engines["n1"].objects), ["tct-lab-app-1", "tct-lab-app-3"])
            out = ct.reconcile("lab")
            self.assertEqual(sorted(e["container"] for e in out if e["event"] == "removed"), ["lab-app-1", "lab-app-3"])
            self.assertEqual(self.engines["n1"].objects, {})
            self.assertEqual(where(), {"lab-app-1": "n2", "lab-app-2": "n2", "lab-app-3": "n2"})   # stays put
            self.assertEqual(sorted(ct.stack_rm("lab")["containers"]), ["lab-app-1", "lab-app-2", "lab-app-3"])
            self.assertEqual((self.engines["n2"].objects, ct._nodes()["placement"]), ({}, {}))

    def test_frozen_node_hiccups_and_copies(self):
        """Review: a node not answering is left alone for two passes, down at the third; a copy removed on its
        return keeps the replica's state on this host; one replica's error does not stop the pass."""
        with unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test"):
            self.cli("node", "add", "n1"); self.cli("node", "add", "n2")
            ct.apply({"stack": "lab", "services": {"app": {"image": "alpine:3.20", "replicas": 2, "spread": "nodes"}}})
            self.frozen = {"n1"}                                                    # VM runs, its agent is silent
            for _ in range(2):
                self.assertEqual([e for e in ct.reconcile("lab") if e["event"] == "failover"], [])
            self.assertEqual([e["container"] for e in ct.reconcile("lab") if e["event"] == "failover"], ["lab-app-1"])
            os.makedirs(os.path.join(ct.STORE, "lab-app-1"), exist_ok=True)
            with open(os.path.join(ct.STORE, "lab-app-1", "want"), "w") as f:
                f.write("stopped")                                                  # the user stopped the new copy
            self.frozen = set()
            out = ct.reconcile("lab")                                               # n1 back: its copy goes...
            self.assertIn(("removed", "lab-app-1"), [(e["event"], e.get("container")) for e in out])
            self.assertTrue(os.path.exists(os.path.join(ct.STORE, "lab-app-1", "want")))   # ...the state stays
            self.refuse = {"n1": {"inspect"}}                                       # made, then not found
            ct.apply({"stack": "lab", "services": {"app": {"image": "alpine:3.20", "replicas": 4, "spread": "nodes"}}})
            kinds = [(e["event"], e.get("container")) for e in ct.events("lab", 50)]
            self.assertIn(("reconcile_failed", "lab-app-3"), kinds)                 # one replica's error...
            self.assertLess(kinds.index(("reconcile_failed", "lab-app-3")),         # ...and the pass went on
                            max(i for i, k in enumerate(kinds) if k[1] == "lab-app-4"))

    def test_pending_said_once_and_rm_forgets_placement(self):
        with unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test"):
            self.cli("node", "add", "n1")
            ct.apply({"stack": "db", "services": {"pg": {"image": "alpine:3.20", "node": "n1"}}})
            self.vms["n1"] = "stopped"; self.engines["n1"].objects.clear()
            self.assertEqual([e["event"] for e in ct.reconcile("db")], ["pending"])
            self.assertEqual(ct.reconcile("db"), [])                                # not again every pass
            self.vms["n1"] = "running"
            ct.reconcile("db")
            with ct.on_node("n1"):
                quiet(ct.rm, "db-pg-1")                                             # as the web or MCP would
            self.assertEqual(ct._nodes()["placement"], {})

    def test_networks_between_nodes(self):
        """10.1/10.2/10.4: nodes on a network; ports on the node's address only; one replica per node; DNS tables."""
        with unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test"):
            for n in ("n1", "n2"):
                self.cli("node", "add", n)
            r = self.cli("node", "join", "n1", "back")
            self.assertEqual((r["ip"], r["gateway"]), ("10.77.1.10", "172.20.0.1"))
            steps = [c[:3] for c in self.vm_calls if c[:2] in (["net", "create"], ["net", "attach"]) or c[0] in ("stop", "start")]
            self.assertEqual(steps[-4:], [["net", "create", "tct-back"], ["stop", "n1"], ["net", "attach", "n1"],
                                          ["start", "n1"]])                       # its cards change while it is off
            script = self.netscripts[-1][1]
            self.assertIn("|| ip addr add 10.77.1.10/24", script)
            self.assertIn("iptables -I DOCKER-USER -i \"$BR\" ! -o \"$BR\" -m conntrack --ctstate NEW ! --ctorigdst 10.77.1.0/24 -j DROP",
                          script)                                              # the address first asked, before DNAT
            self.assertIn("-i docker0 -m conntrack --ctstate NEW --ctorigdst 10.77.0.0/16 -j DROP", script)  # nor via the internet bridge
            self.assertIn('_dns "$GW/${SUB#*/}" "$BR" /var/lib/terminal-ct/dns/back.json', script)
            with self.assertRaisesRegex(SystemExit, "not on back"):
                self.cli("--node", "n2", "create", "x", "--net", "back")
            with self.assertRaisesRegex(SystemExit, "ports"):
                ct.create("y", ports=[80])                                           # not on a node's network
            self.cli("node", "join", "n2", "back")
            with ct.on_node("n1"):
                quiet(ct.create, "solo", nets=["back"], ports=[8080])
            args = next(c for c in self.engines["n1"].calls if c[0] == "create" and "tct-solo" in c)
            self.assertIn("--dns", args); self.assertEqual(args[args.index("--dns") + 1], "172.20.0.1")
            self.assertEqual(args[args.index("-p") + 1], "10.77.1.10:8080:8080")   # the node's address, never 0.0.0.0
            ct.apply({"stack": "lab", "services": {"web": {"image": "alpine:3.20", "replicas": 3, "spread": "nodes",
                                                             "networks": ["back"], "ports": [80]}}})
            where = {r["name"]: r.get("node") for r in ct.all_containers() if r["name"].startswith("lab-")}
            self.assertEqual(sorted(where.values()), ["n1", "n2"])                  # one per node; the third waits
            self.assertIn("no free node on its networks", json.dumps(ct.events("lab", 20)))
            tables = {k[0]: v for k, v in self.tables.items() if k[1].endswith("/back.json")}
            self.assertEqual(tables["n1"], tables["n2"])                             # the same names on every node
            self.assertEqual(tables["n1"]["web"], ["10.77.1.10", "10.77.1.11"])
            self.assertEqual(tables["n1"]["web.lab"], tables["n1"]["web"])
            self.vms["n2"] = "stopped"                                                # n2 falls: its address goes
            ct.reconcile("lab")
            self.assertEqual(self.tables[("n1", "/var/lib/terminal-ct/dns/back.json")]["web"], ["10.77.1.10"])

    def test_network_security(self):
        """Review of phase 10: terminal-vm's values checked before a root script; names not shared; agents kept off
        the user's networks between nodes; a boolean is not a port."""
        good = {"name": "tct-back", "subnet": "10.77.4.0/24"}, {"ip": "10.77.4.10", "mac": "52:54:00:aa:bb:cc"}
        self.assertEqual(ct._clean_net(*good)["bridge"], "tctn4")
        for net, vm in ((dict(good[0], name="tct-x;reboot"), good[1]), (dict(good[0], subnet="192.168.1.0/24"), good[1]),
                        (good[0], dict(good[1], ip="10.77.5.10")), (good[0], dict(good[1], mac="52:54:00:aa:bb:cc; id")),
                        (dict(good[0], subnet="10.77.0.0/16"), good[1]), ({"name": "tct-y"}, good[1])):
            self.assertIsNone(ct._clean_net(net, vm), (net, vm))
        with self.assertRaises(SystemExit):
            ct.check_stack({"stack": "x", "services": {"a": {"spread": "nodes", "networks": ["b"], "ports": [True]}}})
        with unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test"):
            self.cli("node", "add", "n1"); self.cli("node", "join", "n1", "back")
            ct.apply({"stack": "one", "services": {"db": {"image": "alpine:3.20", "node": "n1", "networks": ["back"]}}})
            with self.assertRaisesRegex(SystemExit, "already a service of stack one"):
                ct.apply({"stack": "two", "services": {"db": {"image": "alpine:3.20", "spread": "nodes",
                                                               "networks": ["back"]}}})
            ct.apply({"stack": "ag", "services": {"web": {"image": "alpine:3.20", "node": "n1", "networks": ["back"]}}},
                     created_by="agent")
            self.assertIn("is the user's", json.dumps(ct.events("ag", 10)))
            self.assertNotIn("tct-ag-web-1", self.engines["n1"].objects)          # never made there

    def test_pinned_node_down_waits(self):
        with unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test"):
            self.cli("node", "add", "n1")
            ct.apply({"stack": "db", "services": {"pg": {"image": "alpine:3.20", "node": "n1"}}})
            self.assertIn("tct-db-pg-1", self.engines["n1"].objects)
            self.vms["n1"] = "stopped"
            self.engines["n1"].objects.clear()                                                      # gone with it
            out = ct.reconcile("db")
            self.assertEqual([e["event"] for e in out], ["pending"])                                # never moved
            self.assertEqual(self.local.objects, {})
        for bad in ({"node": "n1", "spread": "nodes"},
                    {"spread": "all"}, {"ports": [80], "spread": "nodes"}, {"ports": [80], "networks": ["b"]},
                    {"node": "n1", "networks": ["b"], "ports": [80], "replicas": 2},
                    {"spread": "nodes", "networks": ["b"], "ports": ["80"]}):
            with self.assertRaises(SystemExit, msg=bad):
                ct.check_stack({"stack": "x", "services": {"a": bad}})

    def test_screen_copies_and_builds_in_a_node(self):
        """9.5: the node's terminal-ct does the screen (copied there once); files cross by push/pull."""
        self.cli("node", "add", "n1")
        n = ct.NodeVM("n1")
        with ct.on_node("n1"):
            self.assertEqual(ct.act("web", "screenshot", {}), (b"PNG!", (1280, 800)))
            self.assertEqual(ct.act("web", "click", {"x": 1, "y": 2}), {"ok": "click"})
        self.assertEqual(sum(1 for c in self.vm_calls if c[:1] == ["push"] and c[3] == ct.NODE_CT + ".new"), 1)  # once
        self.assertEqual(self.acts[0], ["web", "screenshot", "{}"])
        ct._DEPLOYED.clear()
        with ct.on_node("n1"):
            ct.act("web", "status", {})                                          # same file there: not copied again
        self.assertEqual(sum(1 for c in self.vm_calls if c[:1] == ["push"] and c[3] == ct.NODE_CT + ".new"), 1)
        src, out = os.path.join(ct.STORE, "in.tar"), os.path.join(ct.STORE, "out.tar")
        with open(src, "wb") as f:
            f.write(b"\x00binary\xff")
        n.pipe(["cp", "-", "tct-web:/data"], stdin=src)
        n.pipe(["cp", "tct-web:/data", "-"], stdout=out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"tar of /data")
        self.assertTrue(any(re.match(r"docker cp - tct-web:/data < /tmp/tct-\w+/in >/dev/null$", c) for c in self.shell))
        self.assertTrue(any(re.match(r"mkdir -m 700 /tmp/tct-\w+$", c) for c in self.shell))  # private to root
        self.assertTrue(self.shell[-1].startswith("rm -rf /tmp/tct-"))          # nothing left in the node
        ctx = os.path.join(ct.STORE, "ctx"); os.makedirs(ctx)
        with open(os.path.join(ctx, "Dockerfile"), "w") as f:
            f.write("FROM alpine\n")
        n.run(["build", "-t", "mine", ctx])                                     # the context folder sent as a tar
        self.assertRegex(self.shell[-2], r"tar -xf /tmp/tct-\w+/ctx\.tar -C /tmp/tct-\w+/ctx && docker build -t mine /tmp/tct-\w+/ctx$")
        self.assertTrue(any(re.match(r"mkdir -m 700 /tmp/tct-\w+ && mkdir /tmp/tct-\w+/ctx$", c) for c in self.shell))
        import tarfile
        pushed = [c for c in self.vm_calls if c[0] == "push"][-1][3]          # the context, pushed last
        with tarfile.open(fileobj=io.BytesIO(self.files[pushed])) as tar:
            self.assertTrue(tar.extractfile("./Dockerfile").read().startswith(b"FROM alpine"))

    def test_push_pull_with_a_node_container(self):
        self.cli("node", "add", "n1")
        self.cli("--node", "n1", "create", "web")
        src, back = os.path.join(ct.STORE, "notes.txt"), os.path.join(ct.STORE, "back.txt")
        with open(src, "wb") as f:
            f.write(b"hello")
        self.cli("push", "web", src, "/data/")                                   # routed to n1 by its placement
        pushed = [c for c in self.vm_calls if c[0] == "push"][-1]
        self.assertRegex(pushed[3], r"^/tmp/tct-\w+/f$")                 # inside the private folder, a fixed name
        self.assertTrue(any(re.match(r"mv /tmp/tct-\w+/f /tmp/tct-\w+/in/notes\.txt$", c) for c in self.shell))
        self.assertTrue(any(re.match(r"docker cp -a /tmp/tct-\w+/in/notes\.txt tct-web:/data/$", c) for c in self.shell))
        self.cli("pull", "web", "/data/notes.txt", back)
        with open(back, "rb") as f:
            self.assertEqual(f.read(), b"from the container")
        self.assertTrue(self.shell[-1].startswith("rm -rf /tmp/tct-"))           # nothing left in the node

    def test_untrusted_tar_refused_without_the_data_filter(self):
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, kind in (("out/ok.txt", tarfile.REGTYPE), ("out/../../evil", tarfile.REGTYPE)):
                ti = tarfile.TarInfo(name); ti.type = kind; tar.addfile(ti, io.BytesIO(b""))
        for has_filter in (True, False) if hasattr(tarfile, "data_filter") else (False,):  # 3.9: no filter
            buf.seek(0)
            with tarfile.open(fileobj=buf) as tar, tempfile.TemporaryDirectory() as d:
                with unittest.mock.patch.object(tarfile, "data_filter", tarfile.data_filter, create=True) if has_filter \
                        else unittest.mock.patch.object(ct, "tarfile", type("T", (), {"open": tarfile.open})):
                    with self.assertRaises((SystemExit, tarfile.TarError)):
                        ct._extract(tar, d)
                self.assertFalse(os.path.exists(os.path.join(d, "..", "evil")))

    def test_terminal_vm_loaded_in_process(self):
        """install.sh's launcher names the Python file: loaded, its guest_run called directly; its errors become
        RuntimeError as from its command line; reloaded when the file changes; never with a prefix (sudo...)."""
        self.inproc.stop()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        lib = os.path.join(d, "terminal_vm.py")

        def write(version):
            with open(lib, "w") as f:
                f.write(f"VERSION = {version}\ncalls = []\n"
                        "def guest_run(name, argv, timeout):\n"
                        "    calls.append((name, argv, timeout))\n"
                        "    if argv[0] == 'fail':\n"
                        "        raise SystemExit(name + ' is not running')\n"
                        "    if argv[0] == 'qmp':\n"
                        "        raise OSError('timed out')\n"
                        "    return {'exit': 0, 'stdout': 'ok', 'stderr': ''}\n")
        write(1)
        launcher = os.path.join(d, "terminal-vm")
        with open(launcher, "w") as f:
            f.write(f'#!/bin/sh\nexec python3 "{lib}" "$@"\n')
        try:
            with unittest.mock.patch.object(ct, "vm_cli", lambda: [launcher]), \
                    unittest.mock.patch.object(ct, "_VM_MODULE", {}):
                n = ct.NodeVM("n1")
                self.assertEqual(n.run(["ps", "-q"]).stdout, "ok")
                self.assertEqual(ct.vm_module().calls, [("n1", ["docker", "ps", "-q"], 600.0)])
                with self.assertRaisesRegex(RuntimeError, "n1 is not running"):
                    n._exec(["fail"], 5)
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    n._exec(["qmp"], 5)                                           # an OSError: no traceback
                write(2)
                os.utime(lib, (time.time() + 5, time.time() + 5))                 # reinstalled
                self.assertEqual(ct.vm_module().VERSION, 2)
                with open(lib, "w") as f:
                    f.write("VERSION = 0\n")                                    # an older one, without guest_run
                os.utime(lib, (time.time() + 10, time.time() + 10))
                self.assertIsNone(ct.vm_module())
            for cli in (["sudo", "python3", lib], ["python3", "-X", lib], ["wsl", "python3", lib]):
                with unittest.mock.patch.object(ct, "vm_cli", lambda: cli), unittest.mock.patch.object(ct, "_VM_MODULE", {}):
                    self.assertIsNone(ct.vm_module(), cli)                         # their command line knows what those mean
            self.assertEqual(self.vm_calls, [])                                  # no command line was run
        finally:
            self.inproc.start()

    def test_engine_errors_and_free_space(self):
        n = ct.NodeVM("n1")
        with self.assertRaisesRegex(SystemExit, "no such object"):
            n.run(["inspect", "--type", "container", "tct-x"])
        self.assertEqual(n.run(["inspect", "--type", "container", "tct-x"], check=False).returncode, 1)
        self.assertEqual(n.free_bytes("/var/lib/docker"), 10485760 * 1024)
        self.assertEqual(n.host(), (8, 16.0, ct.STORE))
        with open(os.path.join(ct.STORE, "quota.json"), "w") as f:
            json.dump({"cpus": 1, "memory_gb": 1}, f)                                 # the host's quota file
        with ct.on_node("n1"):
            limits, set_ = ct.quota_limits({})
        self.assertEqual((limits["cpus"], limits["memory_gb"], set_["cpus"]), (8, 16, False))  # the node's own size



def dns_query(name, qtype=1, qid=0x1234, flags=0x0100, qd=1, raw_name=None):
    q = raw_name if raw_name is not None else b"".join(bytes([len(l)]) + l.encode() for l in name.split(".")) + b"\0"
    return struct.pack("!HHHHHH", qid, flags, qd, 0, 0, 0) + q + struct.pack("!HH", qtype, 1)


class NodeDns(unittest.TestCase):
    """Phase 10.3: the nodes' DNS is authoritative only, strict, and silent on anything malformed."""
    TABLE = ct.dns_table(json.dumps({"web": ["10.77.1.2", "10.77.1.3"], "web.lab": ["10.77.1.2"], "db": []}))

    def reply(self, packet):
        r = ct.dns_answer(packet, self.TABLE)
        if r is None:
            return None
        qid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", r[:12])
        ips = [socket.inet_ntoa(r[-16 * an:][i * 16 + 12:i * 16 + 16]) for i in range(an)] if an else []
        return {"id": qid, "rcode": flags & 0xF, "aa": bool(flags & 0x400), "rd": bool(flags & 0x100), "ips": ips,
                "ttl": struct.unpack("!I", r[-16 * an + 6:-16 * an + 10])[0] if an else None}

    def test_answers_its_names_only(self):
        r = self.reply(dns_query("WEB"))                                            # names are case-insensitive
        self.assertEqual((r["id"], r["rcode"], r["aa"], r["rd"], r["ips"], r["ttl"]),
                         (0x1234, 0, True, True, ["10.77.1.2", "10.77.1.3"], 5))
        self.assertEqual(self.reply(dns_query("web.lab"))["ips"], ["10.77.1.2"])
        self.assertEqual((self.reply(dns_query("example.com"))["rcode"], self.reply(dns_query("example.com"))["ips"]),
                         (5, []))                                                    # REFUSED: never looked up
        self.assertEqual(self.reply(dns_query("web", qtype=28))["ips"], [])         # AAAA: no data, no error
        self.assertEqual(self.reply(dns_query("web", qtype=28))["rcode"], 0)
        self.assertEqual(self.reply(dns_query("db"))["ips"], [])                    # known, nowhere running now
        for odd in (b"\x04_tcp\x03web\0", b"\x03w_b\0", b"\x07web.lab\0", b"\x03w\xffb\0"):
            r = self.reply(dns_query("", raw_name=odd))                              # well formed, not ours
            self.assertEqual((r["rcode"], r["ips"]), (5, []), odd)                  # REFUSED, not silence
        many = ct.dns_table(json.dumps({"big": [f"10.77.1.{i}" for i in range(1, 40)]}))
        self.assertEqual(len(ct.dns_answer(dns_query("big"), many)) - len(dns_query("big")) - 0, 8 * 16)  # 8 at most

    def test_malformed_gets_no_answer(self):
        bad = [b"", b"\0" * 11, dns_query("web", flags=0x8100),                     # a response, not a query
               dns_query("web", flags=0x2900),                                       # not opcode QUERY (UPDATE)
               dns_query("web", qd=2), dns_query("web") + b"x" * 600,               # two questions; too long
               dns_query("", raw_name=b"\xc0\x0c"),                                # a compression pointer
               dns_query("", raw_name=b"\x40" + b"a" * 64 + b"\0"),               # a label over 63
               dns_query("", raw_name=b"\x03web"),                                  # cut short
               dns_query("", raw_name=b"\0")]                                        # the root name
        for packet in bad:
            self.assertIsNone(ct.dns_answer(packet, self.TABLE), packet[:40])

    def test_table_keeps_only_clean_entries(self):
        t = ct.dns_table(json.dumps({"ok": ["10.0.0.1", "nope", 7, "::1"], "Bad Name": ["10.0.0.2"], "x": "10.0.0.3",
                                     "a-": ["10.0.0.4"]}))
        self.assertEqual(t, {"ok": [socket.inet_aton("10.0.0.1")]})
        self.assertEqual(ct.dns_table("not json"), {})

    def test_server_one_address_rate_limited(self):
        for wild in ("0.0.0.0/24", "224.0.0.1/24", "255.255.255.255/32", "10.0.0.1/0"):
            with self.assertRaisesRegex(SystemExit, "never on all", msg=wild):
                ct.dns_serve(wild, "x")
        with self.assertRaises(ValueError):
            ct.dns_serve("example.com", "x")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]; probe.close()
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        table = os.path.join(d, "table.json")
        with open(table, "w") as f:
            json.dump({"web": ["10.77.1.2"]}, f)
        threading.Thread(target=ct.dns_serve, args=("127.0.0.1/8", table, port), daemon=True).start()
        time.sleep(0.3)
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.settimeout(1)
        self.addCleanup(c.close)
        c.sendto(dns_query("web"), ("127.0.0.1", port))
        self.assertEqual(ct.dns_answer(dns_query("web"), ct.dns_table('{"web": ["10.77.1.2"]}')), c.recv(600))
        with open(table, "w") as f:
            json.dump({"web": ["10.77.1.9"]}, f)                                    # rewritten: seen at once
        os.utime(table, (time.time() + 5, time.time() + 5))
        c.sendto(dns_query("web"), ("127.0.0.1", port))
        self.assertTrue(c.recv(600).endswith(socket.inet_aton("10.77.1.9")))
        for _ in range(300):                                                          # a flood from one source
            c.sendto(dns_query("web"), ("127.0.0.1", port))
        got = 0
        c.settimeout(0.5)
        with contextlib.suppress(socket.timeout):
            while True:
                c.recv(600); got += 1
        self.assertLess(got, 250)                                                     # the burst, not all of it


class SnapshotsAndClones(unittest.TestCase):
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()

    def test_snapshot_cycle(self):
        quiet(ct.create, "w")
        self.n.objects["tct-w"]["SizeRw"] = 1000
        out = ct.snapshot("w", "save", "t1")
        self.assertEqual((out["saved"], out["bytes"]), (True, 1000 + len(b"tar of /data")))
        self.assertIn(["commit", "tct-w", "tct-snap-w:t1"], self.n.calls)
        with self.assertRaisesRegex(SystemExit, "already has a snapshot"):
            ct.snapshot("w", "save", "t1")
        self.assertEqual(list(ct.snapshot("w", "ls")["snapshots"]), ["t1"])
        ct.snapshot("w", "load", "t1")
        self.assertIn("tct-snap-w:t1", self.created("w"))                              # remade from the snapshot
        restore = [c for c in self.n.calls if c[0] == "pipe" and "-i" in c][-1]
        self.assertIn("tct-w-data:/data", restore)                                     # /data back
        with self.assertRaisesRegex(SystemExit, "runs from snapshot"):
            ct.snapshot("w", "rm", "t1")
        for bad in ("../x", "A", ""):
            with self.assertRaises(SystemExit):
                ct.snapshot("w", "save", bad)
        quiet(ct.rm, "w")
        self.assertIn(["image", "rm", "tct-snap-w:t1"], self.n.calls)

    def created(self, name):
        return [c for c in self.n.calls if c[0] == "create" and c[c.index("--name") + 1] == "tct-" + name][-1]

    def test_failed_load_leaves_data_and_container(self):
        quiet(ct.create, "w"); ct.snapshot("w", "save", "t1")
        before = self.n.objects["tct-w"]
        real = self.n.run
        def run(args, **k):
            if args[0] == "create":
                raise SystemExit("engine down")
            return real(args, **k)
        self.n.run = run
        with self.assertRaisesRegex(SystemExit, "engine down"):
            ct.snapshot("w", "load", "t1")
        scripts = [c[-1] for c in self.n.calls if c[0] == "pipe"]
        self.assertTrue(scripts[-2].startswith("rm -rf /data/.tct-restore && mkdir"))     # unpacked aside...
        self.assertEqual(scripts[-1], "rm -rf /data/.tct-restore")                       # ...then dropped: no swap
        self.assertIs(self.n.objects["tct-w"], before)

    def test_snapshot_within_disk_quota(self):
        quiet(ct.create, "w")
        self.n.objects["tct-w"]["SizeRw"] = 5 * 2 ** 30
        with open(os.path.join(ct.STORE, "quota.json"), "w") as f:
            json.dump({"disk_gb": 6}, f)
        with self.assertRaisesRegex(SystemExit, "disk_gb"):
            ct.snapshot("w", "save", "big")
        self.assertEqual(ct.snapshots("w"), {})

    def test_sandbox_has_no_snapshots(self):
        quiet(ct.create, "s", sandbox=True)
        with self.assertRaisesRegex(SystemExit, "sandbox"):
            ct.snapshot("s", "save", "t")

    def test_clone_copies_files_and_data(self):
        quiet(ct.create, "src", project="lab")
        out = ct.clone("src", "copy")
        self.assertEqual((out["cloned_from"], out["project"]), ("src", "lab"))
        self.assertIn(["commit", "tct-src", "tct-keep-copy:latest"], self.n.calls)
        pipes = [c for c in self.n.calls if c[0] == "pipe"]
        self.assertTrue(any("tct-src-data:/data" in c for c in pipes) and any("tct-copy-data:/data" in c for c in pipes))
        with self.assertRaisesRegex(SystemExit, "already exists"):
            ct.clone("src", "copy")

    def test_agent_clone_of_users_container_asks_and_stays_offline(self):
        quiet(ct.create, "mine")
        asked = []
        out = ct.agent_call("ct_clone", {"name": "mine", "new": "c"}, lambda m, c: asked.append(c))
        self.assertEqual((asked, out["internet"]), (["terminal-ct clone mine c"], False))
        self.assertTrue(ct.agent_owned(ct.find("c")))
        with self.assertRaisesRegex(RuntimeError, "snapshot mine load"):
            ct.agent_call("ct_snapshot", {"name": "mine", "action": "load", "tag": "t"},
                          lambda m, c: (_ for _ in ()).throw(RuntimeError(c)))


class Descriptions(unittest.TestCase):
    """Phase 12: one line on each object, central, changeable; the agent's own free, the user's with approval."""
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()
        self.asked = []
        self.approve = AgentAuthority.approve.__get__(self)
        self.refuse = AgentAuthority.refuse.__get__(self)
        p = unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test")  # no background process
        p.start(); self.addCleanup(p.stop)

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            ct.main(list(argv))

    def test_cli_sets_shows_and_rm_forgets(self):
        self.cli("create", "web", "--description", "nginx for the docs site")
        self.assertEqual(ct.normalize(ct.find("web"))["description"], "nginx for the docs site")
        self.cli("net", "create", "lab", "--description", "test bench")
        self.assertEqual(ct.networks()["lab"]["description"], "test bench")
        self.cli("describe", "container", "web", "serves the docs")
        self.assertEqual(ct.dashboard_data()["rows"][0]["description"], "serves the docs")
        self.assertIn("serves the docs", ct.dashboard_text(ct.dashboard_data(), width=200))
        self.cli("describe", "project", "default", "the docs")
        self.assertEqual(ct.project_status()["description"], "the docs")
        for bad in ("two\nlines", "x" * 201):
            with self.assertRaisesRegex(SystemExit, "one line of at most 200"):
                ct.main(["create", "bad", "--description", bad])
        self.assertNotIn("tct-bad", self.n.objects)                       # refused before anything was made
        with self.assertRaisesRegex(SystemExit, "no network 'nope'"):
            ct.describe("net", "nope", "x")
        with self.assertRaisesRegex(SystemExit, "no node named 'n1'"):
            ct.describe("node", "n1", "x")
        quiet(ct.rm, "web"); ct.net_rm("lab")
        self.assertEqual(ct.descriptions(), {"project:default": "the docs"})
        quiet(ct.create, "web")                                           # a new one of that name says nothing
        self.assertNotIn("description", ct.normalize(ct.find("web")))

    def test_review_fixes(self):
        for bad in ("descriptions.json", "stacks", "projects", "daemon.beat", "x.log"):  # STORE/NAME is its folder
            with self.assertRaisesRegex(SystemExit, "reserved"):
                ct.check_name(bad)
        self.assertEqual(ct.check_name("web"), "web")                                 # a common name stays free
        old = os.path.join(ct.STORE, "web"); os.makedirs(old)
        with open(os.path.join(old, "actions.log"), "w") as f:
            f.write("{}\n")
        open(os.path.join(old, "run"), "w").close()                                   # a container web's own
        self.assertTrue(os.path.isfile(os.path.join(ct._web_dir(), "actions.log")))   # the audit log moved out
        self.assertEqual(os.listdir(old), ["run"])
        ct.apply({"stack": "s", "project": "alpha", "services": {"api": {"image": "debian:stable-slim",
                                                                         "networks": ["back"], "node": "n9"}}},
                 created_by="agent")
        with self.assertRaisesRegex(SystemExit, "no network 'back'"):                 # another project's: unseen,
            ct.agent_call("describe", {"kind": "net", "name": "back", "description": "x"}, self.approve,
                          project="beta")                                             # and nothing asked
        self.assertEqual(self.asked, [])
        ct.agent_call("describe", {"kind": "net", "name": "back", "description": "api's"}, self.refuse,
                      project="alpha")                                                # its own stack's: free
        st = {"stack": "u", "services": {"db": {"image": "debian:stable-slim"}}}
        ct.apply(st); ct.reconcile("u")
        with self.assertRaisesRegex(SystemExit, "description on its service"):
            ct.describe("container", "u-db-1", "x")
        with self.assertRaisesRegex(SystemExit, "no node named 'n1'"):               # checked before asking
            ct.agent_call("describe", {"kind": "node", "name": "n1", "description": "x"}, self.refuse)
        ct.stack_rm("s")
        self.assertNotIn("net:back", ct.descriptions())                               # gone with the only stack
        quiet(ct.create, "big", project="beta")
        with unittest.mock.patch.object(ct, "vm_state", lambda project=None: {"vms": []}):
            q = ct.infra("alpha")["pcs_and_nodes"][0]["quota"]
        self.assertEqual(set(q["cpus"]), {"limit", "free", "used"})                   # alpha's use, not beta's

    def test_stack_service_description_recreates_nothing(self):
        st = {"stack": "s", "services": {"api": {"image": "debian:stable-slim", "description": "the API"}}}
        ct.apply(st); ct.reconcile("s")
        before = ct.find("s-api-1")["Id"]
        self.assertEqual(ct.normalize(ct.find("s-api-1"))["description"], "the API")
        st["services"]["api"]["description"] = "the public API"
        ct.apply(st); ct.reconcile("s")
        self.assertEqual(ct.find("s-api-1")["Id"], before)
        self.assertEqual(ct.stack_show("s")["services"]["api"]["description"], "the public API")
        with self.assertRaisesRegex(SystemExit, "one line"):
            ct.check_stack({"stack": "s", "services": {"api": {"description": 5}}})

    def test_agent_free_on_its_own_asks_for_the_users(self):
        ct.agent_call("ct_create", {"name": "job", "description": "a build"}, self.refuse)
        ct.agent_call("net_create", {"net": "mine", "description": "my bench"}, self.refuse)
        ct.agent_call("describe", {"kind": "container", "name": "job", "description": "the build"}, self.refuse)
        ct.agent_call("describe", {"kind": "net", "name": "mine", "description": ""}, self.refuse)
        self.assertEqual(self.asked, [])
        self.assertEqual(ct.descriptions(), {"container:job": "the build"})
        quiet(ct.create, "theirs"); ct.net_create("ours")
        with self.assertRaisesRegex(RuntimeError, "terminal-ct describe container theirs 'the db'"):
            ct.agent_call("describe", {"kind": "container", "name": "theirs", "description": "the db"}, self.refuse)
        for kind, name in (("container", "theirs"), ("net", "ours"), ("project", "default")):
            ct.agent_call("describe", {"kind": kind, "name": name, "description": "x"}, self.approve)
        self.assertEqual(len(self.asked), 3)
        ct.agent_call("describe", {"kind": "project", "name": "lab/web", "description": "mine"}, self.refuse,
                      project="lab")                                      # its server's project: its own
        quiet(ct.create, "other", project="beta")
        with self.assertRaisesRegex(SystemExit, "no container named 'other'"):
            ct.agent_call("describe", {"kind": "container", "name": "other", "description": "x"}, self.approve,
                          project="lab")
        with self.assertRaisesRegex(SystemExit, "not this agent's project"):
            ct.agent_call("describe", {"kind": "project", "name": "beta", "description": "x"}, self.approve,
                          project="lab")


class Doctor(unittest.TestCase):
    """Phase 13.1: known problems with their fix, a masked report."""
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()

    def test_mask_hides_secrets_home_and_names(self):
        home = os.path.expanduser("~")
        text = (f"token=abc123SECRET path {home}/x X-Token: zz9 password: hunter2 "
                f"{'q' * 43} Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41")
        out = ct.mask(text, ["q" * 43])
        for gone in ("abc123SECRET", home, "zz9", "hunter2", "q" * 43):
            self.assertNotIn(gone, out)
        self.assertIn("~/x", out)
        self.assertIn("microsoft-standard-WSL2", out)                     # not a secret: kept

    def test_doctor_json_and_report(self):
        with open(os.path.join(ct.STORE, "web.token"), "w") as f:
            f.write("s" * 43)
        with open(os.path.join(ct.STORE, "events.log"), "w") as f:
            f.write(json.dumps({"event": "x", "detail": "s" * 43}) + "\n")
        with unittest.mock.patch.object(ct.shutil, "which", lambda e: "/usr/bin/" + e if e == "podman" else None):
            checks = ct.doctor_checks()
            path = ct.doctor_report()
        self.assertTrue(all({"check", "ok", "detail", "required"} <= set(c) for c in checks))
        self.assertTrue(next(c for c in checks if c["check"] == "engine")["ok"])
        self.assertFalse([c for c in checks if c["ok"] and "fix" in c])   # a fix only where something is wrong
        with open(path, encoding="utf-8") as f:
            report = f.read()
        self.assertNotIn("s" * 43, report)
        self.assertIn("== events.log", report)


class Installer(unittest.TestCase):
    """Phase 13.2: install.sh into a throwaway HOME (no packages, no agent), update, uninstall."""
    SH = os.path.join(os.path.dirname(os.path.abspath(ct.__file__)), "install.sh")
    COMMON = "# ===== common to terminal-ct and terminal-vm's install.sh ====="

    @unittest.skipIf(ct.WINDOWS or not shutil.which("sh"), "a POSIX shell")
    def test_install_update_uninstall(self):
        home = tempfile.mkdtemp()
        with open(os.path.join(home, ".bashrc"), "w") as f:
            f.write("echo mine\n")
        env = dict(os.environ, HOME=home, PATH="/usr/bin:/bin")
        run = lambda *a: subprocess.run(["sh", self.SH, *a, "--no-deps", "--no-mcp"], env=env, capture_output=True,
                                        text=True, stdin=subprocess.DEVNULL, timeout=120)
        r = run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tool = os.path.join(home, ".local", "bin", "terminal-ct")
        self.assertEqual(subprocess.run([tool, "version"], capture_output=True, text=True).stdout.strip(), ct.VERSION)
        self.assertEqual(run("--update").returncode, 0)
        with open(os.path.join(home, ".bashrc")) as f:
            self.assertEqual(f.read().count("added by terminal-ct"), 1)        # once, update or not
        self.assertEqual(run("--uninstall").returncode, 0)
        self.assertFalse(os.path.exists(tool))
        with open(os.path.join(home, ".bashrc")) as f:
            self.assertNotIn("terminal-ct", f.read())

    def test_common_part_is_terminal_vms(self):
        other = os.path.join(os.path.dirname(self.SH), "..", "terminal-mac-src", "terminal-vm", "install.sh")
        if not os.path.isfile(other):
            self.skipTest("terminal-vm not checked out alongside")
        body = lambda p: open(p, encoding="utf-8").read().replace("\r\n", "\n").split(self.COMMON)[1]
        self.assertEqual(body(self.SH), body(other), "the installers' common part differs: copy it to both")


class ProjectsAndResume(unittest.TestCase):
    def setUp(self):
        self.n = ct.NODE = FakeNode()
        ct.STORE = tempfile.mkdtemp()
        self.refuse = AgentAuthority.refuse.__get__(self)

    def test_project_scope_is_closed(self):
        quiet(ct.create, "mine", project="alpha")
        ct.agent_call("ct_create", {"name": "job"}, self.refuse, project="beta")
        self.assertEqual(ct.find("job")["Config"]["Labels"]["terminal-ct.project"], "beta")
        listed = [c["name"] for c in ct.agent_call("ct_list", {}, self.refuse, project="beta")]
        self.assertEqual(listed, ["job"])                                           # alpha is invisible
        approve = lambda m, c: None                                                 # even approving would not help
        for tool, a in (("ct_start", {"name": "mine"}), ("ct_resources", {"name": "mine"}),
                        ("ct_internet", {"name": "mine"})):
            with self.assertRaisesRegex(SystemExit, "no container named 'mine'"):
                ct.agent_call(tool, a, approve, project="beta")
        ct.net_create("alab", project="alpha")
        with self.assertRaisesRegex(SystemExit, "no network 'alab'"):
            ct.agent_call("net_attach", {"name": "job", "net": "alab"}, approve, project="beta")
        self.assertEqual(ct.agent_call("net_list", {}, approve, project="beta"), {})
        self.assertEqual({p["project"]: p["containers"] for p in ct.projects()}, {"alpha": 1, "beta": 1, "default": 0})

    def test_status_notes_and_log_let_the_next_session_resume(self):
        ct.agent_call("ct_create", {"name": "web"}, self.refuse, project="site")
        ct.agent_call("notes_write", {"text": "web: nginx to install"}, self.refuse, project="site")
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ct_start", "arguments": {"name": "web"}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "project_status", "arguments": {}}}]
        stdin, stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), io.StringIO()
        with unittest.mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(stdout):
            ct.mcp(project="site")
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertIn("Call infra first", replies[0]["result"]["instructions"])
        self.assertIn("Then project_status", replies[0]["result"]["instructions"])
        st = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual([(c["name"], c["status"], c["want"]) for c in st["containers"]], [("web", "running", "running")])
        self.assertIn("nginx to install", st["notes"])
        self.assertEqual([a["tool"] for a in st["recent_actions"]], ["ct_start"])

    def test_resume_starts_what_was_running_only(self):
        for n in ("up", "down", "fresh"):
            quiet(ct.create, n)
        quiet(ct.start, "up"); quiet(ct.start, "down"); quiet(ct.stop, "down")
        self.n.objects["tct-up"]["State"]["Status"] = "exited"                     # the PC went off
        out = quiet(ct.resume)
        self.assertEqual([r["name"] for r in out["resumed"]], ["up"])
        self.assertEqual({n: self.n.objects["tct-" + n]["State"]["Status"] for n in ("up", "down", "fresh")},
                         {"up": "running", "down": "exited", "fresh": "created"})

    def test_project_move_keeps_file_system(self):
        quiet(ct.create, "w")
        self.assertEqual(ct.project_move("w", "other"), {"name": "w", "project": "other"})
        self.assertIn(["commit", "tct-w", "tct-keep-w:latest"], self.n.calls)
        self.assertEqual(ct.normalize(ct.find("w"))["project"], "other")

    def test_bad_project_argument_neither_kills_server_nor_forges_logs(self):
        ct.project_dir("victim")
        quiet(ct.create, "x", project="other")
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "notes_write", "arguments": {"text": "t", "project": "../x"}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "top", "arguments": {"project": 5}}},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "ct_stop", "arguments": {"name": "x", "project": "victim"}}},
                 {"jsonrpc": "2.0", "id": 5, "method": "ping"}]
        stdin, stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), io.StringIO()
        with unittest.mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(stdout):
            ct.mcp()
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])                 # still serving
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertFalse(os.path.exists(os.path.join(ct.STORE, "projects", "victim", "agent.log")))
        with open(os.path.join(ct.STORE, "projects", "other", "agent.log")) as f:  # the container's own project
            self.assertIn("ct_stop", f.read())

    def test_interrupted_remake_is_recovered(self):
        quiet(ct.create, "w")
        self.n.run(["rename", "tct-w", "tct-w-OLD"])                                   # power cut mid-remake
        self.assertEqual(ct.normalize(ct.find("w"))["name"], "w")
        self.assertIn("tct-w", self.n.objects)

    def test_sandbox_move_is_not_frozen(self):
        quiet(ct.create, "s", sandbox=True)
        ct.project_move("s", "p")
        self.assertFalse(any(c[0] == "commit" for c in self.n.calls))

    def test_systemd_unit(self):
        calls = []
        def run(args, **k):
            calls.append(args); return subprocess.CompletedProcess(args, 0, "", "")
        home = tempfile.mkdtemp()
        with unittest.mock.patch.object(ct.subprocess, "run", run), unittest.mock.patch.object(ct, "in_wsl", lambda: False), \
                unittest.mock.patch.object(ct, "WINDOWS", False), unittest.mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "TERMINAL_CT_HOME": "/s t"}):
            out = ct.autostart(True)
        with open(out["unit"]) as f:
            unit = f.read()
        self.assertNotIn("After=", unit)                                               # ordering cycle with WantedBy
        self.assertIn("Environment='TERMINAL_CT_HOME=/s t'", unit)
        self.assertEqual(calls[-1], ["systemctl", "--user", "enable", "terminal-ct-resume.service"])

    def test_sub_projects(self):
        approve = lambda m, c: None
        ct.agent_call("ct_create", {"name": "web", "project": "lab/web"}, approve, project="lab")
        quiet(ct.create, "top", project="lab"); quiet(ct.create, "near", project="labx")  # labx is not under lab
        listed = sorted(c["name"] for c in ct.agent_call("ct_list", {}, approve, project="lab"))
        self.assertEqual(listed, ["top", "web"])
        with self.assertRaisesRegex(SystemExit, "not this agent's project"):
            ct.agent_call("ct_create", {"name": "x", "project": "other"}, approve, project="lab")
        with self.assertRaisesRegex(SystemExit, "no container named 'web'"):
            ct.agent_call("ct_start", {"name": "web"}, approve, project="lab/other")
        st = ct.project_status("lab")
        self.assertEqual((sorted(c["name"] for c in st["containers"]), st["sub_projects"]), (["top", "web"], ["lab/web"]))
        ct.notes_write("lab/web", "n")
        self.assertTrue(os.path.isfile(os.path.join(ct.STORE, "projects", "lab+web", "notes.md")))
        for bad in ("Lab", "lab/", "../x", "a/b/c/d/e"):
            with self.assertRaises(SystemExit):
                ct.check_project(bad)

    def test_small_leaks_closed(self):
        approve = lambda m, c: None
        quiet(ct.create, "theirs", project="other", cpus=2); quiet(ct.start, "theirs")
        for tool, a in (("ct_create", {"name": "theirs"}), ("net_create", {"net": "n"})):
            if tool == "net_create":
                ct.net_create("n", project="other")
            with self.assertRaisesRegex(SystemExit, "is not available: choose another"):   # no project, no detail
                ct.agent_call(tool, a, approve, project="mine")
        q = ct.agent_call("quota", {}, approve, project="mine")
        self.assertEqual(q["cpus"]["used_by_project"], 0)
        self.assertNotIn("used", q["cpus"])
        self.assertEqual(q["cpus"]["free"], q["cpus"]["limit"] - 2)                       # the room left, all told

    def test_kept_image_counts_its_commit_layer_only(self):
        quiet(ct.create, "w")
        self.n.objects["tct-w"]["SizeRw"] = 5 * 2 ** 20
        ct.project_move("w", "p")
        self.assertEqual(ct.keep_bytes("w"), 5 * 2 ** 20)
        self.n.objects["tct-w"]["SizeRw"] = 0
        self.assertAlmostEqual(ct.disk_bytes(ct.raw_containers()), 2e9 + 1e9 + 5 * 2 ** 20)

    def test_autostart_tool_needs_approval(self):
        with self.assertRaisesRegex(RuntimeError, "terminal-ct autostart on"):
            ct.agent_call("autostart", {"on": True}, self.refuse)

    def test_autostart_commands(self):
        calls = []
        def run(args, **k):
            calls.append(args); return subprocess.CompletedProcess(args, 0, "", "")
        with unittest.mock.patch.object(ct.subprocess, "run", run), unittest.mock.patch.object(ct, "in_wsl", lambda: True), \
                unittest.mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Debian"}):
            out = ct.autostart(True)
        self.assertEqual(calls[0][:5], ["schtasks.exe", "/Create", "/TN", "terminal-ct-resume", "/SC"])
        self.assertTrue(out["runs"].startswith("conhost.exe --headless wsl.exe -d Debian --exec "))
        self.assertTrue(out["runs"].endswith(" resume --stay"))


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
                self.assertEqual(args[-4], "tct-desktop:alpine-3.20")
                self.assertEqual(args[-3:-1], ["sh", "-c"]); self.assertIn("xterm -maximized", args[-1])
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


class Hosts(unittest.TestCase):
    """Phase 11.2: other PCs as nodes (host add|ls|rm, --host); their calls go through NodeRemote._exec."""
    FP = "ab" * 32

    def setUp(self):
        self.local = ct.NODE = FakeNode("podman")
        ct.STORE = tempfile.mkdtemp()
        self.engines, self.down, self.uname, self.execs = {}, set(), {}, []
        self.pc_vms, self.pc_files, self.vm_runs, self.podman, self.podman_info = {}, {}, [], set(), {}

        def _exec(node, argv, timeout):
            self.execs.append((node.name, argv))
            if node.name in self.down:
                raise RuntimeError("agent https://pc: connection refused")
            if argv == ["uname", "-s"]:
                return {"exit": 0, "stdout": self.uname.get(node.name, "Linux") + "\n", "stderr": ""}
            if argv == ["true"]:
                return {"exit": 0, "stdout": "", "stderr": ""}
            if argv[:2] == ["sh", "-lc"] and argv[3] == "terminal-vm" and ct.VM_FIND in argv[2]:  # terminal-vm there
                args = argv[4:]
                self.vm_runs.append((node.name, args))
                vms = self.pc_vms.get(node.name)
                if vms is None:
                    return {"exit": 127, "stdout": "", "stderr": "sh: terminal-vm: not found"}
                if args == ["ls"]:
                    if vms == "garbled":
                        return {"exit": 0, "stdout": json.dumps({"error": "half an answer"})}
                    return {"exit": 0, "stdout": json.dumps([{"name": n, "status": s} for n, s in vms.items()])}
                if args == ["top"]:
                    return {"exit": 0, "stdout": json.dumps({"vms": [{"name": n, "cpu_percent": 3.0,
                                                                      "host_memory_gb": 1.5} for n in vms]})}
                if args[:2] == ["events", "-n"]:
                    return {"exit": 0, "stdout": json.dumps({"event": "started", "vm": "deb"}) + "\n"}
                if args[0] in ("start", "stop"):
                    if args[1] not in vms:
                        return {"exit": 1, "stdout": "", "stderr": json.dumps({"error": f"no VM named {args[1]}"})}
                    vms[args[1]] = "running" if args[0] == "start" else "stopped"
                    return {"exit": 0, "stdout": json.dumps({args[0]: args[1]})}
                if args[:3] == ["action", "deb", "screenshot"]:
                    self.pc_files[args[-1]] = b"PNG of deb"
                    return {"exit": 0, "stdout": "{}"}
                if args[:2] == ["action", "win"]:
                    return {"exit": 1, "stdout": "", "stderr": json.dumps({"error": "win is not running"})}
                if args[:2] == ["exec", "deb"] and "missing" in args:              # the guest's own 127
                    return {"exit": 127, "stdout": "", "stderr": "sh: missing: not found"}
                return {"exit": 0, "stdout": "plain text\n", "stderr": ""}
            if argv[0] == "df":
                if argv[-1] != "/" and node.name in getattr(self, "mac", ()):  # Docker Desktop: store in its VM
                    return {"exit": 1, "stdout": "Filesystem 1K-blocks Used Available\n", "stderr": "no such file"}
                return {"exit": 0, "stdout": "Filesystem 1K-blocks Used Available\n/dev/sda1 9 1 10485760 1% /\n"}
            if argv[0] == "podman" and node.name not in self.podman:  # no Podman on most fake PCs
                return {"exit": 127, "stdout": "", "stderr": "sh: podman: not found"}
            if argv[0] in ("docker", "podman"):
                if argv[1] == "info" and "Version" in argv[-1]:  # host add's probe
                    if node.name in getattr(self, "hung_podman", ()) and argv[0] == "podman":
                        raise RuntimeError("agent https://pc: podman timed out (30 s)")
                    return {"exit": 0, "stdout": "27.0\n" if argv[0] == "docker" else
                            self.podman_info.get(node.name, "5.4.2 true\n"), "stderr": ""}
                r = self.engines.setdefault(node.name, FakeNode(argv[0])).run(argv[1:], check=False)
                return {"exit": r.returncode, "stdout": r.stdout, "stderr": "no such object" if r.returncode else ""}
            return {"exit": 0, "stdout": "", "stderr": ""}
        alive = lambda name: name not in self.down

        def copy(node, direction, src, dest, timeout=3600):
            with open(dest, "wb") as f:
                f.write(self.pc_files.pop(src))
        self.patches = [unittest.mock.patch.object(ct.NodeRemote, "_exec", _exec),
                        unittest.mock.patch.object(ct.NodeRemote, "copy", copy),
                        unittest.mock.patch.object(ct, "host_alive", alive),
                        unittest.mock.patch.object(ct, "UP_MAX_AGE", -1),
                        unittest.mock.patch.object(ct, "daemon", lambda cmd="status": "test"),  # no background process
                        unittest.mock.patch.object(ct, "vm_call", lambda args, timeout=60, host=None, **k:
                                                   ct.host_vm_call(host, args, timeout, **k) if host else [])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        ct.NODE = None

    def cli(self, *argv):
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                ct.main(list(argv))
        finally:
            ct.NODE = self.local  # main sets it for its own command, refused or not
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def add(self, name="pc1", url="https://192.168.1.20:7173"):
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_CT_AGENT_TOKEN": "t" * 43}):
            return self.cli("host", "add", name, url, "--fingerprint", self.FP.upper())

    def test_add_checks_everything_first(self):
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_CT_AGENT_TOKEN": "t" * 43}):
            for argv, msg in ((["Bad", "https://1.2.3.4:7173"], "invalid"),
                              (["pc1", "http://1.2.3.4"], "host address"),
                              (["pc1", "https://u@1.2.3.4:7173"], "host address"),
                              (["pc1", "https://1.2.3.4:x"], "host address"),
                              (["pc1", "https://1.2.3.4/p"], "host address"),
                              (["pc1", "https://:pw@1.2.3.4:7173"], "host address"),
                              (["pc-", "https://1.2.3.4:7173"], "invalid"),
                              (["pc.1", "https://1.2.3.4:7173"], "invalid"),
                              (["pc1", "ssh://-oProxyCommand=x@pc"], "host address"),
                              (["pc1", "https://1.2.3.4:7173", "--fingerprint", "12"], "fingerprint")):
                with self.assertRaisesRegex(SystemExit, msg):
                    self.cli("host", "add", *argv)
        for bad in ("short", "t" * 40 + "\r\nX: y"):  # a header injection never reaches http.client
            with unittest.mock.patch.dict(os.environ, {"TERMINAL_CT_AGENT_TOKEN": bad}), \
                    self.assertRaisesRegex(SystemExit, "token"):
                self.cli("host", "add", "pc1", "https://1.2.3.4:7173", "--fingerprint", self.FP)
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_CT_AGENT_TOKEN": "t" * 43}):
            self.cli("host", "add", "v6", "https://[fd00::5]:7173", "--fingerprint", self.FP)
            self.assertNotIn("TERMINAL_CT_AGENT_TOKEN", os.environ)  # not handed down to child processes
        self.assertEqual(ct._hosts()["v6"]["url"], "https://[fd00::5]:7173")
        self.cli("host", "rm", "v6")
        self.uname["win"] = "MINGW64_NT"
        with self.assertRaisesRegex(SystemExit, "Linux or macOS only"):
            self.add("win")
        self.down.add("off")
        with self.assertRaisesRegex(SystemExit, "connection refused"):
            self.add("off")
        self.assertIn(("win", ["uname", "-s"]), self.execs)
        self.assertEqual(ct._nodes()["nodes"], {})  # nothing registered by a failed add
        with self.assertRaisesRegex(SystemExit, "host add NAME"):
            self.cli("host", "add", "pc1")

    def test_add_keeps_the_token_private(self):
        r = self.add()
        self.assertEqual((r["host"], r["os"], r["engine"], r["version"]), ("pc1", "Linux", "docker", "27.0"))
        h = ct._hosts()["pc1"]
        self.assertEqual((h["fingerprint"], h["token"]), (self.FP, "t" * 43))  # lower-case, as compared
        with open(os.path.join(ct.STORE, "nodes.json")) as f:
            self.assertNotIn("t" * 43, f.read())                       # the shared registry never holds it
        if not ct.WINDOWS:
            self.assertEqual(os.stat(os.path.join(ct.STORE, "hosts.json")).st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.add()
        self.assertEqual(self.cli("host", "add", "pc2", "ssh://me@pc2")["host"], "pc2")  # nothing to type
        self.assertEqual([(n["node"], n["status"]) for n in self.cli("host", "ls")],
                         [("pc1", "running"), ("pc2", "running")])
        with unittest.mock.patch.object(ct.sys, "stdin", io.StringIO("p" * 43 + "\n")), \
                unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TERMINAL_CT_AGENT_TOKEN", None)
            self.cli("host", "add", "pc3", "https://10.0.0.3:7173", "--fingerprint", self.FP)  # piped in
        self.assertEqual(ct._hosts()["pc3"]["token"], "p" * 43)

    def test_containers_on_another_pc(self):
        self.add()
        self.cli("--host", "pc1", "create", "web")
        self.assertIn("tct-web", self.engines["pc1"].objects)                 # made there, not here
        self.assertNotIn("tct-web", self.local.objects)
        self.assertEqual(ct._nodes()["placement"], {"web": "pc1"})
        self.cli("start", "web")                                               # routed by its placement
        self.assertEqual(self.engines["pc1"].objects["tct-web"]["State"]["Status"], "running")
        rows = {c["name"]: c.get("node") for c in self.cli("ls")}
        self.assertEqual(rows, {"web": "pc1"})
        for argv in (["--host", "pc1", "create", "d", "--desktop"], ["--host", "pc1", "create", "n", "--net", "lab"]):
            with self.assertRaisesRegex(SystemExit, "phase 11 bis"):
                self.cli(*argv)
        with self.assertRaisesRegex(SystemExit, "phase 11 bis"):
            self.cli("node", "join", "pc1", "lab")
        ct.NODE = None
        with ct.on_node("pc1"), self.assertRaisesRegex(RuntimeError, "another PC"):  # no terminal-ct copied there
            ct.act("web", "screenshot", {})
        ct.NODE = self.local
        self.assertFalse([a for _, a in self.execs if "_act" in a or "mkdir" in " ".join(a)])
        with self.assertRaisesRegex(SystemExit, "phase 11 bis"):
            ct.apply({"stack": "d", "services": {"s": {"image": "alpine:3.20", "node": "pc1",
                                                                             "desktop": True}}})
        self.down.add("pc1")                                                   # that PC gone: left out of ls
        self.assertEqual(self.cli("ls"), [])
        self.assertEqual(self.cli("host", "ls")[0]["status"], "unreachable")
        self.down.discard("pc1")
        with self.assertRaisesRegex(SystemExit, "still has containers"):
            self.cli("host", "rm", "pc1")
        r = self.cli("host", "rm", "pc1", "--force")
        self.assertEqual(r, {"removed": "pc1", "containers": ["web"]})
        self.assertNotIn("tct-web", self.engines["pc1"].objects)
        self.assertEqual((ct._hosts(), ct._nodes()["nodes"]), ({}, {}))
        with self.assertRaisesRegex(SystemExit, "no host named"):
            self.cli("host", "rm", "pc1")

    def test_infra_maps_every_pc_and_what_runs_there(self):
        self.add("pc1"); self.add("pc2", "https://192.168.1.21:7173")
        ct.describe("host", "pc1", "the lab PC")
        quiet(ct.create, "mine", nets=())                                       # the user's, on this PC
        ct.net_create("lab", project="alpha"); ct.describe("net", "lab", "alpha's bench")
        quiet(ct.create, "db", project="alpha", nets=["lab"]); quiet(ct.create, "api", project="alpha", nets=["lab"])
        with ct.on_node("pc1"):
            quiet(ct.create, "job", created_by="agent", sandbox=True, project="alpha")
        ct.place("job", "pc1")
        ct.apply({"stack": "s", "project": "alpha", "services": {"w": {"image": "alpine:3.20"}}},
                 created_by="agent", approved_hosts=["pc2"])
        self.down.add("pc2")
        ct._UP.clear()
        vms = {"vms": [{"name": "deb", "status": "stopped", "project": "alpha", "description": "a desktop"}]}
        with unittest.mock.patch.object(ct, "vm_state", lambda project=None: vms):
            m = ct.infra()
            text = ct.infra_text(m)
            scoped = ct.infra("alpha")
        places = {p["name"]: p for p in m["pcs_and_nodes"]}
        self.assertEqual({k: p["reachable"] for k, p in places.items()}, {"this PC": True, "pc1": True, "pc2": False})
        self.assertEqual((places["pc1"]["description"], places["pc2"]["approved_for"]), ("the lab PC", ["s"]))
        self.assertIn("free", places["pc1"]["quota"]["cpus"])                  # each PC's own quota, with room left
        rows = {r["name"]: r for r in m["containers"]}
        self.assertEqual(rows["job"]["on"], "pc1")
        self.assertEqual((rows["job"]["managed"], rows["mine"]["managed"]), (True, False))
        self.assertEqual(m["networks"]["lab"]["description"], "alpha's bench")
        self.assertEqual(sorted(m["networks"]["lab"]["names"]), ["api", "db"])
        self.assertEqual(m["vms"][0]["description"], "a desktop")
        for s in ("pc2", "DOWN", "the lab PC", "net lab: ", "a desktop"):
            self.assertIn(s, text)
        self.assertEqual(sorted(r["name"] for r in scoped["containers"]), ["api", "db", "job", "s-w-1"])  # alpha's only
        self.assertEqual([p["project"] for p in scoped["projects"]], ["alpha"])
        self.assertEqual(len(scoped["pcs_and_nodes"]), 3)                       # where it may go stays listed

    def test_descriptions_of_pcs_and_their_containers(self):
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_CT_AGENT_TOKEN": "t" * 43}):
            self.cli("host", "add", "pc1", "https://192.168.1.20:7173", "--fingerprint", self.FP,
                     "--description", "the lab PC upstairs")
        self.cli("--host", "pc1", "create", "web", "--description", "built on pc1")
        self.assertEqual(self.cli("host", "ls")[0]["description"], "the lab PC upstairs")
        self.assertEqual({c["name"]: c.get("description") for c in self.cli("ls")}, {"web": "built on pc1"})
        self.cli("describe", "container", "web", "routed to pc1")               # by its placement
        self.assertEqual(ct.description("container", "web"), "routed to pc1")
        with self.assertRaisesRegex(SystemExit, "no node named 'pc1'"):
            ct.describe("node", "pc1", "x")                                     # a PC, not a node
        self.cli("host", "rm", "pc1", "--force")
        self.assertEqual(ct.descriptions(), {})

    def test_spread_counts_other_pcs(self):
        self.add("pc1"); self.add("pc2", "https://192.168.1.21:7173")
        ct.apply({"stack": "lab", "services": {"web": {"image": "alpine:3.20", "replicas": 2, "spread": "nodes"}}})
        ct.reconcile("lab")
        self.assertEqual(sorted(ct._nodes()["placement"].values()), ["pc1", "pc2"])  # one on each PC
        self.down.add("pc2")
        for _ in range(2):  # a missed ping or two (Wi-Fi, sleep): nothing moves, /data stays where it is
            ct.reconcile("lab")
            self.assertEqual(sorted(ct._nodes()["placement"].values()), ["pc1", "pc2"])
        ct.reconcile("lab")
        self.assertEqual(sorted(ct._nodes()["placement"].values()), ["pc1", "pc1"])  # third: moved to the PC still up
        ct.apply({"stack": "desk", "services": {"d": {"image": "alpine:3.20", "spread": "nodes", "desktop": True}}})
        self.assertNotIn("desk-d-1", ct._nodes()["placement"])  # never sent to another PC

    def test_one_silent_pc_counts_once_per_daemon_pass(self):
        """Review: each stack of a pass saw the same silent PC and added one miss: 3 stacks failed over at once."""
        self.add("pc1"); self.add("pc2", "https://192.168.1.21:7173")
        names = ["a", "b", "c"]
        for n in names:
            ct.apply({"stack": n, "services": {"w": {"image": "alpine:3.20", "replicas": 2, "spread": "nodes"}}})
        on_pc2 = lambda: sorted(k for k, v in ct._nodes()["placement"].items() if v == "pc2")
        before = on_pc2()
        self.assertEqual(len(before), 3)
        self.down.add("pc2")
        try:
            for p in (1, 2):  # two daemon passes over the three stacks: a hiccup, nothing moves
                ct.PASS[0] = 1000.0 + p
                for n in names:
                    ct.reconcile(n)
                self.assertEqual(on_pc2(), before, f"pass {p}")
            ct.PASS[0] = 1003.0
            for n in names:
                ct.reconcile(n)
            self.assertEqual(on_pc2(), [])                                       # the third pass: all moved
        finally:
            ct.PASS[0] = None

    def test_stacks_refuse_node_option(self):
        self.add("pc1")
        for argv in (["--host", "pc1", "apply", "x.json"], ["--node", "pc1", "scale", "s", "w", "2"],
                     ["--host", "pc1", "stack", "ls"]):
            with self.assertRaisesRegex(SystemExit, "does not apply to"):
                ct.main(argv)

    def test_engine_of_another_pc(self):
        self.podman.add("pod")
        self.assertEqual(self.add("pod", "https://10.0.0.7:7173")["engine"], "podman")   # Podman first, as here
        self.cli("--host", "pod", "create", "web")
        self.assertEqual(self.engines["pod"].engine, "podman")
        self.assertFalse([a for n, a in self.execs if n == "pod" and a[0] == "docker"])  # never docker there
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_CT_AGENT_TOKEN": "t" * 43}):
            r = self.cli("host", "add", "dk", "https://10.0.0.8:7173", "--fingerprint", self.FP, "--with", "docker")
        self.assertEqual(r["engine"], "docker")
        with unittest.mock.patch.dict(os.environ, {"TERMINAL_CT_AGENT_TOKEN": "t" * 43}), \
                self.assertRaisesRegex(SystemExit, "no container engine works"):
            self.cli("host", "add", "np", "https://10.0.0.9:7173", "--fingerprint", self.FP, "--with", "podman")
        self.assertEqual({n["node"]: n["engine"] for n in self.cli("host", "ls")}, {"pod": "podman", "dk": "docker"})
        self.podman |= {"hung", "old", "root"}
        self.hung_podman = {"hung"}                                           # a stopped Podman machine: Docker then
        self.podman_info.update(old="3.4.4 true\n", root="5.4.2 false\n")
        self.assertEqual(self.add("hung", "https://10.0.0.10:7173")["engine"], "docker")
        self.assertEqual(self.add("old", "https://10.0.0.11:7173")["engine"], "docker")  # Podman 3: too old
        self.add("root", "https://10.0.0.12:7173")
        self.cli("--host", "root", "create", "s", "--sandbox")
        made = [a for a in self.engines["root"].calls if a[0] == "create"][-1]
        self.assertNotIn("--userns=keep-id", made)                            # a rootful Podman refuses it
        legacy = ct.NodeRemote("x", {"url": "https://10.0.0.1:7173"})          # hosts.json before engines were kept
        self.assertEqual((legacy.engine, legacy.rootless), ("docker", True))
        net = lambda n: {"NetworkSettings": {"Networks": {n: {}}}, "HostConfig": {"NetworkMode": "bridge"}}
        self.assertTrue(ct.online(net("podman")) and ct.online(net("bridge")))  # another PC's engine, either
        self.assertFalse(ct.online(net("tct-net-lab")))

    def test_mac_with_docker_desktop(self):
        self.uname["mac"], self.mac = "Darwin", {"mac"}
        self.add("mac")
        self.cli("--host", "mac", "create", "web")  # its quota: the Mac's own disk
        self.assertIn("tct-web", self.engines["mac"].objects)
        self.assertIn(("mac", ["df", "-Pk", "/"]), self.execs)

    def test_quota_on_another_pc_is_not_all_of_it(self):
        self.add()
        with ct.on_node("pc1"):
            limits, _ = ct.quota_limits({})
        self.assertEqual((limits["cpus"], limits["memory_gb"]), (7, 12))  # 8 cores - 1, 75% of 16 GiB
        self.cli("--host", "pc1", "quota", "--cpus", "3", "--memory", "4")       # set for that PC alone
        self.assertTrue(os.path.isfile(os.path.join(ct.STORE, "quota-pc1.json")))
        with ct.on_node("pc1"):
            limits, chosen = ct.quota_limits({})
        self.assertEqual((limits["cpus"], limits["memory_gb"], chosen["cpus"]), (3, 4, True))
        limits, chosen = ct.quota_limits({})                                    # this PC: untouched
        self.assertFalse(chosen["cpus"])

    def test_agent_needs_approval_per_pc(self):
        self.add("pc1")
        asked = []

        def no(msg, cli):
            asked.append(msg)
            raise RuntimeError("the user said no")
        st = {"stack": "ag", "services": {"w": {"image": "alpine:3.20", "spread": "nodes", "replicas": 2}}}
        with self.assertRaisesRegex(RuntimeError, "said no"):
            ct.agent_call("ct_apply", {"stack": dict(st, approved_hosts=["pc1"])}, no, None)  # its own say: dropped
        self.assertIn("other PC pc1", asked[0])
        self.assertNotIn("ag", ct.stacks())
        r = ct.agent_call("ct_apply", {"stack": st}, lambda m, c: asked.append(m), None)
        self.assertEqual(ct.load_stack("ag")["approved_hosts"], ["pc1"])
        self.assertEqual(sorted(ct._nodes()["placement"].values()), ["pc1", "pc1"])
        n = len(asked)
        ct.agent_call("ct_apply", {"stack": st}, no, None)                      # asked once per PC, not again
        self.assertEqual(len(asked), n)
        self.add("pc2", "https://192.168.1.21:7173")                            # added later: not approved
        self.down.add("pc1")
        for _ in range(3):
            ct.reconcile("ag")
        self.assertNotIn("pc2", ct._nodes()["placement"].values())              # never placed there unasked
        self.assertIsNotNone(r)

    def test_agent_paths_around_the_approval(self):
        self.add("pc1")
        yes = lambda m, c: None
        ct.place("ghost", "pc1")                                               # a placement left without a container
        with self.assertRaisesRegex(SystemExit, "not available"):
            ct.agent_call("ct_create", {"name": "ghost"}, yes, None)
        self.assertNotIn("tct-ghost", self.engines.get("pc1", FakeNode()).objects)
        # an agent stack pinned to pc1 from before approvals existed: its replica leaves pc1, it is not kept there
        ct.apply({"stack": "old", "services": {"w": {"image": "alpine:3.20", "node": "pc1"}}}, created_by="agent")
        self.assertNotIn("old-w-1", ct._nodes()["placement"])
        evs = [e for e in ct.events("old", 20) if e["event"] == "pending"]
        self.assertIn("not approved", evs[-1]["detail"])
        # approved, then the PC removed and another machine added under its name: asked again
        ct.agent_call("ct_apply", {"stack": {"stack": "ag", "services": {"w": {"image": "alpine:3.20", "node": "pc1"}}}},
                      yes, None)
        self.assertEqual(ct.load_stack("ag")["approved_hosts"], ["pc1"])
        self.cli("--host", "pc1", "quota", "--cpus", "2")
        self.cli("host", "rm", "pc1", "--force")
        self.assertEqual(ct.load_stack("ag").get("approved_hosts"), [])
        self.assertFalse(os.path.exists(os.path.join(ct.STORE, "quota-pc1.json")))

    def test_vms_of_other_pcs(self):
        self.add("pc1"); self.add("pc2", "https://192.168.1.21:7173")
        self.pc_vms["pc1"] = {"deb": "running", "win": "stopped"}             # pc2: no terminal-vm
        with unittest.mock.patch.object(ct, "vm_cli", lambda: None):
            s = ct._vm_state()
            self.assertEqual([(v["name"], v["host"], v["status"]) for v in s["vms"]],
                             [("deb@pc1", "pc1", "running"), ("win@pc1", "pc1", "stopped")])
            self.assertEqual(s["vms"][0]["cpu_percent"], 3.0)
            self.assertEqual(s["events"], [{"event": "started", "vm": "deb@pc1", "host": "pc1"}])
            self.assertIn("pc2: terminal-vm not found there", s["note"])
            api = lambda m, path, q=None, body=None: ct.web_api(m, path, q or {}, body)
            self.assertEqual(api("GET", "/api/shot", {"kind": "vm", "name": "deb@pc1"}), ("image/png", b"PNG of deb"))
            self.assertEqual(self.pc_files, {})                                # nothing left there
            api("POST", "/api/do", body={"kind": "vm", "op": "start", "name": "win@pc1"})
            self.assertEqual(self.pc_vms["pc1"]["win"], "running")
            for bad in ("win@nope", "../x@pc1", "a@b@pc1", ""):
                with self.assertRaises(ValueError, msg=bad):
                    api("POST", "/api/do", body={"kind": "vm", "op": "stop", "name": bad})
            with self.assertRaisesRegex(RuntimeError, "pc1: no VM named gone"):
                api("POST", "/api/do", body={"kind": "vm", "op": "start", "name": "gone@pc1"})
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as e:
            ct.main(["--host", "pc1", "vm", "exec", "deb", "--", "uname", "-a"])  # passed on as it is
        ct.NODE = self.local
        self.assertEqual((e.exception.code, out.getvalue()), (0, "plain text\n"))
        self.assertEqual(self.vm_runs[-1], ("pc1", ["exec", "deb", "--", "uname", "-a"]))
        with self.assertRaisesRegex(SystemExit, "another PC"):
            self.cli("vm", "ls")
        with unittest.mock.patch.object(ct, "vm_cli", lambda: None):
            with self.assertRaisesRegex(RuntimeError, "not running"):          # failed there: nothing left
                ct.web_api("GET", "/api/shot", {"kind": "vm", "name": "win@pc1"}, None)
            self.assertEqual(os.listdir(os.path.join(ct.STORE, ".web")), [])
            with self.assertRaises(ValueError):
                ct.vm_where("deb@")
            self.pc_vms["pc2"] = "garbled"                                     # one PC answering badly
            ct._VM_CACHE.clear()
            s = ct._vm_state()
            self.assertEqual(len(s["vms"]), 2)                                 # the others still shown
            self.assertIn("pc2: unreadable VM list", s["note"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as e:
            ct.main(["--host", "pc1", "vm", "exec", "deb", "--", "missing"])
        ct.NODE = self.local
        self.assertEqual(e.exception.code, 127)
        self.assertNotIn("terminal-vm not found", err.getvalue())            # the guest's 127, not ours
        with self.assertRaisesRegex(RuntimeError, "nonexistent"):              # TERMINAL_VM naming nothing
            ct.cli_call([os.path.join(ct.STORE, "nonexistent", "terminal-vm")], ["ls"])

    def test_daemon_runs_on_this_store(self):
        seen, stub = [], next(x for x in self.patches if x.attribute == "daemon")
        stub.stop()  # the real daemon(), with Popen recorded
        try:
            with unittest.mock.patch.object(ct, "daemon_alive", lambda: False), \
                    unittest.mock.patch.object(ct.subprocess, "Popen", lambda args, **k: seen.append(k)):
                self.assertEqual(ct.daemon("start"), "started")
        finally:
            stub.start()
        self.assertEqual(seen[0]["env"]["TERMINAL_CT_HOME"], ct.STORE)  # never the default store of the user

    def test_quota_of_a_vm_node_is_its_size(self):
        d = ct._nodes(); d["nodes"]["vm1"] = {"image": "alpine"}; ct._save_nodes(d)
        with ct.on_node("vm1"), self.assertRaisesRegex(SystemExit, "its size"):
            ct.quota({"cpus": 2})
        self.assertFalse(os.path.exists(os.path.join(ct.STORE, "quota.json")))


class WebSession(unittest.TestCase):
    """Phase 11.5: the shared dashboard: a token per user with a role, who is on it, who did what, the network."""

    def setUp(self):
        ct.STORE = tempfile.mkdtemp()
        ct.WEB_SEEN.clear()
        self.calls = []

        def api(method, path, q, body=None):
            self.calls.append((method, path, body))
            if body and body.get("op") == "boom":
                raise SystemExit("it failed")
            if path == "/api/stack":
                return "application/json", {"desired": {"stack": "s", "services": {"db": {"env": {"PW": "secret"}},
                                                                                 "web": {"image": "nginx"}}}}
            return "application/json", {"ok": path}
        self.p = unittest.mock.patch.object(ct, "web_api", api)
        self.p.start()
        self.srv = ct._WebServer(("127.0.0.1", 0), ct._WebHandler)
        port = self.srv.server_address[1]
        self.srv.hosts = {f"127.0.0.1:{port}"}
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = port

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.p.stop()

    def call(self, token, method="GET", path="/api/state", body=None, host=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers={"X-Token": token, "Content-Type": "application/json",
                                                   **({"Host": host} if host else {})})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null")
        c.close()
        return out

    def test_roles_presence_and_actions(self):
        bob = ct.web_user("add", "bob", role="read")
        ann = ct.web_user("add", "ann", role="full")
        with open(os.path.join(ct.STORE, "web.users.json")) as f:
            self.assertNotIn(bob["token"], f.read())                            # its hash only
        if not ct.WINDOWS:
            self.assertEqual(os.stat(os.path.join(ct.STORE, "web.users.json")).st_mode & 0o777, 0o600)
        self.assertEqual(self.call(bob["token"])[0], 200)                       # read: may look
        s, r = self.call(bob["token"], "POST", "/api/do", {"op": "stop", "name": "web"})
        self.assertEqual((s, r["error"]), (403, "read only: this token may look, not act"))
        self.assertNotIn(("POST", "/api/do", {"op": "stop", "name": "web"}), self.calls)  # never reached the tool
        self.assertEqual(self.call(ann["token"], "POST", "/api/do", {"op": "stop", "name": "web"})[0], 200)
        self.assertEqual(self.call(ann["token"], "POST", "/api/do", {"op": "boom", "name": "x"})[0], 400)
        s, me = self.call(bob["token"], path="/api/session")
        self.assertEqual((me["user"], me["role"]), ("bob", "read"))
        self.assertEqual({o["user"] for o in me["online"]}, {"bob", "ann"})
        self.assertEqual([(a["user"], a.get("op"), a.get("error")) for a in me["actions"]],
                         [("ann", "boom", "it failed"), ("ann", "stop", None), ("bob", "stop", "refused: read only")])
        self.assertEqual(self.call(ct.web_token(), path="/api/session")[1]["user"], "owner")
        self.assertEqual(self.call("x" * 32)[0], 401)
        self.assertEqual([u["user"] for u in ct.web_user("ls")], ["ann", "bob"])
        ct.web_user("rm", "bob")
        self.assertEqual(self.call(bob["token"])[0], 401)                       # revoked at once
        for bad in (("add", "owner"), ("add", "Bob"), ("add", "ann"), ("rm", "nobody"), ("add", "x", "admin")):
            with self.assertRaises(SystemExit, msg=bad):
                ct.web_user(*bad[:2], role=bad[2] if len(bad) > 2 else "full")
        with self.assertRaisesRegex(SystemExit, "web user add NAME"):
            ct.web_cli(["users"])

    def test_read_role_forges_no_log_line(self):  # a stack has no env field: read sees it all, as documented
        bob, ann = ct.web_user("add", "bob", role="read"), ct.web_user("add", "ann", role="full")
        forged = "x\n2026-11-10T10:00:00  owner  127.0.0.1  rm  db" + "y" * 5000
        self.call(bob["token"], "POST", "/api/do", {"op": "stop", "name": forged})
        a = ct.web_actions(1)[0]
        self.assertEqual(len(a["name"]), 64)
        self.assertNotIn("\n", a["name"])

    def test_one_address_cannot_take_every_slot(self):
        srv = ct._WebServer(("127.0.0.1", 0), ct._WebHandler)
        srv.hosts, srv.per_address = {f"127.0.0.1:{srv.server_address[1]}"}, 2
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        idle = [socket.create_connection(("127.0.0.1", srv.server_address[1])) for _ in range(2)]
        try:
            time.sleep(0.3)
            third = socket.create_connection(("127.0.0.1", srv.server_address[1])); third.settimeout(5)
            self.assertEqual(third.recv(1), b"")                               # closed at once
            third.close()
            for s in idle:
                s.close()
            time.sleep(0.5)
            self.assertEqual(srv.by_address, {})                               # their places given back
        finally:
            srv.shutdown(); srv.server_close()

    def test_listen_checks(self):
        for bad in ("0.0.0.0", "::", "::ffff:0.0.0.0", "fe80::1%eth0", "pc.local", "300.1.1.1"):
            with self.assertRaises(SystemExit, msg=bad):
                ct.web(False, listen=bad)
        self.assertFalse(os.path.exists(os.path.join(ct.STORE, "web.listen.json")))

    @unittest.skipUnless(shutil.which("openssl"), "HTTPS needs openssl")
    def test_network_side_is_https_only(self):
        import http.client
        with open(os.path.join(ct.STORE, "web.listen.json"), "w") as f:
            json.dump({"ip": "127.0.0.1", "port": 0}, f)
        threading.Thread(target=ct._web_serve_net, args=(ct._web_listen(),), daemon=True).start()
        for _ in range(100):
            net = ct._web_net()
            if net:
                break
            time.sleep(0.1)
        self.assertTrue(net, "the network side did not come up")
        port = int(net["url"].rsplit(":", 1)[1])
        seen = ssl.get_server_certificate(("127.0.0.1", port))
        if hashlib.sha256(ssl.PEM_cert_to_DER_cert(seen)).hexdigest() != net["fingerprint"]:
            self.skipTest("TLS intercepted on this machine (antivirus HTTPS scanning)")
        ann = ct.web_user("add", "ann", role="full")
        self.assertTrue(ann["url"].startswith(net["url"] + "/#t="))             # a link to give with the token
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

        def get(host=None):
            c = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=ctx)
            c.request("GET", "/api/session", headers={"X-Token": ann["token"], **({"Host": host} if host else {})})
            r = c.getresponse()
            out = r.status, json.loads(r.read())
            c.close()
            return out
        self.assertEqual(get()[1]["user"], "ann")
        self.assertEqual(get("evil.example:80")[0], 403)                       # DNS rebinding: refused
        plain = http.client.HTTPConnection("127.0.0.1", port, timeout=5)      # never plain HTTP on the network
        with self.assertRaises((OSError, http.client.HTTPException)):
            plain.request("GET", "/"); plain.getresponse().read()


@unittest.skipUnless(shutil.which("openssl"), "the agent makes its certificate with openssl")
class RemoteAgent(unittest.TestCase):
    """Phase 11.1: another PC's agent: pinned certificate, then token; exec, push, pull; SSH as the other way."""

    @classmethod
    def setUpClass(cls):
        cls.store = ct.STORE = tempfile.mkdtemp()
        cls.srv = ct.agent_serve("127.0.0.1", 0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.host = {"url": f"https://127.0.0.1:{cls.srv.server_address[1]}", "fingerprint": cls.srv.fingerprint,
                    "token": cls.srv.token}
        seen = ssl.get_server_certificate(("127.0.0.1", cls.srv.server_address[1]))
        if hashlib.sha256(ssl.PEM_cert_to_DER_cert(seen)).hexdigest() != cls.srv.fingerprint:
            cls.tearDownClass()  # an antivirus re-signs even local HTTPS (seen: Avast on Windows): pinning refuses it
            raise unittest.SkipTest("TLS intercepted on this machine (antivirus HTTPS scanning)")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        ct.STORE = self.store

    def log(self):
        with open(os.path.join(self.store, "agent", "agent.log"), encoding="utf-8") as f:
            return f.read()

    def test_credentials_kept_and_private(self):
        self.assertEqual(ct.agent_credentials(), (self.srv.fingerprint, self.srv.token))
        self.assertGreaterEqual(len(self.srv.token), 32)
        if not ct.WINDOWS:
            self.assertEqual(os.stat(os.path.join(self.store, "agent", "cert.pem")).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.join(self.store, "agent", "token")).st_mode & 0o777, 0o600)

    def test_ping_and_exec(self):
        self.assertEqual(ct.agent_request(self.host, "GET", "/ping")["tool"], "terminal-ct")
        n = ct.NodeRemote("pc1", self.host)
        r = n._exec([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], 30)
        self.assertEqual((r["exit"], r["stdout"].strip(), r["stderr"].strip()), (3, "out", "err"))
        self.assertEqual(n._exec(["no-such-program-tct"], 30)["exit"], 127)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            ct.agent_request(self.host, "POST", "/exec", {"argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                                                         "timeout": 0.5})
        self.assertIn("exec ", self.log())

    def test_refusals(self):
        for bad in ({"argv": "ls"}, {"argv": []}, {"argv": ["x", 1]}, {"argv": ["x"], "timeout": 0},
                    {"argv": ["x"], "timeout": 99999}, {"argv": ["x"], "stdin": 5}, {}):
            with self.assertRaisesRegex(RuntimeError, "exec wants"):
                ct.agent_request(self.host, "POST", "/exec", bad)
        with self.assertRaisesRegex(RuntimeError, "not found"):
            ct.agent_request(self.host, "GET", "/etc/passwd")
        with self.assertRaises(RuntimeError):  # "too large", or a broken pipe: refused before all was sent
            ct.agent_request(self.host, "POST", "/exec", b"x" * (ct.AGENT_MAX_JSON + 1))
        self.assertEqual(ct.agent_request(self.host, "GET", "/ping")["tool"], "terminal-ct")  # still serving

    def test_wrong_token_refused_and_logged(self):
        ct._REFUSED.clear()
        with self.assertRaisesRegex(RuntimeError, "wrong token"):
            ct.agent_request(dict(self.host, token="x" * 43), "POST", "/exec", {"argv": ["whoami"]})
        self.assertIn("refused POST /exec", self.log())

    def test_wrong_certificate_never_gets_the_token(self):
        before = self.log() if os.path.exists(os.path.join(self.store, "agent", "agent.log")) else ""
        spy = dict(self.host, fingerprint="00" * 32, token="secret-" + "y" * 40)
        with self.assertRaisesRegex(RuntimeError, "not the pinned one"):
            ct.agent_request(spy, "GET", "/ping")
        time.sleep(0.2)
        after = self.log() if os.path.exists(os.path.join(self.store, "agent", "agent.log")) else ""
        self.assertEqual(before, after)  # no request reached the agent: the token never left

    def test_unreachable(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        with self.assertRaisesRegex(RuntimeError, "agent https://127.0.0.1"):
            ct.agent_request(dict(self.host, url=f"https://127.0.0.1:{port}"), "GET", "/ping", timeout=5)

    def test_push_pull_round_trip(self):
        d = tempfile.mkdtemp()
        src, there, back = (os.path.join(d, f) for f in ("src.bin", "there.bin", "back.bin"))
        data = os.urandom(3 << 20) + b"\r\n\x00end"
        with open(src, "wb") as f:
            f.write(data)
        n = ct.NodeRemote("pc1", self.host)
        n.copy("push", src, there)
        n.copy("pull", there, back)
        with open(back, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(sorted(os.listdir(d)), ["back.bin", "src.bin", "there.bin"])  # no part files left
        with self.assertRaisesRegex(SystemExit, "copy failed"):
            n.copy("pull", os.path.join(d, "missing"), back)
        with self.assertRaisesRegex(SystemExit, "copy failed"):
            n.copy("push", src, os.path.join(d, "no-dir", "x"))

    def test_rotate_gives_a_new_token(self):
        old = ct.agent_credentials()
        new = ct.agent_credentials(rotate=True)
        try:
            self.assertEqual(new[0], old[0])
            self.assertNotEqual(new[1], old[1])
        finally:
            ct._web_write(os.path.join(self.store, "agent", "token"), old[1], 0o600)

    def test_ssh_way(self):
        n = ct.NodeRemote("pc2", {"url": "ssh://me@pc2:2222"})
        seen = []

        def run(cmd, **k):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 255 if "fail" in cmd[-1] else 0, "ok\n", "nope")
        with unittest.mock.patch.object(ct.subprocess, "run", run):
            self.assertEqual(n._exec(["docker", "ps", "--format", "{{.Names}} x"], 30)["stdout"], "ok\n")
            self.assertEqual(seen[-1][:2], ["ssh", "-T"])
            self.assertEqual(seen[-1][-3:], ["--", "me@pc2", "docker ps --format '{{.Names}} x'"])
            self.assertIn("2222", seen[-1])
            with self.assertRaisesRegex(RuntimeError, "ssh me@pc2: nope"):
                n._exec(["fail"], 30)
            n.copy("push", "a b.tar", "/tmp/x")
            self.assertEqual(seen[-1][0], "scp")
            self.assertEqual(seen[-1][-3:], ["--", "a b.tar", "me@pc2:/tmp/x"])
            n.copy("pull", "/tmp/x", "y")
            self.assertEqual(seen[-1][-2:], ["me@pc2:/tmp/x", "y"])
            seen.clear()
            for bad in ("ssh://-oProxyCommand=touch@pc", "ssh://-oproxycommand=x", "ssh://pc:abc", "ssh://me:pw@pc",
                        "ssh://pc/path", "ssh://a b@pc", "https://pc", "ssh://", "ssh://pc?x=1"):
                with self.assertRaises((RuntimeError, ValueError, KeyError), msg=bad):  # host add checks first
                    ct.NodeRemote("x", {"url": bad})._exec(["true"], 5)
                with self.assertRaisesRegex(SystemExit, "copy failed", msg=bad):
                    ct.NodeRemote("x", {"url": bad}).copy("push", "a", "b")
            self.assertEqual(seen, [])  # nothing ever ran
        self.assertEqual(ct.ssh_target("ssh://u_1@10.0.0.5:22")[1], 22)

    def test_pushed_file_private_and_log_safe(self):
        d = tempfile.mkdtemp()
        src, there = os.path.join(d, "s"), os.path.join(d, "t")
        with open(src, "wb") as f:
            f.write(b"secret")
        ct.NodeRemote("pc1", self.host).copy("push", src, there)
        if not ct.WINDOWS:
            self.assertEqual(os.stat(there).st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, "not a regular file"):
            ct.agent_request(self.host, "GET", "/pull?path=" + ct.urllib.parse.quote(d), out=os.path.join(d, "o"))
        ct._REFUSED.clear()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        for _ in range(3):  # one line a minute per address, escaped (http.client would not send these bytes)
            with ctx.wrap_socket(socket.create_connection(("127.0.0.1", self.srv.server_address[1]))) as s:
                s.sendall(b"GET /x\x1b[2J\x07forged HTTP/1.0\r\n\r\n")
                self.assertIn(b"401", s.recv(100))
        log = self.log()
        self.assertNotIn("\x1b", log)
        self.assertEqual(log.count("refused GET /x"), 1)
        self.assertNotIn("\x07", log)

    def test_connections_capped(self):
        srv = ct.agent_serve("127.0.0.1", 0)
        srv.slots = threading.BoundedSemaphore(2)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, socks = dict(self.host, url=f"https://127.0.0.1:{srv.server_address[1]}"), []
        try:
            for _ in range(2):  # two idle clients hold the two slots
                socks.append(socket.create_connection(("127.0.0.1", srv.server_address[1])))
            time.sleep(0.3)
            with self.assertRaises(RuntimeError):  # the third is closed at once
                ct.agent_request(host, "GET", "/ping", timeout=5)
            for s in socks:
                s.close()
            time.sleep(0.5)  # their slots come back
            self.assertEqual(ct.agent_request(host, "GET", "/ping")["tool"], "terminal-ct")
        finally:
            for s in socks:
                s.close()
            srv.shutdown(); srv.server_close()


@unittest.skipUnless(os.environ.get("TERMINAL_CT_REAL"), "set TERMINAL_CT_REAL=1 to run against real engines")
class RealEngines(unittest.TestCase):
    def setUp(self):  # its own store, and a quota a small CI runner does not hit (its default: cores - 1)
        ct.STORE = tempfile.mkdtemp()
        with open(os.path.join(ct.STORE, "quota.json"), "w") as f:
            json.dump({"cpus": 64, "memory_gb": 256, "containers": 64}, f)

    def test_round_trip(self):
        for engine in REAL_ENGINES:
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

    def test_phase4(self):
        """Snapshot of files and /data, load back, clone with a copy of /data, push and pull."""
        sh = lambda n, c: quiet(ct.exec_, n, ["sh", "-c", c])["stdout"].strip()
        for engine in REAL_ENGINES:
            if not shutil.which(engine):
                continue
            with self.subTest(engine=engine):
                ct.NODE = ct.Node(engine)
                a, b = f"p4a-{engine}", f"p4b-{engine}"
                for n in (a, b):
                    subprocess.run([engine, "rm", "-f", "tct-" + n], capture_output=True)
                    subprocess.run([engine, "volume", "rm", "-f", f"tct-{n}-data"], capture_output=True)
                quiet(ct.create, a, image="alpine:3.20"); quiet(ct.start, a)
                sh(a, "echo v1 > /etc/mark; echo d1 > /data/mark; mkdir /data/sub; chown 1234 /data/sub")
                self.assertTrue(ct.snapshot(a, "save", "one")["saved"])
                sh(a, "echo v2 > /etc/mark; echo d2 > /data/mark; touch /data/extra")
                quiet(ct.stop, a, 1)
                ct.snapshot(a, "load", "one"); quiet(ct.start, a)
                self.assertEqual(sh(a, "cat /etc/mark /data/mark; ls /data; stat -c %u /data/sub").split(),
                                 ["v1", "d1", "mark", "sub", "1234"])               # files, /data, owners back
                quiet(ct.stop, a, 1)
                self.assertEqual(ct.clone(a, b)["cloned_from"], a); quiet(ct.start, b)
                self.assertEqual(sh(b, "cat /etc/mark /data/mark"), "v1\nd1")
                src = os.path.join(ct.STORE, "in.txt")
                with open(src, "w") as f:
                    f.write("pushed")
                ct.transfer(b, "push", src, "/data/in.txt")
                ct.transfer(b, "pull", "/data/in.txt", os.path.join(ct.STORE, "out.txt"))
                with open(os.path.join(ct.STORE, "out.txt")) as f:
                    self.assertEqual(f.read(), "pushed")
                self.assertGreater(ct.snapshot(a, "ls")["snapshots"]["one"], 0)
                for n in (a, b):
                    quiet(ct.rm, n)
                imgs = subprocess.run([engine, "image", "ls", "--format", "{{.Repository}}"], capture_output=True,
                                      text=True).stdout
                self.assertNotIn(f"tct-snap-{a}", imgs); self.assertNotIn(f"tct-keep-{b}", imgs)

    def test_phase3(self):
        """Limits in the cgroup and changed live, top, isolated network by name, internet off, sandbox reset."""
        sh = lambda n, c: quiet(ct.exec_, n, ["sh", "-c", c])
        for engine in REAL_ENGINES:
            if not shutil.which(engine):
                continue
            with self.subTest(engine=engine):
                ct.NODE = ct.Node(engine)
                a, b, s = f"p3a-{engine}", f"p3b-{engine}", f"p3s-{engine}"
                for n in (a, b, s):
                    subprocess.run([engine, "rm", "-f", "tct-" + n], capture_output=True)
                subprocess.run([engine, "network", "rm", "-f", f"tct-net-p3-{engine}"], capture_output=True)
                net = f"p3-{engine}"
                ct.net_create(net)
                quiet(ct.create, a, image="alpine:3.20", cpus=0.5, memory=0.25, nets=[net])
                quiet(ct.create, b, image="alpine:3.20", nets=[net], internet=False)
                quiet(ct.create, s, image="alpine:3.20", sandbox=True)
                for n in (a, b, s):
                    quiet(ct.start, n)
                self.assertEqual(sh(a, "cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max")["stdout"].split(),
                                 ["50000", "100000", str(2 ** 28)])
                ct.resources(a, cpus=1, memory=0.5)
                self.assertEqual(sh(a, "cat /sys/fs/cgroup/memory.max")["stdout"].strip(), str(2 ** 29))
                self.assertEqual({r["name"] for r in ct.top()["containers"]} >= {a, b, s}, True)
                self.assertEqual(sh(b, f"ping -c1 -W3 {a} >/dev/null && echo ok")["stdout"].strip(), "ok")
                self.assertNotEqual(sh(b, "wget -q -T3 -O /dev/null http://1.1.1.1/")["exit"], 0)  # offline
                self.assertEqual(sh(s, "id -u; touch /etc/x 2>/dev/null || echo ro; echo hi > /data/f; "
                                       "touch /tmp/mark; ls /tmp")["stdout"].split()[:2], [str(os.getuid()), "ro"])
                quiet(ct.stop, s, 1)
                self.assertTrue(quiet(ct.start, s)["fresh"])
                self.assertEqual(sh(s, "ls /tmp /data")["stdout"].split(), ["/data:", "/tmp:"])  # all gone
                for n in (a, b, s):
                    quiet(ct.rm, n)
                ct.net_rm(net)
                left = subprocess.run([engine, "volume", "ls", "-q"], capture_output=True, text=True).stdout.split()
                self.assertFalse([v for v in left if v.startswith("tct-p3")])


if __name__ == "__main__":
    unittest.main()

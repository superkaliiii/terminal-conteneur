import base64, contextlib, hashlib, io, re, json, os, shutil, socket, struct, subprocess, sys, tempfile, threading, time, unittest, unittest.mock, zlib

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
            self.assertEqual(os.listdir(os.path.join(ct.STORE, "web")), [])                 # temporary file removed
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
    SHARED_SHA = "f1c7258c1765ba1682b5e2fb241d7c55b97c6768fb0e10079ae8bec43e333889"

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
                if argv[0] == "sh":
                    self.shell.append(argv[2])
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

    def tearDown(self):
        self.patch.stop(); self.age.stop()
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
                    {"spread": "nodes", "networks": ["back"]}, {"spread": "all"}):
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
        self.assertRegex(self.shell[-2], r"tar -xf /tmp/tct-\w+\.tar -C /tmp/tct-\w+ && docker build -t mine /tmp/tct-")
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
        self.assertRegex(pushed[3], r"^/tmp/tct-\w+/notes\.txt$")
        self.assertTrue(any(re.match(r"docker cp -a /tmp/tct-\w+/notes\.txt tct-web:/data/$", c) for c in self.shell))
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
        for has_filter in (True, False):
            buf.seek(0)
            with tarfile.open(fileobj=buf) as tar, tempfile.TemporaryDirectory() as d:
                with unittest.mock.patch.object(tarfile, "data_filter", tarfile.data_filter, create=True) if has_filter \
                        else unittest.mock.patch.object(ct, "tarfile", type("T", (), {"open": tarfile.open})):
                    with self.assertRaises((SystemExit, tarfile.TarError)):
                        ct._extract(tar, d)
                self.assertFalse(os.path.exists(os.path.join(d, "..", "evil")))

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
        self.assertIn("project_status first", replies[0]["result"]["instructions"])
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

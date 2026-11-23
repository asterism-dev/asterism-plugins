import json
import os
import socket
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# Fake `observatory`: logs argv, answers from FAKE_{OUT,ERR,EXIT}_<noun>_<verb>.
FAKE = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
key = "_".join(sys.argv[1:3])
sys.stdout.write(os.environ.get("FAKE_OUT_" + key, ""))
sys.stderr.write(os.environ.get("FAKE_ERR_" + key, ""))
sys.exit(int(os.environ.get("FAKE_EXIT_" + key, "0")))
"""

VMS = {"vms": [
    {"id": "a", "name": "web", "state": "suspended"},
    {"id": "b", "name": "db", "state": "paused"},
    {"id": "c", "name": "ci", "state": "hibernated"},
    {"id": "d", "name": "old", "state": "error"},
]}
IMAGES = [{"ref": "observatory/standard"}]


class Backend:
    def __init__(self, env=None, settings=None, with_cli=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "log")
        if with_cli:
            fake = os.path.join(self.tmp.name, "observatory")
            with open(fake, "w") as f:
                f.write(FAKE)
            os.chmod(fake, 0o755)
        full_env = {
            "PATH": self.tmp.name + ":/usr/bin:/bin",
            "FAKE_LOG": self.log,
            "FAKE_OUT_vm_list": json.dumps(VMS),
            "FAKE_OUT_image_list": json.dumps(IMAGES),
        }
        full_env.update(env or {})
        self.proc = subprocess.Popen(
            ["/usr/bin/env", "python3", os.path.join(HERE, "backend.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=full_env,
        )
        self.next_id = 0
        self.call("initialize", {"protocol": 1, "settings": settings or {}})

    def call(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def argv(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(5)
        self.proc.stdout.close()
        self.tmp.cleanup()


class BackendTest(unittest.TestCase):
    def start(self, **kw):
        b = Backend(**kw)
        self.addCleanup(b.close)
        return b

    def ssh_listener(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        self.addCleanup(s.close)
        return {"OBSERVATORY_PLUGIN_SSH_ADDR": "127.0.0.1:%d" % s.getsockname()[1]}

    def test_initialize(self):
        b = self.start()
        self.assertEqual(b.call("initialize", {"settings": {}})["result"], {"capabilities": ["machine_provider"]})

    def test_list(self):
        b = self.start(settings={"cpus": 4})
        result = b.call("machine_provider.list", {"provider": "observatory"})["result"]
        self.assertEqual(result["vms"][0], {"ref": "a", "name": "web", "state": "running", "destination": "dev@web.internal"})
        self.assertEqual([v["state"] for v in result["vms"]], ["running", "stopped", "hibernated", "other"])
        options = {o["key"]: o for o in result["create_options"]}
        self.assertEqual(list(options), ["image", "cpus", "memory_gb", "disk_gb"])
        self.assertEqual(options["image"]["type"], "enum")
        self.assertEqual(options["image"]["options"], ["default", "observatory/standard"])
        self.assertEqual(options["image"]["default"], "default")
        self.assertEqual(options["cpus"]["default"], 4)
        self.assertEqual(options["memory_gb"]["default"], 4)

    def test_settings_changed(self):
        b = self.start()
        b.call("settings.changed", {"settings": {"disk_gb": 50}})
        options = {o["key"]: o for o in b.call("machine_provider.list", {})["result"]["create_options"]}
        self.assertEqual(options["disk_gb"]["default"], 50)

    def test_unknown_method(self):
        b = self.start()
        self.assertEqual(b.call("nope")["error"]["code"], -32601)
        self.assertIn("result", b.call("initialize", {}))

    def test_cli_missing(self):
        b = self.start(with_cli=False)
        error = b.call("machine_provider.list", {})["error"]
        self.assertEqual(error["data"]["kind"], "plugin_error")
        self.assertIn("observatory CLI not found", error["message"])


if __name__ == "__main__":
    unittest.main()

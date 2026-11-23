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

    def test_create_argv(self):
        env = {"FAKE_OUT_vm_create": json.dumps({"id": "vm-1", "name": "api"}), **self.ssh_listener()}
        b = self.start(env=env)
        options = {"image": "observatory/standard", "cpus": 2.0, "memory_gb": 4, "disk_gb": 20}
        result = b.call("machine_provider.create", {"provider": "observatory", "name": "api", "options": options})["result"]
        self.assertEqual(result, {"ref": "vm-1", "name": "api", "destination": "dev@api.internal"})
        self.assertEqual(b.argv()[-1], ["vm", "create", "api", "--cpus", "2", "--memory", "4G", "--disk", "20",
                                        "--image", "observatory/standard", "--wait", "--json"])

    def test_create_default_image(self):
        env = {"FAKE_OUT_vm_create": json.dumps({"id": "vm-1", "name": "api"}), **self.ssh_listener()}
        b = self.start(env=env)
        b.call("machine_provider.create", {"name": "api", "options": {"image": "default", "cpus": 1, "memory_gb": 1, "disk_gb": 10}})
        self.assertNotIn("--image", b.argv()[-1])

    def test_resume(self):
        env = {"FAKE_OUT_vm_start": json.dumps({"id": "vm-1", "name": "api"}), **self.ssh_listener()}
        b = self.start(env=env)
        result = b.call("machine_provider.resume", {"provider": "observatory", "ref": "vm-1"})["result"]
        self.assertEqual(result, {"destination": "dev@api.internal"})
        self.assertEqual(b.argv()[-1], ["vm", "start", "vm-1", "--wait", "--json"])

    def test_hibernate(self):
        b = self.start()
        self.assertIsNone(b.call("machine_provider.hibernate", {"ref": "vm-1"})["result"])
        self.assertEqual(b.argv()[-1], ["vm", "hibernate", "vm-1", "--wait", "--json"])

    def test_delete_argv(self):
        b = self.start()
        self.assertIsNone(b.call("machine_provider.delete", {"ref": "vm-1"})["result"])
        self.assertEqual(b.argv()[-1], ["vm", "delete", "vm-1", "--yes", "--wait", "--json"])

    def test_delete_not_found(self):
        b = self.start(env={"FAKE_EXIT_vm_delete": "5"})
        self.assertIsNone(b.call("machine_provider.delete", {"ref": "vm-1"})["result"])

    def test_cli_error_message(self):
        env = {"FAKE_EXIT_vm_hibernate": "3", "FAKE_ERR_vm_hibernate": "observatory: session expired or revoked, run observatory login\n"}
        error = self.start(env=env).call("machine_provider.hibernate", {"ref": "vm-1"})["error"]
        self.assertEqual(error["data"]["kind"], "plugin_error")
        self.assertIn("run observatory login", error["message"])

    def test_wait_timeout(self):
        error = self.start(env={"FAKE_EXIT_vm_start": "6"}).call("machine_provider.resume", {"ref": "vm-1"})["error"]
        self.assertEqual(error["data"]["kind"], "timeout")

    def test_answers_after_stdin_closed(self):
        b = self.start()
        b.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "machine_provider.list", "params": {}}) + "\n")
        b.proc.stdin.close()
        self.assertEqual(json.loads(b.proc.stdout.readline())["id"], 99)
        b.proc.stdin = open(os.devnull, "w")

    def test_cli_missing(self):
        b = self.start(with_cli=False)
        error = b.call("machine_provider.list", {})["error"]
        self.assertEqual(error["data"]["kind"], "plugin_error")
        self.assertIn("observatory CLI not found", error["message"])


if __name__ == "__main__":
    unittest.main()

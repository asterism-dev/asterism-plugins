#!/usr/bin/env python3
"""Asterism machine provider backend for Observatory, wrapping the `observatory` CLI."""
import json
import socket
import subprocess
import sys
import threading
import time

DEFAULTS = {"image": "", "cpus": 2, "memory_gb": 4, "disk_gb": 20}
STATES = {"running": "running", "suspended": "running", "stopped": "stopped", "paused": "stopped", "hibernated": "hibernated"}
KIND_CODES = {"invalid_params": -32602, "not_found": -32001, "timeout": -32007}
SSH_WAIT_SECONDS = 300
# the CLI's own default of 5m can be too short when a node first pulls an image
WAIT = ["--wait", "--timeout", "10m"]
settings = dict(DEFAULTS)
out_lock = threading.Lock()


class RpcError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def run_cli(args):
    try:
        proc = subprocess.run(["observatory", *args, "--json"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    except FileNotFoundError:
        raise RpcError("plugin_error", "observatory CLI not found on PATH")
    if proc.returncode != 0:
        kind = {5: "not_found", 6: "timeout"}.get(proc.returncode, "plugin_error")
        # stderr also carries update hints and session warnings; the error is the "observatory: " line
        errors = [line.removeprefix("observatory: ") for line in proc.stderr.splitlines() if line.startswith("observatory: ")]
        message = errors[-1] if errors else proc.stderr.strip() or "observatory exited with %d" % proc.returncode
        raise RpcError(kind, message)
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def destination(name):
    return "dev@%s.internal" % name


def set_settings(params):
    settings.clear()
    settings.update(DEFAULTS)
    settings.update({k: v for k, v in (params.get("settings") or {}).items() if v is not None})


def initialize(params):
    set_settings(params)
    return {"capabilities": ["machine_provider"]}


def settings_changed(params):
    set_settings(params)
    return None


def number_option(key, title):
    return {"key": key, "title": title, "type": "number", "required": True, "default": settings[key]}


def list_vms(params):
    vms = [
        {"ref": vm["id"], "name": vm["name"], "state": STATES.get(vm["state"], "other"), "destination": destination(vm["name"])}
        for vm in run_cli(["vm", "list"])["vms"]
    ]
    images = ["default"] + [image["ref"] for image in run_cli(["image", "list"])]
    if settings["image"] and settings["image"] not in images:
        images.append(settings["image"])
    create_options = [
        {"key": "image", "title": "Image", "type": "enum", "required": True, "options": images, "default": settings["image"] or "default"},
        number_option("cpus", "CPUs"),
        number_option("memory_gb", "RAM (GB)"),
        number_option("disk_gb", "Disk (GB)"),
    ]
    return {"vms": vms, "create_options": create_options}


def ssh_address(name):
    return "%s.internal" % name, 22


def wait_for_ssh(name):
    host, port = ssh_address(name)
    deadline = time.monotonic() + SSH_WAIT_SECONDS
    while True:
        try:
            socket.create_connection((host, port), timeout=2).close()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise RpcError("timeout", "VM %s did not accept SSH within %d s; is your WireGuard device up?" % (name, SSH_WAIT_SECONDS))
            time.sleep(2)


def whole_number(options, key):
    value = options[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or value < 1:
        raise RpcError("invalid_params", "%s must be a whole number of at least 1, got %r" % (key, value))
    return int(value)


def create(params):
    name, options = params["name"], params.get("options") or {}
    args = ["vm", "create", name, "--cpus", str(whole_number(options, "cpus")),
            "--memory", "%dG" % whole_number(options, "memory_gb"), "--disk", str(whole_number(options, "disk_gb"))]
    if options.get("image", "default") != "default":
        args += ["--image", options["image"]]
    try:
        vm = run_cli(args + WAIT)
        wait_for_ssh(vm["name"])
    except RpcError as e:
        # a timeout means the VM exists but Asterism never gets its ref, so it would be left behind
        if e.kind == "timeout":
            delete({"ref": name})
        raise
    return {"ref": vm["id"], "name": vm["name"], "destination": destination(vm["name"])}


def resume(params):
    vm = run_cli(["vm", "start", params["ref"], *WAIT])
    wait_for_ssh(vm["name"])
    return {"destination": destination(vm["name"])}


def hibernate(params):
    run_cli(["vm", "hibernate", params["ref"], *WAIT])
    return None


def delete(params):
    try:
        run_cli(["vm", "delete", params["ref"], "--yes", *WAIT])
    except RpcError as e:
        if e.kind != "not_found":
            raise
    return None


METHODS = {
    "initialize": initialize,
    "settings.changed": settings_changed,
    "machine_provider.list": list_vms,
    "machine_provider.create": create,
    "machine_provider.resume": resume,
    "machine_provider.hibernate": hibernate,
    "machine_provider.delete": delete,
}


def send(message):
    with out_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def handle(request):
    rid = request.get("id")
    method = METHODS.get(request.get("method"))
    if method is None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method %s" % request.get("method")}})
        return
    try:
        send({"jsonrpc": "2.0", "id": rid, "result": method(request.get("params") or {})})
    except RpcError as e:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": KIND_CODES.get(e.kind, -32000), "message": e.message, "data": {"kind": e.kind}}})
    except KeyError as e:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "missing %s" % e, "data": {"kind": "invalid_params"}}})
    except Exception as e:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(e), "data": {"kind": "plugin_error"}}})


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            print("ignoring malformed line: %r" % line, file=sys.stderr)
            continue
        # create and resume run for minutes; the daemon may call list meanwhile
        threading.Thread(target=handle, args=(request,)).start()


if __name__ == "__main__":
    main()

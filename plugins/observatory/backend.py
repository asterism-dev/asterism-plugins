#!/usr/bin/env python3
"""Asterism machine provider backend for Observatory, wrapping the `observatory` CLI."""
import json
import subprocess
import sys

DEFAULTS = {"image": "", "cpus": 2, "memory_gb": 4, "disk_gb": 20}
STATES = {"running": "running", "suspended": "running", "stopped": "stopped", "paused": "stopped", "hibernated": "hibernated"}
KIND_CODES = {"invalid_params": -32602, "not_found": -32001, "timeout": -32007}
settings = dict(DEFAULTS)


class RpcError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def run_cli(args):
    try:
        proc = subprocess.run(["observatory", *args, "--json"], capture_output=True, text=True)
    except FileNotFoundError:
        raise RpcError("plugin_error", "observatory CLI not found on PATH")
    if proc.returncode != 0:
        kind = {5: "not_found", 6: "timeout"}.get(proc.returncode, "plugin_error")
        message = proc.stderr.strip().removeprefix("observatory: ") or "observatory exited with %d" % proc.returncode
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
    create_options = [
        {"key": "image", "title": "Image", "type": "enum", "required": True, "options": images, "default": settings["image"] or "default"},
        number_option("cpus", "CPUs"),
        number_option("memory_gb", "RAM (GB)"),
        number_option("disk_gb", "Disk (GB)"),
    ]
    return {"vms": vms, "create_options": create_options}


METHODS = {
    "initialize": initialize,
    "settings.changed": settings_changed,
    "machine_provider.list": list_vms,
}


def send(message):
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


def main():
    for line in sys.stdin:
        if line.strip():
            handle(json.loads(line))


if __name__ == "__main__":
    main()

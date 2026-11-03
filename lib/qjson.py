#!/usr/bin/env python3
"""Tiny QMP / qemu-guest-agent client (no deps).

  qjson.py qmp <sock> <json>          one QMP command (handles greeting + capabilities)
  qjson.py qga <sock> <json>          one guest-agent command, prints the reply
  qjson.py exec <sock> <prog> [args]  guest-exec, waits, prints stdout/stderr, exits with guest exit code
"""
import base64, json, socket, sys, time


def connect(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(30)
    s.connect(path)
    return s, s.makefile("rw", encoding="utf-8")


def call(f, obj):
    f.write(json.dumps(obj) + "\n"); f.flush()
    while True:  # skip async events
        line = f.readline()
        if not line:
            raise SystemExit("connection closed")
        msg = json.loads(line)
        if "return" in msg or "error" in msg:
            if "error" in msg:
                raise SystemExit("error: " + msg["error"].get("desc", str(msg["error"])))
            return msg["return"]


def main():
    mode, path = sys.argv[1], sys.argv[2]
    s, f = connect(path)
    if mode == "qmp":
        f.readline()  # greeting
        call(f, {"execute": "qmp_capabilities"})
        print(json.dumps(call(f, json.loads(sys.argv[3]))))
    elif mode == "qga":
        print(json.dumps(call(f, json.loads(sys.argv[3])), indent=1))
    elif mode == "exec":
        pid = call(f, {"execute": "guest-exec", "arguments": {
            "path": sys.argv[3], "arg": sys.argv[4:], "capture-output": True}})["pid"]
        while True:
            st = call(f, {"execute": "guest-exec-status", "arguments": {"pid": pid}})
            if st.get("exited"):
                break
            time.sleep(0.2)
        sys.stdout.write(base64.b64decode(st.get("out-data", "")).decode("utf-8", "replace"))
        sys.stderr.write(base64.b64decode(st.get("err-data", "")).decode("utf-8", "replace"))
        sys.exit(st.get("exitcode", 0))


if __name__ == "__main__":
    main()

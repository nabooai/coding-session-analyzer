"""p50 and p95 of each plugin hook step against its target, on the python3 given (default: python3).
Uses a temp HOME and a local stub, so it touches no real config and no server. Exit 1 when a target is missed.

    python3 server/e2e/plugin_timing.py [path-to-python3] [runs]
"""
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSA = ROOT / "csa-plugin" / "scripts" / "csa.py"
HOOKS = json.loads((ROOT / "csa-plugin" / "hooks" / "hooks.json").read_text())["hooks"]
PYTHON = sys.argv[1] if len(sys.argv) > 1 else "python3"
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 25
TARGETS_MS = {"no config, shell guard": 10, "prompt rejected by the gate": 100, "recall, server time excluded": 250,
              "ship parent": 150, "sweep parent": 150}


class Stub(BaseHTTPRequestHandler):
    def _answer(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        data = json.dumps({"ok": True, "found": False, "archive_days": 0, "directives": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


def timed(argv, stdin, env):
    t0 = time.perf_counter()
    subprocess.run(argv, input=stdin, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return (time.perf_counter() - t0) * 1000


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server = "http://127.0.0.1:%d" % srv.server_address[1]
    results = {}
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HOME=home, CLAUDE_PLUGIN_ROOT=str(ROOT / "csa-plugin"))
        env.pop("CLAUDE_CONFIG_DIR", None)
        guard = HOOKS["UserPromptSubmit"][0]["hooks"][0]["command"]
        results["no config, shell guard"] = [timed(["sh", "-c", guard], b"{}", env) for _ in range(RUNS)]

        csa_dir = Path(home) / ".csa"
        csa_dir.mkdir(mode=0o700)
        (csa_dir / "config.json").write_text(json.dumps({
            "server": server, "identity": "t@x.test", "auth_token": "csa_t", "shipping": "plugin",
            "sweep_from": "2999-01-01T00:00:00Z", "consent": {"policy_version": 2}}))
        project = Path(home) / ".claude" / "projects" / "-p"
        project.mkdir(parents=True)
        path = project / "s.jsonl"
        path.write_text("hello\n")
        base = [PYTHON, "-I", "-S", "-B", str(CSA)]
        rejected = json.dumps({"prompt": "ok"}).encode()
        asks = json.dumps({"session_id": "s", "cwd": "/p", "prompt": "what did we decide about the release rollback plan"}).encode()
        end = json.dumps({"session_id": "s", "transcript_path": str(path)}).encode()
        results["prompt rejected by the gate"] = [timed(base + ["recall"], rejected, env) for _ in range(RUNS)]
        results["recall, server time excluded"] = [timed(base + ["recall"], asks, env) for _ in range(RUNS)]
        results["ship parent"] = [timed(base + ["ship"], end, env) for _ in range(RUNS)]
        results["sweep parent"] = [timed(base + ["sweep"], b'{"session_id":"s"}', env) for _ in range(RUNS)]
        time.sleep(1)                          # let the detached children finish before the temp HOME goes
    version = subprocess.run([PYTHON, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    print("%s on %s, %d runs" % (version, sys.platform, RUNS))
    missed = 0
    for name, samples in results.items():
        p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]
        ok = p95 <= TARGETS_MS[name]
        missed += not ok
        print("%-30s p50 %6.1f  p95 %6.1f  target %4d  %s" % (name, statistics.median(samples), p95, TARGETS_MS[name], "ok" if ok else "MISSED"))
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

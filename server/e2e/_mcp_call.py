"""Minimal MCP streamable-HTTP client for the org E2E harness — stdlib only, no mcp package needed.

    python3 _mcp_call.py <base_url> <bearer> <tool> <json_args> [extra_header:value ...]

Prints the tool's text result to stdout (or "HTTP <code>" / "ERROR …" and exits non-zero), so the harness
can grep it. Exists because the acceptance list requires replaying org-A session/fact ids through the MCP
endpoint class specifically — `ask`/`fact`/`flag` reach the corpus by a different path than the HTTP API
(mcp_server._org(ctx) vs apiserver's identify()), so testing only HTTP would leave that path unproven.
"""
import json
import sys
import urllib.error
import urllib.request


def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    for k, v in headers.items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=30)      # noqa: S310 (fixed http URL from the harness)


def _payload(raw):
    """The response is SSE ('data: {...}') or plain JSON — accept either."""
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def main():
    base, bearer, tool, args = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
    extra = {}
    for h in sys.argv[5:]:                              # spoof headers the gate must ignore, e.g. X-CSA-Org:orgA
        k, _, v = h.partition(":")
        extra[k.strip()] = v.strip()
    hdrs = dict(extra)
    if bearer:
        hdrs["Authorization"] = "Bearer " + bearer
    try:
        r = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                             "clientInfo": {"name": "org-e2e", "version": "1"}}}, hdrs)
    except urllib.error.HTTPError as e:
        print("HTTP %d" % e.code)                       # 401 is a RESULT here, not a failure of the harness
        sys.exit(0)
    sid = r.headers.get("mcp-session-id")
    _payload(r.read())
    if sid:
        hdrs["mcp-session-id"] = sid
    try:
        _post(base, {"jsonrpc": "2.0", "method": "notifications/initialized"}, hdrs).read()
    except Exception:
        pass                                            # some versions don't require it; the call below decides
    try:
        r = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": tool, "arguments": args}}, hdrs)
    except urllib.error.HTTPError as e:
        print("HTTP %d" % e.code)
        sys.exit(0)
    d = _payload(r.read())
    if "error" in d:
        print("ERROR " + json.dumps(d["error"])[:300])
        sys.exit(0)
    out = []
    for c in (d.get("result", {}).get("content") or []):
        if c.get("type") == "text":
            out.append(c.get("text", ""))
    print("\n".join(out) if out else json.dumps(d.get("result", {}))[:2000])


if __name__ == "__main__":
    main()

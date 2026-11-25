"""OTel-shaped tracing, stdlib only — no SDK, no analyzer imports (store.py imports THIS module to
stamp trace_id into queries/served/costs, so an import back would cycle).

One trace per request/ingest, built on a THREAD-LOCAL span stack: every entry point runs its whole
request on one thread (FastMCP executes sync tools inline on the event loop, the apiserver is
ThreadingHTTPServer, the ETL drains each org on one pool thread), so no context hops threads. Entry
points open their span with root=True, which HARD-RESETS the stack: the ETL pool reuses threads
across orgs, and a leaked span would otherwise cross-link one tenant's trace — and db file — into
another's forever.

Spans BUFFER in memory; the ROOT close hands the whole trace to a daemon worker thread that owns
BOTH sinks. Never write SQLite from the request thread: build_fts holds BEGIN IMMEDIATE for seconds
and busy_timeout is 15s — an inline write would stall the MCP event loop (freezing the entire
server, pings included). The worker writes each trace in ONE transaction, so a trace appears fully
or not at all: no partial/interleaved trees under concurrency. OTLP/HTTP protobuf (hand-encoded —
the spec-mandatory encoding; Phoenix 415s the optional JSON one) goes out via urllib only when the
otlp_endpoint setting is set — unset means zero network attempts, checked live each drain.

Tracer failures are COUNTED (stats()), never raised, never printed: stdout is the MCP JSON-RPC
channel. No logging config, no http debuglevel, no import-time side effects.

Attr names are a published contract — docs/traces-attrs.md. Do not rename.
"""
import atexit, contextlib, json, os, sqlite3, struct, threading, time, urllib.request, uuid

DB_DEFAULT = "data/analyzer.db"     # traces rooted without db= land here (single-user/default org)
_tls = threading.local()
_stats = {"db_failures": 0, "otlp_failures": 0, "otlp_sent": 0, "leaked_spans": 0}
_lock = threading.Lock()            # guards _q + worker start + _inflight transition
_q = []                             # finished traces awaiting the worker: (db_path, [span dicts])
_kick = threading.Event()
_inflight = threading.Event()       # set (under _lock) while the worker holds drained work
_worker = None
_last_prune = {}                    # db path -> time.monotonic() of last retention pass

_DDL = ("CREATE TABLE IF NOT EXISTS spans(trace_id TEXT, span_id TEXT, parent_id TEXT, name TEXT, "
        "kind TEXT, at TEXT, dur_ms INT, status TEXT, attrs TEXT); "
        "CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id); "
        "CREATE INDEX IF NOT EXISTS ix_spans_at ON spans(at)")   # retention DELETE keys on `at`


class _Handle:
    __slots__ = ("_rec",)
    def __init__(self, rec):
        self._rec = rec
    @property
    def trace_id(self):
        return self._rec["trace_id"]
    def set(self, **attrs):
        self._rec["attrs"].update((k, v) for k, v in attrs.items() if v is not None)


class _Nop:
    trace_id = None
    def set(self, **attrs):
        pass


_NOP = _Nop()


_on = True                          # disable() turns tracing off for this process


def disable():
    """No spans from this process: for scripts and tests that must not write into a real corpus."""
    global _on
    _on = False


def _enabled():
    return _on


def _endpoint():
    from analyzer import tenant                          # late: tenant -> store -> this module
    return tenant.deployment().otlp_endpoint


def _keep_days(corpus):
    from analyzer import tenant
    return tenant.get_config(corpus).trace_days if os.path.exists(corpus) else 30


def current_trace_id():
    """The active trace's id, or None. store.log_query/log_served/record_cost stamp this."""
    return _tls.trace["id"] if getattr(_tls, "stack", None) else None


def set_attr(**attrs):
    """Attach attrs to the INNERMOST open span (for deep code that doesn't hold the handle)."""
    stack = getattr(_tls, "stack", None)
    if stack:
        stack[-1]["attrs"].update((k, v) for k, v in attrs.items() if v is not None)


@contextlib.contextmanager
def span(name, kind, db=None, root=False, **attrs):
    """Open a span. root=True at every entry point: resets any leaked stack (counted) and binds the
    trace to `db` (the org's SQLite file). A non-root span with no active trace self-roots (CLI
    paths). Yields a handle with .set(**attrs)/.trace_id. Caller exceptions propagate with
    status=ERROR recorded; tracer's own failures never raise."""
    stack = getattr(_tls, "stack", None)
    if root and stack:
        _stats["leaked_spans"] += len(stack)             # reused pool thread with an abandoned trace:
        _tls.stack = _tls.trace = stack = None           # drop it — never cross-link orgs/requests. Clear the
        #                                                  TLS too: with tracing disabled the early-return below would
        #                                                  otherwise leave current_trace_id() stamping the dead id
    if not stack and not _enabled():
        yield _NOP
        return
    if not stack:
        _tls.trace = {"id": uuid.uuid4().hex, "db": db or DB_DEFAULT, "buf": []}
        _tls.stack = stack = []
    rec = {"trace_id": _tls.trace["id"], "span_id": uuid.uuid4().hex[:16],
           "parent_id": stack[-1]["span_id"] if stack else None, "name": name, "kind": kind,
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "start_ns": time.time_ns(),
           "t0": time.perf_counter(), "status": "OK",
           "attrs": {k: v for k, v in attrs.items() if v is not None}}
    stack.append(rec)
    try:
        yield _Handle(rec)
    except BaseException as e:
        rec["status"] = "ERROR"
        rec["attrs"].setdefault("error.message", str(e)[:300])
        raise
    finally:
        rec["dur_ms"] = int((time.perf_counter() - rec["t0"]) * 1000)
        rec["end_ns"] = time.time_ns()
        stack.pop()
        _tls.trace["buf"].append(rec)
        if not stack:
            trace, _tls.trace, _tls.stack = _tls.trace, None, None
            _finish(trace)


def capture():
    """Snapshot the active trace context for handoff to a worker thread (the cold-fill pool is the
    one pipeline spot that hops threads mid-trace). None when no trace is active."""
    stack = getattr(_tls, "stack", None)
    return {"id": _tls.trace["id"], "db": _tls.trace["db"], "parent": stack[-1]["span_id"]} if stack else None


@contextlib.contextmanager
def resume(ctx):
    """Adopt a captured context on another thread: spans opened inside join the SAME trace, parented
    under the capturing span, and flush as a fragment at exit (the origin thread owns the root).
    Without this, pool-thread LLM spans root their own orphan traces while record_cost — running on
    the submitting thread — stamps the ask's trace_id: two sinks disagreeing about one call."""
    if ctx is None or getattr(_tls, "stack", None):
        yield                                            # nothing to adopt / thread already tracing
        return
    _tls.trace = {"id": ctx["id"], "db": ctx["db"], "buf": []}
    _tls.stack = [{"span_id": ctx["parent"], "attrs": {}}]   # anchor: parents children, never emitted
    try:
        yield
    finally:
        trace, _tls.trace, _tls.stack = _tls.trace, None, None
        if trace["buf"]:
            _finish(trace)


def _finish(trace):
    """Root closed: hand the buffered trace to the worker. O(1) on the request thread."""
    with _lock:
        _q.append((trace["db"], trace["buf"]))
        _start_worker()
    _kick.set()


def _start_worker():                                     # call under _lock
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, name="csa-tracer", daemon=True)
        _worker.start()


def _loop():
    while True:
        _kick.wait(timeout=5)
        _kick.clear()
        with _lock:
            work, _q[:] = _q[:], []
            if work:
                _inflight.set()
        try:
            for db, buf in work:
                _write_db(db, buf)
            ep = _endpoint() if work else ""
            if ep:
                spans = [s for _, buf in work for s in buf]
                for i in range(0, len(spans), 100):      # OTLP contract: batches of ≤100 spans
                    _post(ep, spans[i:i + 100])
        except Exception:
            _stats["db_failures"] += 1                   # belt: nothing may kill the worker loop
        finally:
            _inflight.clear()


def _telemetry_path(db):
    """Spans go to the corpus's TELEMETRY sibling, never the corpus itself: one writer per SQLite file,
    and a trace write must never queue behind ingest (or make an `ask` queue behind a trace).
    Duplicates store.telemetry_path deliberately — store imports this module, so importing back would
    cycle. Same rule, two places; change both."""
    if db == ":memory:":
        return db
    return os.path.join(os.path.dirname(os.path.abspath(db)), "telemetry.db")


def _write_db(corpus, buf):
    db = _telemetry_path(corpus)
    rows = [(s["trace_id"], s["span_id"], s["parent_id"], s["name"], s["kind"], s["at"],
             s["dur_ms"], s["status"], json.dumps(s["attrs"])) for s in buf]
    try:
        con = sqlite3.connect(db, timeout=15)
        try:
            con.executescript(_DDL)                      # idempotent belt; the telemetry migration too
            con.executemany("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?)", rows)
            _prune(con, db, corpus)
            con.commit()
        finally:
            con.close()
    except Exception:
        _stats["db_failures"] += 1


def _prune(con, db, corpus):
    """Retention inside the trace-write transaction, at most hourly per db (the workspace's trace_days)."""
    now = time.monotonic()
    if now - _last_prune.get(db, 0) < 3600:
        return
    _last_prune[db] = now
    days = _keep_days(corpus) if corpus != ":memory:" else 30
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    con.execute("DELETE FROM spans WHERE at < ?", (cutoff,))


def _post(ep, spans):
    body = _otlp(spans)
    req = urllib.request.Request(ep + "/v1/traces", data=body, method="POST",
                                 headers={"Content-Type": "application/x-protobuf"})
    try:
        with urllib.request.urlopen(req, timeout=10):    # noqa: S310 (operator-pinned endpoint)
            pass
        _stats["otlp_sent"] += len(spans)
    except Exception:
        _stats["otlp_failures"] += 1                     # counted, batch dropped: no retry queue to grow


# --- OTLP protobuf, hand-encoded. The JSON encoding is OPTIONAL in the OTLP spec and Phoenix rejects
# it (415, verified live against 19.11.0); protobuf is the mandatory one every receiver implements.
# The message shape below is tiny and frozen (opentelemetry-proto v1), so ~40 lines of stdlib varint
# beats shipping an SDK or requiring a collector container in front of Phoenix.
def _pb_varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_len(tag, data):                                  # length-delimited field (wire type 2)
    return _pb_varint(tag << 3 | 2) + _pb_varint(len(data)) + data


def _pb_str(tag, s):
    return _pb_len(tag, s.encode())


def _pb_kv(key, v):                                      # KeyValue{key=1, value=AnyValue=2}
    if isinstance(v, bool):
        av = _pb_varint(2 << 3) + _pb_varint(1 if v else 0)          # AnyValue.bool_value=2
    elif isinstance(v, int) and -(2**63) <= v < 2**63:
        av = _pb_varint(3 << 3) + _pb_varint(v & 0xFFFFFFFFFFFFFFFF)  # AnyValue.int_value=3 (two's compl.)
    elif isinstance(v, float):
        av = _pb_varint(4 << 3 | 1) + struct.pack("<d", v)           # AnyValue.double_value=4 (fixed64)
    else:
        s = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
        av = _pb_str(1, s[:4000])                                    # AnyValue.string_value=1
    return _pb_str(1, key) + _pb_len(2, av)


def _pb_span(s):
    out = _pb_len(1, bytes.fromhex(s["trace_id"])) + _pb_len(2, bytes.fromhex(s["span_id"]))
    if s["parent_id"]:
        out += _pb_len(4, bytes.fromhex(s["parent_id"]))
    out += _pb_str(5, s["name"]) + _pb_varint(6 << 3) + _pb_varint(1)          # kind=SPAN_KIND_INTERNAL
    out += _pb_varint(7 << 3 | 1) + struct.pack("<Q", s["start_ns"])           # fixed64 nanos
    out += _pb_varint(8 << 3 | 1) + struct.pack("<Q", s["end_ns"])
    out += _pb_len(9, _pb_kv("openinference.span.kind", s["kind"]))            # role: Phoenix renders natively
    for k, v in s["attrs"].items():
        if v is not None:
            out += _pb_len(9, _pb_kv(k, v))
    code = 2 if s["status"] == "ERROR" else 1                                  # Status.code=3: OK=1 ERROR=2
    return out + _pb_len(15, _pb_varint(3 << 3) + _pb_varint(code))


def _otlp(spans):
    """ExportTraceServiceRequest{ResourceSpans{Resource{service.name}, ScopeSpans{scope, spans...}}}"""
    scope_spans = _pb_len(1, _pb_str(1, "csa.tracer")) + b"".join(_pb_len(2, _pb_span(s)) for s in spans)
    # Phoenix routes projects on openinference.project.name, NOT service.name (verified live 19.11.0:
    # service.name-only spans land in `default`).
    resource = _pb_len(1, _pb_kv("service.name", "csa")) \
        + _pb_len(1, _pb_kv("openinference.project.name", "csa"))
    return _pb_len(1, _pb_len(1, resource) + _pb_len(2, scope_spans))


def stats():
    return dict(_stats)


def flush(timeout=8.0):
    """Block until the worker drains (tests, atexit). Never raises. True = drained."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            idle = not _q and not _inflight.is_set()
        if idle:
            return True
        _kick.set()
        time.sleep(0.05)
    return False


atexit.register(flush, 2.0)          # stdio MCP / CLI drains exit fast — best-effort tail flush


if __name__ == "__main__":
    import http.server, io, statistics, sys, tempfile, contextlib as _ctx

    # A temp DIRECTORY, not mktemp in the shared /tmp: telemetry is one file per DIRECTORY (= per org
    # corpus dir), so two /tmp corpora would share /tmp/telemetry.db and this check would read a
    # previous run's spans.
    dbp = os.path.join(tempfile.mkdtemp(), "analyzer.db")
    _endpoint = lambda: ""                               # noqa: E731 (the worker reads this global)

    # 1) NEGATIVE: no endpoint -> zero outbound network attempts (urlopen booby-trapped).
    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network with no endpoint"))
    with span("ask", "CHAIN", db=dbp, root=True, **{"input.value": "q"}) as root:
        assert current_trace_id() == root.trace_id
        with span("route", "CHAIN") as r:
            r.set(**{"csa.route.scope": "team"})
        with span("retrieve.fts", "RETRIEVER") as rt:
            set_attr(**{"csa.row_count": 3})             # set_attr targets the innermost open span
    assert current_trace_id() is None, "trace must clear at root close"
    assert flush(), "worker did not drain (urlopen booby trap fired?)"
    urllib.request.urlopen = real_urlopen
    assert _stats["otlp_failures"] == 0 and _stats["otlp_sent"] == 0

    con = sqlite3.connect(_telemetry_path(dbp))   # spans land in the corpus's telemetry SIBLING
    rows = con.execute("SELECT trace_id, span_id, parent_id, name, status, attrs FROM spans").fetchall()
    assert len(rows) == 3 and len({r[0] for r in rows}) == 1, rows
    ids = {r[1] for r in rows}
    root_rows = [r for r in rows if r[2] is None]
    assert len(root_rows) == 1 and root_rows[0][3] == "ask"
    assert all(r[2] in ids for r in rows if r[2] is not None), "every parent_id must resolve"
    assert json.loads(next(r[5] for r in rows if r[3] == "retrieve.fts"))["csa.row_count"] == 3

    # 2) ERROR status recorded + caller exception propagates untouched.
    try:
        with span("boom", "CHAIN", db=dbp, root=True):
            raise ValueError("engine failed")
    except ValueError:
        pass
    else:
        raise SystemExit("FAIL: caller exception swallowed")
    flush()
    st, at = con.execute("SELECT status, attrs FROM spans WHERE name='boom'").fetchone()
    assert st == "ERROR" and "engine failed" in at

    # 3) disable(): no rows, nop handle.
    disable()
    with span("off", "CHAIN", db=dbp, root=True) as h:
        h.set(x=1)
        assert h.trace_id is None
    _on = True
    flush()
    assert not con.execute("SELECT 1 FROM spans WHERE name='off'").fetchone()

    # 4) LEAK RESET: a root on a thread with an abandoned stack drops it and counts it.
    _tls.stack = [{"span_id": "dead", "attrs": {}}]      # simulate a leaked span on a reused pool thread
    _tls.trace = {"id": "deadtrace", "db": dbp, "buf": []}
    with span("ask", "CHAIN", db=dbp, root=True):
        assert current_trace_id() != "deadtrace"
    flush()
    assert _stats["leaked_spans"] == 1
    assert not con.execute("SELECT 1 FROM spans WHERE trace_id='deadtrace'").fetchone()
    # 4b) same leak but tracing DISABLED: the TLS must still clear (else current_trace_id() stamps the
    # dead id into queries/costs forever) and the leak must not be re-counted on every root call.
    _tls.stack = [{"span_id": "dead2", "attrs": {}}]
    _tls.trace = {"id": "deadtrace2", "db": dbp, "buf": []}
    disable()
    with span("ask", "CHAIN", db=dbp, root=True):
        pass
    assert current_trace_id() is None, "disabled root must still clear a leaked trace"
    assert _stats["leaked_spans"] == 2
    with span("ask", "CHAIN", db=dbp, root=True):
        pass
    assert _stats["leaked_spans"] == 2, "an already-cleared leak must not re-count per root"
    _on = True

    # 5) STDOUT SILENCE: span storm with a DEAD endpoint -> nothing on stdout, nothing raised.
    _endpoint = lambda: "http://127.0.0.1:1"             # noqa: E731 (nothing listens on port 1)
    cap = io.StringIO()
    with _ctx.redirect_stdout(cap):
        for _ in range(5):
            with span("storm", "CHAIN", db=dbp, root=True):
                pass
        flush()
    assert cap.getvalue() == "", f"tracer printed to stdout: {cap.getvalue()!r}"
    assert _stats["otlp_failures"] >= 1, "dead endpoint must be counted"

    # 6) STUB OTLP SERVER: the protobuf payload decodes to a well-formed tree with typed attrs.
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers["Content-Type"] == "application/x-protobuf", self.headers["Content-Type"]
            got.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200); self.end_headers()
        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _endpoint = lambda: f"http://127.0.0.1:{srv.server_port}"   # noqa: E731
    with span("ask", "CHAIN", db=dbp, root=True):
        with span("llm", "LLM") as l:
            l.set(**{"llm.model_name": "sonnet", "csa.cost_usd": 0.01, "llm.token_count.prompt": 9})
    assert flush(), "export queue did not drain"
    srv.shutdown()
    _endpoint = lambda: ""                               # noqa: E731

    def _pb_iter(buf):                                   # minimal wire-format walker (self-check only)
        i = 0
        while i < len(buf):
            key = shift = 0
            while True:
                b = buf[i]; i += 1
                key |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            tag, wire = key >> 3, key & 7
            if wire == 0:
                v = shift = 0
                while True:
                    b = buf[i]; i += 1
                    v |= (b & 0x7F) << shift; shift += 7
                    if not b & 0x80:
                        break
            elif wire == 1:
                v, i = buf[i:i + 8], i + 8
            else:
                ln = shift = 0
                while True:
                    b = buf[i]; i += 1
                    ln |= (b & 0x7F) << shift; shift += 7
                    if not b & 0x80:
                        break
                v, i = buf[i:i + ln], i + ln
            yield tag, wire, v

    def _pb_get(buf, tag):
        return [v for t, _, v in _pb_iter(buf) if t == tag]

    rs = _pb_get(got[0], 1)[0]                           # ResourceSpans
    res_keys = {_pb_get(kv, 1)[0].decode() for kv in _pb_get(_pb_get(rs, 1)[0], 1)}
    assert res_keys == {"service.name", "openinference.project.name"}, res_keys  # Phoenix project routing
    ss = _pb_get(rs, 2)[0]                               # ScopeSpans
    pspans = _pb_get(ss, 2)                              # repeated Span
    assert len(pspans) == 2, f"expected 2 spans in payload, got {len(pspans)}"
    by_name = {_pb_get(s, 5)[0].decode(): s for s in pspans}
    assert set(by_name) == {"ask", "llm"}, set(by_name)
    assert _pb_get(by_name["llm"], 4)[0] == _pb_get(by_name["ask"], 2)[0], "llm's parent must be ask's span_id"
    assert len(_pb_get(by_name["ask"], 1)[0]) == 16 and len(_pb_get(by_name["ask"], 2)[0]) == 8
    kv = {}
    for a in _pb_get(by_name["llm"], 9):                 # attributes: KeyValue{key=1, AnyValue=2}
        val = _pb_get(a, 2)[0]
        sv, dv, iv = _pb_get(val, 1), _pb_get(val, 4), _pb_get(val, 3)
        kv[_pb_get(a, 1)[0].decode()] = (sv[0].decode() if sv else
                                         struct.unpack("<d", dv[0])[0] if dv else iv[0])
    assert kv["openinference.span.kind"] == "LLM" and kv["csa.cost_usd"] == 0.01 \
        and kv["llm.token_count.prompt"] == 9 and kv["llm.model_name"] == "sonnet", kv

    # 6b) capture/resume: a pool-thread span joins the SAME trace, parented under the capturing span.
    con.execute("DELETE FROM spans"); con.commit()
    with span("ask", "CHAIN", db=dbp, root=True):
        with span("recall", "CHAIN"):
            ctx = capture()

            def pool_job():
                with resume(ctx):
                    with span("extract", "CHAIN"):
                        pass
            t = threading.Thread(target=pool_job)
            t.start(); t.join()
    flush()
    rows = con.execute("SELECT name, trace_id, parent_id FROM spans").fetchall()
    assert len(rows) == 3 and len({r[1] for r in rows}) == 1, f"fragment must join the trace: {rows}"
    rec_sid = con.execute("SELECT span_id FROM spans WHERE name='recall'").fetchone()[0]
    assert con.execute("SELECT parent_id FROM spans WHERE name='extract'").fetchone()[0] == rec_sid, \
        "worker span must parent under the capturing span"
    assert resume(None).__enter__() is None or True      # ctx=None is a no-op (smoke)

    # 7) CONCURRENCY: 20 threads -> 20 disjoint well-formed trees, no cross-linked parents.
    con.execute("DELETE FROM spans"); con.commit()
    def one(i):
        with span("ask", "CHAIN", db=dbp, root=True):
            with span("route", "CHAIN"):
                with span("llm", "LLM"):
                    time.sleep(0.001)
            with span("rerank", "RERANKER"):
                pass
    ts = [threading.Thread(target=one, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert flush()
    rows = con.execute("SELECT trace_id, span_id, parent_id FROM spans").fetchall()
    assert len(rows) == 80, len(rows)
    by_trace = {}
    for tid, sid, pid in rows:
        by_trace.setdefault(tid, {})[sid] = pid
    assert len(by_trace) == 20, f"expected 20 disjoint traces, got {len(by_trace)}"
    for tid, spans_ in by_trace.items():
        assert len(spans_) == 4
        assert len([s for s, p in spans_.items() if p is None]) == 1, "one root per trace"
        assert all(p in spans_ for p in spans_.values() if p is not None), f"cross-linked parent in {tid}"

    # 8) MICROBENCH: per-span overhead p50 < 5ms (buffer append — no DB on this thread).
    lat = []
    with span("bench-root", "CHAIN", db=dbp, root=True):
        for _ in range(500):
            t0 = time.perf_counter()
            with span("child", "CHAIN"):
                pass
            lat.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(lat)
    assert p50 < 5, f"per-span p50 {p50:.3f}ms >= 5ms"

    # 9) Retention: old rows pruned on the next trace write.
    flush()
    con.execute("INSERT INTO spans VALUES ('old','o1',NULL,'x','CHAIN','2000-01-01T00:00:00Z',1,'OK','{}')")
    con.commit()
    _last_prune.clear()
    with span("fresh", "CHAIN", db=dbp, root=True):
        pass
    flush()
    assert not con.execute("SELECT 1 FROM spans WHERE trace_id='old'").fetchone(), "prune must fire"

    # 10) db failure counted, never raised (unwritable path).
    with span("nowhere", "CHAIN", db="/nonexistent-dir/x.db", root=True):
        pass
    flush()
    assert _stats["db_failures"] >= 1
    os.remove(_telemetry_path(dbp))            # the corpus file itself is never created — spans only
    print(f"stats {stats()}", file=sys.stderr)
    print("OK tracer: tree write + parents resolve + ERROR propagates + kill switch + leak reset + "
          f"stdout silent + no-endpoint zero network + OTLP typed tree + 20-thread disjoint trees + "
          f"p50 {p50:.3f}ms + prune + fail counters")

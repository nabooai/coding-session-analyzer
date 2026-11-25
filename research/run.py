"""Referee for fact-core v2: replays deterministic pipeline checks against a FROZEN snapshot.

    python3 research/run.py                    # latest snapshot, all cases, no network, no LLM
    python3 research/run.py --only case1-...   # one case (a sibling task's inner loop)
    python3 research/run.py --selfcheck        # no snapshot needed: fixture corpus + referee-of-the-referee
    python3 research/run.py --otlp             # ALSO push spans/scores to Phoenix (only network path)
    python3 research/run.py --measure-sweep    # $$ projection for the retro contradiction sweep (real LLM)

Exit codes: 0 green (runnable checks pass, xfails fail as expected) · 1 a check failed, an xfail
unexpectedly PASSED, or an evaluator crashed · 2 environment/usage error (no snapshot, snapshot lacks
a case's material, path outside research/corpus/) — so a sibling session can tell "my feature is
wrong" from "my snapshot is wrong".

Safety, by construction not by convention:
- Only DB paths under research/corpus/ are accepted; the frozen file is never opened read-write.
  All checks run on a per-run scratch COPY, with cwd pinned inside the scratch dir so analyzer's
  cwd-relative side channels (store.org_db() -> data/analyzer.db, data/pending/...) cannot resolve to a
  real corpus. Never imports mcp_server (its import seeds a db at cwd).
- A bracketing guard records the live server db (server/data/analyzer.db) before the run and fails
  the run if it appeared or changed.
- eval_recall.py stays the question-routing gate; this suite is pipeline correctness beside it.
"""
import argparse, hashlib, json, os, re, shutil, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server")
sys.path.insert(0, HERE)
sys.path.insert(0, SERVER)

import cases as cases_mod                     # noqa: E402
from analyzer import store, verify            # noqa: E402  (no db opened at import; verify's only
#                                                network seam is gh, never reached without a
#                                                github:<org> token in the scratch tenant_config)

CORPUS = os.path.join(HERE, "corpus")
RESULTS = os.path.join(HERE, "results")
K = 5
ASKER = "mara@example.com"
# Override to feed a remote Phoenix (e.g. the lab instance through an SSH tunnel:
#   ssh -L 16006:localhost:6006 <phoenix-host>
#   CSA_PHOENIX_BASE=http://localhost:16006 python3 research/run.py --otlp
PHOENIX = os.environ.get("CSA_PHOENIX_BASE", "http://localhost:6006")


class UsageError(Exception):
    """Environment/usage problem (exit 2) — never a case verdict."""


# ---------------------------------------------------------------- snapshots
def manifest_files(snapdir):
    """[(sha256, relpath)] from MANIFEST; comment lines carry provenance."""
    out = []
    with open(os.path.join(snapdir, "MANIFEST")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                sha, rel = line.split(None, 1)
                out.append((sha, rel))
    return out


def verify_snapshot(snapdir):
    """Re-hash every MANIFEST entry. Returns [] when intact — the runner's day-one green floor.
    Also rejects symlinks/escapes (a symlinked analyzer.db once walked the guard straight to the live
    corpus) and requires analyzer.db to be LISTED (deleting its MANIFEST line must not pass)."""
    errs = []
    entries = manifest_files(snapdir)
    if "analyzer.db" not in {rel for _sha, rel in entries}:
        errs.append("MANIFEST does not list analyzer.db — not a complete snapshot")
    real_dir = os.path.realpath(snapdir)
    for sha, rel in entries:
        p = os.path.join(snapdir, rel)
        if os.path.islink(p) or not os.path.realpath(p).startswith(real_dir + os.sep):
            errs.append(f"{rel} is a symlink or escapes the snapshot dir — refusing")
            continue
        if not os.path.exists(p):
            errs.append(f"missing file: {rel}")
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != sha:
            errs.append(f"hash mismatch: {rel} (frozen snapshot was modified?)")
    return errs


def latest_snapshot():
    if os.path.isdir(CORPUS):
        snaps = [d for d in os.listdir(CORPUS)
                 if os.path.exists(os.path.join(CORPUS, d, "MANIFEST"))]
        if snaps:                                  # by freeze time, not name: '-9' lexically beats '-10'
            snaps.sort(key=lambda d: os.path.getmtime(os.path.join(CORPUS, d, "MANIFEST")))
            return os.path.join(CORPUS, snaps[-1])
    raise UsageError("no snapshot found under research/corpus/ — create one first:\n"
                     f"  sh research/snapshot.sh {os.path.join(SERVER, 'data', 'analyzer.db')}\n"
                     "(--selfcheck runs without any snapshot)")


def guard_snapshot_dir(snapdir):
    """The prod-DB refusal: only research/corpus/<name> is a legal snapshot dir."""
    real = os.path.realpath(snapdir)
    if os.path.dirname(real) != os.path.realpath(CORPUS):
        raise UsageError(f"refusing: {snapdir} is not under research/corpus/ — this runner never "
                         "touches a live corpus (store.org_db() resolves data/ against the cwd; "
                         "pointing tools at prod is how the real corpus got seeded once already)")
    if not os.path.exists(os.path.join(real, "MANIFEST")):
        raise UsageError(f"{snapdir} has no MANIFEST — not a frozen snapshot")
    return real


def _main_checkout():
    """The primary checkout's root when running from a git worktree (where <REPO>/server/data never
    exists and the bracket guard would otherwise watch nothing)."""
    import subprocess
    try:
        common = subprocess.run(["git", "-C", REPO, "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True,
                                timeout=5).stdout.strip()
        if common.endswith(os.sep + ".git"):
            return os.path.dirname(common)
    except Exception:
        pass
    return None


def _live_db_state():
    paths = [os.path.join(SERVER, "data", "analyzer.db"),
             os.path.join(os.getcwd(), "data", "analyzer.db")]
    main = _main_checkout()
    if main:
        paths.append(os.path.join(main, "server", "data", "analyzer.db"))
    out = {}
    for p in paths:
        st = os.stat(p) if os.path.exists(p) else None
        out[p] = (st.st_size, st.st_mtime_ns) if st else None
    return out


# ---------------------------------------------------------------- evaluators
def _eval_check(con, ch):
    """-> (outcome, detail): outcome in pass|fail|missing. Raises on unexpected errors (caller reds them)."""
    kind = ch["kind"]
    try:
        if kind == "exists":
            got = con.execute(ch["sql"]).fetchone()[0] or 0
            want = ch.get("min", 1)
            return ("pass" if got >= want else "fail", f"got {got}, want >= {want}")
        if kind == "route":
            from analyzer import route, resolve
            rt = route.classify(con, ch["q"], asked_by=ASKER, eids=resolve.resolve(con, ch["q"]))
            got = [rt.scope, rt.shape]
            return ("pass" if got == list(ch["want"]) else "fail",
                    f"got {got[0]}/{got[1]}, want {ch['want'][0]}/{ch['want'][1]}")
        if kind == "recall":
            from analyzer import search
            hits = search.search(con, ch["q"], k=K)
            for i, h in enumerate(hits, 1):
                row = con.execute("SELECT text FROM docs WHERE doc_id=?", (h["doc_id"],)).fetchone()
                text = row[0] if row else h.get("snippet", "")
                for prefix, rx in ch["gold"]:
                    if (h.get("session_id") or "").startswith(prefix) and re.search(rx, text):
                        return "pass", f"hit at rank {i} ({prefix})"
            return "fail", f"no gold in top-{K} ({len(hits)} hits)"
        if kind == "pr_card":
            from analyzer import resolve
            eids = resolve.resolve(con, ch["q"])
            text = resolve.cards(con, eids, ch["q"]) or ""     # markdown entity card(s)
            for line in text.splitlines():
                if ch["want_session"] in line:
                    return "pass", f"card line: {line.strip()[:90]!r}"
            return "fail", f"card text never names session {ch['want_session']} ({len(text)} chars)"
        raise UsageError(f"unknown check kind {kind}")
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "no such table" in msg or "no such column" in msg:
            return "missing", f"MISSING: {msg}"
        raise
    except (ImportError, AttributeError) as e:
        return "missing", f"MISSING: {e}"


def _check_status(ch, outcome, detail):
    """Fold an outcome and the check's xfail contract into a final status."""
    xf = ch.get("xfail")
    if not xf:
        return ("pass" if outcome == "pass" else "fail"), detail
    if outcome == "missing" or (outcome == "fail" and xf["expect"] in detail):
        return "xfail", f"{detail} — expected to fail until {xf['owner']}"
    if outcome == "pass":
        return "xpass", (f"unexpectedly PASSED. If you just landed {xf['owner']}, delete this check's "
                         "xfail field in research/cases.py and re-run; otherwise the evaluator may be "
                         "vacuous — check --selfcheck")
    return "error", f"xfail failed for an UNEXPECTED reason (pinned: {xf['expect']!r}): {detail}"


# ---------------------------------------------------------------- the suite
def _insert(con, table, row):
    cols = ",".join(row)
    con.execute(f"INSERT OR REPLACE INTO {table}({cols}) VALUES({','.join('?' * len(row))})",
                tuple(row.values()))


def run_suite(case_list, snapdir, only=None, log=print, spans=None):
    """-> (exit_code, results dict). Copies the frozen db to a scratch dir, seeds it, runs every check."""
    snapdir = guard_snapshot_dir(snapdir)
    integrity = verify_snapshot(snapdir)
    if integrity:
        raise UsageError("snapshot integrity: " + "; ".join(integrity))
    selected = [c for c in case_list if only in (None, c["id"])]
    if not selected:
        raise UsageError(f"no case named {only!r} — ids: {[c['id'] for c in case_list]}")

    live_before = _live_db_state()
    scratch = tempfile.mkdtemp(prefix="csa-golden-")
    prev_cwd = os.getcwd()
    results = {"snapshot": os.path.basename(snapdir), "cases": []}
    t_run0 = time.time_ns()
    try:
        os.chdir(scratch)                          # cwd-relative side channels land here, nowhere real
        work = os.path.join(scratch, "work.db")
        shutil.copy(os.path.join(snapdir, "analyzer.db"), work)
        os.chmod(work, 0o644)                      # the frozen file is 444; the copy must be writable
        con = store.sqlite_db(work)                # migrations/WAL hit the scratch copy only
        for c in selected:
            for table, row in c.get("seed", []):
                _insert(con, table, row)
        con.commit()
        # Replay the durable pipeline stages on the seeded scratch — the "replays pipeline stages"
        # half of this runner's contract: S2 refresh_facts (seeded mentions get their facts rows,
        # exactly what load_sqlite does per session) then S3 verify. The verify replay is SCOPED to
        # the seeded refs (the ingest-path shape): case 5 arms registry_refute over the shared
        # scratch, and an unscoped sweep would let its one-email roster silently refute matching
        # REAL-snapshot facts — a cross-case bleed the determinism selfcheck can't see.
        seeded_refs = {row["ref"] for c in selected for table, row in c.get("seed", [])
                       if table == "hidden_context" and row.get("ref")}
        store.refresh_facts(con)
        verify.verify_refs(con, refs=seeded_refs)
        store.build_fts(con)                       # recall checks read the seeded material through docs
        con.commit()

        lack = [(c["id"], p) for c in selected for p in c["needs"]
                if not con.execute("SELECT 1 FROM sessions WHERE session_id LIKE ?||'%' LIMIT 1",
                                   (p,)).fetchone()]
        if lack:
            raise UsageError("snapshot lacks case material — re-snapshot including these sessions "
                             "(research/sessions.txt): " + ", ".join(f"{c}:{p}" for c, p in lack))

        bad = 0
        for c in selected:
            rec = {"id": c["id"], "title": c["title"], "checks": []}
            for ch in c["checks"]:
                t0 = time.time_ns()
                try:
                    outcome, detail = _eval_check(con, ch)
                    status, detail = _check_status(ch, outcome, detail)
                except UsageError:
                    raise
                except Exception as e:            # evaluator crash: red, never silently xfail
                    status, detail = "error", f"{type(e).__name__}: {e}"
                rec["checks"].append({"name": ch["name"], "status": status, "detail": detail,
                                      "owner": (ch.get("xfail") or {}).get("owner")})
                if spans is not None:
                    spans.append({"name": f"{c['id']}/{ch['name']}", "kind": ch["kind"],
                                  "case": c["id"], "status": status, "detail": detail,
                                  "input": ch.get("q") or ch.get("sql", ""),
                                  "start_ns": t0, "end_ns": time.time_ns()})
                mark = {"pass": "PASS ", "fail": "FAIL ", "xfail": "XFAIL", "xpass": "XPASS",
                        "error": "ERROR"}[status]
                log(f"{mark}  {c['id']} · {ch['name']} — {detail}")
                if status in ("fail", "xpass", "error"):
                    bad = 1
            order = {"error": 4, "xpass": 3, "fail": 2, "xfail": 1, "pass": 0}
            rec["status"] = max((k["status"] for k in rec["checks"]), key=order.get)
            results["cases"].append(rec)
        con.close()
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(scratch, ignore_errors=True)

    live_after = _live_db_state()
    if live_after != live_before:
        log("FATAL: a live data/analyzer.db appeared or changed during this run — the sandbox leaked")
        return 1, results
    n = {s: sum(1 for c in results["cases"] for k in c["checks"] if k["status"] == s)
         for s in ("pass", "fail", "xfail", "xpass", "error")}
    results["summary"] = n
    results["duration_ms"] = (time.time_ns() - t_run0) // 1_000_000
    log(f"\n{n['pass']} pass · {n['xfail']} xfail (expected until their owner task lands) · "
        f"{n['fail']} fail · {n['xpass']} xpass · {n['error']} error → exit {bad}")
    return bad, results


def write_results(results, tag="run"):
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{tag}-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}.json")
    with open(path, "w") as f:
        json.dump(dict(results, ran_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       git_sha=_git_sha()), f, indent=1)
    return path


def _git_sha():
    import subprocess
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- Phoenix (--otlp only; zero sockets otherwise)
# Minimal proto3 wire-format encoder for OTLP ExportTraceServiceRequest: Phoenix's /v1/traces is
# protobuf-only (JSON => 415, verified live), and the repo rule is stdlib-only — no otel SDK.
def _pb_varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_ld(field, payload):                    # length-delimited: strings, bytes, sub-messages
    return _pb_varint(field << 3 | 2) + _pb_varint(len(payload)) + payload


def _pb_f64(field, val):                       # fixed64: the time fields
    import struct
    return _pb_varint(field << 3 | 1) + struct.pack("<Q", val)


def _pb_attr(key, val):                        # KeyValue{key=1, value=2:AnyValue{string_value=1}}
    return _pb_ld(1, key.encode()) + _pb_ld(2, _pb_ld(1, str(val).encode()))


def _pb_span(trace_id, span_id, parent_id, name, start_ns, end_ns, attrs, ok=True, msg=""):
    body = (_pb_ld(1, bytes.fromhex(trace_id)) + _pb_ld(2, bytes.fromhex(span_id))
            + (_pb_ld(4, bytes.fromhex(parent_id)) if parent_id else b"")
            + _pb_ld(5, name.encode()) + _pb_varint(6 << 3 | 0) + _pb_varint(1)   # kind=INTERNAL
            + _pb_f64(7, start_ns) + _pb_f64(8, end_ns))
    for k, v in attrs:
        body += _pb_ld(9, _pb_attr(k, v))
    status = (_pb_ld(2, msg.encode()) if msg else b"")                            # Status.message
    body += _pb_ld(15, status + _pb_varint(3 << 3 | 0) + _pb_varint(1 if ok else 2))  # Status.code
    return body


def _otlp_spans(spans, results):
    """-> (http_status, trace_id). Child span ids mix in the trace_id: Phoenix enforces GLOBAL span-id
    uniqueness and silently drops duplicates, so deterministic ids made every run after the first
    render as a childless root (found by expert review, verified live)."""
    import urllib.request
    trace_id = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:32]
    root_id = hashlib.sha256(b"root" + trace_id.encode()).hexdigest()[:16]
    root_start = min(s["start_ns"] for s in spans) if spans else time.time_ns()
    root_end = max(s["end_ns"] for s in spans) if spans else time.time_ns()
    run_ok = not any(results.get("summary", {}).get(k) for k in ("fail", "xpass", "error"))
    session = f"golden-{results.get('snapshot', '?')}-{time.strftime('%Y%m%dT%H%M%S')}"
    kind_map = {"recall": "RETRIEVER", "pr_card": "TOOL", "route": "CHAIN", "exists": "CHAIN"}
    pb_spans = [_pb_span(trace_id, root_id, None, "golden-run", root_start, root_end,
                         [("openinference.span.kind", "CHAIN"), ("session.id", session),
                          ("snapshot", results.get("snapshot", "?")),
                          ("summary", json.dumps(results.get("summary", {})))],
                         ok=run_ok)]
    for i, s in enumerate(spans):
        ok = s["status"] in ("pass", "xfail")
        pb_spans.append(_pb_span(
            trace_id, hashlib.sha256(f"{trace_id}{i}{s['name']}".encode()).hexdigest()[:16], root_id,
            s["name"], s["start_ns"], s["end_ns"],
            [("openinference.span.kind", kind_map[s["kind"]]), ("case", s["case"]),
             ("check.status", s["status"]), ("session.id", session),
             ("input.value", s.get("input", "")), ("output.value", s["detail"])],
            ok=ok, msg="" if ok else s["detail"]))
    scope_spans = _pb_ld(1, _pb_ld(1, b"research/run.py")) + b"".join(_pb_ld(2, p) for p in pb_spans)
    resource = _pb_ld(1, _pb_attr("service.name", "csa-research")) \
        + _pb_ld(1, _pb_attr("openinference.project.name", "csa-research"))   # what Phoenix routes on
    body = _pb_ld(1, _pb_ld(1, resource) + _pb_ld(2, scope_spans))
    req = urllib.request.Request(f"{PHOENIX}/v1/traces", data=body, method="POST",
                                 headers={"Content-Type": "application/x-protobuf"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, trace_id


def _phoenix_experiment(results, trace_id=None):
    """Dataset + experiment + per-case RUNS + per-check CODE evaluations via Phoenix's REST API
    (stdlib urllib, no client lib — run_experiment is client-side orchestration over these same
    endpoints anyway). Phoenix semantics: dataset outputs = the GROUND-TRUTH contract (from cases.py,
    stable example_ids, action=update — never a per-run append that duplicates examples); observed
    results = experiment runs; per-check pass/fail = named CODE evaluations, so each check is an
    evaluator column and runs compare side by side. Returns a status string."""
    import urllib.error, urllib.request

    def post(path, payload):
        req = urllib.request.Request(f"{PHOENIX}{path}", data=json.dumps(payload).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def get(path):
        with urllib.request.urlopen(f"{PHOENIX}{path}", timeout=10) as r:
            return json.loads(r.read() or b"{}")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = {
        "action": "create", "name": "csa-golden-cases",
        "inputs": [{"case": c["id"], "title": c["title"]} for c in cases_mod.CASES],
        "outputs": [{"expected": {ch["name"]: (f"xfail until {ch['xfail']['owner']}"
                                               if ch.get("xfail") else "pass")
                                  for ch in c["checks"]}} for c in cases_mod.CASES],
        "example_ids": [c["id"] for c in cases_mod.CASES],
    }
    try:
        ds = post("/v1/datasets/upload?sync=true", payload)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
        ds = post("/v1/datasets/upload?sync=true", dict(payload, action="update"))
    d = ds.get("data") or {}
    ds_id, ver = d.get("dataset_id"), d.get("version_id")
    if not ds_id:
        return f"dataset upload gave no id ({str(ds)[:120]})"
    ex = post(f"/v1/datasets/{ds_id}/experiments", {
        "name": f"golden-{results.get('snapshot', '?')}-{_git_sha()}-{time.strftime('%Y%m%dT%H%M%S')}",
        "version_id": ver, "repetitions": 1,
        "metadata": {"snapshot": results.get("snapshot", "?"), "git_sha": _git_sha(),
                     "summary": results.get("summary", {})}})
    ex_id = (ex.get("data") or {}).get("id")
    if not ex_id:
        return f"dataset {ds_id} uploaded; experiment create refused ({str(ex)[:120]})"
    examples = (get(f"/v1/datasets/{ds_id}/examples").get("data") or {}).get("examples") or []
    # runs want the GLOBAL example id (node_id) — the public example_id 500s (verified live)
    by_case = {(e.get("input") or {}).get("case"): e.get("node_id") or e["id"] for e in examples}
    runs = evals = 0
    for c in results["cases"]:
        eg_id = by_case.get(c["id"])
        if not eg_id:
            continue
        payload_run = {
            "dataset_example_id": eg_id, "repetition_number": 1,
            "output": {"status": c["status"],
                       "checks": [{"name": k["name"], "status": k["status"]} for k in c["checks"]]},
            "start_time": now, "end_time": now}
        if trace_id:
            payload_run["trace_id"] = trace_id
        run = post(f"/v1/experiments/{ex_id}/runs", payload_run)
        run_id = (run.get("data") or {}).get("id")
        if not run_id:
            continue
        runs += 1
        for k in c["checks"]:
            post("/v1/experiment_evaluations", {
                "experiment_run_id": run_id, "name": k["name"], "annotator_kind": "CODE",
                "start_time": now, "end_time": now,
                "result": {"label": k["status"],
                           "score": 1.0 if k["status"] in ("pass", "xfail") else 0.0,
                           "explanation": k["detail"][:500]}})
            evals += 1
    return (f"dataset {ds_id} ({len(cases_mod.CASES)} examples, ground truth) + experiment {ex_id}: "
            f"{runs} runs, {evals} evaluations in Phoenix")


# ---------------------------------------------------------------- --measure-sweep
def measure_sweep(snapdir, sample, prompt_file, model="haiku", log=print):
    """Cost the S4-style retro contradiction sweep on the FROZEN snapshot (read-only), project prod $$."""
    from analyzer import llm
    snapdir = guard_snapshot_dir(snapdir)
    integrity = verify_snapshot(snapdir)           # the SPENDING path verifies too — a tampered
    if integrity:                                  # snapshot must not produce a citable $ projection
        raise UsageError("snapshot integrity: " + "; ".join(integrity))
    if not os.path.exists(prompt_file):
        raise UsageError(f"prompt file not found: {prompt_file}")
    # immutable=1: the file is chmod-444 and hash-verified, and older snapshots carry a WAL header
    # that plain mode=ro cannot open from a read-only dir (SQLite would need to create the -shm)
    con = sqlite3.connect(f"file:{os.path.join(snapdir, 'analyzer.db')}?mode=ro&immutable=1", uri=True)
    total = con.execute("SELECT COUNT(DISTINCT ref) FROM hidden_context "
                        "WHERE type IN ('decision','constraint')").fetchone()[0]
    rows = con.execute(
        "SELECT ref, MIN(claim), MIN(session_id) FROM hidden_context "
        "WHERE type IN ('decision','constraint') GROUP BY ref ORDER BY ref LIMIT ?",
        (sample,)).fetchall()
    with open(prompt_file) as f:
        template = f.read()
    summary = llm.provider_summary()
    if summary.startswith("NONE"):
        # known false negative: a keychain-logged-in claude CLI is invisible to the env-var checks
        summary = "NONE in env (claude CLI keychain login not visible here — proceeding; calls fail loudly if truly unconfigured)"
    log(f"provider: {summary}")
    log(f"prompt: {prompt_file} (PROVISIONAL — resolution-engine re-measures with the real "
        f"contradict.py prompt before --yes-spend)")
    log(f"sampling {len(rows)} of {total} decision/constraint facts, model={model}\n")
    costs, cand_sizes = [], []
    for ref, claim, sid in rows:
        cands = con.execute(
            "SELECT DISTINCT h.ref, h.claim FROM hidden_context h "
            "JOIN mentions m ON m.session_id=h.session_id "
            "WHERE m.entity_id IN (SELECT entity_id FROM mentions WHERE session_id=?) "
            "AND h.type IN ('decision','constraint') AND h.ref!=? LIMIT 20", (sid, ref)).fetchall()
        if len(cands) < 20:                        # top up from FTS same-type, S4's second candidate arm
            terms = " OR ".join(f'"{t}"' for t in re.findall(r"[a-zA-Z]{4,}", claim)[:8])
            if terms:
                try:
                    cands += con.execute(
                        "SELECT doc_id, text FROM docs WHERE docs MATCH ? AND etype IN "
                        "('decision','constraint') LIMIT ?", (terms, 20 - len(cands))).fetchall()
                except sqlite3.OperationalError:
                    pass                           # unlucky token for FTS syntax: entity arm stands alone
        cand_sizes.append(len(cands))
        numbered = "\n".join(f"{i}. [{r}] {c[:300]}" for i, (r, c) in enumerate(cands, 1)) or "(none)"
        prompt = template.format(claim=claim[:500], candidates=numbered)
        _, cost, usage = llm.complete(prompt, model=model)
        costs.append(cost)                         # None = provider reported no cost (openai path)
        log(f"  {ref}: {'$%.4f' % cost if cost is not None else 'cost UNKNOWN'} ({len(cands)} candidates)")
    con.close()
    unknown = sum(1 for c in costs if c is None)
    costs = [c for c in costs if c is not None]
    if not costs:
        raise UsageError("the provider reported no cost_usd for any call (openai path returns None) — "
                         "cannot produce a $ projection; run on the claude path")
    costs_sorted = sorted(costs)
    med = costs_sorted[len(costs) // 2]
    p90 = costs_sorted[min(len(costs) - 1, int(len(costs) * 0.9))]
    report = {
        "sample_n": len(costs), "cost_unknown_n": unknown, "population": total, "model": model,
        "prompt_file": os.path.relpath(prompt_file, REPO),
        "per_fact_usd": {"mean": sum(costs) / len(costs), "median": med, "p90": p90},
        "candidates": {"mean": sum(cand_sizes) / len(cand_sizes), "max": max(cand_sizes)},
        "projection_usd": {"median_based": med * total, "p90_ceiling": p90 * total},
        "snapshot": os.path.basename(snapdir),
    }
    path = write_results(report, tag="sweep")
    log(f"\nprojection for the retro sweep over {total} facts:")
    log(f"  median-based: ${report['projection_usd']['median_based']:.2f}   "
        f"p90 ceiling: ${report['projection_usd']['p90_ceiling']:.2f}")
    log(f"  per-fact: mean ${report['per_fact_usd']['mean']:.4f}, median ${med:.4f}, p90 ${p90:.4f} "
        f"(sample {len(costs)}/{total}, candidates mean {report['candidates']['mean']:.0f})")
    log(f"report: {path}  (citable at rollout-step-3 approval; nothing was written to any corpus)")
    return 0


# ---------------------------------------------------------------- --selfcheck
def _fixture_snapshot(root):
    """A tiny synthetic corpus shaped like a real snapshot; the cases seed the rest."""
    snapdir = os.path.join(root, "fixture")
    os.makedirs(snapdir)
    db = os.path.join(snapdir, "analyzer.db")
    con = store.sqlite_db(db)
    con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,user,summary) "
                "VALUES('fixture-session','ledger','2026-07-20T10:00:00Z','2026-07-20T10:00:00Z',"
                "'mara@example.com','fixture')")
    store.build_fts(con)
    con.commit()
    con.close()
    for ext in ("-wal", "-shm"):                     # leave a clean single file, like the backup API does
        p = db + ext
        if os.path.exists(p):
            sqlite3.connect(db).execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            break
    h = hashlib.sha256(open(db, "rb").read()).hexdigest()
    with open(os.path.join(snapdir, "MANIFEST"), "w") as f:
        f.write(f"# fixture snapshot for --selfcheck\n{h}  analyzer.db\n")
    os.chmod(db, 0o444)
    return snapdir


def selfcheck():
    """The referee referees itself: fixture green, every flipped expectation reds, XPASS reds,
    prod paths refused, no sockets opened. Runs with no snapshot, no docker, no LLM."""
    import copy, urllib.request
    global CORPUS
    real_corpus = CORPUS
    tmp = tempfile.mkdtemp(prefix="csa-selfcheck-")
    real_urlopen = urllib.request.urlopen

    def no_net(*a, **k):
        raise AssertionError("network attempted without --otlp")
    urllib.request.urlopen = no_net
    quiet = lambda *a: None
    try:
        CORPUS = tmp                                  # fixture dir must pass the corpus-path guard
        snapdir = _fixture_snapshot(tmp)

        # Cover every case the fixture CAN satisfy: seeded cases carry their own material; a
        # corpus-backed case is covered when the fixture has its prefix. Such a case (case11+) is
        # SKIPPED here with a note — extend _fixture_snapshot if you want it selfchecked too.
        fix_con = sqlite3.connect(f"file:{os.path.join(snapdir, 'analyzer.db')}?mode=ro&immutable=1",
                                  uri=True)
        fix_sids = [r[0] for r in fix_con.execute("SELECT session_id FROM sessions")]
        fix_con.close()
        covered, skipped = [], []
        for c in cases_mod.CASES:
            seeded = {r.get("session_id", "") for t, r in c.get("seed", []) if t == "sessions"}
            if all(any(s.startswith(p) for s in fix_sids) or any(s.startswith(p) for s in seeded)
                   for p in c["needs"]):
                covered.append(c)
            else:
                skipped.append(c["id"])
        if skipped:
            print(f"note: fixture does not cover {skipped} — extend _fixture_snapshot to selfcheck them")
        want_xfail = sum(1 for c in covered for ch in c["checks"] if ch.get("xfail"))
        want_flips = sum(1 for c in covered for ch in c["checks"] if not ch.get("xfail"))

        code, res = run_suite(covered, snapdir, log=quiet)
        assert code == 0, f"fixture run must be green, got exit {code}: " + json.dumps(res["cases"], indent=1)
        assert res["summary"]["xfail"] == want_xfail, \
            f"{want_xfail} xfail expected (derived from cases.py), got {res['summary']}"

        code2, res2 = run_suite(covered, snapdir, log=quiet)
        assert [c["checks"] for c in res["cases"]] == [c["checks"] for c in res2["cases"]], \
            "two runs on the same snapshot must be identical (determinism)"

        flips = 0
        for ci, c in enumerate(covered):
            for ki, ch in enumerate(c["checks"]):
                if ch.get("xfail"):
                    continue
                mut = copy.deepcopy(covered)
                mch = mut[ci]["checks"][ki]
                if ch["kind"] == "exists":
                    mch["min"] = 10 ** 9
                elif ch["kind"] == "route":
                    mch["want"] = ["flipped", "flipped"]
                elif ch["kind"] == "recall":
                    mch["gold"] = [[p, "zzz-never-matches"] for p, _ in ch["gold"]]
                elif ch["kind"] == "pr_card":
                    mch["want_session"] = "ffffffff"
                lines = []
                code, _ = run_suite(mut, snapdir, only=c["id"], log=lines.append)
                assert code != 0, f"flipped {c['id']}/{ch['name']} must exit nonzero"
                assert any(ln.startswith("FAIL") and c["id"] in ln for ln in lines), \
                    f"a FAIL line must name the case (XFAIL lines don't count): {lines}"
                flips += 1
        assert flips == want_flips, f"expected {want_flips} flippable runnable checks, got {flips}"

        mut = copy.deepcopy(covered)
        for ch in mut[0]["checks"]:
            if ch.get("xfail"):
                ch["sql"] = "SELECT 1"                # future mechanism 'lands' without flipping the flag
        lines = []
        code, _ = run_suite(mut, snapdir, only=mut[0]["id"], log=lines.append)
        assert code != 0 and any("XPASS" in ln for ln in lines), \
            f"an unexpectedly-passing xfail must turn the run red: {lines}"
        assert any("resolution-engine" in ln for ln in lines), "XPASS message must name the owner task"

        for bad in (os.path.join(SERVER, "data"), SERVER, "/tmp"):
            try:
                guard_snapshot_dir(bad)
                raise SystemExit(f"guard accepted {bad}")
            except UsageError:
                pass
    finally:
        urllib.request.urlopen = real_urlopen
        CORPUS = real_corpus
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"OK selfcheck: fixture green ({want_xfail} xfail) + determinism + {flips} flipped "
          "expectations all red-and-named + XPASS reds with owner + prod paths refused + "
          "zero sockets without --otlp")
    return 0


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", help="snapshot dir under research/corpus/ (default: latest)")
    ap.add_argument("--only", help="run a single case id")
    ap.add_argument("--list", action="store_true", help="list case ids and their xfail owners")
    ap.add_argument("--json", action="store_true", help="print results JSON to stdout")
    ap.add_argument("--otlp", action="store_true", help=f"push spans+scores to Phoenix at {PHOENIX}")
    ap.add_argument("--selfcheck", action="store_true", help="fixture-based referee-of-the-referee")
    ap.add_argument("--measure-sweep", action="store_true", help="$$ projection for the retro sweep (spends real LLM money)")
    ap.add_argument("--sample", type=int, default=10, help="sweep sample size (default 10, max 50)")
    ap.add_argument("--prompt-file", default=os.path.join(HERE, "sweep_prompt.txt"))
    a = ap.parse_args(argv)

    try:
        if a.selfcheck:
            return selfcheck()
        if a.list:
            for c in cases_mod.CASES:
                owners = sorted({ch["xfail"]["owner"] for ch in c["checks"] if ch.get("xfail")})
                fc2 = c.get("fc2_case", "-")       # extension cases may omit fc2_case
                print(f"{c['id']:<32} fc2 #{fc2!s:<3} xfail→ {', '.join(owners) or '-'}")
            return 0
        if a.snapshot is not None and not a.snapshot.strip():
            raise UsageError("--snapshot got an empty string (unset variable in a wrapper?) — "
                             "refusing to silently fall back to the latest snapshot")
        snapdir = a.snapshot or latest_snapshot()
        if a.measure_sweep:
            if not 1 <= a.sample <= 50:
                raise UsageError("--sample must be 1..50 (each sampled fact is a real haiku call)")
            return measure_sweep(snapdir, a.sample, a.prompt_file)
        spans = [] if a.otlp else None
        code, results = run_suite(cases_mod.CASES, snapdir, only=a.only, spans=spans)
        path = write_results(results)
        print(f"results: {path}  (snapshot {results['snapshot']}, git {_git_sha()})")
        if a.otlp:
            trace_id = None
            try:
                st, trace_id = _otlp_spans(spans, results)
                print(f"phoenix: spans pushed to {PHOENIX}/v1/traces (HTTP {st}, project csa-research)")
            except Exception as e:
                print(f"phoenix: span push failed ({e}) — run is unaffected")
                code = code or 0
            try:
                print(f"phoenix: {_phoenix_experiment(results, trace_id=trace_id)}")
            except Exception as e:
                print(f"phoenix: experiment upload failed ({e}) — spans still visible; run unaffected")
        if a.json:
            print(json.dumps(results, indent=1))
        return code
    except UsageError as e:
        print(f"usage error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

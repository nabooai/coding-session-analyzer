"""The 10 golden cases from research/fact-core-v2.md §Research environment, as data.

A case = dict(id, title, fc2_case, needs, seed, checks). run.py interprets it; this module holds
no logic beyond its own shape self-check.

  needs   session-id PREFIXES that must exist in the snapshot (fixture snapshots use "<prefix>-fixture"
          ids, so the same cases run against both). Missing needs = environment error (exit 2), never
          a case failure — a wrong snapshot must diagnose itself, not fail confusingly.
  seed    rows inserted into the per-run SCRATCH COPY only (the frozen snapshot is never written).
          Every case seeds what it needs (a graph-store supersession, a CEO-vs-dev conflict, a roster
          mismatch...), so the suite runs against any corpus, including the empty fixture — the seed is
          what makes each case runnable at all, and what the sibling task will resolve when it lands.
  checks  each: dict(name, kind, ...params, xfail=None).
          kinds: exists   — SQL, first cell >= min. Absent table/column => MISSING.
                 route    — route.classify(q) must equal want=(scope, shape). Deterministic, no LLM.
                 recall   — search.search(q) top-K joined to docs text; hit iff session prefix + regex
                            match (eval_recall.gold_rank pattern).
                 pr_card  — resolve.resolve+cards on q must surface want_session (the pr_links creator).
          xfail = dict(owner="research/tasks/<sibling>.md", expect="<pinned failure substring>"):
          expected to fail until the OWNER task lands. Satisfied by MISSING(anything) or a FAIL whose
          detail contains `expect`; any other outcome (incl. an unexpected PASS) turns the run red.
          PICKING expect: when the check's TABLES arrive before its rows/edges do (fact-core-schema-v2
          creates facts+fact_edges; the resolution/registry/taint owners fill them later), the failure
          transitions from "MISSING: no such table" to "got 0, want >= 1" mid-program — pin
          expect="got 0" so both regimes stay xfail (MISSING is accepted unconditionally). Pin
          "MISSING" only for checks on tables no earlier task creates (cases 9, 10);
          case 7 pins "got 0" — PR #68 ships the spans table ahead of the card-path TOOL span.
          (Cases 3, 5, 8 flipped 2026-08-04: schema-v2 + registries-verify + entity-noise-gate
          landed, and run.py now replays S2 refresh_facts + S3 verify on the scratch.)
          FLIPPING: when the owner task lands, delete the check's xfail field — one line, nothing else.

Ground truth for future mechanisms asserts against the agreed DDL in research/fact-core-v2.md §Schema v2
(facts / fact_edges / registry / authority / author_ledger / spans) — the contract fixed across the
sibling docs — never against invented APIs.
"""

# Case 7's future assertion, corrected from the design text: the standalone PR-card path answers
# BEFORE the planner and is logged engine="fts" (mcp_server.py), so "trace shows sql engine on
# pr_links" would fail forever. The honest ground truth is a TOOL span whose attributes reference
# pr_links — that is what the otel instrumentation of the card path will record.

SEED_NOTE = "seeded by research/cases.py"

CASES = [
    dict(
        id="case1-graph-store-supersession", fc2_case=1,
        title="SQLite->DuckDB graph-store supersession",
        needs=["seed-graph-old", "seed-graph-new"],
        seed=[
            ("sessions", dict(session_id="seed-graph-old-session", project="acme-ai",
                              started_at="2026-06-01T10:00:00Z", ended_at="2026-06-01T12:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("sessions", dict(session_id="seed-graph-new-session", project="ledger",
                              started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T12:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("hidden_context", dict(session_id="seed-graph-old-session", type="decision", scope="organizational",
                                    claim="SQLite was kept for graph.db after benchmarking",
                                    evidence=SEED_NOTE, ref="seedgraphold")),
            ("hidden_context", dict(session_id="seed-graph-new-session", type="decision", scope="organizational",
                                    claim="Move the graph store to DuckDB for analytical reads",
                                    evidence=SEED_NOTE, ref="seedgraphnew")),
        ],
        checks=[
            dict(name="old graph-store-on-SQLite decision present", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE session_id LIKE 'seed-graph-old%' "
                     "AND type='decision' AND claim LIKE '%SQLite%' AND claim LIKE '%graph%'"),
            dict(name="newer DuckDB proposal present", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE session_id LIKE 'seed-graph-new%' "
                     "AND claim LIKE '%DuckDB%'"),
            dict(name="route: team/lookup", kind="route",
                 q="why did we move off SQLite for the graph store DuckDB decision",
                 want=["team", "lookup"]),
            dict(name="recall serves the material top-5", kind="recall",
                 q="why did we move off SQLite for the graph store DuckDB decision",
                 gold=[["seed-graph-new", "(?i)duckdb|sqlite"], ["seed-graph-old", "(?i)sqlite"]]),
            dict(name="supersedes edge DuckDB->SQLite, loser superseded", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM fact_edges e "
                     "JOIN facts n ON n.ref=e.src_ref JOIN facts o ON o.ref=e.dst_ref "
                     "WHERE e.rel='supersedes' AND n.claim LIKE '%DuckDB%' "
                     "AND o.claim LIKE '%SQLite%' AND o.state='superseded' AND o.invalid_at IS NOT NULL",
                 xfail=dict(owner="research/tasks/resolution-engine.md", expect="got 0")),
        ]),

    dict(
        id="case2-etl-always-queues", fc2_case=2,
        title="'ingest always queues' supersedes the older defer-on-busy ETL design",
        # The 'Superseded' marker lived in a design doc, not a fact. Both facts are therefore
        # SEEDED with real dates so the resolution engine has an actual pair to resolve.
        needs=["seed-etl-old", "seed-etl-new"],
        seed=[
            ("sessions", dict(session_id="seed-etl-old-session", project="claude-session-analyzer",
                              started_at="2026-07-06T10:00:00Z", ended_at="2026-07-06T12:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("sessions", dict(session_id="seed-etl-new-session", project="claude-session-analyzer",
                              started_at="2026-07-24T10:00:00Z", ended_at="2026-07-24T12:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("hidden_context", dict(session_id="seed-etl-old-session", type="decision", scope="organizational",
                                    claim="ETL design: /api/ingest extracts inline when the worker is idle "
                                          "and defers to the queue only under load",
                                    evidence=SEED_NOTE, ref="seedetlold01")),
            ("hidden_context", dict(session_id="seed-etl-new-session", type="decision", scope="organizational",
                                    claim="ETL design superseded: /api/ingest ALWAYS queues; the csa-etl "
                                          "service drains all orgs in parallel",
                                    evidence=SEED_NOTE, ref="seedetlnew01")),
        ],
        checks=[
            dict(name="both ETL design decisions present", kind="exists", min=2,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE ref IN ('seedetlold01','seedetlnew01')"),
            dict(name="route: team/lookup", kind="route",
                 q="What did we decide about the ETL architecture?", want=["team", "lookup"]),
            dict(name="supersedes edge new->old", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM fact_edges WHERE rel='supersedes' "
                     "AND src_ref='seedetlnew01' AND dst_ref='seedetlold01'",
                 xfail=dict(owner="research/tasks/resolution-engine.md", expect="got 0")),
        ]),

    dict(
        id="case3-prefect-current", fc2_case=3,
        title="'Do we still use Prefect?' — current-check via invalid_at (positive control: nothing invalidates it)",
        needs=["seed-prefect"],
        seed=[
            ("sessions", dict(session_id="seed-prefect-session", project="ledger",
                              started_at="2026-07-18T10:00:00Z", ended_at="2026-07-18T12:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("hidden_context", dict(session_id="seed-prefect-session", type="constraint", scope="organizational",
                                    claim="Prefect orchestrates the ledger ETL flows",
                                    evidence=SEED_NOTE, ref="seedprefect1")),
        ],
        checks=[
            dict(name="Prefect facts present", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE claim LIKE '%Prefect%'"),
            dict(name="route: team/lookup", kind="route",
                 q="Do we use Prefect in our ETL?", want=["team", "lookup"]),
            dict(name="recall serves Prefect material top-5", kind="recall",
                 q="Do we use Prefect in our ETL?", gold=[["seed-prefect", "(?i)prefect"]]),
            # Passes as soon as facts+backfill exist (invalid_at defaults NULL) — flipped when the
            # schema task landed AND run.py started replaying S2 (refresh_facts) on the scratch.
            dict(name="Prefect fact is CURRENT (invalid_at IS NULL)", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM facts WHERE claim LIKE '%Prefect%' AND invalid_at IS NULL"),
        ]),

    dict(
        id="case4-authority-gate", fc2_case=4,
        title="Seeded CEO-vs-dev conflict must land in the conflict queue, NOT auto-resolve (R3)",
        needs=["seed-ceo", "seed-dev"],
        seed=[
            ("sessions", dict(session_id="seed-ceo-session", project="business",
                              started_at="2026-07-10T09:00:00Z", ended_at="2026-07-10T10:00:00Z",
                              user="sam@example.com", summary=SEED_NOTE)),
            ("sessions", dict(session_id="seed-dev-session", project="business",
                              started_at="2026-07-25T09:00:00Z", ended_at="2026-07-25T10:00:00Z",
                              user="dev@example.com", summary=SEED_NOTE)),
            ("hidden_context", dict(session_id="seed-ceo-session", type="decision", scope="organizational",
                                    claim="Onboard client Acme as a design partner this quarter",
                                    evidence=SEED_NOTE, ref="seedceoacme1")),
            ("hidden_context", dict(session_id="seed-dev-session", type="decision", scope="organizational",
                                    claim="Off-board client Acme, drop them from the design-partner program",
                                    evidence=SEED_NOTE, ref="seeddevacme1")),
        ],
        checks=[
            dict(name="both conflicting decisions present", kind="exists", min=2,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE ref IN ('seedceoacme1','seeddevacme1')"),
            # R3: NEW author's bin < OLD author's -> contradicts edge, both 'conflict', both served.
            dict(name="conflict state on both, contradicts edge, NO auto-supersede", kind="exists", min=1,
                 sql="SELECT (SELECT COUNT(*) FROM facts WHERE ref IN ('seedceoacme1','seeddevacme1') "
                     "        AND state='conflict') = 2 "
                     "AND EXISTS(SELECT 1 FROM fact_edges WHERE rel='contradicts' "
                     "           AND src_ref='seeddevacme1' AND dst_ref='seedceoacme1') "
                     "AND NOT EXISTS(SELECT 1 FROM fact_edges WHERE rel='supersedes' "
                     "           AND src_ref='seeddevacme1' AND dst_ref='seedceoacme1')",
                 xfail=dict(owner="research/tasks/resolution-engine.md", expect="got 0")),
        ]),

    dict(
        id="case5-registry-refute", fc2_case=5,
        title="Registry refute: employment/role fact vs the roster (the Priya case)",
        # The seed arms the engine the way a real tenant would: a synced sso slice (WITHOUT Priya),
        # the two-step opt-in (registry_sources = connected-and-complete, registry_refute = allowed
        # to refute), and the person entity extraction would have emitted for her. tenant_config MUST
        # carry id:1 — the CHECK(id=1) passes NULL, and an id-less row is invisible to `WHERE id=1`.
        needs=["seed-priya"],
        seed=[
            ("sessions", dict(session_id="seed-priya-session", project="claude-session-analyzer",
                              started_at="2026-07-15T09:00:00Z", ended_at="2026-07-15T10:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("hidden_context", dict(session_id="seed-priya-session", type="constraint", scope="organizational",
                                    claim="Priya is the on-call DevOps lead and must approve lab deploys",
                                    evidence=SEED_NOTE, ref="seedpriya001")),
            ("entities", dict(id="seed-priya-person", type="person", name="Priya")),
            ("registry", dict(kind="member", key="mara@example.com", value="default",
                              as_of="2026-07-20T00:00:00Z", source="sso")),
            ("tenant_config", dict(id=1, registry_sources="sso", registry_refute="sso")),
        ],
        checks=[
            dict(name="Priya role fact present", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE ref='seedpriya001'"),
            # S3 verify: roster (known-complete registry) has no Priya -> refuted, invalid_at set to the
            # registry as_of, supersedes edge from a synthetic registry-fact carrying the receipt.
            dict(name="fact refuted by roster with receipt edge", kind="exists", min=1,
                 sql="SELECT EXISTS(SELECT 1 FROM facts WHERE ref='seedpriya001' "
                     "              AND state IN ('superseded','refuted') AND invalid_at IS NOT NULL) "
                     "AND EXISTS(SELECT 1 FROM fact_edges WHERE rel='supersedes' AND dst_ref='seedpriya001' "
                     "           AND note LIKE '%registry%')"),
        ]),

    dict(
        id="case6-taint-banner", fc2_case=6,
        title="Taint: gotcha depending on the superseded SQLite decision carries the durable edge",
        # Pins only the DURABLE contract (depends_on edge + superseded target). The serving banner is
        # asserted by the owner task through its own Search-page acceptance — no invented API here.
        needs=["seed-taint"],
        seed=[
            ("sessions", dict(session_id="seed-taint-session", project="ledger",
                              started_at="2026-07-12T09:00:00Z", ended_at="2026-07-12T10:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("hidden_context", dict(session_id="seed-taint-session", type="gotcha", scope="organizational",
                                    claim="Graph-store compaction assumes the SQLite graph store: run VACUUM "
                                          "after bulk deletes or reads slow 10x",
                                    evidence=SEED_NOTE, ref="seedtaint001")),
        ],
        checks=[
            dict(name="dependent gotcha present", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE ref='seedtaint001'"),
            dict(name="depends_on edge to a superseded fact (taint source)", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM fact_edges d JOIN facts f ON f.ref=d.dst_ref "
                     "WHERE d.rel='depends_on' AND d.src_ref='seedtaint001' AND f.state='superseded'",
                 xfail=dict(owner="research/tasks/taint-confidence-search-page.md", expect="got 0")),
        ]),

    dict(
        id="case7-pr-locate-trace", fc2_case=7,
        title="'which session created PR #438 of ledger' — locate route, deterministic card, TOOL span on pr_links",
        needs=["seed-pr438"],
        seed=[
            ("sessions", dict(session_id="seed-pr438-session", project="ledger",
                              started_at="2026-07-11T10:00:00Z", ended_at="2026-07-11T12:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("pr_links", dict(session_id="seed-pr438-session", repo="acme/ledger", pr_number=438,
                              url="https://github.com/acme/ledger/pull/438", first_seen="2026-07-11T21:11:24Z",
                              last_seen="2026-07-11T21:11:24Z", n_events=1)),
        ],
        checks=[
            dict(name="pr_links has the PR with its creator", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM pr_links WHERE pr_number=438 AND repo LIKE '%ledger%' "
                     "AND session_id LIKE 'seed-pr438%'"),
            dict(name="route: team/locate", kind="route",
                 q="which session created PR #438 of ledger?", want=["team", "locate"]),
            dict(name="deterministic card names the creating session", kind="pr_card",
                 q="which session created PR #438 of ledger?", want_session="seed-pr438"),
            dict(name="TOOL span shows the answer came from pr_links", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM spans WHERE kind='TOOL' AND attrs LIKE '%pr_links%' "
                     "AND attrs LIKE '%438%'",
                 xfail=dict(owner="research/tasks/otel-traces-native.md", expect="got 0")),
        ]),

    dict(
        id="case8-entity-noise", fc2_case=8,
        title="Entity noise: singleton never-dominant entity is excluded from serving + S4 candidates",
        needs=["seed-noise"],
        seed=[
            ("sessions", dict(session_id="seed-noise-session", project="ledger",
                              started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T12:00:00Z",
                              user="mara@example.com", summary=SEED_NOTE)),
            ("entities", dict(id="noise-1", type="concept", name="one-off scratch idea")),
            ("mentions", dict(session_id="seed-noise-session", entity_id="noise-1", dominant=0)),
        ],
        checks=[
            dict(name="singleton never-dominant entities exist (the noise)", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM (SELECT entity_id FROM mentions "
                     "GROUP BY entity_id HAVING COUNT(DISTINCT session_id)=1 AND MAX(IFNULL(dominant,0))=0)"),
            # entity-noise-gate ships entity_stats at S6 + the serving floor; S4 is contractually
            # required to consult the same gate, so this one table carries both halves' flip.
            # (Flipped 2026-08-04: the gate landed via PR #69 — this xfail was XPASS-red on base.)
            dict(name="serving gate excludes the singletons", kind="exists", min=1,
                 sql="SELECT COUNT(*) FROM entity_stats WHERE sessions=1 AND dominant_n=0"),
        ]),

    dict(
        id="case9-authority-ab", fc2_case=9,
        title="Authority A/B: activity and git providers disagree — labeled ground truth decides",
        # Ground truth label: the GIT provider is right here — steady committer beats burst-of-sessions.
        # dev-b has more sessions in `ledger` (activity says GREEN), dev-a owns the repo's history.
        needs=["seed-ab"],
        seed=[
            ("sessions", dict(session_id="seed-ab-a1", project="ledger", started_at="2026-05-01T09:00:00Z",
                              ended_at="2026-05-01T10:00:00Z", user="dev-a@example.com", summary=SEED_NOTE)),
            ("sessions", dict(session_id="seed-ab-b1", project="ledger", started_at="2026-07-20T09:00:00Z",
                              ended_at="2026-07-20T10:00:00Z", user="dev-b@example.com", summary=SEED_NOTE)),
            ("sessions", dict(session_id="seed-ab-b2", project="ledger", started_at="2026-07-21T09:00:00Z",
                              ended_at="2026-07-21T10:00:00Z", user="dev-b@example.com", summary=SEED_NOTE)),
            ("sessions", dict(session_id="seed-ab-b3", project="ledger", started_at="2026-07-22T09:00:00Z",
                              ended_at="2026-07-22T10:00:00Z", user="dev-b@example.com", summary=SEED_NOTE)),
            ("hidden_context", dict(session_id="seed-ab-a1", type="decision", scope="organizational",
                                    claim="ledger ETL cursors persist in CursorWriter, one writer per event loop",
                                    evidence=SEED_NOTE, ref="seedaba00001", repo="ledger")),
            ("hidden_context", dict(session_id="seed-ab-b3", type="decision", scope="organizational",
                                    claim="ledger ETL cursors should move to per-integration files, drop CursorWriter",
                                    evidence=SEED_NOTE, ref="seedabb00001", repo="ledger")),
        ],
        checks=[
            dict(name="disagreement material present", kind="exists", min=2,
                 sql="SELECT COUNT(*) FROM hidden_context WHERE ref IN ('seedaba00001','seedabb00001')"),
            dict(name="both providers computed; resolution matches ground truth (no auto-win for dev-b)",
                 kind="exists", min=1,
                 sql="SELECT EXISTS(SELECT 1 FROM authority WHERE user='dev-a@example.com' AND domain='ledger' "
                     "              AND provider='git') "
                     "AND EXISTS(SELECT 1 FROM authority WHERE user='dev-b@example.com' AND domain='ledger' "
                     "              AND provider='activity') "
                     "AND NOT EXISTS(SELECT 1 FROM fact_edges WHERE rel='supersedes' "
                     "               AND src_ref='seedabb00001' AND dst_ref='seedaba00001')",
                 xfail=dict(owner="research/tasks/authority-ab-hardening.md", expect="MISSING")),
        ]),

    dict(
        id="case10-extractor-error-flag", fc2_case=10,
        title="Extractor-error flag: evidence-claim mismatch must NOT dock the author",
        needs=["seed-flag"],
        seed=[
            ("sessions", dict(session_id="seed-flag-session", project="claude-session-analyzer",
                              started_at="2026-07-18T09:00:00Z", ended_at="2026-07-18T10:00:00Z",
                              user="jules@example.com", summary=SEED_NOTE)),
            # The evidence quote does NOT support the claim — a classic extractor hallucination.
            ("hidden_context", dict(session_id="seed-flag-session", type="constraint", scope="organizational",
                                    claim="The lab API caches recall results in Redis for 15 minutes",
                                    evidence="we decided against adding any cache layer for now",
                                    ref="seedflag0001")),
            ("reviews", dict(kind="hc", ref="seedflag0001", status="wrong", note="flagged wrong by teammate",
                             flagger="tomasz@example.com")),
        ],
        checks=[
            dict(name="mismatched fact + wrong-flag present", kind="exists", min=1,
                 sql="SELECT EXISTS(SELECT 1 FROM hidden_context WHERE ref='seedflag0001') "
                     "AND EXISTS(SELECT 1 FROM reviews WHERE ref='seedflag0001' AND status='wrong')"),
            dict(name="ledger says extractor error, author track record untouched", kind="exists", min=1,
                 sql="SELECT EXISTS(SELECT 1 FROM author_ledger WHERE ref='seedflag0001' "
                     "              AND kind='flag_extractor_error') "
                     "AND NOT EXISTS(SELECT 1 FROM author_ledger WHERE ref='seedflag0001' "
                     "               AND kind='flag_confirmed')",
                 xfail=dict(owner="research/tasks/authority-ab-hardening.md", expect="MISSING")),
        ]),
]


if __name__ == "__main__":
    # Shape check. Deliberately extension-friendly: adding case11+ must not fight you — the only
    # pinned quantity is that fc2 cases 1..10 (the design doc's mapping) all remain present.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    ids = [c["id"] for c in CASES]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate case ids: {sorted(dupes)}"
    fc2 = sorted(c["fc2_case"] for c in CASES if "fc2_case" in c)   # extra cases may omit fc2_case
    assert fc2 == list(range(1, 11)), \
        f"the 10 research/fact-core-v2.md cases must all stay present (fc2_case 1..10 once each), got {fc2}"
    kinds = {"exists", "route", "recall", "pr_card"}
    xfail_total = 0
    for c in CASES:
        assert c["checks"], f"{c['id']}: no checks"
        for ch in c["checks"]:
            assert ch["kind"] in kinds, f"{c['id']}: unknown kind {ch['kind']}"
            xf = ch.get("xfail")
            if xf:
                xfail_total += 1
                owner = os.path.join(repo, xf["owner"])
                assert os.path.exists(owner), f"{c['id']}: xfail owner doc missing: {xf['owner']}"
                assert xf.get("expect"), f"{c['id']}: xfail must pin an expected failure mode"
                assert ch["kind"] == "exists", f"{c['id']}: only 'exists' checks may be xfail (future DDL)"
        for table, row in c.get("seed", []):
            assert table in ("sessions", "hidden_context", "reviews", "entities", "registry",
                             "tenant_config", "pr_links", "mentions"), f"{c['id']}: seed table {table}"
            assert row, f"{c['id']}: empty seed row"
            if table == "tenant_config":
                assert row.get("id") == 1, f"{c['id']}: tenant_config seed must pin id=1 — the " \
                    "CHECK(id=1) passes NULL and an id-less row is invisible to WHERE id=1"
        sids = {r.get("session_id", "") for t, r in c.get("seed", []) if t == "sessions"}
        for p in c["needs"]:
            if p.startswith("seed"):               # a corpus-backed prefix is validated at run time
                assert any(s.startswith(p) for s in sids), \
                    f"{c['id']}: needs prefix {p!r} matches none of its own seed sessions"
        runnable = [ch for ch in c["checks"] if not ch.get("xfail")]
        assert runnable, f"{c['id']}: every case needs at least one runnable-today check (the green floor)"
    seeded = sum(1 for c in CASES if c.get("seed"))
    print(f"OK cases: {len(CASES)} cases (fc2 1..10 present), {xfail_total} xfail assertion(s) with "
          f"owners verified on disk, {seeded} seeded, every case has a runnable check")

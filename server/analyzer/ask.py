"""Query layer: canned stats + NL->query (Claude writes SQL/FTS, we run it read-only)."""
import json, re

from sqlalchemy import func, select

from analyzer import db, search, llm, tracer
from analyzer.models import Entity, mentions

# canned stats — the data-freak dashboard. Raw SQL strings by design: this dict is a DISPLAY
# SURFACE (each query is printed next to its result, and run through run_sql so cur.description
# names the columns) — a Core rewrite would stop the shown query being the executed query.
CANNED = {
    "overview": [
        ("sessions", "SELECT COUNT(*) FROM sessions"),
        ("projects", "SELECT COUNT(DISTINCT project) FROM sessions"),
        ("hidden-context items", "SELECT COUNT(*) FROM hidden_context"),
        ("entities", "SELECT COUNT(*) FROM entities"),
        ("relations (entity->entity)", "SELECT COUNT(*) FROM relations"),
        ("actions", "SELECT COUNT(*) FROM actions"),
        ("links", "SELECT COUNT(*) FROM links"),
        ("data-source mentions", "SELECT COUNT(*) FROM data_sources"),
        ("memories ingested", "SELECT COUNT(*) FROM sessions WHERE source='memory'"),
        ("reviewed items", "SELECT COUNT(*) FROM reviews"),
        ("FTS docs indexed", "SELECT COUNT(*) FROM docs"),
    ],
    "projects": [("sessions per project",
                  "SELECT project, COUNT(*) n FROM sessions GROUP BY project ORDER BY n DESC LIMIT 12")],
    "hidden": [("hidden-context by type",
                "SELECT type, COUNT(*) n FROM hidden_context GROUP BY type ORDER BY n DESC"),
               ("sample claims (copy ref -> csa classify <ref> noise)",  # pure SQL, never sent to an LLM
                "SELECT h.ref, h.type, substr(h.claim,1,66) claim FROM hidden_context h "
                "LEFT JOIN reviews r ON r.kind='hc' AND r.ref=h.ref "
                "WHERE r.status IS NULL ORDER BY RANDOM() LIMIT 12")],
    # ref-keyed history that no longer joins a live fact = judgement destroyed by a re-extract (see
    # store.carry_refs). Read the DELTA, never the absolute: `forget` produces the same signature with no ref
    # churn at all — it deletes `served WHERE session=?` on the ASKING mcp session id (log_served), not the
    # corpus session whose facts were served, and it never touches hc_types — so every deliberately deleted
    # session (e.g. the 225 self-ingested ones purged 2026-07-16) sits in this number permanently. It has a
    # non-zero floor and will step up on every legitimate forget; only a jump right after a re-extract is a
    # regression. Stated in the label so nobody chases a phantom.
    "orphans": [("ref-keyed history rows with no surviving fact "
                 "(floor > 0: `forget` orphans served/hc_types too — read the delta across a re-extract)",
                 "SELECT 'hc_types' t, COUNT(*) orphaned, (SELECT COUNT(*) FROM hc_types) total FROM hc_types "
                 "WHERE ref NOT IN (SELECT ref FROM hidden_context WHERE ref IS NOT NULL) "
                 "UNION ALL SELECT 'reviews(hc)', COUNT(*), (SELECT COUNT(*) FROM reviews WHERE kind='hc') "
                 "FROM reviews WHERE kind='hc' AND ref NOT IN (SELECT ref FROM hidden_context WHERE ref IS NOT NULL) "
                 "UNION ALL SELECT 'served', COUNT(*), (SELECT COUNT(DISTINCT ref) FROM tel.served WHERE ref IS NOT NULL) "
                 "FROM (SELECT DISTINCT ref FROM tel.served WHERE ref IS NOT NULL "
                 "AND ref NOT IN (SELECT ref FROM hidden_context WHERE ref IS NOT NULL)) "
                 "UNION ALL SELECT 'signals', COUNT(*), (SELECT COUNT(DISTINCT ref) FROM signals) "
                 "FROM (SELECT DISTINCT ref FROM signals "
                 "WHERE ref NOT IN (SELECT ref FROM hidden_context WHERE ref IS NOT NULL))")],
    "scope": [("personal vs organizational insights",
               "SELECT IFNULL(scope,'(ungraded)') scope, COUNT(*) n FROM hidden_context GROUP BY scope ORDER BY n DESC")],
    "dups": [("near-duplicate claims (same finding rediscovered across sessions)",
              "SELECT cc.sessions, substr(MIN(h.claim),1,72) canonical_claim FROM claim_clusters cc "
              "JOIN hidden_context h ON h.ref=cc.ref WHERE cc.sessions>1 "
              "GROUP BY cc.cluster_id ORDER BY cc.sessions DESC, cc.cluster_id LIMIT 40")],
    "entities": [("most-mentioned",
                  "SELECT e.id, e.type, COUNT(*) n FROM mentions m JOIN entities e ON e.id=m.entity_id "
                  "GROUP BY e.id ORDER BY n DESC LIMIT 15")],
    "relations": [("entity -> entity relations",
                   "SELECT source_id, rel, target_id, COUNT(*) n FROM relations "
                   "GROUP BY source_id,rel,target_id ORDER BY n DESC LIMIT 20")],
    "data": [("company data sources used",  # lesson: how employees use company data
              "SELECT name, COUNT(*) n, COUNT(DISTINCT session_id) sessions FROM data_sources "
              "GROUP BY name ORDER BY n DESC LIMIT 20")],
    "links": [("tickets / PRs / urls",
               "SELECT substr(url,1,60) url, substr(title,1,40) title FROM links ORDER BY rowid DESC LIMIT 20")],
    "memory": [("ingested Claude Code memories (high-trust)",
                "SELECT project, substr(summary,1,72) summary FROM sessions WHERE source='memory' "
                "ORDER BY project LIMIT 30")],
    "reviews": [("HITL value-review status",
                 "SELECT kind, status, COUNT(*) n, SUM(note='auto') auto FROM reviews "
                 "GROUP BY kind, status ORDER BY n DESC")],
    "deploys": [
        ("deploy actions (verb=deploy)",
         "SELECT s.project, a.target_id, substr(a.context,1,40) ctx, s.started_at FROM actions a "
         "JOIN sessions s ON s.session_id=a.session_id WHERE a.verb LIKE 'deploy%' "
         "ORDER BY s.started_at DESC LIMIT 20"),
    ],
}

NL2Q_PROMPT = """Translate the question into ONE read-only query.

SQLite tables:
  sessions(session_id, project, git_branch, started_at, ended_at, n_user, n_asst, summary, deployed_to_prod, source, user)
    -- deployed_to_prod is INTEGER 0/1 (filter `=1`, NOT `IS NOT NULL`).  source is 'session' or 'memory'.
    -- user = the session OWNER's email. "who did X" and first-person questions filter or group by this.
    -- Claude Code memories live here too; for session questions add `WHERE source='session'` unless asked about memories.
  worked_on(session_id, item)
  hidden_context(session_id, type, claim, evidence)
  entities(id, type, name, attributes)        -- type: ticket|pr|release|service|repo|file|person|data_source|env|url
  mentions(session_id, entity_id)
  actions(session_id, verb, target_id, context)
  relations(session_id, source_id, rel, target_id, context)   -- entity->entity edge
  links(session_id, url, title)
  data_sources(session_id, name)
  pr_links(session_id, repo, pr_number, url, first_seen, last_seen, n_events)
    -- DETERMINISTIC ground truth: rows exist ONLY for the session that CREATED the PR (from Claude Code's
    -- pr-link events; viewer sessions have none). Earliest first_seen = the creation moment. PR numbers
    -- collide across repos — always filter/return repo alongside pr_number.
  session_workspace(session_id, kind, value)
    -- kind 'dir'|'branch': worktree dirs + git branches a session ACTUALLY touched (harvested from its raw
    -- commands). Use this for branch/worktree questions — sessions.git_branch only shows the main checkout.
    -- "worktree" questions = kind='dir' (value is the checkout PATH); match the repo on the path TAIL
    -- (value LIKE '%/ledger%'), NOT a bare '%ledger%' which also counts branch names.
  session_dirs(session_id, path)             -- view of session_workspace where kind='dir'
  session_branches(session_id, branch)       -- view of session_workspace where kind='branch'
  docs(doc_id, kind, session_id, project, etype, source, status, corroboration, text, first_at, valid_at, invalid_at, state)  -- FTS5: `docs MATCH 'terms'` searches text via the BM25 index. etype = the hidden_context type | 'summary' | 'worked_on' | 'entity'. corroboration = # distinct sessions that rediscovered a claim (>1 = corroborated/stronger). hidden_context rows are ONE per fact: started_at/first_at = last/first assertion date; invalid_at IS NULL = the fact is still current (add "AND invalid_at IS NULL" unless the question is explicitly about superseded/old decisions); state = extracted|verified|unverifiable|current|superseded|conflict.

Use 'fts' for CONTENT/keyword questions over free-text (claims/evidence/summaries) — e.g. "what do we know about X",
  "find notes on Y"; for fts the query is just the keywords. NEVER use SQL LIKE '%..%' on free-text columns
  (claim/evidence/summary/context) — use fts (it uses the BM25 index). sql = counts/aggregates/structured filters
  and relationship questions over `relations` (rel e.g. depends_on, implements, part_of, deploys).
For COUNT / "how many ... about <topic>" over free-text, use sql over the FTS docs table (EXACT, index-backed; NOT fts, NOT LIKE):
  "how many gotchas about ETL" -> {"engine":"sql","query":"SELECT COUNT(*) FROM docs WHERE etype='gotcha' AND docs MATCH 'etl'"}
Entity `name` is free-text description, NOT a key. Match an entity FUZZILY on id OR name — never by exact name:
  WHERE lower(e.id) LIKE '%term%' OR lower(e.name) LIKE '%term%'
Prefer LIKE over '=' so variants ("graph.db", "tenant graph database (graph.db)") all match.
Example -- "which session created PR #322 (of ledger)":
  {"engine":"sql","query":"SELECT session_id, repo, first_seen FROM pr_links WHERE pr_number=322 AND repo LIKE '%ledger%' ORDER BY first_seen LIMIT 1"}
Example -- "what PRs did session abcd1234 create / which sessions used branch etl3-prefect":
  {"engine":"sql","query":"SELECT repo, pr_number, first_seen FROM pr_links WHERE session_id LIKE 'abcd1234%'"}
  {"engine":"sql","query":"SELECT DISTINCT session_id, value FROM session_workspace WHERE kind='branch' AND value LIKE '%etl3-prefect%'"}
Example -- "what depends on graph.db":
  {"engine":"sql","query":"SELECT DISTINCT r.source_id, a.type, b.name FROM relations r JOIN entities a ON a.id=r.source_id JOIN entities b ON b.id=r.target_id WHERE r.rel='depends_on' AND (lower(b.id) LIKE '%graph.db%' OR lower(b.name) LIKE '%graph.db%')"}
DIALECT: SQLite SQL only: lower(x) and LIKE '%term%' (no toLower, no CONTAINS).
Read-only only. Return ONLY JSON: {"engine":"sql"|"fts","query":"..."}
"""

DEGRADE_NOTE = ("_(Relationship traversal is not available. Answered from lexical recall instead. "
                "Ask for the specific service, file or ticket name for the best result.)_")


def run_sql(con, sql):
    """PERMANENT raw-SQL carve-out (sqlalchemy migration) — this stays `con.execute(sql)` forever:
    1. the self-repair loop's contract is the raw sqlite3 ERROR TEXT: plan_and_run feeds the DBAPI
       message back to the model to fix its own query, and any wrapper that rewrites or re-raises
       would change the very text that loop depends on;
    2. `text()` would mangle model-written SQL — it treats every bare `:name` as a bindparam, and
       LLM SQL legitimately contains colons (string literals, JSON paths, time formats);
    3. there is nothing to type at build time: the statement arrives as a runtime string from the
       planner (or CANNED), so a Core construction has no columns/tables to be typed AGAINST;
    4. the safety boundary here is ro_connect, not SQLAlchemy: `mode=ro` (+ ATTACH denied) blocks
       writes at the ENGINE, which is the only guarantee that holds against arbitrary SQL anyway."""
    cur = con.execute(sql)
    return [d[0] for d in cur.description], cur.fetchall()


def print_table(cols, rows, max_w=70):
    if len(rows) == 1 and len(cols) == 1:        # scalar
        print(rows[0][0]); return
    print(" | ".join(cols))
    print("-" * min(90, sum(len(c) for c in cols) + 3 * len(cols)))
    for r in rows:
        print(" | ".join("" if x is None else str(x)[:max_w] for x in r))
    print(f"({len(rows)} rows)")


def stats(con, name="overview"):
    block = CANNED.get(name)
    if not block:
        print("available stats:", ", ".join(CANNED)); return
    for label, sql in block:
        print(f"\n## {label}")
        print_table(*run_sql(con, sql))


def nl_to_query(question, con=None, model="main"):
    vocab = ""
    if con is not None:                          # ground the query in the corpus's real entity ids
        e, m = Entity.__table__.alias("e"), mentions.alias("m")
        ids = [r[0] for r in db.run(con, select(e.c.id)
                                    .select_from(e.outerjoin(m, m.c.entity_id == e.c.id))
                                    .group_by(e.c.id)
                                    .order_by(func.count(m.c.session_id).desc()).limit(60)) if r[0]]
        if ids:
            vocab = "Known entity ids (use the closest existing one): " + ", ".join(ids) + "\n"
    prompt = f"{NL2Q_PROMPT}{vocab}Question: {question}"
    text, _, _ = llm.complete(prompt, model, timeout=120)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no query in model output")
    return json.loads(m.group(0))


def _reached(rows):
    """Distinct first-column values a traversal reached, capped — queries.result_ids semantics."""
    seen = dict.fromkeys(str(r[0]) for r in rows if r and r[0] is not None)
    return ",".join(list(seen)[:40])[:800] or None


def plan_and_run(con, question, model="main"):
    """Planner path for the MCP `ask` tool. NL -> {engine,query} (one ~6s claude call), then run it read-only with
    ONE self-repair retry. Quiet (no stdout — MCP protocol). Returns (engine, planned_query, cols, rows):
      * engine=='fts'  -> (fts, terms, None, None); the CALLER routes fts to app.recall (grouped provenance,
                          NOT search.answer's RAG prose) so ids stay flaggable and output stays scannable.
      * engine=='fts-graphless' -> the planner named an engine this server does not run (a model that saw
                          Cypher in training still emits it); returned un-run, the caller answers from
                          lexical recall and appends DEGRADE_NOTE.
      * rows is None (sql) -> the query hard-failed even after repair; engine+planned are still returned
                          so the caller can LOG the attempt for the readout."""
    plan = nl_to_query(question, con, model)
    if plan["engine"] == "fts":
        return ("fts", plan["query"], None, None)
    if plan["engine"] != "sql":
        with tracer.span(f"retrieve.{plan['engine']}", "RETRIEVER", **{"csa.engine": plan["engine"],
                         "db.statement": plan["query"], "csa.executed": False}):
            return ("fts-graphless", plan["query"], None, None)
    try:
        with tracer.span("retrieve.sql", "RETRIEVER", **{"csa.engine": "sql",
                         "db.statement": plan["query"], "csa.retry.attempt": 1}) as sp:
            cols, rows = run_sql(con, plan["query"])                 # mode=ro -> writes raise OperationalError
            sp.set(**{"csa.row_count": len(rows), "csa.result_ids": _reached(rows)})
        return ("sql", plan["query"], cols, rows)
    except Exception as e:                    # feed the error back once so the model fixes its own query
        try:
            retry = (f"{question}\n\nThe previous sql query FAILED:\n{plan['query']}\n"
                     f"Error: {str(e)[:200]}\nReturn a corrected sql query.")
            plan2 = nl_to_query(retry, con, model)
            with tracer.span("retrieve.sql", "RETRIEVER", **{"csa.engine": "sql",
                             "db.statement": plan2["query"], "csa.retry.attempt": 2}) as sp:
                cols, rows = run_sql(con, plan2["query"])
                sp.set(**{"csa.row_count": len(rows), "csa.result_ids": _reached(rows)})
            return ("sql", plan2["query"], cols, rows)
        except Exception:                     # ponytail: hard fail -> log engine+planned (rows=None), don't crash ask
            return ("sql", plan["query"], None, None)


def ask(con, question, model="main"):
    """CLI entry: ('table', cols, rows) for sql, or ('answer', text, hits) for fts (RAG prose).
    `con` MUST be read-only. One self-repair retry if a sql query errors.
    (The MCP `ask` tool calls plan_and_run + app.recall instead — grouped provenance, not RAG prose.)"""
    engine, planned, cols, rows = plan_and_run(con, question, model)
    print(f"[{engine}] {planned}\n")
    if engine in ("fts", "fts-graphless"):    # content -> RAG answer (reuse search.answer; no lossy hit-copy)
        text, hits = search.answer(con, question, model=model)
        if engine == "fts-graphless":
            text += "\n\n" + DEGRADE_NOTE
        return ("answer", text, hits)
    if rows is None:
        raise RuntimeError(f"{engine} query failed (unrepairable)")
    return ("table", cols, rows)

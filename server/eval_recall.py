"""Golden-set eval: questions -> (a) did search() surface the known-gold fact in top-K, when a question
pins one, (b) did route.classify pick the expected scope/shape?

Measures ROUTING on any corpus, and QUALITY once you pin gold from your own. Questions are deliberately in
*user* vocabulary; a corpus often says it differently ("classification" vs judge/label) — exactly the gap
GROUNDED semantic expansion is supposed to close, gated on its own eval. The ungrounded `--expand` hop this
file used to offer is deleted: it regressed two controls (see search.py).

    uv run python eval_recall.py            # the only mode: free, deterministic — routing included

Gold matching: hit counts when session_id starts with the gold prefix AND the regex matches the doc text
(session ids survive re-extraction; claim wording doesn't, so regexes stay coarse). `control: True`
questions must hit on the reference corpus — no retrieval change may regress them; exit 1 if one misses.
Session prefixes are corpus-specific, so the shipped set pins none: add `gold=[(prefix, regex)]` from yours.
`route`: expected (scope, shape) from route.classify (deterministic, no LLM — asked_by is pinned to ASKER).
ANY route mismatch fails the exit code — classification has no flakiness to tolerate.
gold=[] questions are route-only (e.g. honest-miss probes where the right answer is NO facts)."""
import argparse, re, sys
from analyzer import store, search, route, resolve, tracer
tracer.disable()   # eval traffic must not write spans into the real corpus

K = 5
ASKER = "mara@example.com"

GOLDEN = [
    # --- questions whose recall quality matters: route-only until you pin gold from your corpus ---
    dict(q="What was the session that I changed the classification to use haiku?",   # a vocabulary-gap question
         gold=[], route=("self", "lookup")),
    dict(q="What did we decide about the ETL architecture?", gold=[], route=("team", "lookup")),
    dict(q="Do we use Prefect in our ETL?", gold=[], route=("team", "lookup")),
    dict(q="Deploy to production", gold=[], route=("team", "lookup")),                # a standing rule should answer it
    dict(q="why did we move off SQLite for the graph store DuckDB decision", gold=[], route=("team", "lookup")),

    # --- routing seams ---
    dict(q="What did I do about Bug 1234?",                       # the misattribution probe, forever
         gold=[], route=("self", "miss")),                        # right answer = honest miss, NO fact page
    dict(q="how many gotchas did I log?",                          # first-person aggregate -> planner + asker hint
         gold=[], route=("self", "relational")),
    dict(q="how many gotchas about ETL?",                          # team aggregate
         gold=[], route=("team", "relational")),
    dict(q="how do I deploy to prod?",                             # first-person CLOTHING, team knowledge (modal guard)
         gold=[], route=("team", "lookup")),
    # LOCATE: "find me the session that talked about running the new ETL" got 8 misses and fell back to grepping
    # ~/.claude/projects by hand. "Which session" is a different QUESTION SHAPE than "what was decided" —
    # answering it with facts makes the asker reconstruct a session from provenance. gold=[] because the right
    # answer is a ranked SESSION LIST, which hit@K can't score.
    dict(q="find me the session that ran the ETL E2E in a fresh worktree",
         gold=[], route=("self", "locate")),                       # "find ME" is first-person: scope reorders only
    dict(q="which sessions touched the graph writer lock",
         gold=[], route=("team", "locate")),                       # locate beats relational ("touched"/multi-hop)
    dict(q="what did we decide in the session about auth",         # NEGATIVE: contains "session", wants FACTS
         gold=[], route=("team", "lookup")),
    dict(q="which session created PR #438 of ledger?",             # exact-id short-circuit: cards() answers, not FTS
         # was ("team","lookup"): "which session" is now its own shape. What this entry PINS is unchanged —
         # _standalone_card fires BEFORE the shape check in mcp_server.ask, so a resolved PR id still returns
         # the deterministic card; locate only changes the non-standalone fallback, from a fact page to the
         # session list this question literally asks for.
         gold=[], route=("team", "locate")),
    dict(q="what touched client/main.go?",                         # lookup-shaped: must NOT burn the ~7s planner
         gold=[], route=("team", "lookup")),
    dict(q="sqlite write amplification",                           # bare-keywords lookup
         gold=[], route=("team", "lookup")),
    dict(q="gotcha that caused a failed attempt and backtrack, non-obvious pitfall",
         gold=[], route=("team", "lookup")),                       # vague phrasing stays a cheap lookup
]


def gold_rank(con, hits, gold):
    """1-based rank of the first hit matching any (session_prefix, text_regex) gold, else None.
    Mention-aware (schema v2): a hidden_context doc is ONE row per fact, carrying the most RECENT asserting
    session's id — the gold prefix may pin an EARLIER asserter of the same fact, so match against every
    session that asserted it. Without this, a gold fact re-asserted later stops matching its pinned prefix
    even though the identical fact is served — an eval failure with zero user-facing regression."""
    for i, h in enumerate(hits, 1):
        text_row = con.execute("SELECT text FROM docs WHERE doc_id=?", (h["doc_id"],)).fetchone()
        text = text_row[0] if text_row else h["snippet"]
        sids = [h["session_id"] or ""]
        if h.get("kind") == "hidden_context" and h["doc_id"][:2] == "hc" and h["doc_id"][2:].isdigit():
            sids += [s for (s,) in con.execute(
                "SELECT session_id FROM hidden_context WHERE ref IS NOT NULL AND ref="
                "(SELECT ref FROM hidden_context WHERE rowid=?)", (int(h["doc_id"][2:]),))]
        for prefix, rx in gold:
            if any(s.startswith(prefix) for s in sids if s) and re.search(rx, text):
                return i
    return None


def run():
    con = store.sqlite_db()
    failed_controls, failed_routes = [], []
    print(f"{'rank':>4}  {'hit@'+str(K):>6}  {'route':>16}  question")
    for case in GOLDEN:
        rank, hit = None, None
        if case["gold"]:
            hits = search.search(con, case["q"], k=K)
            rank = gold_rank(con, hits, case["gold"])
            hit = rank is not None
            if case.get("control") and not hit:
                failed_controls.append(case["q"])
        rmark = ""
        if case.get("route"):
            rt = route.classify(con, case["q"], asked_by=ASKER, eids=resolve.resolve(con, case["q"]))
            got = (rt.scope, rt.shape)
            if got == case["route"]:
                rmark = f"{got[0]}/{got[1]} ✓"
            else:
                rmark = f"{got[0]}/{got[1]} ✗ want {case['route'][0]}/{case['route'][1]}"
                failed_routes.append(case["q"])
        print(f"{rank or '-':>4}  {('HIT' if hit else 'miss') if hit is not None else '—':>6}  {rmark:>16}  "
              f"{case['q']}" + ("  [control]" if case.get("control") else ""))
    bad = 0
    if failed_controls:
        print(f"\nFAIL: {len(failed_controls)} control question(s) missed top-{K}: {failed_controls}")
        bad = 1
    if failed_routes:
        print(f"\nFAIL: {len(failed_routes)} route mismatch(es): {failed_routes}")
        bad = 1
    return bad


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()      # no options left; --help and typos still exit cleanly
    sys.exit(run())

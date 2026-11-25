"""Incremental extraction — extract each session turn ~once instead of re-running `claude -p` over the whole
transcript on every re-POST of a growing session (the dominant usage-limit burn: one session was extracted 77x).

Both server extraction paths (apiserver.ingest_session, csa.cmd_drain._extract) route through extract_session():
it persists data/extracted/<sid>.json with a watermark, then decides per session:
  - SKIP        : byte-identical re-POST (transcript hash matches) -> return prior, NO claude call, NO bill.
  - INCREMENTAL : session grew append-only -> extract ONLY the new turns against a digest of the prior facts,
                  naive-union merge into the stored record. Cost is proportional to the tail, not the transcript.
  - FULL        : first extract, schema bump, or a rewritten/compacted prefix -> extract the whole transcript.

The caller indexes the returned record and THEN calls persist() — persist-after-index so a failed index never
leaves a hash-stamped record that would SKIP (and silently lose the session) forever.
"""
import os, json, hashlib
from pathlib import Path
from analyzer import extract, store, redact, entity_types, tenant

EXTRACT_DIR = "data/extracted"   # relative on purpose: extract_dir() anchors it to the cwd on every call,
#                                  the way store.data_root does for the corpus.
_MAX = 60000                     # mirror ingest's transcript cap for the incremental tail prompt


def _out(sid, org_id=None):
    """The default org keeps `data/extracted/<sid>.json` verbatim (csa.py + do_index read it); a
    hosted org gets its own dir. This cache is keyed by session_id ALONE and recall publishes session ids as
    provenance, so without the split, org B ingesting org A's session id would clobber A's extraction
    watermark — and on a hash match would be handed A's facts back to index as its own.
    `sid` is validated: it is a path component, so a '../' in it would escape the org dir (see
    store.valid_session_id) — the guard is at the SINK so no caller can bypass it."""
    return os.path.join(extract_dir(org_id), f"{store.valid_session_id(sid)}.json")


def extract_dir(org_id=None):
    """One org's extraction cache, absolute. THE name for this directory: csa.py and app.py read the
    default org's copy through it."""
    return Path(EXTRACT_DIR).absolute() if not org_id or store.valid_org_id(org_id) == store.DEFAULT_ORG \
        else store.org_dir(org_id) / "extracted"


def _sha(text):
    return hashlib.sha1((text or "").encode()).hexdigest()


def _truncate(text, max_chars=_MAX):
    if len(text) <= max_chars:                # ponytail: same head+tail idiom as ingest — a giant tail still bounds cost
        return text
    half = max_chars // 2
    return text[:half] + "\n\n...[TRUNCATED]...\n\n" + text[-half:]


def _load_prior(sid, org_id=None):
    """Prior persisted record, or None. A missing OR corrupt/legacy file -> None -> FULL (JSONDecodeError is a
    ValueError; swallowing it here keeps a bad *cache* from being misread as bad *session content* by the drain)."""
    try:
        with open(_out(sid, org_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _stamp(data, sess):
    """(Re)stamp the watermark from the CURRENT session — never inherit a stale hash across a merge."""
    turns = sess.get("turns") or []
    data["_schema_version"] = extract.SCHEMA_VERSION
    data["_transcript_hash"] = _sha(sess["transcript"])
    data["_extracted_msgs"] = sess["n_user"] + sess["n_asst"]
    data["_prefix_hash"] = _sha("\n\n".join(turns))   # all current turns = the prefix the NEXT pass must match
    return data


def persist(sess, data, org_id=None):
    """Atomic write of the extracted record (tmp + os.replace, same idiom as csa._extract_sessions/queue)."""
    os.makedirs(extract_dir(org_id), exist_ok=True)
    dst = _out(sess["session_id"], org_id)
    tmp = dst + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, dst)


def drop(sid, org_id=None):
    """Delete one session's extraction cache and any half-written `.tmp` beside it. Idempotent."""
    try:
        dst = _out(sid, org_id)
    except ValueError:
        return                                    # a path-unsafe id could never have been written
    for p in (dst, dst + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass


def build_digest(prior, cap=40):
    """Compact 'what we already know' block for the continuation prompt. Built from the STORED record, which is
    redaction-clean (facts were derived from an already-redacted transcript). Capped so it stays small on huge sessions."""
    lines = [f"SUMMARY: {prior.get('summary', '')}"]
    wo = prior.get("worked_on") or []
    if wo:
        lines.append("WORKED_ON: " + ", ".join(str(w) for w in wo[:cap]))
    hc = prior.get("hidden_context") or []
    if hc:
        lines.append("FACTS ALREADY EXTRACTED:")
        lines += [f"- {h.get('claim', '')}" for h in hc[-cap:]]   # newest facts (merge appends) — likeliest to be re-touched
    ents = prior.get("entities") or []
    ent_lines = [f"- {(e.get('id') or '').strip()} ({e.get('type')})" for e in ents[:cap] if (e.get('id') or '').strip()]
    if ent_lines:
        lines.append("ENTITIES ALREADY KNOWN:")
        lines += ent_lines
    return "\n".join(lines)


def _dedup_dicts(seq):
    """Order-preserving dedup of a list of dicts/scalars by a canonical json key."""
    seen, out = set(), []
    for x in seq:
        k = json.dumps(x, sort_keys=True, default=str)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def merge_records(prior, new):
    """Naive union with exact-`ref` dedup. NEVER rewrites a prior claim, so a human label keyed by
    store.hc_ref(claim) (hc_types/reviews/signals/clusters) survives by construction. A reworded restatement
    just becomes a new unlabeled row (verified harmless: write_clusters counts distinct sessions, so same-session
    near-dups don't inflate ⟳N corroboration). Bills only the NEW (tail) call."""
    m = dict(prior)
    prior_refs = {store.hc_ref(h.get("claim")) for h in prior.get("hidden_context", [])}
    m["hidden_context"] = list(prior.get("hidden_context", [])) + \
        [h for h in new.get("hidden_context", []) if store.hc_ref(h.get("claim")) not in prior_refs]
    prior_eids = {(e.get("id") or "").strip() for e in prior.get("entities", [])}
    m["entities"] = list(prior.get("entities", [])) + \
        [e for e in new.get("entities", []) if (e.get("id") or "").strip() and (e.get("id") or "").strip() not in prior_eids]
    # ponytail: union by id keeps the PRIOR entity's attributes for a re-seen id (a tail 'PR-1 status open->merged'
    #   goes stale vs a full re-extract). Accepted: entity labels key off id (not attrs) so nothing is lost; the
    #   next schema-bump/prefix-change full pass refreshes attrs. Shallow per-key attr-merge if this bites.
    for k in ("relations", "actions", "links", "data_sources", "worked_on"):
        m[k] = _dedup_dicts(list(prior.get(k, [])) + list(new.get(k, [])))
    m["summary"] = new.get("summary") or prior.get("summary")
    m["deployed_to_prod"] = bool(prior.get("deployed_to_prod")) or bool(new.get("deployed_to_prod"))
    m["_cost_usd"] = new.get("_cost_usd")               # bill the tail call, not the (already-billed) prior full
    m["_usage"] = new.get("_usage") or {}
    return m


def decide(prior, sess):
    """(action, reason). action in {skip, incremental, full}. Pure — the testable core of the decision rule."""
    # Membership in COMPAT_SCHEMA_VERSIONS, not equality: an additive prompt change (as_of, v2) keeps every
    # v1 cache valid — equality here would have turned the version bump into a corpus-wide re-extract bill
    # for byte-identical re-shipped sessions ("full" short-circuits BEFORE the unchanged-hash skip below).
    if not prior or prior.get("_schema_version") not in extract.COMPAT_SCHEMA_VERSIONS:
        return "full", ("no-prior" if not prior else "schema-mismatch")
    if prior.get("_transcript_hash") == _sha(sess["transcript"]):
        return "skip", "unchanged"
    turns = sess.get("turns")
    wm = prior.get("_extracted_msgs")
    cur = sess["n_user"] + sess["n_asst"]
    if (turns is not None and isinstance(wm, int) and cur > wm
            and prior.get("_prefix_hash") is not None
            and _sha("\n\n".join(turns[:wm])) == prior["_prefix_hash"]):
        return "incremental", "grew"
    return "full", "prefix-rewrite"     # same-count-but-changed, shrank/compacted, or turns/prefix unavailable


def _run_llm(call):
    """Retry once on a transient CLI/transport blip. A ValueError (unparseable model output) re-raises
    immediately so the drain's is_poison classifier quarantines the session instead of stopping the whole batch."""
    for attempt in range(2):
        try:
            return call()
        except ValueError:
            raise
        except Exception:
            if attempt:
                raise


def extract_session(sess, model, con, org_id=None):
    """Decide skip/incremental/full, call `claude -p` only when needed, merge, bill (never on skip), and STAMP the
    returned record. Returns (data, action). The CALLER must index THEN persist() (persist-after-index is the
    safe ordering — a failed index must not leave a hash-stamped record that SKIPs forever)."""
    sid = sess["session_id"]
    prior = _load_prior(sid, org_id)
    action, reason = decide(prior, sess)
    if action == "skip":
        return prior, "skip"                            # already stamped + indexed; caller short-circuits, no bill
    ct = entity_types.custom(con)                       # this org's team-defined entity types -> the prompt
    if action == "incremental":
        wm = prior["_extracted_msgs"]
        # Re-redact the JOIN, then truncate — mirrors ingest.py's belt-and-suspenders on `transcript`.
        # `turns` are redacted individually, so a secret SPLIT across the "\n\n" boundary between two
        # messages matches in neither turn alone but does in the join. ingest re-redacts `transcript` for
        # exactly this; `turns` never got the same pass, and this is the one place they are re-joined and
        # handed to the LLM (whose output store.py writes without re-redacting). Redact BEFORE _truncate:
        # capping first would behead the anchor and silently disarm every length/terminator-based pattern.
        _min, _deny, _allow = tenant.redaction_policy()
        _mode = redact.max_mode(sess.get("redaction_mode", "secrets"), _min)
        tail = _truncate(redact.redact("\n\n".join(sess["turns"][wm:]), _mode, _deny, _allow)[0])
        new = _run_llm(lambda: extract.extract_incremental(tail, build_digest(prior), model=model, custom_types=ct))
        data = merge_records(prior, new)
    else:                                               # full
        data = _run_llm(lambda: extract.extract(sess["transcript"], model=model, custom_types=ct))
    _stamp(data, sess)
    data["_action"] = action
    data["_full_reason"] = reason if action == "full" else None
    step = "extract-incremental" if action == "incremental" else "extract-full"
    store.record_cost(con, step, model, sid, data.get("_usage"), data.get("_cost_usd"))   # single biller
    return data, action


if __name__ == "__main__":
    # --- decision matrix (pure, no LLM) ---
    def sess_of(turns, transcript=None):
        return {"session_id": "s", "n_user": sum(t.startswith("USER") for t in turns),
                "n_asst": sum(t.startswith("ASSISTANT") for t in turns),
                "transcript": transcript if transcript is not None else "\n\n".join(turns), "turns": turns}

    t1 = ["USER: a", "ASSISTANT: b"]
    s1 = sess_of(t1)
    rec = _stamp({"hidden_context": []}, s1)
    assert decide(None, s1) == ("full", "no-prior")
    assert decide({"_schema_version": 999, "_transcript_hash": rec["_transcript_hash"]}, s1)[0] == "full", "schema bump -> full"
    assert decide(rec, s1) == ("skip", "unchanged"), "byte-identical -> skip"

    t2 = t1 + ["USER: c", "ASSISTANT: d"]               # append-only growth
    s2 = sess_of(t2)
    assert decide(rec, s2) == ("incremental", "grew"), "append-only growth -> incremental"

    t2b = ["USER: REWRITTEN", "ASSISTANT: b", "USER: c", "ASSISTANT: d"]   # prefix compacted/rewritten
    assert decide(rec, sess_of(t2b))[0] == "full", "rewritten prefix -> full (never a corrupt incremental)"
    assert decide(rec, sess_of(t1[:1]))[0] == "full", "shrank -> full"
    assert decide({**rec, "_prefix_hash": None}, s2)[0] == "full", "missing prefix_hash -> full"
    assert decide(rec, {**s2, "turns": None})[0] == "full", "legacy pending w/o turns -> full (no crash)"

    # --- merge: union + exact-ref dedup + label-safety ---
    prior = {"summary": "v1", "worked_on": ["x"], "deployed_to_prod": False,
             "hidden_context": [{"type": "gotcha", "claim": "CLAIM ONE"}],
             "entities": [{"id": "PR-1", "type": "pr"}], "relations": [], "actions": [], "links": [], "data_sources": []}
    new = {"summary": "v2", "worked_on": ["x", "y"], "deployed_to_prod": True,
           "hidden_context": [{"type": "gotcha", "claim": "CLAIM ONE"},        # exact restatement of a prior claim
                              {"type": "decision", "claim": "CLAIM TWO"}],      # genuinely new
           "entities": [{"id": "PR-1", "type": "pr"}, {"id": "PR-2", "type": "pr"}],
           "relations": [], "actions": [], "links": [], "data_sources": [],
           "_cost_usd": 0.02, "_usage": {"input_tokens": 500}}
    m = merge_records(prior, new)
    claims = [h["claim"] for h in m["hidden_context"]]
    assert claims == ["CLAIM ONE", "CLAIM TWO"], f"exact-ref dedup: prior kept once, new added: {claims}"
    assert store.hc_ref("CLAIM ONE") == store.hc_ref(prior["hidden_context"][0]["claim"]), "prior claim text is byte-identical -> its ref (and any label) survives"
    assert [e["id"] for e in m["entities"]] == ["PR-1", "PR-2"], "entities union by id"
    assert m["worked_on"] == ["x", "y"] and m["summary"] == "v2" and m["deployed_to_prod"] is True
    assert m["_usage"] == {"input_tokens": 500} and m["_cost_usd"] == 0.02, "bill the TAIL call only"

    # --- retry / poison classification ---
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "ok"
    assert _run_llm(flaky) == "ok", "retry once on a transient blip"
    try:
        _run_llm(lambda: (_ for _ in ()).throw(ValueError("bad output")))
        assert False, "ValueError must NOT be retried (it's the poison signal)"
    except ValueError:
        pass

    print("OK incremental: decision matrix (skip/full/incremental/prefix-rewrite/shrink/no-turns); "
          "union merge label-safe + tail-billed; retry-once vs poison-raise")

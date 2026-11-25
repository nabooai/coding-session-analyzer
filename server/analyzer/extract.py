"""transcript -> structured knowledge JSON. The model call goes through `analyzer.llm.complete`, so it
runs on whatever provider is configured (Claude via `claude -p`, or OpenAI — see analyzer/llm.py)."""
import json
from analyzer import llm, entity_types

# Bump on any change to PROMPT/schema below — and add the OLD number to COMPAT_SCHEMA_VERSIONS when the change
# is purely additive. analyzer.incremental gates the persisted cache on membership in that set, so only a bump
# that also SHRINKS the set forces a full re-extract of cached sessions — a corpus-wide LLM bill: measure it
# before you type the digit. Purely ADDITIVE optional fields stay compatible: new ingests carry them, cached
# sessions keep their record, incremental.merge_records unions old items (without the key) alongside new ones
# (with it), and a fact simply lacks the field until its session is re-extracted (`csa.py reextract <sid>` buys
# that per session). History: `repo`/`path` (2026-07-16) shipped additive WITHOUT a bump; `as_of` (2026-07-31)
# shipped additive WITH the bump to 2 + v1 kept compatible — same economics, and the version number now
# actually records the prompt generation. Per-org CUSTOM entity types (entity_types.prompt_block below) are
# also no-bump: no JSON-shape change, and a tenant editing its taxonomy must not re-bill its back catalog.
SCHEMA_VERSION = 2
COMPAT_SCHEMA_VERSIONS = {1, 2}   # v1 caches stay valid under v2 (as_of is additive) — NO surprise re-extracts

PROMPT = """You analyze a Claude Code session transcript and extract a structured knowledge record as JSON.

Return ONLY a JSON object (no prose, no markdown fence):
{
  "summary": "one sentence on what this session was about",
  "worked_on": ["short topic/file strings"],
  "hidden_context": [
    {"type": "decision|gotcha|constraint|todo|preference",
     "scope": "organizational|personal",
     "confidence": "high|low",
     "claim": "knowledge that exists ONLY in this session — not in any doc/README/ticket",
     "evidence": "short quote or paraphrase from the transcript",
     "repo": "repo name this claim is ABOUT, if it is about code (else omit)",
     "path": "repo-relative file/dir the claim is about, if any (else omit)",
     "as_of": "ISO date (YYYY-MM-DD) when this became true, ONLY if the transcript states it (else omit)"}
  ],
  "entities": [
    {"id": "stable canonical id (UPPERCASE tickets e.g. KLG-200; repo/service/file names as-is)",
     "type": "ticket|pr|release|service|repo|file|person|data_source|env|url",
     "name": "human name",
     "attributes": {"version": "...", "status": "...", "owner": "...", "env": "..."}}
  ],
  "relations": [
    {"source_id": "entity id", "rel": "implements|fixes|blocks|depends_on|part_of|deploys|references|owns",
     "target_id": "entity id", "context": "short phrase"}
  ],
  "actions": [
    {"verb": "deploy|delete|create|fix|investigate|...", "target_id": "entity id", "context": "short phrase"}
  ],
  "links": [{"url": "http...", "title": "what it is"}],
  "data_sources": ["company data sources/systems referenced, e.g. acme.com, analytics.db, BigQuery"],
  "deployed_to_prod": false
}

Rules:
- deployed_to_prod: true ONLY if code or config was ACTUALLY RELEASED to a production environment in
  THIS session — a deploy/release command ran, a rollout was applied, a tag was pushed and shipped.
  It is FALSE for debugging a production bug, investigating prod logs, reading prod data, preparing or
  reviewing a release, or deploying to staging/lab/dev. The question this answers is "did production
  change because of this session", not "was production discussed". When unsure, false.
- hidden_context is the PRIORITY: capture knowledge that exists ONLY here.
- scope EACH hidden_context item: "organizational" = about the project/system/codebase/team (configs, architecture, deploys, services, incidents — shareable knowledge); "personal" = the individual's local/machine setup (MCP/OAuth/onboarding, local .env quirks, personal preferences) relevant only to that user.
- confidence EACH hidden_context item honestly: "high" = you are sure this is a genuine undocumented fact AND its type is right; "low" = you are UNSURE whether it's real hidden-context (vs a passing remark) OR unsure which type fits. Do NOT force a confident guess — "low" routes it to a human for review.
- as_of ONLY when the transcript itself SAYS when the claim became true ("we decided this in March", "since the
  2026-06-12 deploy") — an ISO date (YYYY-MM-DD, optionally with a Z-form time; no timezone offsets),
  best-effort day precision. NEVER infer it from the session's own date (omitting it means "true as of this
  session", which is the default). Most items have no as_of.
- CODE-HOME: if a hidden_context claim is about code, set "repo" (and "path" when it names a specific file/dir) to
  where that code ACTUALLY LIVES — read it off the transcript's file paths and commands, NOT the session's working
  directory: a session often runs from one repo while working on another's code. Omit both when the claim isn't
  about code, and omit "repo" rather than guess. This BINDS the referent: a claim like "prefect_deploy.py does not
  exist anywhere in the repo" is false as written and true once "the repo" is pinned — an unbound claim like that
  was recorded from a search of the wrong repo and misled readers for 10 days. Prefer a bare repo name ("ledger")
  and a repo-relative path ("ledgerv2_core/etl3/prefect_deploy.py") — no absolute or user-specific paths.
- Make an entity for every Ticket, PR, Release, Service, Repo, and important File/Person referenced; give it a
  stable canonical id and any attributes you can infer (version, status, owner, env, url).
- relations connect two entity ids that appear in "entities" (e.g. PR-2276 implements KLG-200, svcA depends_on svcB).
- Reuse the SAME id on every mention of the same thing. Resolve "KLG-200" to id "KLG-200".
- If nothing for a field use [] / {} / false. Be concise and accurate.

TRANSCRIPT:
"""


def _extract_json(text):
    i, j = text.find("{"), text.rfind("}")     # brace-slice ignores ``` fences & prose around the object
    if i == -1 or j == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[i:j + 1])


def _run(prompt_text, model, timeout):
    text, cost_usd, usage = llm.complete(prompt_text, model, timeout)   # provider seam (claude/openai)
    data = _extract_json(text)                            # raises ValueError on unparseable output (poison signal)
    data["_cost_usd"] = cost_usd
    data["_usage"] = usage or {}                          # input/output/cache tokens, for the cost ledger
    return data


# schema/rules block only (everything before the "TRANSCRIPT:" label) — reused for the continuation prompt
_SCHEMA_RULES = PROMPT[:PROMPT.rindex("TRANSCRIPT:")]


def extract(transcript, model="main", timeout=300, custom_types=None):
    """`custom_types` = the org's team-defined entity types ([{key, description}, ...], from
    entity_types.custom(con)). None/[] -> the prompt is byte-identical to PROMPT (asserted below)."""
    return _run(_SCHEMA_RULES + entity_types.prompt_block(custom_types) + "TRANSCRIPT:\n" + transcript,
                model, timeout)


def extract_incremental(new_turns_text, prior_digest, model="main", timeout=300, custom_types=None):
    """Extract ONLY the knowledge in a session's CONTINUATION turns, given a digest of what was already extracted
    earlier in the same session. Same schema/rules as extract(); the model is told not to repeat prior facts, so
    the input (and cost) is the tail, not the whole grown transcript."""
    prompt_text = (_SCHEMA_RULES + entity_types.prompt_block(custom_types) +
                   "This transcript is the CONTINUATION of a session whose earlier turns were ALREADY extracted. "
                   "Extract ONLY knowledge that is NEW in the continuation below — do NOT repeat anything already "
                   "listed here. Return the same JSON shape (facts/entities/etc. found ONLY in the continuation).\n\n"
                   "ALREADY EXTRACTED (do not repeat):\n" + prior_digest +
                   "\n\nCONTINUATION TRANSCRIPT:\n" + new_turns_text)
    return _run(prompt_text, model, timeout)


if __name__ == "__main__":
    d = _extract_json('noise ```json\n{"summary":"x","relations":[]}\n``` trailing')
    assert d["summary"] == "x" and d["relations"] == [], d
    try:
        _extract_json("no json here")
        assert False, "unparseable output must raise ValueError (the drain's poison signal)"
    except ValueError:
        pass
    # the continuation reuses the schema/rules but drops the raw "TRANSCRIPT:" tail (replaced by the digest + tail)
    assert "hidden_context" in _SCHEMA_RULES and "TRANSCRIPT:" not in _SCHEMA_RULES, "schema/rules slice is wrong"
    # SELF-INGESTION GUARD. ingest.is_self_session matches a session's first user message against
    # ingest.INTERNAL_PROMPTS to keep CSA from mining its OWN `claude -p` calls. That prefix is this PROMPT's
    # opening sentence, duplicated as a literal in another file. Reword line 1 and the guard stops matching:
    # CSA re-ingests its own extraction transcripts (last time: 225 poisoned sessions, 2435 dup facts, 452MB).
    # Append to the schema/rules all you like — leave the first sentence alone.
    from analyzer import ingest
    assert PROMPT.startswith(ingest.INTERNAL_PROMPTS[0]), \
        "PROMPT's first sentence no longer matches ingest.INTERNAL_PROMPTS[0] -> CSA will re-ingest itself"
    assert '"repo"' in _SCHEMA_RULES and '"path"' in _SCHEMA_RULES, "code-home fields dropped from the schema"
    # custom entity types: no customs -> the default prompt, byte-identical (no cache/behavior drift for
    # every tenant that defines none); with customs -> the block lands between the rules and TRANSCRIPT:
    assert _SCHEMA_RULES + entity_types.prompt_block(None) + "TRANSCRIPT:\n" == PROMPT, \
        "no-customs prompt must be byte-identical to PROMPT"
    _blk = entity_types.prompt_block([{"key": "client", "description": "a paying customer account"}])
    _p = _SCHEMA_RULES + _blk + "TRANSCRIPT:\n"
    assert '"client" = a paying customer account' in _p and _p.endswith("TRANSCRIPT:\n")
    print("OK extract: json fences+noise; unparseable->ValueError; schema slice reused for continuation; "
          "code-home in schema; self-ingestion guard prefix intact; custom-types block injected, none==default")

"""Contract pins for the fuzzy port (analyzer.fuzzy_resolve / fuzzy_axes / fuzzy_index).

Covers the precision tiers, the anti-hallucination abstains, and the cross-axis resolve_everywhere
(identity>containment, prefix arm, family collapse) that make "what did <person> …" scope correctly.
"""
import sqlite3

from analyzer.fuzzy_resolve import resolve, Status, normalize, _surfaces
from analyzer.fuzzy_index import Candidate, build_full, resolve_everywhere
from analyzer import fuzzy_axes


def test_resolve_tiers():
    # identity outranks containment: the value that IS the mention beats one that merely contains it
    r = resolve("acme", ["acme", "production-acme"], word_tokens=True)
    assert r.status is Status.RESOLVED and r.value == "acme" and not r.via_token, r
    # unique token carrier resolves, flagged via_token — "jules" is a component of the email
    r = resolve("jules", ["jules@example.com", "mara@example.com"], word_tokens=True)
    assert r.status is Status.RESOLVED and r.value == "jules@example.com" and r.via_token, r
    # a one-edit typo on a >4-char token resolves (prefix/fuzzy); the roster is the pool
    r = resolve("julesr", ["jules@example.com", "mara@example.com", "hermes@example.com"], word_tokens=True)
    assert r.status is Status.RESOLVED and r.value == "jules@example.com", r


def test_anti_hallucination():
    owners = ["jules@example.com", "mara@example.com", "hermes@example.com"]
    # absent name never fabricates a match
    assert resolve("xavier", owners, word_tokens=True).status is Status.ABSTAIN
    # short-token guard: a 4-char non-exact token abstains ("mraa" is 1 transposition off "mara")
    assert resolve("mraa", owners, word_tokens=True).status is Status.ABSTAIN
    # a generic query word must not spuriously match an owner
    for noise in ("work", "last", "week", "deploy"):
        assert resolve(noise, owners, word_tokens=True).status is not Status.RESOLVED, noise


def test_resolve_everywhere():
    cands = [
        Candidate("jules@example.com", "sessions.user", "person"),      # owner axis
        Candidate("mara@example.com", "sessions.user", "person"),
        Candidate("Jules Renner", "entities[person].name", "person"),  # person entity
        Candidate("jules.md", "entities[file].name", "file"),         # a file carrying the token
        Candidate("mokafor42", "entities[person].id", "person"),     # prefix-arm target
    ] + [Candidate(f"ENG-{i}", "entities[ticket].id", "ticket") for i in range(1, 8)]  # a family
    idx = build_full(cands)

    # "jules" surfaces across kinds (person owner, person entity, file) — the honest cross-axis map;
    # a person entity must be present (this is what owner-scoping keys off).
    ents = resolve_everywhere(idx, "jules")
    persons = [e for e in ents if e.kind == "person"]
    assert persons and any("jules" in e.value.lower() for e in persons), ents

    # prefix arm: "mokafor" uniquely narrows to the person mokafor42 (bounded-edit retrieval misses it)
    ents = resolve_everywhere(idx, "mokafor")
    assert any(e.kind == "person" and e.value == "mokafor42" and e.resolved for e in ents), ents

    # family: "eng" is a token of 7 ENG-\d ids in ONE axis -> collapsed to one n_members entity
    ents = resolve_everywhere(idx, "eng")
    fam = [e for e in ents if e.n_members > 1]
    assert fam and fam[0].n_members == 7, [(e.value, e.n_members) for e in ents]


def test_axes_and_kind():
    # normalize + surface tiers (the anti-hallucination substrate)
    assert normalize("Orbit-AI") == normalize("orbit_ai") == "orbitai"
    surfs = {n: t for _, n, t in _surfaces("jules@example.com", word_tokens=True)}
    assert surfs["jules"] is True and surfs["julesexamplecom"] is False, surfs  # token vs identity tier
    # kind derivation: an owner/user column is a person axis; a repo column is a repo
    assert fuzzy_axes.referent_kind("sessions", "user") == "person"
    assert fuzzy_axes.referent_kind("sessions", "project") == "project"

    # axis auto-selection over a tiny SQLite store: user -> person axis; the polymorphic entities
    # table expands per type; an opaque-id / datetime column is rejected.
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE sessions(session_id TEXT, user TEXT, project TEXT, started_at TEXT, source TEXT);"
        "CREATE TABLE entities(id TEXT, type TEXT, name TEXT, attributes TEXT);"
    )
    con.execute("INSERT INTO sessions VALUES('s1','jules@example.com','ledger','2026-07-13T00:00:00Z','session')")
    con.execute("INSERT INTO sessions VALUES('s2','mara@example.com','csa','2026-07-12T00:00:00Z','session')")
    for i, (t, n) in enumerate([("person", "Jules"), ("person", "Mara"), ("file", "build_all.py"),
                                ("file", "store_env.py"), ("repo", "ledger"), ("repo", "csa")]):
        con.execute("INSERT INTO entities VALUES(?,?,?,?)", (f"e{i}", t, n, "{}"))
    axes = fuzzy_axes.select_axes(con)
    keys = {a.key: a.kind for a in axes}
    assert keys.get("sessions.user") == "person", keys
    assert any(k.startswith("entities[person]") for k in keys), keys
    assert any(k.startswith("entities[file]") for k in keys), keys
    assert "sessions.started_at" not in keys and "sessions.session_id" not in keys, keys  # rejected

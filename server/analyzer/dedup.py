"""Claim-level near-duplicate clustering (rung-1, NO deps): the same finding rediscovered across sessions.

overlap-coefficient (|A∩B|/min) on content tokens + an absolute shared-token floor. The coefficient (not Jaccard)
handles the common case where one session states a finding more fully than another; the floor kills short-claim
vocab-collisions (two unrelated one-liners sharing 'docker-compose'). Tuned on the real corpus: clusters the
disk-IO finding that was double-extracted (gold in one session, 'already-documented' in another) and 9 other
genuine cross-session dups, with no obvious false merges. Embeddings would catch semantic dups token-overlap
misses — not worth the torch weight at hundreds/low-thousands of claims.
"""
import re

_STOP = set("the a an and or of to in on for with is are was were be been it this that these those as at by "
            "from not no into its their his her you your i we our they them but so than then only also can could "
            "will would should may might must have has had do does did re vs".split())
_TOK = re.compile(r"[a-z0-9][a-z0-9._/+-]*")
THRESHOLD, MIN_SHARED = 0.5, 6          # see module docstring — tuned on the live corpus


def tokens(text):
    return {t for t in _TOK.findall((text or "").lower()) if len(t) > 1 and t not in _STOP}


def overlap(a, b):
    """|A∩B|/min(|A|,|B|) on token sets. Public: store.carry_refs reuses it for same-session rewording."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def cluster(rows):
    """rows: [(ref, claim), ...] -> {ref: cluster_id}. cluster_id = lexicographically smallest ref in the
    component (deterministic). Singletons map to themselves.
    ponytail: O(n^2) token-set intersections — instant at hundreds, fine at low-thousands; if it ever matters,
    generate candidate pairs via the FTS index first instead of the full triangle."""
    toks = {ref: tokens(claim) for ref, claim in rows}
    refs = list(toks)
    parent = {r: r for r in refs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(refs)):
        a = toks[refs[i]]
        for j in range(i + 1, len(refs)):
            b = toks[refs[j]]
            if len(a & b) >= MIN_SHARED and overlap(a, b) >= THRESHOLD:
                parent[find(refs[i])] = find(refs[j])

    groups = {}
    for r in refs:
        groups.setdefault(find(r), []).append(r)
    return {m: min(members) for members in groups.values() for m in members}


if __name__ == "__main__":
    # golden test: the disk-IO finding extracted in two sessions (the bug that motivated this) MUST land in one
    # cluster; an unrelated claim must NOT join. Paraphrased from the real corpus rows 42cc.. and caca...
    rows = [
        ("A", "Disk-IO measurement: SQLite ~548 MB/s vs Modern ~13 MB/s (~42x), corr(write,disk)=+0.74, "
              "~12 KB/applied-row — real WAL + 9-index checkpoint write-amplification, not VM-floor noise"),
        ("B", "The reviewer's VM-floor-noise hypothesis was WRONG: an isolated cgroup io.stat measurement shows "
              "SQLite disk is genuinely ~548 MB/s, corr +0.74, ~12 KB/applied-row of WAL + 9-index checkpoint "
              "write-amplification (~42x over raw data)"),
        ("C", "Jira scope-id is the numeric project.id, not the project key; paths must be [[project.id]]"),
    ]
    cl = cluster(rows)
    assert cl["A"] == cl["B"], cl
    assert cl["C"] != cl["A"], cl
    print("OK dedup: cross-session disk-IO dup clusters together; unrelated claim stays separate")

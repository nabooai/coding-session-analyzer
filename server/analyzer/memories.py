"""Ingest Claude Code's curated memory files as high-trust, pre-validated knowledge (no LLM)."""
import os, glob, time
from analyzer import redact

MEM_ROOT = os.path.expanduser("~/.claude/projects")
MEM_GLOB = os.path.join(MEM_ROOT, "*", "memory", "*.md")
_TYPE_MAP = {"feedback": "preference", "project": "constraint", "user": "preference", "reference": "todo"}


def _parse_frontmatter(text):
    """Split leading YAML-ish frontmatter (stdlib only — no pyyaml)."""
    fm, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            body = text[end + 4:].strip()
    return fm, body


def _project(rel):
    projdir = rel.split(os.sep)[0]            # e.g. -Users-mara-Documents-Git-acme-ai
    return projdir.split("-Git-")[-1] if "-Git-" in projdir else "memory"


def load_memories():
    """Return (session_dict, data_dict) pairs shaped like ingest+extract output — index VERBATIM, no LLM."""
    out = []
    for path in sorted(glob.glob(MEM_GLOB)):
        if os.path.basename(path) == "MEMORY.md":      # index files are redundant with the detail files
            continue
        with open(path) as fh:
            text = fh.read()
        if not text.strip():
            continue
        fm, body = _parse_frontmatter(text)
        rel = os.path.relpath(path, MEM_ROOT)
        claim = redact.redact(fm.get("description") or fm.get("name") or os.path.basename(path))[0]
        mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))
        sess = {"session_id": f"mem:{rel}", "path": path, "project": _project(rel), "git_branch": None,
                "started_at": mtime, "ended_at": mtime, "n_user": 0, "n_asst": 0, "source": "memory"}
        data = {"summary": claim, "deployed_to_prod": False,
                "hidden_context": [{"type": _TYPE_MAP.get(fm.get("type", ""), "preference"),
                                    "claim": claim, "evidence": redact.redact(body[:4000])[0]}],
                "entities": [], "relations": [], "actions": [], "links": [], "data_sources": []}
        out.append((sess, data))
    return out


if __name__ == "__main__":
    mems = load_memories()
    assert all("session_id" in s and d["hidden_context"][0]["claim"] for s, d in mems), "bad memory shape"
    print(f"OK memories: parsed {len(mems)} memory files (verbatim, no LLM)")

"""AI Models: the model providers an admin registers, and which model each role runs on.

Two roles: `main` reads each session and answers questions, `small` judges facts and routes. A role
setting is "<provider id>/<model>". With no providers registered, both roles run on the Claude
credential the deployment's environment carries (the Helm secret), on sonnet and haiku, so a fresh
install works before anyone opens the page.
"""
import json
import os
import re
import tempfile
from dataclasses import dataclass

from sqlalchemy import delete, insert, select, update

from analyzer import db, netguard, orgs
from analyzer.tenant import LlmProvider

_P = LlmProvider.__table__

KINDS = {
    "anthropic": "Anthropic",
    "bedrock": "AWS Bedrock",
    "vertex": "Google Vertex",
    "openai": "OpenAI",
    "azure_openai": "Azure OpenAI",
}
CLI_KINDS = {"bedrock", "vertex"}                # run through the Claude Code CLI; the rest are one HTTPS POST
ENV_DEFAULT = {"main": "sonnet", "small": "haiku"}
AZURE_API_VERSION = "2024-10-21"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    kind: str
    endpoint: str
    region: str
    project: str
    api_version: str
    credential: str
    models: tuple
    updated_at: str = ""


@dataclass(frozen=True)
class Target:
    """What one call runs on. `provider` None = the environment's Claude credential."""
    provider: Provider | None
    model: str


_COLS = tuple(c.name for c in _P.columns)


def _row(r):
    d = dict(zip(_COLS, r))
    return Provider(id=d["id"], name=d["name"] or "", kind=d["kind"], endpoint=d["endpoint"] or "",
                    region=d["region"] or "", project=d["project"] or "", api_version=d["api_version"] or "",
                    credential=d["credential"] or "", models=tuple(json.loads(d["models"] or "[]")),
                    updated_at=d["updated_at"] or "")


def list_all(con=None):
    con = con or orgs.control_db()
    return [_row(r) for r in db.run(con, select(*_P.columns).order_by(_P.c.created_at))]


def get(provider_id, con=None):
    con = con or orgs.control_db()
    r = db.run(con, select(*_P.columns).where(_P.c.id == provider_id)).fetchone()
    return _row(r) if r else None


def public(p: Provider):
    """What the page may show: everything but the credential, which reads as set or not."""
    return {"id": p.id, "name": p.name, "kind": p.kind, "kind_label": KINDS[p.kind], "endpoint": p.endpoint,
            "region": p.region, "project": p.project, "api_version": p.api_version,
            "credential_set": bool(p.credential), "models": list(p.models), "updated_at": p.updated_at or None}


def checked(body, existing: Provider | None = None):
    """A provider from a form body -> Provider, raising ValueError with a sentence. A blank credential
    on an edit keeps the stored one, so the page never has to show it."""
    kind = (body.get("kind") or (existing.kind if existing else "")).strip()
    if kind not in KINDS:
        raise ValueError("Pick a provider type.")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Name the provider.")
    models = [m.strip() for m in body.get("models") or [] if isinstance(m, str) and m.strip()]
    if not models or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}", m) for m in models):
        raise ValueError("List at least one model id, like claude-sonnet-4-5.")
    credential = str(body.get("credential") or "").strip() or (existing.credential if existing else "")
    p = Provider(id=existing.id if existing else _new_id(name), name=name, kind=kind,
                 endpoint=str(body.get("endpoint") or "").strip().rstrip("/"),
                 region=str(body.get("region") or "").strip(), project=str(body.get("project") or "").strip(),
                 api_version=str(body.get("api_version") or "").strip(), credential=credential,
                 models=tuple(dict.fromkeys(models)))
    need = {"anthropic": ("credential",), "openai": ("credential",),
            "azure_openai": ("credential", "endpoint"), "bedrock": ("credential", "region"),
            "vertex": ("credential", "region", "project")}[kind]
    missing = [f for f in need if not getattr(p, f)]
    if missing:
        raise ValueError(f"{KINDS[kind]} needs {', '.join(missing).replace('credential', 'a key')}.")
    if kind == "vertex":
        try:
            json.loads(credential)
        except ValueError:
            raise ValueError("A Vertex key is the service account's JSON key file.") from None
    if p.endpoint and not re.fullmatch(r"https://[^\s/?#]+(/[^\s?#]*)?", p.endpoint):
        raise ValueError("The endpoint is a full https:// address.")
    if p.endpoint:
        netguard.check_url(p.endpoint)
    return p


def _new_id(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "provider"
    taken = {p.id for p in list_all()}
    return base if base not in taken else next(f"{base}-{i}" for i in range(2, 1000) if f"{base}-{i}" not in taken)


def save(p: Provider, updated_by, con=None):
    con = con or orgs.control_db()
    now = orgs._now()
    vals = {"name": p.name, "kind": p.kind, "endpoint": p.endpoint, "region": p.region, "project": p.project,
            "api_version": p.api_version, "credential": p.credential, "models": json.dumps(list(p.models)),
            "updated_at": now, "updated_by": updated_by or None}
    if get(p.id, con):
        db.run(con, update(_P).values(**vals).where(_P.c.id == p.id))
    else:
        db.run(con, insert(_P).values(id=p.id, created_at=now, **vals))
    orgs._audit(con, "provider_saved", None, f"{updated_by or '-'} {p.id} ({p.kind}, {len(p.models)} models)")
    con.commit()
    return p


def remove(provider_id, updated_by, con=None):
    """Delete a provider; a role that pointed at it falls back to the environment."""
    from analyzer import tenant
    con = con or orgs.control_db()
    n = db.run(con, delete(_P).where(_P.c.id == provider_id)).rowcount
    if n:
        orgs._audit(con, "provider_removed", None, f"{updated_by or '-'} {provider_id}")
        con.commit()
        stale = {f: "" for f in ("main_model", "small_model")
                 if getattr(tenant.deployment(con), f).split("/", 1)[0] == provider_id}
        if stale:
            tenant._write_deployment(con, updated_by, stale)
            con.commit()
    return n > 0


def target(ref, con=None) -> Target:
    """A role (`main` / `small`), a "<provider>/<model>" ref, or a bare model name -> what to call."""
    from analyzer import tenant
    if ref in ENV_DEFAULT:
        stored = getattr(tenant.deployment(con), f"{ref}_model")
        if not stored:
            return Target(None, ENV_DEFAULT[ref])
        ref = stored
    if "/" in ref:
        pid, model = ref.split("/", 1)
        p = get(pid, con)
        if p is None:
            raise RuntimeError(f"the model provider {pid!r} was removed; pick another on AI Models")
        return Target(p, model)
    return Target(None, ref)


def claude_env(p: Provider | None):
    """The environment one Claude Code CLI call runs with: the process's own for the Helm default,
    otherwise the process's minus every Claude credential, plus this provider's."""
    if p is None:
        return None, None
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN",
                        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL",
                        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                        "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                        "OPENAI_MAIN_MODEL", "OPENAI_SMALL_MODEL")}
    keyfile = None
    if p.kind == "bedrock":
        env.update(CLAUDE_CODE_USE_BEDROCK="1", AWS_REGION=p.region)
        if ":" in p.credential:                          # an access key pair
            key_id, secret = p.credential.split(":", 1)
            env.update(AWS_ACCESS_KEY_ID=key_id, AWS_SECRET_ACCESS_KEY=secret)
        else:                                            # a Bedrock API key
            env["AWS_BEARER_TOKEN_BEDROCK"] = p.credential
    elif p.kind == "vertex":
        fd, keyfile = tempfile.mkstemp(prefix="csa-vertex-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(p.credential)
        env.update(CLAUDE_CODE_USE_VERTEX="1", CLOUD_ML_REGION=p.region, ANTHROPIC_VERTEX_PROJECT_ID=p.project,
                   GOOGLE_APPLICATION_CREDENTIALS=keyfile)
    return env, keyfile

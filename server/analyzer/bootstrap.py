"""The deployment's config file, applied by every `csa.py migrate`.

The Helm chart renders it from values and the compose mounts it from ./config, so an install needs no
command: the workspace, the issuer and who is admin come from the file, and editing it then deploying
again changes the deployment. No file, or an unset value, leaves the box as it is."""
import json
import os
from pathlib import Path

from analyzer import auth, orgs, store, tenant

CONFIG_PATH = Path(os.environ.get("CSA_CONFIG_PATH", "/etc/csa/config.json"))


def settings():
    """The config file as {section: {key: value}}, empty when this box has none."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        doc = json.loads(CONFIG_PATH.read_text())
    except ValueError as e:
        raise ValueError(f"{CONFIG_PATH} is not valid JSON ({e})") from e
    if not isinstance(doc, dict) or not all(isinstance(v, dict) for v in doc.values()):
        raise ValueError(f"{CONFIG_PATH} must be an object of sections (workspace, okta, telemetry)")
    return doc


def missing_config():
    """A warning when the config folder is mounted but holds no config.json, else ''."""
    if CONFIG_PATH.exists() or not CONFIG_PATH.parent.is_dir():
        return ""
    return (f"{CONFIG_PATH.parent} is mounted but holds no config.json, so no workspace is named and no "
            "sign-in link is printed. If you did write config/config.json, Docker did not share that folder "
            "with its VM: on Colima or Docker Desktop, keep the project under your home directory or add "
            "its path to the VM's shared folders, then run `docker compose up -d` again.")


def _value(section, key):
    return str(settings().get(section, {}).get(key) or "").strip()


def admin_group():
    """The Okta group whose members are admins, or '' when roles are managed in CSA."""
    return _value("okta", "adminGroup")


def admin_email():
    """The address the config file names as first admin, or '' when it names none."""
    return _value("workspace", "adminEmail").lower()


def _admin_link(con, domain):
    """A one-person sign-in link for workspace.adminEmail while that person holds no live session -> the
    log line, or None. The link names the address exactly, so it needs no invite code and nobody else
    can use it; a new run replaces an unused one."""
    email = admin_email()
    if not email:
        return None
    if not orgs.default_is_adopted(con):
        raise ValueError("workspace.adminEmail needs workspace.name")
    if "@" not in email or (domain and email.rsplit("@", 1)[1] != domain):
        raise ValueError(f"workspace.adminEmail {email!r} must be an address on {domain or 'your domain'}")
    if auth.sso_config(con) or any(t["active"] and not t["service"] and (t["email"] or "").lower() == email
                                   for t in orgs.list_tokens(store.DEFAULT_ORG, con=con)):
        return None
    return _new_link(con, email)


def _new_link(con, email):
    _, token = orgs.create_invite(store.DEFAULT_ORG, email, "chart", con=con)
    base = os.environ.get("CSA_PUBLIC_URL", "").rstrip("/")
    return f"admin sign-in link for {email}, valid {orgs.INVITE_TTL_DAYS} days: {base}/join/{token}"


def recovery_link(con=None):
    """A fresh one-time sign-in link for workspace.adminEmail, ending that person's lost browser sessions.
    Raises ValueError when the admin cannot be recovered this way."""
    con = con or orgs.control_db()
    email = admin_email()
    if not email:
        raise ValueError(missing_config() or f"workspace.adminEmail is not set in {CONFIG_PATH}")
    if not orgs.default_is_adopted(con):
        raise ValueError("workspace.adminEmail needs workspace.name")
    if auth.sso_config(con):
        raise ValueError("sign-in goes through your identity provider; grant admin with the Okta group "
                         "or `csa.py org role default <email> admin`")
    mine = [t for t in orgs.list_tokens(store.DEFAULT_ORG, con=con)
            if t["active"] and not t["service"] and (t["email"] or "").lower() == email]
    if any(t["hostname"] not in orgs.BROWSER_HOSTNAMES for t in mine):
        raise ValueError(f"{email} already has a machine token; run `csa.py org role default {email} admin`")
    for t in mine:
        orgs.revoke(t["token_hash"], con=con, org_id=store.DEFAULT_ORG)
    return _new_link(con, email)


def apply(con=None):
    """Bring the control plane in line with the config file -> the changes made, for the log."""
    con = con or orgs.control_db()
    done = []
    name = _value("workspace", "name")
    domain = _value("workspace", "domain").lower() or None
    if name and not orgs.default_is_adopted(con):
        orgs.adopt_default_org(name, domain, con=con)
        done.append(f"workspace {name!r} ({domain or 'no domain'})")
    elif name:
        org = next(o for o in orgs.list_orgs(con=con) if o["id"] == store.DEFAULT_ORG)
        if org["name"] != name:
            orgs.set_name(store.DEFAULT_ORG, name, con=con)
            done.append(f"workspace renamed {name!r}")
        if domain and org.get("domain") != domain:
            orgs.set_domain(store.DEFAULT_ORG, domain, con=con)
            done.append(f"workspace domain {domain}")
    issuer = _value("okta", "issuer")
    client_id = _value("okta", "clientId")
    if issuer and client_id:
        issuer = orgs.valid_issuer(issuer)
        cur = auth.sso_config(con)
        if not cur or (cur.issuer, cur.client_id) != (issuer, client_id):
            auth.discovery(issuer, force=True)          # refuses a document that names another issuer
            orgs.set_sso(issuer, client_id, None, "Okta", updated_by="chart", con=con)
            done.append(f"sign-in with Okta ({issuer})")
    otlp = _value("telemetry", "otlpEndpoint")
    if otlp and tenant.deployment(con).otlp_endpoint != otlp.rstrip("/"):
        values, errors = tenant.checked({"otlp_endpoint": otlp})
        if errors:
            raise ValueError(f"telemetry.otlpEndpoint: {errors['otlp_endpoint']}")
        tenant.set_deployment(con, "chart", **values)
        done.append("telemetry to the OTLP endpoint")
    link = _admin_link(con, domain)
    if link:
        done.append(link)
    return done

"""Browser-authorize handshake for `csa-client setup` and the plugin's `/csa:connect`.

The flow, and where trust actually sits:
  1. CLI  -> POST /api/cli-auth/new      : server mints a nonce + a word-pair code, starts the TTL.
  2. CLI  -> opens <server>/cli-auth?nonce=…&hostname=…, prints the SAME word-pair code.
  3. Human-> reads the page, TYPES the word pair from their terminal, supplies the org invite
             credential, clicks Authorize -> POST /api/cli-auth/approve records the authorization.
  4. CLI  -> polls GET /api/cli-auth/<nonce>; the poll that claims the pairing mints the per-user
             token and hands back the credentials — exactly ONCE.

The word pair is DERIVED from the nonce (sha256 -> two word indices), so it needs no storage and
both sides compute it independently. It is not a secret — the nonce is. Its job is to bind "the
terminal I ran" to "the page I am clicking Authorize on".

DELIBERATE DEVIATION from the task doc (documented in the PR): the doc says the page DISPLAYS the
code and the user confirms it matches, copying Poco. We require the user to TYPE it instead. A
displayed code that an attacker also controls is an advisory, not a control: with display-only, a
phished link ("authorize your new laptop") shows the attacker's code and hostname to a victim who
has no terminal open, and the button still works. Requiring entry means the code must come from
YOUR terminal — the GitHub device-flow property. Same cost, strictly stronger.

Nonce lifetime is 15 minutes, NOT the doc's 5 (also a documented deviation): this page exists to be
read — it carries the full "what leaves your laptop" disclosure — and a 5-minute clock fails
exactly the careful reader we are selling trust to. Single-use is what makes replay useless; the
TTL only bounds how long an unclicked handshake stays open.

A handshake is a row in control.db (`cli_pairings`; model in tenant.py, table in the control
baseline), so it survives a restart and a second api replica. The row holds no secret: the nonce is stored as its
sha256, exactly as `user_tokens` stores a token, and the per-user token is minted by the claiming
poll rather than by the Authorize click, so no plaintext credential is ever written to the one file
that holds every credential.
"""
import hashlib
import hmac
import html
import logging
import re
import secrets
import urllib.parse

from sqlalchemy import column, delete, func, insert, select, update

from analyzer import archive, auth, bootstrap, db, orgs, store, tenant

_CP = tenant.CliPairing.__table__

log = logging.getLogger("csa.cliauth")

# --- the web dashboard's sign-in ---------------------------------------------------------------------
# Same credential, same invite gate, same mint as the CLI handshake above — only the delivery differs
# (an httpOnly cookie instead of a polled JSON body). Deliberately NOT a second credential path.
SESSION_MAX_AGE = 30 * 24 * 3600                         # 30d — the browser's COOKIE eviction hint, NOT a
# server-side TTL. orgs.lookup() checks revoked_at AND expires_at (sliding idle window), so the
# token stays replayable as a bearer until someone runs orgs.revoke — Sign out does that, an evicted cookie
# does not. A server-side token TTL is the org layer's call, not this module's.


CONSENT_POLICY = 2        # what a client must record to ship; client/config.go requiredPolicyVersion
PLUGIN_SHIPPING = ("plugin", "plugin-manual")
TTL_SECONDS = 15 * 60     # nonce lifetime (see the module docstring: the doc says 5; deliberate)
_MAX_HOSTNAME = 120
# Unclaimed handshakes kept before _prune drops the oldest. The TTL bounds the table in normal use;
# this bounds a flood on `new`, which is public and unauthenticated. A flood past the ceiling drops an
# in-flight handshake and its owner's `csa-client setup` then times out; a per-IP rate limit is the fix.
_MAX_PAIRINGS = 10000

# Two-word codes, Poco-style ("JEEP-IONOSPHERE"): short, unambiguous read-aloud, no homophone pairs.
# 64 words -> 4096 combinations. That is NOT the security boundary (the nonce is) — it only has to
# make "the code on my screen is not the code in my terminal" obvious to a human.
_WORDS = (
    "ANCHOR", "BADGER", "BALLOON", "BANJO", "BEACON", "BISON", "BLANKET", "BOULDER",
    "CACTUS", "CANYON", "CARBON", "CASTLE", "CHISEL", "COMET", "COPPER", "CRAYON",
    "DELTA", "DIESEL", "DOMINO", "DRAGON", "ECHO", "EMBER", "FALCON", "FERRY",
    "FOSSIL", "GALLERY", "GIZMO", "GLACIER", "GRANITE", "HARBOR", "HELMET", "IGLOO",
    "IONOSPHERE", "JEEP", "JIGSAW", "JUNIPER", "KETTLE", "LANTERN", "LEDGER", "MAGNET",
    "MAMMOTH", "MARBLE", "MERIDIAN", "NEBULA", "NOMAD", "ORBIT", "OTTER", "PELICAN",
    "PIGMENT", "PLATEAU", "QUARTZ", "RADAR", "RIVET", "SAFARI", "SEQUOIA", "SHERPA",
    "SILO", "TANGO", "THIMBLE", "TUNDRA", "VELVET", "WALRUS", "YONDER", "ZEPPELIN",
)


def code_for(nonce: str) -> str:
    """The word pair for a nonce. Deterministic, so the CLI and the page agree with no shared state."""
    digest = hashlib.sha256(("csa-cli-auth:" + (nonce or "")).encode("utf-8")).digest()
    return _WORDS[digest[0] % len(_WORDS)] + "-" + _WORDS[digest[1] % len(_WORDS)]


def _normalize_code(s: str) -> str:
    """Accept what a human actually types: any case, stray space, and -/_/space between the words.
    A typo must fail; "jeep ionosphere" for JEEP-IONOSPHERE must not."""
    return re.sub(r"-+", "-", re.sub(r"[\s_]+", "-", (s or "").strip().upper())).strip("-")


def _live(nonce: str):
    """The WHERE that matches the one handshake a caller may still act on."""
    return (_CP.c.nonce_hash == orgs._hash(nonce), _CP.c.claimed_at.is_(None),
            _CP.c.created >= orgs._now(-TTL_SECONDS))


def _pairing(con, nonce: str):
    """(approved_at, shipping) for a live handshake, or None when it is unknown, expired or claimed."""
    return db.run(con, select(_CP.c.approved_at, _CP.c.shipping).where(*_live(nonce))).fetchone()


def shipping_of(nonce: str) -> str:
    """Which plugin mode a live handshake was started for, "" for csa-client or no live handshake.
    Read from the row, so a link edited on its way to a browser cannot change what the page promises."""
    row = _pairing(orgs.control_db(), nonce or "")
    return (row[1] or "") if row else ""


def _prune(con):
    """Drop claimed and expired handshakes, and any surplus past _MAX_PAIRINGS oldest first, leaving
    room for the row `new` is about to insert. `created` is second-resolution, so rowid orders ties."""
    db.run(con, delete(_CP).where(_CP.c.claimed_at.is_not(None)
                                  | (_CP.c.created < orgs._now(-TTL_SECONDS))))
    live = db.run(con, select(func.count()).select_from(_CP)).fetchone()[0]
    if live >= _MAX_PAIRINGS:
        db.run(con, delete(_CP).where(_CP.c.nonce_hash.in_(
            select(_CP.c.nonce_hash).order_by(_CP.c.created, column("rowid"))
            .limit(live - _MAX_PAIRINGS + 1))))


def _machine_name(hostname):
    """The machine label a pairing request may claim. `web` and `web-sso` are how the dashboard's own
    sign-ins are recorded and `orgs.is_mutating_admin` / `set_role` read them as authority, so a
    phished pairing link may not claim one."""
    name = (hostname or "").strip()[:_MAX_HOSTNAME]
    return name + " (cli)" if name.lower() in orgs.BROWSER_HOSTNAMES else name


def new(hostname: str = "", shipping: str = ""):
    """Start a handshake -> (nonce, code, ttl_seconds). The 43-char urlsafe nonce IS the secret, and
    only its hash is written. `shipping` is the plugin's mode; anything else reads as csa-client."""
    nonce = secrets.token_urlsafe(32)
    con = orgs.control_db()
    _prune(con)
    db.run(con, insert(_CP).values(nonce_hash=orgs._hash(nonce), hostname=_machine_name(hostname),
                                   created=orgs._now(), approved_at=None, email=None, org_id=None,
                                   verified=None, claimed_at=None,
                                   shipping=shipping if shipping in PLUGIN_SHIPPING else None))
    con.commit()
    return nonce, code_for(nonce), TTL_SECONDS


#: The ONE answer /join gives to every unusable link. Expired, revoked, never existed and opened by
#: the wrong account are byte-identical on purpose, so the endpoint cannot be asked which workspaces
#: or which people exist. It names the address the visitor signed in as because that is THEIR OWN
#: state, not the token's, and without it someone signed in to the wrong account is stuck.
JOIN_REFUSED = ("This invite link is not usable{as_who}. It may have expired, been revoked, or been "
                "meant for a different account. Ask your admin for a new one.")


def mint_gate(identity, email: str, invite: str, join: str = ""):
    """(email, org_id, err): WHO is minting, and INTO WHICH org. THE identity decision, shared by the
    CLI handshake and the dashboard so they cannot drift apart.

    `identity` is (email, domain_claim) as auth.verify_id_token established it, or None for the typed
    paths. An identity wins over anything typed. `join` is the sha256 of a per-person invite link,
    the one thing that admits an address orgs.org_for_identity would refuse: an admin named it
    exactly and the link expires, so a lookalike domain still buys nothing. Once an sso row exists
    the typed paths are refused outright, not left as a quieter way in.
    """
    try:
        cfg = auth.sso_config(orgs.control_db())
    except Exception as e:                               # control plane unreadable: mint nothing
        log.warning("mint gate: control plane unreadable: %s", str(e)[:120])
        return (None, None, "Could not sign you in right now. Try again in a moment.")
    if identity:
        who, claim = identity
        org_id = (orgs.redeem_invite_hash(join, who) if join else None) or orgs.org_for_identity(who, claim)
        if org_id:
            return (who, org_id, None)
        if join:
            return (None, None, JOIN_REFUSED.format(as_who=f" for {who}"))
        # Says WHICH account was refused (the likeliest cause is the wrong account in the
        # provider's chooser) but never which rule missed.
        return (None, None, f"{who} isn't allowed to sign in to this CSA. Check you picked your "
                            f"work account, or ask your admin for an invite link.")
    if cfg:
        return (None, None, f"This CSA requires signing in with {cfg.display_name}.")
    if join:
        # No issuer to verify anyone, so the link IS the credential and the address it names is who
        # signs in: strictly narrower than the shared invite, where the email is whatever was typed.
        row = orgs.invite_for_hash(join)
        who = row["email"] if row else None
        org_id = orgs.redeem_invite_hash(join, who) if who else None
        return (who, org_id, None) if org_id else (None, None, JOIN_REFUSED.format(as_who=""))
    email = (email or "").strip()
    if not email:
        return (None, None, "Enter the email address to attribute your sessions to.")
    if "@" not in email or len(email) > 254:
        return (None, None, "Enter a valid email address.")
    org_id = orgs.verify_invite(invite)
    if not org_id:
        return (None, None, "Invalid invite credential. Ask your CSA admin for your team's invite code.")
    domain = orgs.domain_of(org_id)
    if domain and email.rsplit("@", 1)[1].lower() != domain:
        return (None, None, f"This CSA admits only {domain} addresses. Enter your {domain} email.")
    return (email, org_id, None)


class RetentionNotShown(Exception):
    """A plugin's Authorize is its consent, so the click must have seen this workspace's retention."""

    def __init__(self, days):
        super().__init__(days)
        self.days = days


def approve(nonce: str, code: str, email: str, invite: str, session=None, ack_days=None):
    """The Authorize click. -> (ok, error). Records WHO the pairing is for; the token itself is minted
    by the poll that claims it (see the module docstring). Raises RetentionNotShown when a plugin
    pairing's click (`ack_days`) did not carry the retention of the workspace the identity resolved to.

    `session` is (email, org_id, verified) the CALLER already resolved from the dashboard cookie, never
    anything the request asserted; it stands in for mint_gate when no credential was typed. `verified`
    is what the poll's mint inherits a role from (orgs.mint_token), so an admin who signed in through
    the issuer pairs an admin laptop and an invite-era session pairs a member.

    Every denial returns the same shape and leaks nothing about WHICH check failed beyond what the
    user needs to fix their own typo. Explicit branch per failure: this is the mint gate.
    """
    con = orgs.control_db()
    row = _pairing(con, nonce or "")
    if row is None:
        return (False, "This authorization request has expired or is unknown. "
                       "Run `csa-client setup` or `/csa:connect` again and use the fresh link.")
    if row[0]:
        return (False, "This request was already authorized.")
    # Compare as BYTES: hmac.compare_digest raises TypeError on a str with non-ASCII characters, and
    # `code` is attacker-controlled on this public endpoint (a `café` code would otherwise escape as
    # an unhandled 500). Encoding both sides makes any input a clean, constant-time mismatch.
    #
    # The word code is checked BEFORE identity, and stays checked on the SSO path: the issuer proves
    # who you are, not that you are the person sitting at the terminal that asked. It is what stops
    # a phished "authorize your new laptop" link from attaching an attacker's machine to a real
    # user's account on one careless click.
    if not hmac.compare_digest(_normalize_code(code).encode("utf-8"), code_for(nonce).encode("utf-8")):
        return (False, "That verification code does not match the one in your terminal. "
                       "Check where you ran `csa-client setup` or `/csa:connect`.")
    if session and not (email or invite):
        email, org_id, verified = session
    else:
        email, org_id, err = mint_gate(None, email, invite)
        if err:
            return (False, err)
        verified = False
    if row[1]:
        days = archive.retention_days(org_id)
        if ack_days != days:
            raise RetentionNotShown(days)
    # Conditional on approved_at: two Authorize clicks racing on one nonce cannot both win.
    try:
        won = db.run(con, update(_CP).values(approved_at=orgs._now(), email=email, org_id=org_id,
                                             verified=1 if verified else 0)
                     .where(*_live(nonce or ""), _CP.c.approved_at.is_(None))).rowcount == 1
        con.commit()
    except Exception as e:                               # control plane unwritable: authorize nothing
        log.warning("cli-auth: approve failed for %s: %s", email, e)
        return (False, "Could not authorize right now. Try again in a moment.")
    if not won:
        return (False, "This request was already authorized." if _pairing(con, nonce or "")
                else "This authorization request expired while you were authorizing. "
                     "Run `csa-client setup` or `/csa:connect` again.")
    log.info("cli-auth: authorized %s (org=%s)", email, org_id)
    return (True, "")


def claim(nonce: str):
    """The CLI's poll. -> credentials dict exactly ONCE, else None. Mints the per-user token.

    None covers pending, unknown, expired and already-claimed alike: the caller cannot tell them
    apart, so this is not an oracle for guessing nonces. The conditional UPDATE decides who claimed
    the pairing, so two racing polls cannot both be served, and the loser's token is revoked rather
    than left live. Minting BEFORE that update is deliberate: a mint that fails must leave the
    handshake claimable, or the user hears about it as a 15-minute timeout instead of a retry.
    """
    con = orgs.control_db()
    ready = (*_live(nonce or ""), _CP.c.approved_at.is_not(None))
    row = db.run(con, select(_CP.c.hostname, _CP.c.email, _CP.c.org_id, _CP.c.verified, _CP.c.shipping)
                 .where(*ready)).fetchone()
    if row is None:
        return None
    hostname, email, org_id, verified, shipping = row
    try:
        pol = policy(org_id)                             # read the org's db BEFORE minting, so a failure
        token = orgs.mint_token(email, org_id, hostname, verified=bool(verified), con=con)
    except Exception as e:                               # here costs nothing and the next poll retries
        log.warning("cli-auth: claim failed for %s: %s", email, e)
        return None
    if db.run(con, update(_CP).values(claimed_at=orgs._now()).where(*ready)).rowcount != 1:
        orgs.revoke(token, con=con)                      # a concurrent poll won it: leave no live token behind
        return None
    _prune(con)                                          # the handshake is spent; do not keep the row
    con.commit()
    log.info("cli-auth: minted token for %s (host=%s, org=%s)", email, hostname or "?", org_id)
    return {"token": token, "email": email, "policy": pol, "consent_policy": CONSENT_POLICY,
            "shipping": shipping or ""}


def web_login(email: str, invite: str, join: str = ""):
    """The dashboard's invite sign-in -> (True, token) | (False, error). No nonce and no word code:
    the browser IS the thing being authorized. The mint gate is the same call approve() makes, and
    it refuses this path outright once an issuer is configured. The token is labelled `web`, a
    self-asserted identity that never carries a role (orgs.mint_token). `join` is the link itself,
    off the cookie; the gate takes its hash."""
    join_hash = orgs._hash(join) if join else ""
    first_use = bool(join) and orgs.claim_invite(join_hash)
    email, org_id, err = mint_gate(None, email, invite, join_hash)
    if err:
        return (False, err)
    first_admin = first_use and org_id == store.DEFAULT_ORG and email.lower() == bootstrap.admin_email()
    return _mint(email, org_id, "web", verified=False, role="admin" if first_admin else None)


def sso_login(identity, join_hash: str = ""):
    """The callback's mint -> (True, token) | (False, error). `identity` is (email, domain_claim) the
    issuer vouched for; `join_hash` is what the sso_logins row kept of the link this browser carried.
    The token is labelled `web-sso` and verified, so it inherits the person's existing role."""
    email, org_id, err = mint_gate(identity, "", "", join_hash)
    if err:
        return (False, err)
    return _mint(email, org_id, "web-sso", verified=True)


def _mint(email, org_id, hostname, verified, role=None):
    try:
        token = orgs.mint_token(email, org_id, hostname, verified=verified, role=role)
    except Exception as e:                               # DB down: say so, mint nothing
        log.warning("ui-login: mint failed for %s: %s", email, e)
        return (False, "Could not sign you in right now. Try again in a moment.")
    log.info("ui-login: minted %s token for %s (org=%s)", hostname, email, org_id)
    return (True, token)


PAGE_TOKENS = """
  @font-face { font-family: "Inter"; font-weight: 400; font-display: swap; src: url("/fonts/inter-latin-400-normal.woff2") format("woff2"); }
  @font-face { font-family: "Inter"; font-weight: 600; font-display: swap; src: url("/fonts/inter-latin-600-normal.woff2") format("woff2"); }
  @font-face { font-family: "Source Serif 4"; font-weight: 600; font-display: swap; src: url("/fonts/source-serif-4-latin-600-normal.woff2") format("woff2"); }
  :root {
    color-scheme: light;
    --paper: #f8f5ee; --card: #fffdf8; --line: #e4dfd3;
    --ink: #1b1d22; --muted: #726d62; --accent: #1b1d22; --on-accent: #f8f5ee;
    --good: #3f8a55; --good-soft: #3f8a5520; --bad: #b4433c; --bad-soft: #b4433c20;
    --serif: "Source Serif 4", "Iowan Old Style", "Palatino Linotype", Georgia, serif;
    --sans: "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --paper: #1b1a17; --card: #23221e; --line: #37352f;
      --ink: #ece7db; --muted: #a59e90; --accent: #ece7db; --on-accent: #1b1a17;
      --good: #7fb98f; --good-soft: #7fb98f26; --bad: #e0837a; --bad-soft: #e0837a26;
    }
  }
"""
"""The SPA's tokens (server/ui/src/style.css), for the pages the server renders itself. They are
copied, not imported: Python cannot read a Vite stylesheet. Change one, change the other. The fonts
are the SPA's own, served from ui/dist, so a box without it falls back down each stack."""


def session_cookie(token: str) -> str:
    """The Set-Cookie value that logs a browser in. Every attribute is load-bearing:

      HttpOnly          JS cannot read it, so an XSS in the SPA cannot exfiltrate the credential.
      Secure + Path=/   required by the __Host- prefix (auth.SESSION_COOKIE) — and no Domain, which
                        is what stops a sibling subdomain planting a fixated session.
      SameSite=Strict   the CSRF control. apiserver's do_POST never checks Content-Type, so a
                        cross-site `fetch` with Content-Type: text/plain is a SIMPLE request — no
                        preflight, so returning 204 from OPTIONS saves nobody. Strict means the
                        browser does not attach this cookie to a cross-SITE request at all.
                        (Same-SITE is a wider boundary than same-ORIGIN, so apiserver adds a
                        Sec-Fetch-Site check on top — see _same_origin there.)
    """
    return "%s=%s; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=%d" % (
        auth.SESSION_COOKIE, token, SESSION_MAX_AGE)


JOIN_COOKIE = "__Host-csa_join"
JOIN_COOKIE_MAX_AGE = 3600


def join_cookie(token: str) -> str:
    """Carry a /join link's token to the sign-in POST WITHOUT it living in the SPA's URL.

    Same attributes as session_cookie for the same reasons, and short-lived because it exists only
    for the gap between opening the link and signing in. The token is a credential, so keeping
    it out of the address bar keeps it out of history, out of `document.referrer`, and out of any
    screenshot of the tab.

    An hour, not minutes: when it lapses the sign-in falls back to the ordinary domain gate, and an
    invited contractor would then be told to ask their admin to add their DOMAIN, which is the wrong
    advice and not something they can act on. Re-opening the link fixes it, but the window should be
    wide enough that almost nobody meets that message."""
    return "%s=%s; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=%d" % (
        JOIN_COOKIE, token, JOIN_COOKIE_MAX_AGE)


def clear_join_cookie() -> str:
    """Attributes must MATCH join_cookie's or the browser keeps the original."""
    return "%s=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0" % JOIN_COOKIE


def logout_cookie() -> str:
    """Expire the cookie. Attributes must MATCH session_cookie()'s or the browser keeps the original
    (a Set-Cookie only overwrites one with the same name+domain+path). The caller also revokes the
    token server-side — clearing the cookie alone would leave a live credential in user_tokens."""
    return "%s=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0" % auth.SESSION_COOKIE


def policy(org_id=None):
    """What a workspace discloses to `csa-client setup` before consent: its minimum redaction mode
    and `archive_days`, how long it keeps the redacted transcript (0 = none; tenant.KEEP_FOREVER =
    until forgotten). `org_id` None, a caller with no credential on a multi-workspace server, gets
    the operator redaction floor and `archive_days: None`."""
    if org_id is None:
        return {"redaction_min": tenant.redaction_policy()[0], "archive_days": None}
    return {"redaction_min": tenant.redaction_policy(db_path=store.org_db(org_id))[0],
            "archive_days": archive.retention_days(org_id)}


def sso_start_url(return_path: str) -> str:
    return "/api/sso/start?" + urllib.parse.urlencode({"return": return_path})


def retention_sentence(archive_days) -> str:
    """How long a workspace keeps the redacted transcript, or that the sign-in decides it."""
    if archive_days is None:
        return "Your workspace sets how long it keeps the redacted transcript. You will see that before this finishes."
    if archive_days == 0:
        return "This workspace keeps no transcript once facts are extracted."
    kept = "until you or a workspace admin deletes it" if archive_days < 0 else f"for {archive_days} days"
    return f"This workspace keeps the redacted transcript {kept}. You and workspace admins can read it."


def _disclosure(shipping: str, retention: str) -> str:
    """The "what this turns on" groups for the client that asked."""
    if shipping == "plugin-manual":
        return f"""<div class="group">
  <p class="grouplabel">Authorizing is your consent</p>
  <ul>
    <li>Nothing leaves this computer until you run <code>/csa:send-session</code>, and then only that
        session. Each prompt stays here: <strong>recall is off</strong>.</li>
    <li>A session you send goes to this server and its AI model. Teammates see the facts and quotes
        extracted from it, under your name. {retention}</li>
    <li>Secrets (API keys, tokens, JWTs, private keys) are stripped on your machine before sending.
        Your prompts, code, file paths, emails and names are <strong>kept on purpose</strong>.</li>
  </ul>
</div>
<div class="group">
  <p class="grouplabel">How to undo</p>
  <ul>
    <li><code>/csa:forget</code> deletes a sent session from the server.
        <code>/csa:disconnect</code> revokes this computer's credential and removes <code>~/.csa</code>.</li>
  </ul>
</div>"""
    if shipping == "plugin":
        return f"""<div class="group">
  <p class="grouplabel">Authorizing is your consent</p>
  <ul>
    <li>Every new Claude Code session on this computer ships to this server and its AI model when it
        ends, and each prompt is sent for recall. Teammates see the facts and quotes extracted from
        them, under your name. {retention}</li>
    <li>Secrets (API keys, tokens, JWTs, private keys) are stripped on your machine before sending.
        Your prompts, code, file paths, emails and names are <strong>kept on purpose</strong>: they are
        what makes recall work.</li>
  </ul>
</div>
<div class="group">
  <p class="grouplabel">What's included</p>
  <ul>
    <li>Every project folder, by default. Narrow it with <code>/csa:scope</code>.</li>
    <li>Your <strong>existing</strong> sessions stay put. Only sessions you start from now on ship.</li>
  </ul>
</div>
<div class="group">
  <p class="grouplabel">How to undo</p>
  <ul>
    <li><code>/csa:forget</code> deletes a shipped session from the server.
        <code>/csa:disconnect</code> revokes this computer's credential and removes <code>~/.csa</code>.</li>
  </ul>
</div>"""
    return f"""<div class="group">
  <p class="grouplabel">What ships</p>
  <ul>
    <li>CSA ships <strong>full transcripts</strong> of your Claude Code sessions from
        <code>~/.claude/projects</code> to this server. Teammates see the facts and quotes extracted
        from them, under your name. {retention}</li>
    <li>Secrets (API keys, tokens, JWTs, private keys) are stripped on your machine before sending.
        Your prompts, code, file paths, emails and names are <strong>kept on purpose</strong>: they are
        what makes recall work.</li>
  </ul>
</div>
<div class="group">
  <p class="grouplabel">What's included</p>
  <ul>
    <li>Every project folder, by default. Narrow it any time with <code>csa-client only pick</code>,
        or keep one out with <code>csa-client ignore '*repo*'</code>.</li>
    <li>Your <strong>existing</strong> sessions stay put. Only sessions you start from now on ship.
        History ships only if you re-run setup with <code>--backfill</code>.</li>
  </ul>
</div>
<div class="group">
  <p class="grouplabel">How to undo</p>
  <ul>
    <li><code>csa-client uninstall</code> removes CSA's hooks and binary and revokes this computer's
        credential; <code>csa-client uninstall --purge</code> also wipes your local config.</li>
    <li><code>csa-client forget &lt;id&gt;</code> deletes an already-shipped session from the server.</li>
  </ul>
</div>
<small>Your terminal shows this same notice and asks you to accept it before this page can finish.</small>"""


def page(nonce: str, hostname: str, session_email: str = "", cfg=None, shipping: str = "",
         archive_days=None) -> bytes:
    """The /cli-auth page: hostname + code entry + the full disclosure + Authorize.

    `shipping` is shipping_of(nonce) and `archive_days` the retention the caller resolved for this
    visitor (None: not known until the identity is).

    `session_email` is who the CALLER resolved from the dashboard cookie; when set, the page offers
    that identity as the primary button and the word code is all that is still asked for. `cfg` is
    the issuer (auth.sso_config) the caller read: with one, the only other way in is a link to
    /api/sso/start that brings the browser back here signed in; without one, the invite fields.

    Self-contained: nothing external loads. `nonce` and `hostname` arrive from the query string:
    attacker-controlled, so they are html-escaped into text and into a JS string literal via a
    JSON-safe path. Nothing here is trusted.
    """
    # The nonce reaches the page only through _js_string() (it's a JS value, not shown as text), so it
    # is not html-escaped here. `hostname` IS shown as text, so it is.
    hostname = (hostname or "").strip()[:_MAX_HOSTNAME]
    h = html.escape(hostname or "this computer", quote=True)
    sso_box = "" if cfg is None else '<div class="sso-box"><a class="sso" href="%s">Sign in with %s</a></div>' % (
        html.escape(sso_start_url("/cli-auth?" + urllib.parse.urlencode({"nonce": nonce or "", "hostname": hostname})),
                    quote=True),
        html.escape(cfg.display_name, quote=True))
    asker, code_label = ("<code>/csa:connect</code>", "Verification code printed by <code>/csa:connect</code>") if shipping \
        else ("<code>csa-client setup</code>", "Verification code shown in your terminal")
    session_block = "" if not session_email else """
  <button id="mine" type="button">Authorize as %s</button>
  <small>The session already signed in to this browser. Nothing else is asked for.</small>
""" % html.escape(session_email[:254], quote=True)
    # With an issuer these fields do not exist at all, so there is no second way in to forget about
    # later. The JS below reads them defensively for exactly that reason.
    invite_fields = "" if cfg is not None else """
  <label for="email">Attribute sessions to</label>
  <input id="email" name="email" type="email" placeholder="you@company.com" required>

  <label for="invite">Team invite code</label>
  <input id="invite" name="invite" type="password" required
         aria-describedby="invhelp">
  <small id="invhelp">From your CSA admin.</small>

  <button id="go" type="submit">Authorize</button>
"""
    # Offering the session AND laying the other way in open underneath contradicts the line that
    # says nothing else is asked for, and puts a second Authorize button on the screen. Folded away,
    # it is still one click from a different browser or a different person. `required` inside a
    # closed <details> is safe here: nothing submits the form except the button folded in with them.
    if session_block:
        sso_box = ('<details class="other"><summary>Sign in as someone else</summary>'
                   + sso_box + invite_fields + '</details>')
        invite_fields = ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize CSA on {h}</title>
<style>
{PAGE_TOKENS}  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.55 var(--sans); background: var(--paper); color: var(--ink);
         margin: 0; padding: 3rem 1.25rem; display: flex; justify-content: center; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
           box-shadow: 0 1px 2px #0000000f, 0 8px 24px #00000014;
           max-width: 30rem; width: 100%; padding: 1.75rem 1.85rem 2rem; }}
  .eyebrow {{ font: 600 .72rem/1 var(--sans); text-transform: uppercase; letter-spacing: .08em;
              color: var(--muted); margin: 0 0 .6rem; }}
  h1 {{ font: 600 1.5rem/1.25 var(--serif); margin: 0 0 .4rem; letter-spacing: -.01em; }}
  .sub {{ color: var(--muted); margin: 0 0 1.6rem; }}
  fieldset {{ border: 1px solid var(--line); border-radius: 10px; padding: 1rem 1.15rem;
              margin: 0 0 1.25rem; min-width: 0; }}
  legend {{ padding: 0 .4rem; font-weight: 600; font-size: .78rem; text-transform: uppercase;
            letter-spacing: .06em; color: var(--muted); }}
  .group {{ margin: 0 0 1rem; }}
  .group:last-of-type {{ margin-bottom: .6rem; }}
  .grouplabel {{ font-weight: 700; font-size: .82rem; margin: 0 0 .35rem; color: var(--ink); }}
  ul {{ margin: 0; padding-left: 1.1rem; }} li {{ margin: .3rem 0; }}
  label {{ display: block; font-weight: 600; margin: .8rem 0 .3rem; }}
  input {{ width: 100%; padding: .6rem .65rem; font: inherit; border: 1px solid var(--line);
           border-radius: 7px; background: var(--card); color: inherit; }}
  input:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  /* The offset puts the ring on the CARD, never on the button's own fill, so one accent ring
     stays visible in both themes (WCAG 2.4.7) where a ring the colour of the fill would not. */
  button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  #code {{ font-family: var(--mono); text-transform: uppercase; letter-spacing: .08em; }}
  code {{ font-family: var(--mono); font-size: .92em; }}
  button {{ width: 100%; padding: .75rem; font: 600 1rem/1 var(--sans); border: 0; border-radius: 7px;
            background: var(--accent); color: var(--on-accent); cursor: pointer; margin-top: 1rem; }}
  button:hover {{ filter: brightness(1.07); }}
  /* With a session offered above, the invite form is the fallback: one primary action per screen. */
  #mine ~ button {{ background: transparent; color: var(--accent); border: 1px solid var(--accent); }}
  button:disabled {{ opacity: .55; cursor: default; filter: none; }}
  .host {{ font-family: var(--mono); font-weight: 600; }}
  .sso-box {{ margin-top: 1rem; }}
  a.sso {{ display: block; text-align: center; padding: .75rem; font: 600 1rem/1 var(--sans);
           border-radius: 7px; background: var(--accent); color: var(--on-accent); text-decoration: none; }}
  a.sso:hover {{ filter: brightness(1.07); }}
  a.sso:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .msg {{ padding: .75rem .9rem; border-radius: 8px; margin-top: 1rem; display: none;
          font-size: .92rem; }}
  .err {{ background: var(--bad-soft); border: 1px solid var(--bad); color: var(--bad); }}
  .ok {{ background: var(--good-soft); border: 1px solid var(--good); color: var(--good); }}
  small {{ color: var(--muted); }}
  .other {{ margin-top: 1.2rem; border-top: 1px solid var(--line); padding-top: .9rem; }}
  .other summary {{ cursor: pointer; color: var(--muted); font-size: .85rem; }}
  .other summary:hover {{ color: var(--ink); }}
  @media (max-width: 30rem) {{
    body {{ padding: 1.5rem 1rem; }}
    .card {{ padding: 1.35rem 1.25rem 1.5rem; }}
    h1 {{ font-size: 1.3rem; }}
    fieldset {{ padding: 1rem .75rem; }}
  }}
</style></head><body>
<div class="card">
<p class="eyebrow">Device sign-in</p>
<h1>Authorize CSA on <span class="host">{h}</span></h1>
<p class="sub">A {asker} on that computer is waiting for this.</p>

<fieldset><legend>What this turns on</legend>
{_disclosure(shipping, retention_sentence(archive_days))}
</fieldset>

<form id="f" autocomplete="off">
<fieldset><legend>Confirm it is you</legend>
  <label for="code">{code_label}</label>
  <input id="code" name="code" placeholder="WORD-WORD" required autofocus
         aria-describedby="codehelp">
  <small id="codehelp">Type the pair printed by {asker}. If you did not run it, someone else sent
    you this link. Close this page.</small>
{session_block}{sso_box}{invite_fields}
</fieldset>
</form>
<div id="msg" class="msg"></div>
</div>
<script>
const NONCE = {_js_string(nonce)};
let ackDays = {_js_value(archive_days)};
const f = document.getElementById('f'), msg = document.getElementById('msg'),
      go = document.getElementById('go'), mine = document.getElementById('mine');
function show(text, cls) {{ msg.textContent = text; msg.className = 'msg ' + cls; msg.style.display = 'block'; }}
function reenable() {{ if (go) go.disabled = false; if (mine) mine.disabled = false; }}

// Both ways in land here. The nonce and the typed word-code ride EVERY attempt: the sign-in proves who
// you are, the code proves you are the person at the terminal that asked, and the server needs both.
async function send(extra) {{
  if (go) go.disabled = true;
  if (mine) mine.disabled = true;
  show('Authorizing…', 'ok');
  try {{
    const r = await fetch('/api/cli-auth/approve', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(Object.assign({{nonce: NONCE, code: f.code.value, ack_days: ackDays}}, extra))
    }});
    const d = await r.json().catch(() => ({{}}));
    if (r.ok && d.ok) {{
      f.style.display = 'none';
      show('Authorized. You can close this page and go back to where you started.', 'ok');
      msg.append(document.createElement('br'));
      const back = document.createElement('a');
      back.href = '/#/connect';
      back.textContent = 'Open CSA';
      msg.append(back);
    }} else if (d.needs_ack) {{
      ackDays = d.archive_days;
      show(d.error, 'ok');
      reenable();
    }} else {{
      show(d.error || 'Authorization failed.', 'err');
      reenable();
    }}
  }} catch (err) {{
    show('Could not reach the CSA server. Check your connection and try again.', 'err');
    reenable();
  }}
}}

f.addEventListener('submit', (e) => {{
  e.preventDefault();
  send({{email: f.email ? f.email.value : '', invite: f.invite ? f.invite.value : ''}});
}});

// Sends NO credential: the server resolves the identity from the session cookie, and only when the
// body carries none of its own.
if (mine) mine.addEventListener('click', () => {{
  if (!f.code.value.trim()) {{
    show('Type the verification code from your terminal first.', 'err');
    return;
  }}
  send({{}});
}});
</script>
</body></html>
""".encode()


def _js_value(n) -> str:
    return "null" if n is None else str(int(n))


def _js_string(s: str) -> str:
    """A JS string literal that cannot break out, whatever the query string contained.

    json.dumps alone is NOT enough inside a <script> block: it leaves `<` and `/` intact, so a nonce
    of `</script><script>…` closes the tag and executes. (tests/test_cliauth.py pins it — that bug
    was live in the first draft of this file.) Escape the HTML-significant characters and the two
    line separators JS treats as newlines.
    """
    import json
    out = json.dumps(s or "")
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                    (" ", "\\u2028"), (" ", "\\u2029")):
        out = out.replace(ch, esc)
    return out

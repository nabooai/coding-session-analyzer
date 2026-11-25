// csa-client ships Claude Code sessions to a CSA server: it redacts each transcript locally and
// POSTs it to /api/ingest, from Claude Code hooks (`setup`, `init`) or a polling watcher (`init --install`).
// No extraction, no database, no LLM here. Nothing ships without the consent record in
// ~/.csa/config.json (config.go).
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/user"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// redactPolicy is the mode + deny/allow lists a redaction runs under. Mode names the additive
// pattern level (secrets<privacy<strict); deny/allow apply whenever the lists are non-empty,
// regardless of mode (an empty list is a no-op) — so a user or the server operator can kill
// specific terms without also turning on every privacy pattern. This mirrors the server's
// `if deny:` reconciliation. See config.go for where the mode is stored and
// docs/redaction.md for the spec.
type redactPolicy struct {
	Mode  string   `json:"mode"`
	Deny  []string `json:"deny,omitempty"`
	Allow []string `json:"allow,omitempty"`
}

// redactLevels: the ordered, additive modes. A higher level redacts strictly MORE, never less,
// so an unknown/empty mode falls back to secrets (0) — the client never ships weaker than default.
var redactLevels = map[string]int{"secrets": 0, "privacy": 1, "strict": 2}

func redactLevel(mode string) int { return redactLevels[mode] } // unknown/"" -> 0 (secrets)

// secretPatterns + privacyPatterns mirror ../server/analyzer/redact.py and must stay in
// sync (same labels, same regexes, same order) so the two redactors are byte-identical. The server
// re-redacts as a backstop, but the client is the only thing between a live token and the wire.
//
// tokenStart is where a token may start: a word boundary, or just after what glues a word character to it in
// raw JSONL or tool output, where `\\nghp_...` or an ANSI `...[0mghp_...` has no boundary and the token would
// ship: a JSON escape, or a terminal colour sequence, raw or as `\\u001b`. The prefix is consumed and kept by
// the `v` group rule in redactReport. Mirrors redact.py.
const tokenStart = `(?:\\[bfnrt]|\\u[0-9a-fA-F]{4}(?:\[[0-9;]*[A-Za-z])?|\x1b\[[0-9;]*[A-Za-z]|\b)`

// SECRETS (level 0, ALWAYS on): high-precision token/key shapes. Do NOT weaken these.
var secretPatterns = []struct {
	label string
	re    *regexp.Regexp
}{
	// Leading \b is load-bearing, NOT cosmetic: without it "sk-" matches INSIDE a word, so ask-/task-/
	// risk-/disk- followed by >=20 word chars were redacted as keys (branch `ask-questions-as-decisions`
	// shipped as `a[REDACTED:openai_key]`). A real key never follows a word char. Mirrors redact.py.
	{"openai_key", regexp.MustCompile(tokenStart + `(?P<v>sk-[A-Za-z0-9_-]{20,})`)},
	{"aws_key", regexp.MustCompile(`AKIA[0-9A-Z]{16}`)},
	// {4,} not {20,}: truncated fragments ("ghp_Io9y...") identify a real token and survived the
	// old length floor — found live in the corpus by a user. Prefix-shaped false positives are cheap.
	{"github_token", regexp.MustCompile(tokenStart + `(?P<v>(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{4,})`)},
	{"slack_token", regexp.MustCompile(`xox[baprs]-[A-Za-z0-9-]{10,}`)},
	{"private_key", regexp.MustCompile(`(?s)-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----`)},
	{"jwt", regexp.MustCompile(tokenStart + `(?P<v>eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})`)},
	// LEADING \b only, never trailing. Leading: [0-9A-Za-z_-] covers 62 of base64's 64 chars, so a chance
	// "AIza" inside a pasted-image/PDF/thinking-signature blob ate 39 chars (found live 4x). Trailing \b
	// would FAIL OPEN — real keys exceed 35 chars and {35} is exact. Mirrors redact.py.
	{"gcp_key", regexp.MustCompile(tokenStart + `(?P<v>AIza[0-9A-Za-z_-]{35})`)},
	// Vendor-prefixed shapes — mirrors redact.py. hook.go:49-54 already documents that real credentials
	// reach the wire raw; the ambient hook refuses to SEND them while ship stored them. Each is
	// prefix-anchored + high-entropy, so no prose false-positives; excluding `_` from the class after the
	// prefix means snake_case identifiers (hf_hub_download) break the run. Case-EXPLICIT on purpose: never
	// use (?i) in a level-0 pattern — Go folds Unicode, Python's re.A folds ASCII, so the server fails open.
	// Stripe pk_ (publishable, ships in client JS) is deliberately NOT redacted; it is not a credential.
	{"stripe_key", regexp.MustCompile(tokenStart + `(?P<v>(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,})`)},
	{"docker_pat", regexp.MustCompile(tokenStart + `(?P<v>dckr_pat_[A-Za-z0-9_-]{20,})`)},
	// {36,}: excluding `_` from the class already kills npm_config_registry; a trailing \b would only
	// risk failing open if npm lengthens the token. Same for glpat- below. Mirrors redact.py.
	{"npm_token", regexp.MustCompile(tokenStart + `(?P<v>npm_[A-Za-z0-9]{36,})`)},
	{"sendgrid_key", regexp.MustCompile(tokenStart + `(?P<v>SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})`)},
	{"hf_token", regexp.MustCompile(tokenStart + `(?P<v>hf_[A-Za-z0-9]{30,})`)},
	{"gitlab_pat", regexp.MustCompile(tokenStart + `(?P<v>glpat-[A-Za-z0-9_-]{20,})`)},
	{"pypi_token", regexp.MustCompile(tokenStart + `(?P<v>pypi-AgEI[A-Za-z0-9_-]{16,})`)},
	{"telegram_bot", regexp.MustCompile(tokenStart + `(?P<v>[0-9]{8,10}:AA[A-Za-z0-9_-]{32,})`)},
	{"slack_webhook", regexp.MustCompile(`https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{20,}`)},
	{"azure_key", regexp.MustCompile(`AccountKey=[A-Za-z0-9+/]{86}==`)},
	// Last, so a token above keeps its own label. Only the `v` group is replaced, and a reference
	// (credentialReference) is kept. `\\?` takes the escaped quotes of raw JSONL. A quoted key needs a
	// quoted value, so `"secret":true` is left alone instead of becoming invalid JSON; the value takes an
	// escape pair but never `\"`, so a redaction never eats the escape of a closing quote. Mirrors redact.py.
	{"credential", regexp.MustCompile(`(?:` + nocase("secret_key", "private_key", "access_key", "access_token",
		"auth_token", "refresh_token", "client_secret", "api_key", "password", "passwd", "secret") +
		`)\b(?:\\?["'][ \t]*:[ \t]*\\?["']|[ \t]*[:=][ \t]*(?:\\?["'])?)(?P<v>(?:\\[^"']|[^\t\n\v\f\r "'\\]){4,})`)},
}

// nocase is an alternation of words in any ASCII case, `_` meaning an optional `_` or `-`. Spelled out
// because (?i) folds Unicode here and ASCII in redact.py, so the server would fail open.
func nocase(words ...string) string {
	alts := make([]string, len(words))
	for i, w := range words {
		var b strings.Builder
		for _, c := range w {
			if c == '_' {
				b.WriteString("[_-]?")
			} else {
				b.WriteString("[" + string(c) + strings.ToUpper(string(c)) + "]")
			}
		}
		alts[i] = b.String()
	}
	return strings.Join(alts, "|")
}

// credentialReference: a credential value that names where the secret lives, or one a pattern above
// already redacted.
var credentialReference = regexp.MustCompile(`^(?:\$\{|\$[A-Z_][A-Z0-9_]*\b|\[REDACTED:|os\.environ|os\.getenv|process\.env)`)

// PRIVACY (level >= 1, additive): emails, credential-bearing URLs, bearer tokens, credential
// prose, and internal-TLD hostnames. Applied in this exact order (conn_string first so it eats
// the whole scheme://user:pass@host before email/hostname can nibble at its parts).
var privacyPatterns = []struct {
	label string
	re    *regexp.Regexp
}{
	{"conn_string", regexp.MustCompile(`[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:[^\s:/@]+@\S+`)},
	{"bearer", regexp.MustCompile(`(?i)` + tokenStart + `(?P<v>bearer\s+[A-Za-z0-9._-]{8,})`)},
	{"email", regexp.MustCompile(`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`)},
	// No LEADING \b: the keyword may be glued to a preceding word char (aws_secret_access_key=…,
	// _secret=…) — a common, dangerous config spelling. The TRAILING \b still blocks mid-word hits.
	// Compound *_key forms FIRST (leftmost-first alternation in both engines): a bare `secret` branch
	// matches "SECRET" in `SECRET_KEY` then fails its \b on the `_`. Mirrors redact.py.
	{"credential", regexp.MustCompile(`(?i)(?:secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|password|passwd|pwd|secret|api[_-]?key|access[_-]?key|client[_-]?secret|token)\b\s*[:=]\s*["']?[^\s"']{4,}`)},
	{"hostname", regexp.MustCompile(`(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:internal|local|lan|corp|intranet|localdomain|home|test)\b`)},
}

// redactWith applies a policy to s and returns the redacted text. See redactReport for the labels.
func redactWith(p redactPolicy, s string) string {
	out, _ := redactReport(p, s)
	return out
}

// redactReport runs SECRETS, then PRIVACY (level>=1), then the deny list (whenever non-empty),
// each substituting `[REDACTED:<label>]` (only the `v` group, when the pattern has one), unless the
// matched text or that group (lowercased) is an allow term, in which case it is KEPT (guards pattern
// false positives, exact match not substring).
// Returns the redacted text and the labels that actually fired, in application order.
func redactReport(p redactPolicy, s string) (string, []string) {
	level := redactLevel(p.Mode)
	allow := map[string]bool{}
	for _, a := range p.Allow {
		if a = strings.ToLower(strings.TrimSpace(a)); a != "" {
			allow[a] = true
		}
	}
	var found []string
	seen := map[string]bool{}
	sub := func(re *regexp.Regexp, label string) {
		v := re.SubexpIndex("v")
		s = re.ReplaceAllStringFunc(s, func(m string) string {
			head, value := "", m
			if v > 0 {
				at := re.FindStringSubmatchIndex(m)[2*v]
				head, value = m[:at], m[at:]
				if credentialReference.MatchString(value) {
					return m
				}
			}
			if allow[strings.ToLower(m)] || allow[strings.ToLower(value)] {
				return m
			}
			if !seen[label] {
				seen[label] = true
				found = append(found, label)
			}
			return head + "[REDACTED:" + label + "]"
		})
	}
	for _, pat := range secretPatterns {
		sub(pat.re, pat.label)
	}
	if level >= 1 {
		for _, pat := range privacyPatterns {
			sub(pat.re, pat.label)
		}
	}
	if len(p.Deny) > 0 { // whenever configured — matches the server's `if deny:` (not gated on strict)
		// deny: literal terms, case-insensitive SUBSTRING replace (QuoteMeta -> literal match).
		// Substring, not \b, because terms include paths/hostnames where \b misbehaves and
		// over-redaction is the safe direction. A bad term is skipped, never crashes the watcher.
		for _, term := range p.Deny {
			t := strings.TrimSpace(term)
			if t == "" {
				continue
			}
			re, err := regexp.Compile("(?i)" + regexp.QuoteMeta(t))
			if err != nil {
				continue
			}
			sub(re, "denylisted")
		}
	}
	return s, found
}

// redact applies the secrets level only; the ambient hook redacts its query with it.
func redact(s string) string { return redactWith(redactPolicy{Mode: "secrets"}, s) }

type session struct {
	path  string
	mtime time.Time
}

// entries maps session_id (basename sans .jsonl) -> {path, mtime} for every session under root.
func entries(root string) map[string]session {
	out := map[string]session{}
	filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(p, ".jsonl") {
			return nil // skip unreadable/rotated entries, keep walking
		}
		info, ierr := d.Info()
		if ierr != nil {
			return nil
		}
		out[strings.TrimSuffix(d.Name(), ".jsonl")] = session{p, info.ModTime()}
		return nil
	})
	return out
}

// due: sessions that are SETTLED (quiet for >= quiet), NEW or GROWN vs last send, and past
// their per-session send cooldown. Pure, tested.
// The cooldown trades freshness for cost — every send triggers a full LLM
// re-extraction server-side, so a chatty 2h session must not ship 20 times. The price:
// a still-active session's final state ships at most `cooldown` late (first scan after expiry).
func due(now time.Time, all map[string]session, sent, lastSent map[string]time.Time, quiet, cooldown time.Duration) []string {
	var out []string
	for sid, s := range all {
		if now.Sub(s.mtime) < quiet || sent[sid].Equal(s.mtime) {
			continue
		}
		if now.Sub(lastSent[sid]) < cooldown { // zero time (never sent) is always past cooldown
			continue
		}
		out = append(out, sid)
	}
	return out
}

// backfillQueue: the one-time upload list — every session already on disk that hasn't been
// backfilled before (per the done set) and isn't excluded, oldest mtime first so the team
// memory grows in history order. Pure, tested. The watcher drains it a few sessions per
// scan (backfillChunk) so a big backlog never starves live traffic or floods the server.
func backfillQueue(all map[string]session, done map[string]bool, ignores, only []string) []string {
	var out []string
	for sid, s := range all {
		if done[sid] || excludedReason(s.path, ignores, only) != "" {
			continue
		}
		out = append(out, sid)
	}
	sort.Slice(out, func(i, j int) bool { return all[out[i]].mtime.Before(all[out[j]].mtime) })
	return out
}

// backfillChunk: backlog sessions shipped per scan tick. Each send is a synchronous POST the
// server answers only after its LLM analysis, so the real throttle is server latency; the
// chunk just bounds how long one tick can block live-session shipping.
const backfillChunk = 3

// drainVerdict is one backlog session's fate this tick. Pure decision, tested.
type drainVerdict int

const (
	drainSend drainVerdict = iota // untouched since startup and settled — ship it
	drainSkip                     // the live path shipped it (or owns it) — mark done, no send
	drainWait                     // young mtime, may be mid-write — retry next tick
)

// drainVerdictFor keeps backfill from double-shipping or shipping half-written files:
// a session that already shipped live this run, or whose mtime moved past its startup
// seed, belongs to the live path (which sends the WHOLE transcript on settle) — backfill
// just marks it done. A still-young mtime gets the same quiet window live sends get.
func drainVerdictFor(now time.Time, cur session, seededMtime, lastSentAt time.Time, quiet time.Duration) drainVerdict {
	if !lastSentAt.IsZero() || cur.mtime.After(seededMtime) {
		return drainSkip
	}
	if now.Sub(cur.mtime) < quiet {
		return drainWait
	}
	return drainSend
}

// retryAfter: how long the watcher pauses ALL work after n consecutive server-side failures —
// `base` doubled per failure, capped at `max`. Pure; tested.
//
// Without this the cost of a dead server was unbounded: `due()` gates on lastSent, which is only
// recorded on SUCCESS, so every queued session was re-read, re-redacted, re-marshalled and
// re-POSTed every poll tick (2.5s) for as long as the outage lasted. A 44h outage on one lab box
// burned ~24h of CPU across three watchers and 1.8GB of journald, for zero successful sends.
// Backing off the whole pipeline — not just the send — is what makes it
// cheap: no walk, no read, no redact while the server is known-down.
func retryAfter(n int, base, max time.Duration) time.Duration {
	d := base
	for i := 1; i < n && d < max; i++ {
		d *= 2
	}
	if d > max {
		d = max
	}
	return d
}

func backfilledPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "backfilled")
}

// loadBackfilled reads the backfill ledger: one session_id per line, appended on each
// successful backfill send. Missing file = nothing backfilled yet. This is what makes the
// one-time upload resumable — a restart re-queues only what never made it to the server.
func loadBackfilled(path string) map[string]bool {
	out := map[string]bool{}
	b, err := os.ReadFile(path)
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			out[line] = true
		}
	}
	return out
}

func markBackfilled(path, sid string) {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		log.Printf("backfill ledger %s: %v (session may re-ship after a restart)", path, err)
		return
	}
	defer f.Close()
	fmt.Fprintln(f, sid)
}

func ignorePath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "ignore")
}

func onlyPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "only")
}

// denyPath / allowPath: the strict-mode term lists, one term per line ('#' comments), loaded
// with loadIgnores like ignore/only — reloaded each send, editable without a restart.
func denyPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "redact.deny")
}

func allowPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "redact.allow")
}

// loadIgnores reads ~/.csa/ignore: one glob per line, '#' comments and blank lines skipped.
// Missing file = ignore nothing. It's tiny, so the watcher reloads it every scan tick and
// edits apply without a restart.
func loadIgnores(path string) []string {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var out []string
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "#") {
			out = append(out, line)
		}
	}
	return out
}

// projectDir is the project folder name a session belongs to: its parent directory, except a
// subagent transcript (<project>/<parent-uuid>/subagents/[workflows/<wf>/]agent-*.jsonl)
// belongs to <project>, so the parent session's only/ignore rule covers its subagents too.
func projectDir(sessionPath string) string {
	dir := filepath.Dir(sessionPath)
	if wf := filepath.Dir(dir); filepath.Base(wf) == "workflows" && filepath.Base(filepath.Dir(wf)) == "subagents" {
		dir = filepath.Dir(wf)
	}
	if filepath.Base(dir) == "subagents" {
		dir = filepath.Dir(filepath.Dir(dir))
	}
	return filepath.Base(dir)
}

// ignoredBy returns the first pattern matching the session's projectDir (the munged cwd under
// ~/.claude/projects, e.g. -Users-you-Git-secret-repo — what users realistically target with
// '*secret-repo*'), the session FILE's basename (so non-standard roots can select by filename,
// e.g. only 'session_*' over a bot's mixed storage dir), or its full path; "" if none. Glob '*'
// never crosses a path separator, so the dir-name and basename checks are the ones that
// usually matter.
func ignoredBy(sessionPath string, patterns []string) string {
	dir := projectDir(sessionPath)
	base := filepath.Base(sessionPath)
	for _, p := range patterns {
		if ok, _ := filepath.Match(p, dir); ok {
			return p
		}
		if ok, _ := filepath.Match(p, base); ok {
			return p
		}
		if ok, _ := filepath.Match(p, sessionPath); ok {
			return p
		}
	}
	return ""
}

// excludedReason reports why a session must NOT ship, or "" if it may. An ignore glob wins
// first; then, when an allowlist (~/.csa/only) has any pattern, a session that matches none of
// them is excluded. An empty allowlist means index every folder (the default). Both lists use
// ignoredBy's matching. Pure; tested.
func excludedReason(sessionPath string, ignores, only []string) string {
	if pat := ignoredBy(sessionPath, ignores); pat != "" {
		return "matches ignore '" + pat + "'"
	}
	// ignore also matches the file's own folder (e.g. 'subagents'): a blocklist that over-matches
	// fails closed, so only the allowlist is limited to the project folder.
	for _, p := range ignores {
		if ok, _ := filepath.Match(p, filepath.Base(filepath.Dir(sessionPath))); ok {
			return "matches ignore '" + p + "'"
		}
	}
	if len(only) > 0 && ignoredBy(sessionPath, only) == "" {
		return "not in the ~/.csa/only allowlist"
	}
	return ""
}

var httpc = &http.Client{Timeout: 180 * time.Second} // server runs `claude -p` per ingest; be generous

// Set from config by runWatcher (and by runInit for its own requests); empty in tests.
var (
	ownerID        string
	cfID, cfSecret string
	authToken      string // shared bearer secret (token auth); sent as Authorization: Bearer on every request
	watchRoot      string // the watched projects dir; enables per-session .owner sidecars
	redactMode     string // effective redaction mode for send(); "" -> secrets (see send)
)

// emailish: the sidecar's first line must LOOK like one email address — "TODO ping @alice"
// must not mint a Person server-side. Shape check, not RFC 5322; good enough to reject prose.
var emailish = regexp.MustCompile(`^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$`)

// ownerFor returns a per-session owner override, or "". Convention for DELEGATED machines
// (a bot/CI host shipping sessions it runs on humans' behalf, opted in via `init
// --delegated` — never honored on a default install): the launcher drops a "<name>.owner"
// file whose first line is the requester's email, either next to "<name>.jsonl" or naming
// an ancestor directory under the watch root — the latter is how subagent transcripts
// (<parent-uuid>/subagents/agent-*.jsonl) inherit <parent-uuid>.owner. A line that isn't
// email-shaped is ignored, so a stray file can only fall back to the configured identity.
func ownerFor(root, path string) string {
	if root == "" {
		return ""
	}
	read := func(p string) string {
		b, err := os.ReadFile(p)
		if err != nil {
			return ""
		}
		line, _, _ := strings.Cut(string(b), "\n")
		if line = strings.TrimSpace(line); emailish.MatchString(line) {
			return line
		}
		return ""
	}
	if o := read(strings.TrimSuffix(path, ".jsonl") + ".owner"); o != "" {
		return o
	}
	root = filepath.Clean(root)
	for d := filepath.Dir(filepath.Clean(path)); d != root && strings.HasPrefix(d, root+string(filepath.Separator)); d = filepath.Dir(d) {
		if o := read(filepath.Join(filepath.Dir(d), filepath.Base(d)+".owner")); o != "" {
			return o
		}
	}
	return ""
}

// addAuthHeaders attaches the credentials every outbound request carries. Two independent
// concerns, either or both may be configured: a shared bearer token authenticates the ORIGIN
// (Authorization: Bearer <token>, the token-auth mode the external trial runs), and Cloudflare
// Access service-token headers authenticate the EDGE. CF for the edge, bearer for the origin.
func addAuthHeaders(req *http.Request) {
	if authToken != "" {
		req.Header.Set("Authorization", "Bearer "+authToken)
	}
	if cfID != "" {
		req.Header.Set("CF-Access-Client-Id", cfID)
		req.Header.Set("CF-Access-Client-Secret", cfSecret)
	}
}

// resolveAuthToken picks the shared bearer secret: $CSA_AUTH_TOKEN (dev/CI) overrides the
// configured token. The `init --token` flag writes the winner into config, so at run time
// the precedence is $CSA_AUTH_TOKEN > config auth_token. Empty means no token is sent
// (off/loopback dev, or cloudflare mode where the edge mints the credential).
func resolveAuthToken(configured string) string {
	if v := strings.TrimSpace(os.Getenv("CSA_AUTH_TOKEN")); v != "" {
		return v
	}
	return configured
}

// claudeEmail returns the logged-in Claude Code account email from ~/.claude.json, or "".
func claudeEmail() string {
	home, _ := os.UserHomeDir()
	var cfg struct {
		OauthAccount struct {
			EmailAddress string `json:"emailAddress"`
		} `json:"oauthAccount"`
	}
	if b, err := os.ReadFile(filepath.Join(home, ".claude.json")); err == nil {
		if json.Unmarshal(b, &cfg) == nil {
			return cfg.OauthAccount.EmailAddress
		}
	}
	return ""
}

// owner returns who this machine's sessions belong to: the logged-in Claude Code account
// email (~/.claude.json — the same source the server uses for itself), else the OS username.
func owner() string {
	if e := claudeEmail(); e != "" {
		return e
	}
	if u, err := user.Current(); err == nil {
		return u.Username
	}
	return ""
}

// gateExit refuses to run the watcher: a styled, human line when someone runs it in a
// terminal (the docs-skipper's first impression), the plain log form under launchd/redirects.
func gateExit(pretty, plain string) {
	if styled {
		fmt.Fprintf(os.Stderr, "%s %s\n", failMark(), pretty)
	} else {
		log.Printf("%s", plain)
	}
	os.Exit(exitConfig)
}

// tlsFailed is set when a send died on certificate verification, so `ship` can exit non-zero.
var tlsFailed bool

// send reads, redacts, and POSTs one session. `ok` is false only on transport-level failure
// (server down, bad reply) so the caller retries next scan; a decoded {ok:false} is logged and
// counted as sent — the content won't change until the file grows again (mirrors etl.py).
// `down` says the SERVER is the reason (vs this one file being unreadable): it trips the
// watcher's backoff, which pauses the whole pipeline. A local read error must not.
func send(server, sid, path string) (ok, down bool) {
	raw, err := os.ReadFile(path)
	if err != nil {
		log.Printf("read %s FAILED: %v (retry next scan)", sid, err)
		return false, false
	}
	owner := ownerID
	if watchRoot != "" {
		if o := ownerFor(watchRoot, path); o != "" {
			owner = o
		}
	}
	// Policy from config mode + the (reloaded-each-send) deny/allow files. Empty mode -> secrets,
	// so the wire contract always carries a concrete redaction_mode string. The server re-redacts
	// at max(declared, its own minimum), but this is the only gate before a live token hits the wire.
	mode := redactMode
	if mode == "" {
		mode = "secrets"
	}
	policy := redactPolicy{Mode: mode, Deny: loadIgnores(denyPath()), Allow: loadIgnores(allowPath())}
	body, _ := json.Marshal(map[string]string{"session_id": sid, "content": redactWith(policy, string(raw)), "owner": owner, "redaction_mode": mode})
	req, err := http.NewRequest("POST", server+"/api/ingest", bytes.NewReader(body))
	if err != nil {
		log.Printf("send %s FAILED: %v", sid, err)
		return false, false
	}
	req.Header.Set("Content-Type", "application/json")
	addAuthHeaders(req)
	resp, err := httpc.Do(req)
	if err != nil {
		if certError(err) {
			tlsFailed = true
			markTLSBroken(certAdvice(err))
			log.Printf("send %s FAILED: %s", sid, certAdvice(err))
			return false, true
		}
		log.Printf("send %s FAILED: %v — server down? will retry", sid, err)
		return false, true
	}
	defer resp.Body.Close()
	// A 401 is the one non-2xx that will NEVER fix itself by retrying, and since tokens now expire
	// on idle it is the expected end-of-life for any machine that went quiet for a while. Retrying
	// forever behind a generic "HTTP 401 (will retry)" is how a laptop silently stops shipping for
	// weeks. Nothing is lost — the watermark is not advanced, so everything re-ships after re-auth —
	// but the user has to be told what to do, in words, in the log they will actually be pointed at.
	if resp.StatusCode == http.StatusUnauthorized {
		log.Printf("send %s FAILED: HTTP 401 — this machine's CSA credential is no longer accepted "+
			"(expired after inactivity, or revoked). Run `csa-client setup` to sign in again. "+
			"Nothing is lost: sessions keep queueing locally and ship once you do.", sid)
		// `send` runs detached (shipDetach) — this log line reaches ~/.csa/ship.log, not the user.
		// Mark it so the ambient hook (synchronous, on the very next prompt) can actually tell them.
		markAuthBroken()
		return false, true
	}
	// Spec (etl.py/urlopen): any other non-2xx is a transport failure — retry. A proxy 502 or a
	// misconfigured-path 404 can carry a JSON body that would otherwise decode as ok=false
	// and silently drop the session forever.
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		log.Printf("send %s FAILED: HTTP %s (will retry)", sid, resp.Status)
		return false, true
	}
	clearAuthBroken() // any 2xx here proves the credential works, independent of application-level r.OK below
	clearTLSBroken()
	var r struct {
		OK            bool   `json:"ok"`
		HiddenContext int    `json:"hidden_context"`
		Entities      int    `json:"entities"`
		Error         string `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		log.Printf("send %s: unreadable reply: %v (will retry)", sid, err)
		return false, true
	}
	if r.OK {
		if owner != ownerID { // a sidecar override is worth seeing in the log
			log.Printf("sent %s ok=true hidden_context=%d entities=%d owner=%s", sid, r.HiddenContext, r.Entities, owner)
		} else {
			log.Printf("sent %s ok=true hidden_context=%d entities=%d", sid, r.HiddenContext, r.Entities)
		}
	} else {
		log.Printf("sent %s ok=false error=%q (server rejected; not retrying until file changes)", sid, r.Error)
	}
	return true, false
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envDur(k string, def time.Duration) time.Duration {
	if v := os.Getenv(k); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
		log.Printf("bad %s=%q, using default %s", k, os.Getenv(k), def)
	}
	return def
}

func usage(w io.Writer) { fmt.Fprint(w, usageText) }

// commandUsage returns the lines of usageText that describe cmd, or "" when it has none.
func commandUsage(cmd string) string {
	var b strings.Builder
	in := false
	for _, line := range strings.Split(usageText, "\n") {
		if line == "  "+cmd || strings.HasPrefix(line, "  "+cmd+" ") {
			in = true
		} else if !strings.HasPrefix(line, "   ") {
			in = false
		}
		if in {
			b.WriteString(line + "\n")
		}
	}
	return b.String()
}

const usageText = `usage: csa-client <command> [flags]

Connect this machine
  setup            sign in through your browser, then wire the Claude Code hooks.
                   flags: --server URL  --backfill  --yes  --no-browser
  init             consent wizard for a self-hosted server with a shared token, then
                   wires the same hooks as setup. --install ships through a login watcher instead.
                   flags: --server URL  --token SECRET  --email E  --only GLOBS
                          --redact-mode MODE  --backfill=false  --delegated
                          --cf-id ID  --cf-secret S  --yes  --install  --ambient
  uninstall        remove the hooks, the MCP server and the login watcher, and revoke this
                   machine's credential.
                   flags: --purge (also delete ~/.csa)

Choose what ships
  only [GLOB]      index ONLY matching project folders. 'only pick' chooses from a list.
  ignore [GLOB]    never ship matching project folders.
  redact           show the redaction level. 'redact set <secrets|privacy|strict>' changes it,
                   'redact check [--mode M] [--file F]' previews it.
  forget ID...     delete shipped sessions and their facts from the server.
                   flags: --project GLOB  --owner EMAIL  --yes   (flags go before the ids)

Check on it
  doctor           read-only health report to send to your CSA admin.
  team             who else ships to this server.
  demo             ask the team memory a warm question in a new Claude Code session.
  version          print the version, and note if the server publishes a different build.
  update           replace this binary with the one your server publishes. flags: --yes

Run by Claude Code or the system (you rarely type these)
  hook             UserPromptSubmit hook: ambient recall. 'hook off' removes every CSA hook.
  ship             SessionEnd hook: ship the session that just ended.
  ship --sweep     SessionStart hook: ship sessions whose SessionEnd never fired.
  (no command)     the watcher, for 'init --install' machines.
  install          start the watcher at login.

Environment overrides: CSA_SERVER, CSA_AUTH_TOKEN, CSA_ROOT, CSA_QUIET, CSA_INTERVAL,
CSA_COOLDOWN, CSA_RETRY_MAX, CSA_HOOK_TIMEOUT. See README.md.
`

// version is stamped at build time via -ldflags "-X main.version=..." (release.yml, server/Dockerfile). "dev" for local builds.
var version = "dev"

func main() {
	if len(os.Args) > 2 && (os.Args[2] == "-h" || os.Args[2] == "--help" || os.Args[2] == "-help") {
		switch os.Args[1] {
		case "init", "setup", "forget", "doctor": // their own -h describes every flag
		default:
			if help := commandUsage(os.Args[1]); help != "" {
				fmt.Print(help)
				return
			}
		}
	}
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "version", "--version", "-v":
			fmt.Println(version)
			notifyIfUpdate() // best-effort, silent on no-server/offline/dev build
		case "update":
			runUpdate(os.Args[2:])
		case "init":
			runInit(os.Args[2:])
		case "setup":
			runSetup(os.Args[2:])
		case "install":
			runInstall()
		case "uninstall":
			runUninstall(os.Args[2:])
		case "ignore":
			runIgnore(os.Args[2:])
		case "only":
			runOnly(os.Args[2:])
		case "forget":
			runForget(os.Args[2:])
		case "demo":
			runDemo()
		case "team":
			runTeam()
		case "hook":
			runHook(os.Args[2:])
		case "ship":
			// `ship` reads a SessionEnd event from stdin; `ship --sweep` (the SessionStart hook)
			// re-checks every transcript against its marker. Both POST /api/ingest, which can take
			// minutes server-side, so both hand the work to a background copy and return now — a
			// hook must never make the user wait on the server (shipDetach).
			ev, parent := shipDetach(os.Stdin)
			if parent {
				if advice := tlsBrokenAdvice(); advice != "" {
					fmt.Fprintln(os.Stderr, "csa-client: the last ship failed on the server's certificate: "+advice)
					os.Exit(exitTLS)
				}
				return
			}
			if len(os.Args) > 2 && os.Args[2] == "--sweep" {
				runSweep(ev)
			} else {
				runShip(ev)
			}
			if tlsFailed {
				os.Exit(exitTLS)
			}
		case "redact":
			runRedact(os.Args[2:])
		case "doctor":
			runDoctor(os.Args[2:])
		case "help", "-h", "--help":
			usage(os.Stdout)
		default:
			fmt.Fprintf(os.Stderr, "csa-client: unknown command %q. Run 'csa-client help' for the list.\n", os.Args[1])
			os.Exit(2)
		}
		return
	}
	runWatcher()
}

// forgetOK decides whether a `forget` reply is a success. The server's forget() returns HTTP
// 200 with an application-level {"ok":false,"error":...} for an unknown/failed session
// (apiserver.py), so a 2xx status alone is NOT success — the JSON `ok` field must be truthy
// too. Any non-2xx, an unparseable body, or ok=false is a failure. Pure; tested.
func forgetOK(status int, body []byte) bool {
	if status < 200 || status > 299 {
		return false
	}
	var r struct {
		OK bool `json:"ok"`
	}
	if json.Unmarshal(body, &r) != nil {
		return false
	}
	return r.OK
}

// forgetOne POSTs a single /api/forget and prints the server's reply line, returning forgetOK's
// verdict so bulk callers can tally. Shared by the single-id and --project paths.
func forgetOne(server, owner, id string) bool {
	body, _ := json.Marshal(map[string]string{"session_id": id, "owner": owner})
	req, _ := http.NewRequest("POST", server+"/api/forget", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	addAuthHeaders(req)
	resp, err := httpc.Do(req)
	if err != nil {
		fmt.Println(failMark() + " forget " + id + " FAILED: " + err.Error())
		return false
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	ok := forgetOK(resp.StatusCode, b)
	mark := failMark()
	if ok {
		mark = okMark()
	}
	fmt.Println(mark + " " + strings.TrimSpace(string(b)))
	return ok
}

// bulkConfirmed reports whether a typed answer authorizes a bulk delete. Empty — plain Enter, or
// EOF from non-interactive/closed stdin — is NO: a destructive bulk delete must be opt-in, never
// the default. Pure; tested (it guards the one irreversible path in this binary).
func bulkConfirmed(answer string) bool {
	a := strings.ToLower(strings.TrimSpace(answer))
	return a == "y" || a == "yes"
}

// sessionIDsUnder returns the session ids (basename sans .jsonl) whose project folder or full
// path matches glob, under root — reusing ignoredBy so `forget --project` globs behave exactly
// like ignore/only globs ('*repo*' targets the munged project folder). Sorted for stable output.
// Only LOCAL sessions are visible: a session shipped from another machine, or whose local .jsonl
// was deleted, won't be found — forget those by id. Pure; tested.
func sessionIDsUnder(root, glob string) []string {
	var ids []string
	for sid, s := range entries(root) {
		if ignoredBy(s.path, []string{glob}) != "" {
			ids = append(ids, sid)
		}
	}
	sort.Strings(ids)
	return ids
}

// runForget deletes already-shipped sessions and everything derived from them: one or more by id,
// or every LOCAL session matching --project '<glob>'. No consent gate — deleting your own data is
// a right, not a shipment. Bulk deletes (more than one id, or any --project) confirm first — the
// deletion is irreversible server-side — unless --yes. Needs a server URL (config or CSA_SERVER).
func runForget(args []string) {
	fs := flag.NewFlagSet("forget", flag.ExitOnError)
	ownerF := fs.String("owner", "", "owner the session was attributed to (delegated machines: the sidecar email; default: your init identity)")
	projectF := fs.String("project", "", "delete every LOCAL session whose project folder/path matches this glob (e.g. '*repo-name*')")
	yesF := fs.Bool("yes", false, "skip the confirmation prompt for a bulk delete")
	fs.Parse(args)

	server := os.Getenv("CSA_SERVER")
	owner := *ownerF
	var cfgToken string
	if cfg, err := loadConfig(configPath()); err == nil {
		if server == "" {
			server = cfg.Server
		}
		cfID, cfSecret = cfg.CFID, cfg.CFSecret
		cfgToken = cfg.AuthToken
		if owner == "" {
			owner = cfg.Identity // server enforces owner-matched delete
		}
	}
	authToken = resolveAuthToken(cfgToken)
	server = strings.TrimRight(server, "/")
	if server == "" {
		fatalf(exitConfig, "no server configured. Run 'csa-client setup --server <url>' first.")
	}

	// Build the delete list: --project (local glob) OR positional ids — not both.
	var ids []string
	if *projectF != "" {
		if leftover := fs.Args(); len(leftover) > 0 {
			fatalf(2, "give session ids OR --project '<glob>', not both (got %v alongside --project)", leftover)
		}
		if _, err := filepath.Match(*projectF, "x"); err != nil {
			fatalf(2, "bad --project pattern %q: %v", *projectF, err)
		}
		home, _ := os.UserHomeDir()
		root := env("CSA_ROOT", filepath.Join(home, ".claude", "projects"))
		ids = sessionIDsUnder(root, *projectF)
		if len(ids) == 0 {
			fmt.Printf("No local sessions match %q under %s — nothing to forget.\n", *projectF, root)
			return
		}
	} else {
		ids = fs.Args()
		// Go's flag package stops at the first positional, so a flag placed AFTER an id (e.g.
		// `forget a b --yes`) would otherwise be swallowed as a session id to delete. Session ids
		// never start with '-', so reject that and point the user at the right order.
		for _, id := range ids {
			if strings.HasPrefix(id, "-") {
				fatalf(2, "unexpected %q — flags must come BEFORE the ids (e.g. `forget --yes <id> <id>`)", id)
			}
		}
	}
	if len(ids) == 0 {
		fmt.Fprintln(os.Stderr, "usage: csa-client forget [--owner email] <session-id> [<id>...]\n"+
			"       csa-client forget --project '*repo-name*' [--yes]\n"+
			"(the id is shown in 'sent <id> ok=true' logs; flags go BEFORE the ids)")
		os.Exit(2)
	}

	// Confirm irreversible bulk deletes. A single explicit id keeps the old no-prompt behavior.
	// The prompt defaults to NO and reads stdin directly: on EOF / non-interactive stdin (a pipe,
	// /dev/null — which is a *char device*, so a TTY check would wrongly pass) it aborts and
	// deletes nothing, telling the user to pass --yes. Fail-safe is the only safe default here.
	bulk := *projectF != "" || len(ids) > 1
	if bulk && !*yesF {
		fmt.Printf("About to permanently delete %d session(s) and every fact mined from them on %s:\n",
			len(ids), cyan(server))
		for _, id := range ids {
			fmt.Println("  " + id)
		}
		fmt.Printf("%s %s ", bold("Delete these permanently? This cannot be undone."), dim("[y/N]"))
		ans, _ := stdinR.ReadString('\n')
		if !bulkConfirmed(ans) {
			fmt.Println("Aborted — nothing deleted (pass --yes to skip this prompt).")
			return
		}
	}

	ok, fail := 0, 0
	for _, id := range ids {
		if id == "" {
			continue
		}
		if forgetOne(server, owner, id) {
			ok++
		} else {
			fail++
		}
	}
	if bulk {
		fmt.Printf("%s %d deleted, %d failed.\n", okMark(), ok, fail)
	}
	if fail > 0 {
		os.Exit(1)
	}
}

// redactCheckSample is the built-in preview input for `redact check`: one adversarial line
// carrying a coworker name, a project codename, an internal hostname, a live-shaped token, an
// email, and a prose secret — so `check` shows exactly what each mode keeps vs strips.
const redactCheckSample = "Standup: Sarah on the Bluefin launch pushed a fix to db01.internal, " +
	"token ghp_xxxxxxxxxxxxxxxxxxxx, ping her at sarah@acme.com, box password: hunter2."

// effectiveRedactMode returns the configured mode (or secrets when unconfigured). Read-only —
// used by the redact subcommand, which never writes config.
func effectiveRedactMode() string {
	if cfg, err := loadConfig(configPath()); err == nil {
		return cfg.redactionMode()
	}
	return "secrets"
}

// runRedact (`csa-client redact`): print the current mode + deny/allow counts. `redact check`
// previews on a sample; `redact set <mode>` changes the mode in place. `check` is read-only;
// `set` writes only config.redaction.mode (init still owns consent).
func runRedact(args []string) {
	if len(args) > 0 && args[0] == "check" {
		runRedactCheck(args[1:])
		return
	}
	if len(args) > 0 && args[0] == "set" {
		runRedactSet(args[1:])
		return
	}
	mode := effectiveRedactMode()
	deny, allow := loadIgnores(denyPath()), loadIgnores(allowPath())
	fmt.Printf("Redaction level: %s\n", bold(mode))
	fmt.Printf("  %s\n", dim(redactModeDesc(mode)))
	fmt.Printf("  %s — %d deny term(s)\n", cyan(denyPath()), len(deny))
	fmt.Printf("  %s — %d allow term(s)  %s\n", cyan(allowPath()), len(allow), dim("(deny/allow apply whenever set)"))
	fmt.Printf("Preview it: %s\n", cyan(selfInvocation()+" redact check --mode "+mode))
}

// runRedactSet (`csa-client redact set <mode>`): change the redaction level in place. Writes ONLY
// config.redaction.mode (all other fields — server, identity, CF creds, consent — are preserved by
// saveConfig marshalling the loaded struct). No re-consent: every mode ships <= what `secrets`
// ships, and secrets is the already-consented v2 baseline, so a mode change never widens the
// shipped-data promise. Needs an existing config — you can't set a mode before `init` records consent.
func runRedactSet(args []string) {
	if len(args) != 1 {
		fatalf(2, "usage: %s redact set <secrets|privacy|strict>", selfInvocation())
	}
	mode := strings.ToLower(strings.TrimSpace(args[0]))
	if _, ok := redactLevels[mode]; !ok {
		fatalf(2, "unknown mode %q — use secrets, privacy, or strict", mode)
	}
	cfg, err := loadConfig(configPath())
	if err != nil {
		fatalf(exitConfig, "no config at %s. Run '%s setup --server <url>' first.", configPath(), selfInvocation())
	}
	old := cfg.redactionMode()
	if cfg.Redaction == nil {
		cfg.Redaction = &redactPolicy{}
	}
	cfg.Redaction.Mode = mode
	if err := saveConfig(configPath(), cfg); err != nil {
		fatalf(exitConfig, "writing %s: %v", configPath(), err)
	}
	if mode == old {
		fmt.Printf("%s Redaction level already %s.\n", okMark(), bold(mode))
	} else {
		fmt.Printf("%s Redaction level %s -> %s.\n  %s\n", okMark(), old, bold(mode), dim(redactModeDesc(mode)))
	}
	fmt.Printf("Preview it: %s\n", cyan(selfInvocation()+" redact check --mode "+mode))
}

// runRedactCheck (`csa-client redact check`): redact a sample (--file, else piped stdin, else the
// built-in sample) under a mode (--mode, else the configured one) and print the redacted text plus
// the labels that fired. Read-only; uses the real ~/.csa deny/allow files so strict is realistic.
func runRedactCheck(args []string) {
	fs := flag.NewFlagSet("redact check", flag.ExitOnError)
	modeF := fs.String("mode", "", "redaction level to preview: secrets|privacy|strict (default: configured mode)")
	fileF := fs.String("file", "", "file to redact (default: piped stdin, else a built-in sample)")
	fs.Parse(args)

	mode := *modeF
	if mode == "" {
		mode = effectiveRedactMode()
	}
	if _, ok := redactLevels[mode]; !ok {
		fatalf(2, "unknown --mode %q — use secrets, privacy, or strict", mode)
	}

	var input, source string
	switch {
	case *fileF != "":
		b, err := os.ReadFile(*fileF)
		if err != nil {
			fatalf(1, "reading %s: %v", *fileF, err)
		}
		input, source = string(b), *fileF
	case !stdinIsTTY():
		b, _ := io.ReadAll(os.Stdin)
		if strings.TrimSpace(string(b)) != "" {
			input, source = string(b), "stdin"
		} else {
			input, source = redactCheckSample, "built-in sample"
		}
	default:
		input, source = redactCheckSample, "built-in sample"
	}

	p := redactPolicy{Mode: mode, Deny: loadIgnores(denyPath()), Allow: loadIgnores(allowPath())}
	out, labels := redactReport(p, input)
	fmt.Printf("%s %s  %s\n\n%s\n", dim("mode:"), bold(mode), dim("("+source+")"), out)
	if len(labels) == 0 {
		fmt.Printf("\n%s %s\n", dim("labels:"), "none — nothing matched")
	} else {
		fmt.Printf("\n%s %s\n", dim("labels:"), strings.Join(labels, ", "))
	}
	if mode != "strict" {
		fmt.Printf("%s\n", dim("Names, codenames, and public hostnames need strict mode + a ~/.csa/redact.deny list."))
	}
}

// runWatcher is the default (no-subcommand) path. CONSENT GATE: it refuses to ship anything
// without a current consent record, and it never prompts — it may run under launchd, where a
// blocked prompt respawn-loops. Exit 78 (EX_CONFIG) so "needs init" is distinguishable from
// a crash. CSA_SERVER still overrides the configured server for dev use, but there is no
// consent-free path and no silent default server.
func runWatcher() {
	cfg, err := loadConfig(configPath())
	if err != nil {
		gateExit(fmt.Sprintf("Not set up yet. Run %s first.", cyan(selfInvocation()+" setup --server <url>")),
			fmt.Sprintf("no consent on record (no config at %s). Run 'csa-client setup', or 'csa-client init' on a shared-token server.", configPath()))
	}
	if !cfg.consentOK() {
		gateExit(fmt.Sprintf("Your consent predates policy v%d — run %s to review what changed.",
			requiredPolicyVersion, cyan(selfInvocation()+" init")),
			fmt.Sprintf("no current consent on record (policy v%d required) — run 'csa-client init'", requiredPolicyVersion))
	}
	// Identity drift: consent was given by a person, not a machine. If the logged-in Claude
	// account is now a different email, stop shipping until that person re-consents.
	if live := claudeEmail(); identityDrift(cfg.Identity, live) {
		gateExit(fmt.Sprintf("Identity changed (%s at consent, %s now) — re-run %s.",
			cfg.Identity, live, cyan(selfInvocation()+" init")),
			fmt.Sprintf("identity changed (%s at consent, %s now) — re-run 'csa-client init'", cfg.Identity, live))
	}
	if !strings.Contains(cfg.Identity, "@") {
		log.Printf("warning: identity %q is an OS-username fallback, not an email — attribution will be weak", cfg.Identity)
	}

	server := strings.TrimRight(env("CSA_SERVER", cfg.Server), "/")
	if server == "" {
		gateExit("No server configured. Re-run setup or init.", "no server configured and no CSA_SERVER set. Re-run 'csa-client setup' or 'csa-client init'.")
	}
	ownerID = cfg.Identity
	if ownerID == "" {
		ownerID = owner()
	}
	cfID, cfSecret = cfg.CFID, cfg.CFSecret
	authToken = resolveAuthToken(cfg.AuthToken)
	redactMode = cfg.redactionMode() // default "secrets"; deny/allow come from the files in send()

	home, _ := os.UserHomeDir()
	root := env("CSA_ROOT", filepath.Join(home, ".claude", "projects"))
	if cfg.Delegated { // sidecar attribution is an explicit operator opt-in (see ownerFor)
		watchRoot = root
	}
	quiet := envDur("CSA_QUIET", 10*time.Second) // settle window: don't ship a half-written session
	interval := envDur("CSA_INTERVAL", 2500*time.Millisecond)
	cooldown := envDur("CSA_COOLDOWN", 10*time.Minute) // see due(): one LLM extraction per send

	// Seed current mtimes so startup does NOT re-ship the whole backlog — only sessions that
	// appear or grow after the client starts are sent. With backfill on, the pre-existing
	// sessions instead go on a one-time queue, drained a few per scan below. ONE walk feeds
	// both, so no session can slip between the seed and the queue.
	initial := entries(root)
	sent := map[string]time.Time{}
	for sid, s := range initial {
		sent[sid] = s.mtime
	}
	lastSent := map[string]time.Time{} // wall-clock time of last successful send, per session

	var backlog []string
	if cfg.Backfill {
		backlog = backfillQueue(initial, loadBackfilled(backfilledPath()),
			loadIgnores(ignorePath()), loadIgnores(onlyPath()))
	}
	log.Printf("watching %s (%d existing sessions seeded, %d queued for backfill) -> %s (quiet=%s, poll=%s, cooldown=%s)",
		root, len(sent), len(backlog), server, quiet, interval, cooldown)

	// Server-down backoff: while retryAt is in the future the loop does NOTHING — no walk, no
	// read, no redact, no POST, no log line. See retryAfter for why that matters.
	retryMax := envDur("CSA_RETRY_MAX", 5*time.Minute)
	failures := 0
	var retryAt time.Time

	ignoredLogged := map[string]bool{} // "ignoring ..." logs once per session, not per scan
	for {
		if time.Now().Before(retryAt) {
			time.Sleep(interval)
			continue
		}
		all := entries(root)
		// Client-side filtering: drop excluded sessions BEFORE anything reads their content.
		// ~/.csa/ignore is a blocklist; ~/.csa/only is an allowlist (when non-empty, index ONLY
		// matching folders). Both reload every tick, so edits apply without a restart. Advancing
		// the dedup watermark while excluded means a later un-exclude does NOT ship sessions that
		// were only active while private (same rule as startup seeding); a session that continues
		// afterwards ships whole, though — transcripts are the unit.
		ignores := loadIgnores(ignorePath())
		only := loadIgnores(onlyPath())
		for sid, s := range all {
			if reason := excludedReason(s.path, ignores, only); reason != "" {
				if !ignoredLogged[sid] {
					log.Printf("skipping %s (%s)", sid, reason)
					ignoredLogged[sid] = true
				}
				sent[sid] = s.mtime
				delete(all, sid)
			}
		}
		// One transport failure stops this tick: the rest of the queue would fail identically,
		// and each attempt costs a full read+redact+marshal of a possibly-multi-MB transcript.
		stalled := false
		queued := 0
		for _, sid := range due(time.Now(), all, sent, lastSent, quiet, cooldown) {
			queued++
			if stalled {
				continue // just counting what's waiting, for the one log line below
			}
			ok, down := send(server, sid, all[sid].path)
			if ok {
				sent[sid] = all[sid].mtime
				lastSent[sid] = time.Now()
				failures = 0
			}
			stalled = down
		}
		// Backfill drain: live sessions above ship first, then up to backfillChunk backlog
		// sessions per tick. drainVerdictFor keeps this from double-shipping what the live
		// path already handled and from reading a file that's still being written. A
		// transport failure stops the drain until the next tick (server down — no point
		// burning the rest of the chunk); success is recorded in the ledger so restarts
		// resume where this left off.
		for n := 0; n < backfillChunk && len(backlog) > 0 && !stalled; n++ {
			sid := backlog[0]
			s, ok := all[sid]
			if !ok { // deleted or newly excluded since startup
				backlog = backlog[1:]
				continue
			}
			v := drainVerdictFor(time.Now(), s, initial[sid].mtime, lastSent[sid], quiet)
			if v == drainWait { // settling — head of an oldest-first queue, so just retry next tick
				break
			}
			if v == drainSend {
				ok, down := send(server, sid, s.path)
				stalled = down
				if !ok {
					break
				}
				sent[sid] = s.mtime
				lastSent[sid] = time.Now()
				failures = 0
			}
			markBackfilled(backfilledPath(), sid)
			backlog = backlog[1:]
			if len(backlog) == 0 {
				log.Printf("backfill complete")
			} else if len(backlog)%25 == 0 {
				log.Printf("backfill: %d sessions remaining", len(backlog))
			}
		}
		if stalled {
			failures++
			d := retryAfter(failures, interval, retryMax)
			retryAt = time.Now().Add(d)
			// ONE line per backoff window, not one per session per tick — that log line is what
			// filled 1.8GB of journald during the outage.
			log.Printf("server unreachable (%d in a row) — pausing: %d session(s) queued, retry in %s",
				failures, queued+len(backlog), d)
		}
		time.Sleep(interval)
	}
}

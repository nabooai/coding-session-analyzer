package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRedact(t *testing.T) {
	in := "key sk-abcdEFGH1234567890ijklmnop aws AKIAABCDEFGHIJKLMNOP tok ghp_" + strings.Repeat("x", 25) +
		" mail mara@example.com host db.internal.example.com"
	out := redact(in)
	for _, leaked := range []string{"sk-abcd", "AKIAABCD", "ghp_xxx"} {
		if strings.Contains(out, leaked) {
			t.Fatalf("secret %q leaked: %s", leaked, out)
		}
	}
	// emails and hostnames are KEPT on purpose (identity/ownership signal, per user)
	if !strings.Contains(out, "mara@example.com") || !strings.Contains(out, "db.internal.example.com") {
		t.Fatalf("email/hostname must NOT be redacted: %s", out)
	}
	if got := redact("just normal text"); got != "just normal text" {
		t.Fatalf("false positive on clean text: %s", got)
	}
}

func TestCommandUsage(t *testing.T) {
	ship := commandUsage("ship")
	if !strings.Contains(ship, "SessionEnd hook") || !strings.Contains(ship, "ship --sweep") || strings.Contains(ship, "hook off") {
		t.Fatalf("ship --help must print both ship lines and nothing else: %q", ship)
	}
	if got := commandUsage("uninstall"); !strings.Contains(got, "--purge") {
		t.Fatalf("a command's continuation lines belong to it: %q", got)
	}
	if got := commandUsage("forget"); !strings.Contains(got, "flags go before the ids") {
		t.Fatalf("forget --help must say where the flags go: %q", got)
	}
	if got := commandUsage("install"); !strings.Contains(got, "watcher at login") || strings.Contains(got, "uninstall") {
		t.Fatalf("install --help must print install only, never run it: %q", got)
	}
	if got := commandUsage("bogus"); got != "" {
		t.Fatalf("an unknown command has no usage: %q", got)
	}
}

func TestRedactCredentialValueAtSecrets(t *testing.T) {
	if got := redact("Connect with password=hunter2-sekrit only via env"); got != "Connect with password=[REDACTED:credential] only via env" {
		t.Fatalf("the password value must go and the key stay: %s", got)
	}
	for _, keep := range []string{"password reset flow", "the secret is to cache", "token: the bearer header",
		"password=$DB_PASSWORD", `password="${DB_PASSWORD}"`, `password=os.environ["DB_PASSWORD"]`,
		"pwd=/Users/mara/project", "password=abc"} {
		if got := redact(keep); got != keep {
			t.Fatalf("not a secret, must be kept: %q -> %q", keep, got)
		}
	}
	if got := redactWith(redactPolicy{Mode: "secrets", Allow: []string{"hunter2-sekrit"}}, "password=hunter2-sekrit"); got != "password=hunter2-sekrit" {
		t.Fatalf("an allowed value must be kept: %s", got)
	}
}

// Mode-aware redaction: secrets keeps identity signal, privacy strips PII classes, strict
// honors the deny/allow lists — and no mode ever lets a secret through.
func TestRedactModes(t *testing.T) {
	tok := "ghp_" + strings.Repeat("x", 25)

	// secrets: the token dies; the coworker name, email, and hostname are KEPT on purpose.
	sec := redactWith(redactPolicy{Mode: "secrets"},
		"Sarah pushed "+tok+" mail mara@example.com host db01.internal")
	if strings.Contains(sec, "ghp_xxx") {
		t.Fatalf("secrets must redact the token: %s", sec)
	}
	if !strings.Contains(sec, "Sarah") || !strings.Contains(sec, "mara@example.com") || !strings.Contains(sec, "db01.internal") {
		t.Fatalf("secrets must KEEP name/email/hostname: %s", sec)
	}

	// privacy: email, bearer, conn_string, credential, and internal-TLD hostname all redacted.
	priv := redactWith(redactPolicy{Mode: "privacy"},
		"mail mara@example.com auth Bearer abcDEF12345678 "+
			"db postgres://admin:s3cr3tpw@pg.example.com:5432/prod "+
			"cred password=hunter2xyz host app.corp")
	for _, leak := range []string{"mara@example.com", "Bearer abcDEF", "admin:s3cr3tpw", "password=hunter2xyz", "app.corp"} {
		if strings.Contains(priv, leak) {
			t.Fatalf("privacy must redact %q, got: %s", leak, priv)
		}
	}
	for _, lbl := range []string{"conn_string", "bearer", "email", "credential", "hostname"} {
		if !strings.Contains(priv, "[REDACTED:"+lbl+"]") {
			t.Fatalf("privacy missing label %s: %s", lbl, priv)
		}
	}

	// strict: a deny term is redacted; an allow term is kept even though it's also on deny.
	strict := redactWith(
		redactPolicy{Mode: "strict", Deny: []string{"Bluefin", "acme"}, Allow: []string{"acme"}},
		"codename Bluefin for acme corp")
	if strings.Contains(strict, "Bluefin") {
		t.Fatalf("strict must redact the deny term: %s", strict)
	}
	if !strings.Contains(strict, "[REDACTED:denylisted]") {
		t.Fatalf("strict must label a deny hit denylisted: %s", strict)
	}
	if !strings.Contains(strict, "acme") {
		t.Fatalf("strict must honor the allow exception for 'acme': %s", strict)
	}

	// Mirror parity: deny/allow apply whenever configured, NOT only at strict (matches the server's
	// `if deny:`). A secrets-mode user/operator with a deny term still gets it redacted.
	denySecrets := redactWith(redactPolicy{Mode: "secrets", Deny: []string{"Bluefin"}}, "codename Bluefin")
	if strings.Contains(denySecrets, "Bluefin") || !strings.Contains(denySecrets, "[REDACTED:denylisted]") {
		t.Fatalf("deny must apply at secrets mode when configured: %s", denySecrets)
	}

	// redactReport surfaces the labels that fired (for `redact check`), in application order.
	if _, labels := redactReport(redactPolicy{Mode: "privacy"}, "mail a@b.co host x.internal"); len(labels) != 2 ||
		labels[0] != "email" || labels[1] != "hostname" {
		t.Fatalf("redactReport labels = %v, want [email hostname]", labels)
	}
}

// No mode — including unknown/empty — may ever weaken secret redaction; higher modes only add.
func TestRedactWithNeverWeakensSecrets(t *testing.T) {
	in := "tok ghp_" + strings.Repeat("x", 25) +
		" key sk-abcdEFGH1234567890ijklmnop aws AKIAABCDEFGHIJKLMNOP"
	for _, mode := range []string{"secrets", "privacy", "strict", "", "bogus"} {
		out := redactWith(redactPolicy{Mode: mode}, in)
		for _, leak := range []string{"ghp_xxx", "sk-abcd", "AKIAABCD"} {
			if strings.Contains(out, leak) {
				t.Fatalf("mode %q leaked secret %q: %s", mode, leak, out)
			}
		}
	}
	// The effective-mode default is secrets when the config field is nil (shipped-data promise).
	if (&config{}).redactionMode() != "secrets" || (*config)(nil).redactionMode() != "secrets" {
		t.Fatal("nil/empty redaction config must default to secrets")
	}
	if (&config{Redaction: &redactPolicy{Mode: "privacy"}}).redactionMode() != "privacy" {
		t.Fatal("configured mode must be returned")
	}
}

// `redact set <mode>` must change ONLY the mode and preserve every other config field
// (server, identity, consent) — it is a second writer to config.json and must not clobber
// the consent record or credentials.
func TestRedactSetPreservesConfig(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	orig := &config{Server: "http://x", Identity: "me@x.com", CFID: "cfid",
		Consent: &consentRecord{PolicyVersion: requiredPolicyVersion, AcceptedAt: "2026-01-01T00:00:00Z"}}
	if err := saveConfig(configPath(), orig); err != nil {
		t.Fatal(err)
	}
	runRedactSet([]string{"strict"}) // no redaction field yet -> must create it
	got, err := loadConfig(configPath())
	if err != nil {
		t.Fatal(err)
	}
	if got.redactionMode() != "strict" {
		t.Fatalf("mode not set: %q", got.redactionMode())
	}
	if got.Server != "http://x" || got.Identity != "me@x.com" || got.CFID != "cfid" {
		t.Fatalf("non-mode fields clobbered: %+v", got)
	}
	if got.Consent == nil || got.Consent.PolicyVersion != requiredPolicyVersion || got.Consent.AcceptedAt != "2026-01-01T00:00:00Z" {
		t.Fatalf("consent record not preserved: %+v", got.Consent)
	}
	runRedactSet([]string{"privacy"}) // switching again must still preserve consent
	if got, _ := loadConfig(configPath()); got.redactionMode() != "privacy" || got.Consent == nil {
		t.Fatalf("second switch broke config: %+v", got)
	}
}

func TestInstallClaudeHookAt(t *testing.T) {
	settings := filepath.Join(t.TempDir(), "settings.json")
	os.WriteFile(settings, []byte(`{
  "hooks": {
    "PostToolUse": [{"matcher":"Write","hooks":[{"type":"command","command":"/bin/true"}]}],
    "UserPromptSubmit": [
      {"matcher":"","hooks":[{"type":"command","command":"/old/.csa/bin/csa-client","args":["hook"]}]},
      {"matcher":"","hooks":[{"type":"command","command":"/bin/echo","args":["keep"]}]}
    ]
  }
}`), 0o644)
	if err := installAmbientRecallHookAt(settings, "/new/csa-client"); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(settings)
	if err != nil {
		t.Fatal(err)
	}
	var root map[string]any
	if err := json.Unmarshal(b, &root); err != nil {
		t.Fatal(err)
	}
	hooks := root["hooks"].(map[string]any)
	if _, ok := hooks["PostToolUse"]; !ok {
		t.Fatal("unrelated hooks must be preserved")
	}
	ups := hooks["UserPromptSubmit"].([]any)
	blob := string(b)
	if strings.Contains(blob, "/old/.csa/bin/csa-client") {
		t.Fatalf("old CSA hook was not removed:\n%s", blob)
	}
	if !strings.Contains(blob, "/bin/echo") || !strings.Contains(blob, "/new/csa-client") {
		t.Fatalf("kept/new hooks missing:\n%s", blob)
	}
	if len(ups) != 2 {
		t.Fatalf("expected kept hook + new CSA hook, got %d: %s", len(ups), blob)
	}
}

func TestUninstallClaudeHookAt(t *testing.T) {
	settings := filepath.Join(t.TempDir(), "settings.json")
	os.WriteFile(settings, []byte(`{
  "hooks": {
    "PostToolUse": [{"matcher":"Write","hooks":[{"type":"command","command":"/bin/true"}]}],
    "UserPromptSubmit": [
      {"matcher":"","hooks":[{"type":"command","command":"/old/.csa/bin/csa-client","args":["hook"]}]},
      {"matcher":"","hooks":[{"type":"command","command":"/bin/echo","args":["keep"]}]}
    ]
  }
}`), 0o644)
	if err := uninstallClaudeHookAt(settings); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(settings)
	blob := string(b)
	if strings.Contains(blob, "csa-client") {
		t.Fatalf("CSA hook was not removed:\n%s", blob)
	}
	if !strings.Contains(blob, "/bin/echo") || !strings.Contains(blob, "PostToolUse") {
		t.Fatalf("unrelated hooks must be preserved:\n%s", blob)
	}
	var root map[string]any
	if err := json.Unmarshal(b, &root); err != nil {
		t.Fatal(err)
	}
	ups := root["hooks"].(map[string]any)["UserPromptSubmit"].([]any)
	if len(ups) != 1 {
		t.Fatalf("expected only the kept hook, got %d:\n%s", len(ups), blob)
	}
	// idempotent: removing again is a no-op, not an error
	if err := uninstallClaudeHookAt(settings); err != nil {
		t.Fatalf("second uninstall should be a no-op: %v", err)
	}
	// no-op on a path that doesn't exist
	if err := uninstallClaudeHookAt(filepath.Join(t.TempDir(), "absent.json")); err != nil {
		t.Fatalf("uninstall on missing settings should be a no-op: %v", err)
	}
}

func TestRunHookInjectsContext(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("CSA_HOOK_TIMEOUT", "1s")

	var sawQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ask" {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		sawQuery, _ = body["query"].(string)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true,"found":true,"system_message":"CSA recalled 1 past-session fact","context":"# Recalled context for \"mcp\"\n\n## Decisions\n- use platform _(mara · id:abc)_"}`))
	}))
	defer srv.Close()

	cfg := &config{Server: srv.URL, Identity: "mara@example.com",
		Consent: &consentRecord{PolicyVersion: requiredPolicyVersion}}
	if err := saveConfig(configPath(), cfg); err != nil {
		t.Fatal(err)
	}

	oldIn, oldOut := os.Stdin, os.Stdout
	defer func() { os.Stdin, os.Stdout = oldIn, oldOut }()
	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	os.Stdin, os.Stdout = inR, outW
	inW.WriteString(`{"session_id":"s1","prompt":"Why Tomasz decided about MCP? token ghp_` + strings.Repeat("x", 20) + `"}`)
	inW.Close()
	runHook(nil)
	outW.Close()
	b, _ := io.ReadAll(outR)
	got := string(b)
	if !strings.Contains(got, "hookSpecificOutput") || !strings.Contains(got, "CSA ambient team memory") {
		t.Fatalf("hook did not emit additionalContext: %q", got)
	}
	if !strings.Contains(got, "systemMessage") || !strings.Contains(got, "recalled 1 past-session fact") {
		t.Fatalf("hook did not surface a user-visible systemMessage: %q", got)
	}
	if strings.Contains(sawQuery, "ghp_") || !strings.Contains(sawQuery, "[REDACTED:github_token]") {
		t.Fatalf("hook query was not redacted: %q", sawQuery)
	}
}

func TestDue(t *testing.T) {
	now := time.Unix(1000, 0)
	all := map[string]session{
		"new": {"/x/new.jsonl", now.Add(-20 * time.Second)}, // quiet 20s -> due
		"hot": {"/x/hot.jsonl", now.Add(-1 * time.Second)},  // written 1s ago -> debounced
	}
	sent := map[string]time.Time{}
	none := map[string]time.Time{} // no recent sends -> cooldown never bites
	cool := 10 * time.Minute
	if got := due(now, all, sent, none, 10*time.Second, cool); len(got) != 1 || got[0] != "new" {
		t.Fatalf("quiet file due, active file debounced; got %v", got)
	}
	sent["new"] = all["new"].mtime
	if got := due(now, all, sent, none, 10*time.Second, cool); len(got) != 0 {
		t.Fatalf("same (session, mtime) must not resend; got %v", got)
	}
	all["new"] = session{"/x/new.jsonl", now.Add(-15 * time.Second)} // grew -> due again
	if got := due(now, all, sent, none, 10*time.Second, cool); len(got) != 1 || got[0] != "new" {
		t.Fatalf("a grown file must resend; got %v", got)
	}
}

func TestDueCooldown(t *testing.T) {
	now := time.Unix(100000, 0)
	cool := 10 * time.Minute
	all := map[string]session{"s": {"/x/s.jsonl", now.Add(-30 * time.Second)}} // grown + settled
	sent := map[string]time.Time{"s": now.Add(-5 * time.Minute)}               // older mtime on record
	last := map[string]time.Time{"s": now.Add(-5 * time.Minute)}               // sent 5m ago
	if got := due(now, all, sent, last, 10*time.Second, cool); len(got) != 0 {
		t.Fatalf("grown+settled but inside cooldown must wait; got %v", got)
	}
	last["s"] = now.Add(-11 * time.Minute) // cooldown expired -> ships on first scan
	if got := due(now, all, sent, last, 10*time.Second, cool); len(got) != 1 || got[0] != "s" {
		t.Fatalf("expired cooldown must ship; got %v", got)
	}
	delete(last, "s") // never sent (zero time) -> cooldown must not throttle a first send
	if got := due(now, all, sent, last, 10*time.Second, cool); len(got) != 1 || got[0] != "s" {
		t.Fatalf("never-sent session must not be throttled; got %v", got)
	}
}

// Ignore matching in the scan path: project-dir globs, non-matches, comments/blanks,
// and the missing-file default.
func TestIgnore(t *testing.T) {
	dir := t.TempDir()
	ig := filepath.Join(dir, "ignore")
	os.WriteFile(ig, []byte("# private repos\n\n*secret-repo*\n   \n"), 0o644)
	pats := loadIgnores(ig)
	if len(pats) != 1 || pats[0] != "*secret-repo*" {
		t.Fatalf("comments/blank lines must be skipped: %v", pats)
	}
	if got := loadIgnores(filepath.Join(dir, "missing")); got != nil {
		t.Fatalf("missing ignore file must ignore nothing: %v", got)
	}
	// the munged project-dir name is what users realistically target
	p := "/Users/x/.claude/projects/-Users-x-Git-secret-repo/abc123.jsonl"
	if got := ignoredBy(p, pats); got != "*secret-repo*" {
		t.Fatalf("project-dir glob must match, got %q", got)
	}
	if got := ignoredBy("/Users/x/.claude/projects/-Users-x-Git-public/abc.jsonl", pats); got != "" {
		t.Fatalf("non-matching session must not be ignored, got %q", got)
	}
	if got := ignoredBy(p, nil); got != "" {
		t.Fatalf("no patterns must ignore nothing, got %q", got)
	}
	// a full-path glob counts too
	if got := ignoredBy("/tmp/x/s.jsonl", []string{"/tmp/x/*.jsonl"}); got == "" {
		t.Fatal("full-path glob must match")
	}
}

// The allowlist path: an empty ~/.csa/only indexes everything; a non-empty one indexes ONLY
// matching folders; an ignore glob still wins over an allowlist match.
func TestExcludedReason(t *testing.T) {
	secret := "/Users/x/.claude/projects/-Users-x-Git-secret-repo/a.jsonl"
	work := "/Users/x/.claude/projects/-Users-x-Git-work-repo/b.jsonl"

	// No filters at all -> ship everything.
	if r := excludedReason(work, nil, nil); r != "" {
		t.Fatalf("no filters must ship, got %q", r)
	}
	// Allowlist set -> a non-match is excluded, a match ships.
	only := []string{"*work-repo*"}
	if r := excludedReason(secret, nil, only); r == "" {
		t.Fatal("session outside the allowlist must be excluded")
	}
	if r := excludedReason(work, nil, only); r != "" {
		t.Fatalf("session in the allowlist must ship, got %q", r)
	}
	// Ignore wins even when the allowlist would admit it.
	if r := excludedReason(work, []string{"*work-repo*"}, only); r == "" {
		t.Fatal("an ignore glob must win over an allowlist match")
	}
}

// A subagent transcript sits in <project>/<parent-uuid>/subagents/[workflows/<wf>/], so it takes
// its project's rule, and a bare 'subagents' allowlist entry no longer admits every project's.
func TestExcludedReasonSubagents(t *testing.T) {
	home := "/Users/x/.claude/projects/-Users-x/1111-2222/subagents/agent-a1.jsonl"
	work := "/Users/x/.claude/projects/-Users-x-Git-work-repo/3333-4444/subagents/agent-b2.jsonl"
	wf := "/Users/x/.claude/projects/-Users-x-Git-work-repo/3333-4444/subagents/workflows/wf_1/agent-c3.jsonl"
	for _, p := range []string{work, wf} {
		if got := projectDir(p); got != "-Users-x-Git-work-repo" {
			t.Fatalf("projectDir(%s) = %q, want the project folder", p, got)
		}
	}
	for p, want := range map[string]string{
		"/storage/C1/session_1.jsonl":                "C1",
		"/srv/subagents/sessions/C1/session_1.jsonl": "C1",
	} {
		if got := projectDir(p); got != want {
			t.Fatalf("projectDir(%s) = %q, want %q", p, got, want)
		}
	}
	if r := excludedReason(home, []string{"-Users-x"}, nil); r == "" {
		t.Fatal("an ignored project's subagents must be excluded")
	}
	if r := excludedReason(home, []string{"subagents"}, nil); r == "" {
		t.Fatal("an ignore 'subagents' entry must still keep every subagent transcript home")
	}
	if r := excludedReason(home, nil, []string{"subagents"}); r == "" {
		t.Fatal("a 'subagents' allowlist entry must not admit an unlisted project's subagents")
	}
	if r := excludedReason(work, nil, []string{"-Users-x-Git-work-repo"}); r != "" {
		t.Fatalf("an allowlisted project's subagents must ship, got %q", r)
	}
	if r := excludedReason(home, nil, []string{"-Users-x-Git-work-repo"}); r == "" {
		t.Fatal("subagents of a project outside the allowlist must be excluded")
	}
}

// The consent gate: nil config, missing consent, or a stale policy version must all refuse.
func TestConsentGate(t *testing.T) {
	var nilCfg *config
	if nilCfg.consentOK() {
		t.Fatal("nil config must not pass the consent gate")
	}
	if (&config{Server: "http://x"}).consentOK() {
		t.Fatal("config without a consent record must not pass")
	}
	stale := &config{Consent: &consentRecord{PolicyVersion: requiredPolicyVersion - 1}}
	if stale.consentOK() {
		t.Fatal("consent for an older policy version must not pass")
	}
	ok := &config{Consent: &consentRecord{PolicyVersion: requiredPolicyVersion}}
	if !ok.consentOK() {
		t.Fatal("current consent must pass")
	}
	// identity drift: only two DIFFERENT non-empty emails count; username fallbacks don't
	if !identityDrift("a@x.com", "b@x.com") {
		t.Fatal("different emails must be drift")
	}
	for _, c := range [][2]string{{"a@x.com", "a@x.com"}, {"mara", "a@x.com"}, {"a@x.com", ""}, {"", ""}} {
		if identityDrift(c[0], c[1]) {
			t.Fatalf("%q vs %q must not be drift", c[0], c[1])
		}
	}
}

func TestConfigRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "csa", "config.json")
	in := &config{
		Server: "http://csa.team:58767", Identity: "mara@example.com",
		CFID: "id123", CFSecret: "sec456",
		Consent: &consentRecord{
			PolicyVersion: requiredPolicyVersion, AcceptedAt: "2026-07-04T00:00:00Z",
			Method: "interactive-tty", IdentityAtConsent: "mara@example.com",
		},
	}
	if err := saveConfig(path, in); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("config holds credentials and must be 0600, got %v", fi.Mode().Perm())
	}
	if di, err := os.Stat(filepath.Dir(path)); err != nil || di.Mode().Perm() != 0o700 {
		t.Fatalf("config dir must be 0700, got %v (%v)", di.Mode().Perm(), err)
	}
	out, err := loadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if out.Server != in.Server || out.Identity != in.Identity ||
		out.CFID != in.CFID || out.CFSecret != in.CFSecret || *out.Consent != *in.Consent {
		t.Fatalf("round-trip mismatch:\n got %+v %+v\nwant %+v %+v", out, out.Consent, in, in.Consent)
	}
}

// A non-2xx reply with a decodable JSON body (proxy 502, stray-path 404) is a transport
// failure -> send returns false so the session retries; a 200 ok=true is a real send.
func TestSendStatusCode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "s.jsonl")
	os.WriteFile(path, []byte(`{"message":{"role":"user","content":"hi"}}`), 0o644)

	status := http.StatusNotFound
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		w.Write([]byte(`{"ok":true,"error":"not found"}`))
	}))
	defer srv.Close()

	if ok, down := send(srv.URL, "s", path); ok || !down {
		t.Fatal("non-2xx must return ok=false, down=true (retry), even with a JSON body")
	}
	status = http.StatusOK
	if ok, _ := send(srv.URL, "s", path); !ok {
		t.Fatal("200 ok=true must return true")
	}
}

// forget: the server returns HTTP 200 even for a failed delete, carrying {"ok":false,...}
// (apiserver.py forget()). So a 200 ok=false must print the FAIL mark, and only a 2xx ok=true
// the OK mark. Also guards non-2xx and unparseable bodies. Mirrors runForget's mark choice.
func TestForgetOK(t *testing.T) {
	old := styled
	defer func() { styled = old }()
	styled = false // deterministic plain marks

	cases := []struct {
		name     string
		status   int
		body     string
		wantOK   bool
		wantMark string
	}{
		{"200 ok:false is a failure", http.StatusOK, `{"ok":false,"error":"unknown session x"}`, false, "✗"},
		{"200 ok:true is a success", http.StatusOK, `{"ok":true,"deleted":3}`, true, "✓"},
		{"non-2xx is a failure even with ok:true", http.StatusBadGateway, `{"ok":true}`, false, "✗"},
		{"unparseable 2xx body is a failure", http.StatusOK, `not json`, false, "✗"},
	}
	for _, c := range cases {
		got := forgetOK(c.status, []byte(c.body))
		if got != c.wantOK {
			t.Fatalf("%s: forgetOK(%d, %q) = %v, want %v", c.name, c.status, c.body, got, c.wantOK)
		}
		mark := failMark()
		if got {
			mark = okMark()
		}
		if mark != c.wantMark {
			t.Fatalf("%s: mark = %q, want %q", c.name, mark, c.wantMark)
		}
	}
}

// `forget --project <glob>` selection: the glob picks the right LOCAL session ids (sorted) and
// nothing else; a non-matching glob picks none. Same project-folder/full-path matching as ignore.
func TestSessionIDsUnder(t *testing.T) {
	root := t.TempDir()
	mk := func(proj, id string) {
		d := filepath.Join(root, proj)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, id+".jsonl"), []byte("{}"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mk("-Users-x-Git-secret-repo", "bbb")
	mk("-Users-x-Git-secret-repo", "aaa")
	mk("-Users-x-Git-work-repo", "ccc")

	got := sessionIDsUnder(root, "*secret-repo*")
	if len(got) != 2 || got[0] != "aaa" || got[1] != "bbb" {
		t.Fatalf("glob must select both secret-repo ids sorted, got %v", got)
	}
	if got := sessionIDsUnder(root, "*nope*"); len(got) != 0 {
		t.Fatalf("non-matching glob must select nothing, got %v", got)
	}
}

// bulkConfirmed guards the only irreversible path: a bulk delete proceeds ONLY on an explicit
// yes. Empty (plain Enter / EOF from non-interactive stdin) and anything else must be NO.
func TestBulkConfirmed(t *testing.T) {
	for _, yes := range []string{"y", "Y", "yes", "YES", " yes \n", "y\n"} {
		if !bulkConfirmed(yes) {
			t.Fatalf("%q must confirm", yes)
		}
	}
	for _, no := range []string{"", "\n", "  ", "n", "no", "yep", "delete", "yesplease"} {
		if bulkConfirmed(no) {
			t.Fatalf("%q must NOT confirm (fail-safe: no delete)", no)
		}
	}
}

// Style must degrade to plain strings: NO_COLOR set, or stdout not a terminal, means
// zero escape codes anywhere in the output.
func TestStyleDegradation(t *testing.T) {
	f, err := os.Create(filepath.Join(t.TempDir(), "not-a-tty"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if styleOK("1", os.Stdout) {
		t.Fatal("NO_COLOR set must disable styling even on a terminal")
	}
	if styleOK("", f) {
		t.Fatal("a regular file is not a terminal — styling must be off")
	}

	old := styled
	defer func() { styled = old }()
	styled = false
	for _, c := range []struct{ got, want string }{
		{bold("x"), "x"}, {dim("x"), "x"}, {cyan("http://s"), "http://s"},
		{okMark(), "✓"}, {warnMark(), "!"}, {failMark(), "✗"},
	} {
		if c.got != c.want {
			t.Fatalf("degraded style must be the plain string %q, got %q", c.want, c.got)
		}
	}
	styled = true
	if got := cyan("u"); got != "\x1b[36mu\x1b[0m" {
		t.Fatalf("styled cyan must wrap in escape codes, got %q", got)
	}
}

// mcpEndpoint: absolute mcp_url wins; else scheme+host from the server URL with the
// advertised port+path; "" when the server advertises nothing.
func TestMCPEndpoint(t *testing.T) {
	if got := mcpEndpoint("http://csa.team:58767", &healthReply{MCPURL: "https://x/mcp"}); got != "https://x/mcp" {
		t.Fatalf("mcp_url override must win, got %q", got)
	}
	if got := mcpEndpoint("http://csa.team:58767", &healthReply{MCPPort: 58768, MCPPath: "/mcp"}); got != "http://csa.team:58768/mcp" {
		t.Fatalf("port+path form wrong: %q", got)
	}
	if got := mcpEndpoint("http://csa.team:58767", &healthReply{}); got != "" {
		t.Fatalf("no advertisement must yield \"\", got %q", got)
	}
}

func TestRelDay(t *testing.T) {
	now := time.Now().UTC()
	cases := map[string]string{
		now.Format(time.RFC3339):                          "today",
		now.Add(-25 * time.Hour).Format(time.RFC3339):     "yesterday",
		now.Add(-3 * 24 * time.Hour).Format(time.RFC3339): "3d ago",
		"2020-01-02T03:04:05Z":                            "2020-01-02", // >week -> plain date
		"2020-01-02":                                      "2020-01-02", // unparseable -> first 10 chars
		"":                                                "",
	}
	for in, want := range cases {
		if got := relDay(in); got != want {
			t.Fatalf("relDay(%q) = %q, want %q", in, got, want)
		}
	}
	if shortName("alice@team.com") != "alice" || shortName("bob") != "bob" {
		t.Fatal("shortName should take the email local-part, else the value as-is")
	}
	if plural(1) != "" || plural(0) != "s" || plural(2) != "s" {
		t.Fatal("plural: only 1 is singular")
	}
}

func TestFetchTeam(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/team" {
			w.WriteHeader(404)
			return
		}
		w.Write([]byte(`[{"user":"mara@example.com","sessions":42,"last_active":"2026-07-12T09:00:00Z"}]`))
	}))
	defer srv.Close()
	team, err := fetchTeam(srv.URL)
	if err != nil {
		t.Fatalf("fetchTeam: %v", err)
	}
	if len(team) != 1 || team[0].User != "mara@example.com" || team[0].Sessions != 42 {
		t.Fatalf("decoded wrong: %+v", team)
	}
	if _, err := fetchTeam("http://127.0.0.1:0"); err == nil {
		t.Fatal("unreachable server must error")
	}
}

func TestTeamCacheFallback(t *testing.T) {
	t.Setenv("HOME", t.TempDir()) // isolate ~/.csa/team.json from the real home
	want := []teamMember{{User: "alice@team.com", Sessions: 5, LastActive: "2026-07-10T00:00:00Z"}}
	saveTeamCache(want)
	if got, at, ok := loadTeamCache(); !ok || len(got) != 1 || got[0].Sessions != 5 || at.IsZero() {
		t.Fatalf("cache round-trip failed: ok=%v at=%v got=%+v", ok, at, got)
	}
	// The whole point: server unreachable -> serve the cache, marked not-live.
	team, live, at, err := teamRoster("http://127.0.0.1:0")
	if err == nil {
		t.Fatal("expected a live-fetch error when unreachable")
	}
	if live || len(team) != 1 || at.IsZero() {
		t.Fatalf("offline must fall back to cache: live=%v team=%+v", live, team)
	}
	if offlineNote(true, at) != "" || !strings.HasPrefix(offlineNote(false, at), "offline") {
		t.Fatal("offlineNote: banner only when not live")
	}
}

func TestBackfillQueue(t *testing.T) {
	now := time.Now()
	all := map[string]session{
		"old":     {path: "/p/-proj-a/old.jsonl", mtime: now.Add(-3 * time.Hour)},
		"mid":     {path: "/p/-proj-a/mid.jsonl", mtime: now.Add(-2 * time.Hour)},
		"new":     {path: "/p/-proj-a/new.jsonl", mtime: now.Add(-1 * time.Hour)},
		"done":    {path: "/p/-proj-a/done.jsonl", mtime: now.Add(-4 * time.Hour)},
		"private": {path: "/p/-proj-secret/private.jsonl", mtime: now.Add(-5 * time.Hour)},
	}
	q := backfillQueue(all, map[string]bool{"done": true}, []string{"*secret*"}, nil)
	if len(q) != 3 || q[0] != "old" || q[1] != "mid" || q[2] != "new" {
		t.Fatalf("want [old mid new] (oldest first, done+ignored skipped), got %v", q)
	}
	// Allowlist: only -proj-secret would qualify, but ignore still wins.
	if q := backfillQueue(all, nil, []string{"*secret*"}, []string{"*proj-secret*"}); len(q) != 0 {
		t.Fatalf("ignore must win over allowlist, got %v", q)
	}
}

func TestBackfillLedger(t *testing.T) {
	path := filepath.Join(t.TempDir(), "backfilled")
	if got := loadBackfilled(path); len(got) != 0 {
		t.Fatalf("missing ledger must mean empty, got %v", got)
	}
	markBackfilled(path, "aaa")
	markBackfilled(path, "bbb")
	got := loadBackfilled(path)
	if !got["aaa"] || !got["bbb"] || len(got) != 2 {
		t.Fatalf("ledger round-trip failed: %v", got)
	}
}

func TestDrainVerdictFor(t *testing.T) {
	now := time.Now()
	quiet := 10 * time.Second
	settled := session{path: "/p/-x/a.jsonl", mtime: now.Add(-time.Hour)}
	// Untouched since startup + settled -> ship.
	if v := drainVerdictFor(now, settled, settled.mtime, time.Time{}, quiet); v != drainSend {
		t.Fatalf("settled untouched session must drainSend, got %v", v)
	}
	// Already shipped by the live path this run -> mark done, never re-analyze.
	if v := drainVerdictFor(now, settled, settled.mtime, now.Add(-time.Minute), quiet); v != drainSkip {
		t.Fatalf("live-shipped session must drainSkip, got %v", v)
	}
	// Grew since startup -> live path owns the whole transcript; backfill must not double-ship.
	grown := session{path: settled.path, mtime: now.Add(-time.Minute)}
	if v := drainVerdictFor(now, grown, settled.mtime, time.Time{}, quiet); v != drainSkip {
		t.Fatalf("grown-since-seed session must drainSkip, got %v", v)
	}
	// Written seconds before startup, unchanged since -> wait out the quiet window (mid-write guard).
	young := session{path: settled.path, mtime: now.Add(-2 * time.Second)}
	if v := drainVerdictFor(now, young, young.mtime, time.Time{}, quiet); v != drainWait {
		t.Fatalf("young unchanged session must drainWait, got %v", v)
	}
}

func TestOwnerFor(t *testing.T) {
	root := t.TempDir()
	proj := filepath.Join(root, "-var-acme-ai")
	sub := filepath.Join(proj, "1111-2222", "subagents")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	writeF := func(p, content string) {
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	writeF(filepath.Join(proj, "aaaa.jsonl"), "{}")
	writeF(filepath.Join(proj, "aaaa.owner"), "tomasz@example.com\n")
	writeF(filepath.Join(proj, "bbbb.jsonl"), "{}")
	writeF(filepath.Join(sub, "agent-x.jsonl"), "{}")
	writeF(filepath.Join(proj, "1111-2222.owner"), "mara@example.com\n")
	writeF(filepath.Join(proj, "cccc.jsonl"), "{}")
	writeF(filepath.Join(proj, "cccc.owner"), "TODO ping @alice about this\n")

	if o := ownerFor(root, filepath.Join(proj, "aaaa.jsonl")); o != "tomasz@example.com" {
		t.Fatalf("direct sidecar: want tomasz@example.com, got %q", o)
	}
	if o := ownerFor(root, filepath.Join(sub, "agent-x.jsonl")); o != "mara@example.com" {
		t.Fatalf("subagent must inherit parent-uuid sidecar, got %q", o)
	}
	if o := ownerFor(root, filepath.Join(proj, "bbbb.jsonl")); o != "" {
		t.Fatalf("no sidecar must mean no override, got %q", o)
	}
	if o := ownerFor(root, filepath.Join(proj, "cccc.jsonl")); o != "" {
		t.Fatalf("non-email sidecar must be ignored, got %q", o)
	}
	// A path outside the root must never pick up sidecars via the ancestor walk.
	if o := ownerFor(filepath.Join(root, "elsewhere"), filepath.Join(sub, "agent-x.jsonl")); o != "" {
		t.Fatalf("outside-root walk must find nothing, got %q", o)
	}
}

func TestIgnoreFileBasename(t *testing.T) {
	// Non-standard roots (a bot's mixed storage dir) select sessions by FILE basename:
	// only 'session_*' must admit pi session files and reject sibling churn like thread logs.
	only := []string{"session_*"}
	sess := "/storage/C057291BVH8/session_1783924625_186139.jsonl"
	if excludedReason(sess, nil, only) != "" {
		t.Fatalf("session_* file must pass the allowlist")
	}
	for _, noise := range []string{
		"/storage/C057291BVH8/thread_1783924625_186139.jsonl",
		"/storage/C057291BVH8/channel.jsonl",
		"/storage/transcripts/2026-07-13/20260713T081500Z-aa.jsonl",
	} {
		if excludedReason(noise, nil, only) == "" {
			t.Fatalf("%s must be excluded by the allowlist", noise)
		}
	}
	// And ignore (blocklist) matches file basenames too.
	if ignoredBy("/storage/C1/thread_123.jsonl", []string{"thread_*"}) == "" {
		t.Fatal("thread_* must match the file basename")
	}
}

// addAuthHeaders: bearer authenticates the origin, CF service-token headers the edge — the two
// are independent, either or both may be set, and the token-only path (the external trial) must
// emit exactly Authorization: Bearer <token> and no CF headers.
func TestAuthHeaders(t *testing.T) {
	defer func() { authToken, cfID, cfSecret = "", "", "" }()

	authToken, cfID, cfSecret = "sekret", "", ""
	req, _ := http.NewRequest("GET", "http://x/api/health", nil)
	addAuthHeaders(req)
	if got := req.Header.Get("Authorization"); got != "Bearer sekret" {
		t.Fatalf("token mode: Authorization = %q, want %q", got, "Bearer sekret")
	}
	if req.Header.Get("CF-Access-Client-Id") != "" {
		t.Fatal("no CF creds configured — CF header must be absent")
	}

	authToken, cfID, cfSecret = "", "cfid", "cfsec"
	req, _ = http.NewRequest("GET", "http://x", nil)
	addAuthHeaders(req)
	if req.Header.Get("Authorization") != "" {
		t.Fatal("no token configured — Authorization must be absent")
	}
	if req.Header.Get("CF-Access-Client-Id") != "cfid" || req.Header.Get("CF-Access-Client-Secret") != "cfsec" {
		t.Fatal("CF service-token headers must be set when CF creds are configured")
	}

	authToken, cfID, cfSecret = "tok", "cfid", "cfsec"
	req, _ = http.NewRequest("GET", "http://x", nil)
	addAuthHeaders(req)
	if req.Header.Get("Authorization") != "Bearer tok" || req.Header.Get("CF-Access-Client-Id") != "cfid" {
		t.Fatal("token and CF creds are independent — both headers must be present when both set")
	}
}

// resolveAuthToken precedence: $CSA_AUTH_TOKEN (dev/CI) overrides the configured token; a
// blank/whitespace env value is treated as unset and falls through to config.
func TestResolveAuthToken(t *testing.T) {
	t.Setenv("CSA_AUTH_TOKEN", "")
	if got := resolveAuthToken("cfgtok"); got != "cfgtok" {
		t.Fatalf("empty env must fall through to config, got %q", got)
	}
	t.Setenv("CSA_AUTH_TOKEN", "  ")
	if got := resolveAuthToken("cfgtok"); got != "cfgtok" {
		t.Fatalf("whitespace env must be treated as unset, got %q", got)
	}
	t.Setenv("CSA_AUTH_TOKEN", "envtok")
	if got := resolveAuthToken("cfgtok"); got != "envtok" {
		t.Fatalf("env must override config, got %q", got)
	}
	if got := resolveAuthToken(""); got != "envtok" {
		t.Fatalf("env with no config token, got %q", got)
	}
}

// send must carry the bearer so a token-auth server (the external-trial mode) accepts the
// ingest; a request without it would 401. Also confirms the auth_token round-trips through
// config and that send reaches an authenticated endpoint.
func TestSendCarriesBearer(t *testing.T) {
	defer func() { authToken = "" }()
	authToken = "trial-secret"

	path := filepath.Join(t.TempDir(), "s.jsonl")
	os.WriteFile(path, []byte(`{"message":{"role":"user","content":"hi"}}`), 0o644)

	gotAuth := ""
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		if gotAuth != "Bearer trial-secret" { // a real token server would 401 here
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte(`{"ok":false,"code":"unauthorized"}`))
			return
		}
		w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	if ok, _ := send(srv.URL, "s", path); !ok {
		t.Fatal("send must succeed when the bearer authenticates it")
	}
	if gotAuth != "Bearer trial-secret" {
		t.Fatalf("ingest must carry Authorization: Bearer <token>, got %q", gotAuth)
	}
}

// writeOnly REPLACES ~/.csa/only atomically; an empty list REMOVES the file (index everything),
// and removing an absent file is a no-op, not an error.
func TestWriteOnly(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	if err := writeOnly([]string{"-Users-x-Git-a", "-Users-x-Git-b"}); err != nil {
		t.Fatal(err)
	}
	if pats := loadIgnores(onlyPath()); len(pats) != 2 || pats[0] != "-Users-x-Git-a" {
		t.Fatalf("write = %v, want the two names", pats)
	}
	fi, err := os.Stat(onlyPath())
	if err != nil || fi.Mode().Perm() != 0o644 {
		t.Fatalf("only file mode = %v (err %v), want 0644", fi.Mode().Perm(), err)
	}
	// Empty list removes the file -> empty allowlist -> index every folder.
	if err := writeOnly(nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(onlyPath()); !os.IsNotExist(err) {
		t.Fatalf("empty writeOnly must remove the file, stat err = %v", err)
	}
	// Removing an already-absent file is a no-op.
	if err := writeOnly(nil); err != nil {
		t.Fatalf("writeOnly(nil) on missing file must not error: %v", err)
	}
}

// applyOnlyAllowlist writes ~/.csa/only (REPLACE, idempotent) from a comma-separated glob spec,
// and excludedReason then honors it — the noninteractive equivalent of `csa-client only`.
func TestApplyOnlyAllowlist(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	applyOnlyAllowlist(" *repo-a* , *repo-b* ")
	pats := loadIgnores(onlyPath())
	if len(pats) != 2 || pats[0] != "*repo-a*" || pats[1] != "*repo-b*" {
		t.Fatalf("allowlist = %v, want [*repo-a* *repo-b*]", pats)
	}
	// A folder outside the allowlist is excluded; one inside passes.
	if excludedReason("/Users/you/.claude/projects/-Users-you-repo-c/x.jsonl", nil, pats) == "" {
		t.Fatal("a folder outside the allowlist must be excluded")
	}
	if excludedReason("/Users/you/.claude/projects/-Users-you-repo-a/x.jsonl", nil, pats) != "" {
		t.Fatal("a folder in the allowlist must pass")
	}
	// Re-running REPLACES rather than appends — idempotent scripted re-init.
	applyOnlyAllowlist("*repo-a*")
	if pats := loadIgnores(onlyPath()); len(pats) != 1 || pats[0] != "*repo-a*" {
		t.Fatalf("re-apply must replace, got %v", pats)
	}
}

// applyOnlyAllowlist is the noninteractive path (`init --only=...`) — nobody's watching its
// output the way they would `csa-client only`'s, so it must run the same unmatched-pattern
// check runOnly does rather than silently writing an allowlist that excludes everything. This
// only proves the happy path still works once real sessions exist to check against; the refusal
// itself calls fatalf/os.Exit and isn't exercised in-process, matching this file's convention for
// fatalf-wrapped CLI commands.
func TestApplyOnlyAllowlistChecksAgainstRealSessions(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dir := filepath.Join(home, ".claude", "projects", "-Users-you-Desktop-repo-a")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "abc123.jsonl"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	applyOnlyAllowlist("*repo-a*")
	if pats := loadIgnores(onlyPath()); len(pats) != 1 || pats[0] != "*repo-a*" {
		t.Fatalf("a glob matching a real session must still write, got %v", pats)
	}
}

// unmatchedOnlyPatterns catches the trap that let a mistyped `only` allowlist silently ship
// nothing: a real filesystem path (e.g. /Users/you/Desktop/repo) is a syntactically valid glob
// but can never match the munged folder name (-Users-you-Desktop-repo) only/ignore actually
// check against, so runOnly must refuse it instead of writing an allowlist that excludes
// everything.
func TestUnmatchedOnlyPatterns(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dir := filepath.Join(home, ".claude", "projects", "-Users-you-Desktop-repo")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "abc123.jsonl"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	// A real path never matches the munged folder name -> refused.
	if bad := unmatchedOnlyPatterns([]string{"/Users/you/Desktop/repo"}); len(bad) != 1 {
		t.Fatalf("unmatchedOnlyPatterns(real path) = %v, want the path flagged", bad)
	}
	// A glob against the munged name matches -> not flagged.
	if bad := unmatchedOnlyPatterns([]string{"*repo*"}); len(bad) != 0 {
		t.Fatalf("unmatchedOnlyPatterns(matching glob) = %v, want none flagged", bad)
	}
	// Mixed input: only the unmatchable one comes back.
	bad := unmatchedOnlyPatterns([]string{"*repo*", "/Users/you/Desktop/repo"})
	if len(bad) != 1 || bad[0] != "/Users/you/Desktop/repo" {
		t.Fatalf("unmatchedOnlyPatterns(mixed) = %v, want only the real path flagged", bad)
	}

	// No sessions to check against yet -> skip the check rather than flag everything.
	t.Setenv("HOME", t.TempDir())
	if bad := unmatchedOnlyPatterns([]string{"/Users/you/Desktop/repo"}); bad != nil {
		t.Fatalf("unmatchedOnlyPatterns with no sessions yet = %v, want nil (check skipped)", bad)
	}
}

// parseFolderSelection turns a picker response into 1-based indices (deduped, in listed order)
// or the "index everything" signal, tolerating garbage/out-of-range tokens.
func TestParseFolderSelection(t *testing.T) {
	cases := []struct {
		resp    string
		n       int
		wantIdx []int
		wantAll bool
	}{
		{"", 5, nil, false},    // blank -> caller cancels
		{"   ", 5, nil, false}, // whitespace-only -> same
		{"all", 5, nil, true},  // index everything
		{"A", 5, nil, true},    // case-insensitive
		{"*", 5, nil, true},    // glob-y shorthand
		{"1 3 4", 5, []int{1, 3, 4}, false},
		{"1,3,4", 5, []int{1, 3, 4}, false},   // comma-separated
		{" 2 , 2  4 ", 5, []int{2, 4}, false}, // dedup, keep first-seen order
		{"0 6 9", 5, nil, false},              // all out of range -> nothing
		{"2 x 3", 5, []int{2, 3}, false},      // non-numeric token skipped
	}
	for _, c := range cases {
		idx, all := parseFolderSelection(c.resp, c.n)
		if all != c.wantAll || len(idx) != len(c.wantIdx) {
			t.Errorf("parseFolderSelection(%q,%d) = %v,%v; want %v,%v", c.resp, c.n, idx, all, c.wantIdx, c.wantAll)
			continue
		}
		for i := range idx {
			if idx[i] != c.wantIdx[i] {
				t.Errorf("parseFolderSelection(%q,%d) = %v; want %v", c.resp, c.n, idx, c.wantIdx)
				break
			}
		}
	}
}

// TestRetryAfter pins the server-down backoff: it doubles from the poll interval and stops at the
// cap. This is the whole fix for the runaway-CPU outage — if it ever
// returns the poll interval for a long outage, the client is back to burning a core for nothing.
func TestRetryAfter(t *testing.T) {
	base, max := 2500*time.Millisecond, 5*time.Minute
	for _, c := range []struct {
		n    int
		want time.Duration
	}{
		{1, 2500 * time.Millisecond},
		{2, 5 * time.Second},
		{3, 10 * time.Second},
		{7, 160 * time.Second},
		{8, max}, // 320s would exceed the cap
		{100, max},
	} {
		if got := retryAfter(c.n, base, max); got != c.want {
			t.Errorf("retryAfter(%d) = %s, want %s", c.n, got, c.want)
		}
	}
}

// TestSendDownFlag: a LOCAL read failure must not trip the server-down backoff (it would stall
// every other session over one unreadable file), but a dead server must.
func TestSendDownFlag(t *testing.T) {
	if ok, down := send("http://127.0.0.1:1/", "s", filepath.Join(t.TempDir(), "missing.jsonl")); ok || down {
		t.Fatal("unreadable local file must be ok=false, down=false")
	}
	f := filepath.Join(t.TempDir(), "s.jsonl")
	os.WriteFile(f, []byte("{}\n"), 0o600)
	if ok, down := send("http://127.0.0.1:1", "s", f); ok || !down {
		t.Fatal("connection refused must be ok=false, down=true")
	}
}

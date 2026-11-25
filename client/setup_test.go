package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// csaEntriesFor counts the hook entries in `event` whose command is csa-client, and the entries
// that are NOT ours (which must survive untouched).
func csaEntriesFor(t *testing.T, settings, event string) (mine, theirs int) {
	t.Helper()
	b, err := os.ReadFile(settings)
	if err != nil {
		t.Fatal(err)
	}
	var root map[string]any
	if err := json.Unmarshal(b, &root); err != nil {
		t.Fatalf("settings.json is not valid JSON after our write: %v\n%s", err, b)
	}
	hooks, _ := root["hooks"].(map[string]any)
	entries, _ := hooks[event].([]any)
	for _, e := range entries {
		em, ok := e.(map[string]any)
		if !ok {
			continue
		}
		hs, _ := em["hooks"].([]any)
		for _, h := range hs {
			hm, ok := h.(map[string]any)
			if !ok {
				continue
			}
			if isCSAClientHook(hm) {
				mine++
			} else {
				theirs++
			}
		}
	}
	return mine, theirs
}

// TestInstallHooksAtIsAdditiveAndIdempotent is the regression guard for the two ways this can go
// wrong on a real machine: clobbering someone else's hooks, and appending a duplicate of our own
// every time setup re-runs (which would double-ship every session and double the server's
// extraction spend).
func TestInstallHooksAtIsAdditiveAndIdempotent(t *testing.T) {
	settings := filepath.Join(t.TempDir(), "settings.json")
	// A third-party hook in the SAME events we touch — Poco-style coexistence.
	os.WriteFile(settings, []byte(`{
  "hooks": {
    "PostToolUse": [{"matcher":"Write","hooks":[{"type":"command","command":"/bin/true"}]}],
    "UserPromptSubmit": [{"matcher":"","hooks":[{"type":"command","command":"/opt/poco/bin/poco","args":["hook"]}]}],
    "SessionEnd": [{"hooks":[{"type":"command","command":"\"/opt/poco/bin/poco\" ship"}]}]
  },
  "model": "opus"
}`), 0o644)

	if err := installHooksAt(settings, "/home/u/.csa/bin/csa-client", csaHooks()); err != nil {
		t.Fatal(err)
	}
	for _, ev := range []string{"UserPromptSubmit", "SessionEnd", "SessionStart"} {
		mine, _ := csaEntriesFor(t, settings, ev)
		if mine != 1 {
			t.Errorf("%s: got %d CSA hooks, want exactly 1", ev, mine)
		}
	}
	// Re-run: still exactly one of ours per event.
	if err := installHooksAt(settings, "/home/u/.csa/bin/csa-client", csaHooks()); err != nil {
		t.Fatal(err)
	}
	for _, ev := range []string{"UserPromptSubmit", "SessionEnd", "SessionStart"} {
		mine, theirs := csaEntriesFor(t, settings, ev)
		if mine != 1 {
			t.Errorf("re-run %s: got %d CSA hooks, want exactly 1 (duplicate = double-ship)", ev, mine)
		}
		if ev != "SessionStart" && theirs != 1 {
			t.Errorf("re-run %s: third-party hook count = %d, want 1 (never clobber)", ev, theirs)
		}
	}
	b, _ := os.ReadFile(settings)
	blob := string(b)
	for _, must := range []string{"/opt/poco/bin/poco", "PostToolUse", `"model": "opus"`} {
		if !strings.Contains(blob, must) {
			t.Errorf("clobbered unrelated settings (%s missing):\n%s", must, blob)
		}
	}

	// The ship hooks must NOT inherit the 3s ambient timeout — /api/ingest runs `claude -p`.
	var root map[string]any
	json.Unmarshal(b, &root)
	hooks := root["hooks"].(map[string]any)
	for _, ev := range []string{"SessionEnd", "SessionStart"} {
		for _, e := range hooks[ev].([]any) {
			em := e.(map[string]any)
			for _, h := range em["hooks"].([]any) {
				hm := h.(map[string]any)
				if !isCSAClientHook(hm) {
					continue
				}
				if to, _ := hm["timeout"].(float64); int(to) != shipHookTimeout {
					t.Errorf("%s timeout = %v, want %d (a 3s ship hook is killed mid-POST)", ev, to, shipHookTimeout)
				}
			}
		}
	}
}

// TestUninstallRemovesEveryCSAHook: an uninstall that leaves SessionEnd behind keeps shipping the
// user's sessions after they asked us to stop. That is the failure this guards.
// TestSweepFloorStartsOverFromThePluginsConsent: sessions manual mode kept home were never consented to ship here.
func TestSweepFloorStartsOverFromThePluginsConsent(t *testing.T) {
	old := "2020-01-01T00:00:00Z"
	got := sweepFloor(false, &config{SweepFrom: old, Shipping: "plugin-manual", Consent: &consentRecord{PolicyVersion: requiredPolicyVersion}})
	if got == old {
		t.Fatal("kept the plugin's floor")
	}
	if got := sweepFloor(false, &config{SweepFrom: old, Consent: &consentRecord{PolicyVersion: requiredPolicyVersion}}); got != old {
		t.Fatalf("this client's own floor must stay: %q", got)
	}
}

func TestUninstallRemovesEveryCSAHook(t *testing.T) {
	settings := filepath.Join(t.TempDir(), "settings.json")
	os.WriteFile(settings, []byte(`{"hooks":{"PostToolUse":[{"matcher":"Write","hooks":[{"type":"command","command":"/bin/true"}]}]}}`), 0o644)
	if err := installHooksAt(settings, "/home/u/.csa/bin/csa-client", csaHooks()); err != nil {
		t.Fatal(err)
	}
	if err := uninstallClaudeHookAt(settings); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(settings)
	if strings.Contains(string(b), "csa-client") {
		t.Fatalf("uninstall left a CSA hook behind — sessions would keep shipping:\n%s", b)
	}
	if !strings.Contains(string(b), "/bin/true") {
		t.Fatalf("uninstall removed an unrelated hook:\n%s", b)
	}
	if err := uninstallClaudeHookAt(settings); err != nil {
		t.Fatalf("second uninstall must be a no-op: %v", err)
	}
}

func TestIsCSAClientHookMatchesBothForms(t *testing.T) {
	// The exec form carries args; the shell form embeds them in the command string. `args` marks
	// which we're looking at (mirrors what installHooksAt / the plugin actually write).
	execHook := func(cmd string) map[string]any { return map[string]any{"command": cmd, "args": []any{"hook"}} }
	shellHook := func(cmd string) map[string]any { return map[string]any{"command": cmd} }
	cases := []struct {
		name string
		hook map[string]any
		want bool
	}{
		{"exec form", execHook("/home/u/.csa/bin/csa-client"), true},
		{"one-liner install path", execHook("/home/u/.local/lib/csa/csa-client"), true},
		{"HOME with a space (exec form must not split on it)", execHook("/Users/john smith/.csa/bin/csa-client"), true},
		{"shell form quoted", shellHook(`"/home/u/.csa/bin/csa-client" ship`), true},
		{"shell form with flags", shellHook(`"/home/u/.csa/bin/csa-client" ship --sweep`), true},
		{"windows exe", execHook("/usr/local/bin/csa-client.exe"), true},
		{"competitor hook", execHook("/opt/poco/bin/poco"), false},
		{"competitor shell form", shellHook(`"/opt/poco/bin/poco" hook`), false},
		{"a binary that merely ENDS in csa-client is NOT ours", execHook("/opt/acme/acme-csa-client"), false},
		{"wrapper script is not ours", execHook("/usr/bin/my-csa-client-wrapper.sh"), false},
		{"empty", shellHook(""), false},
	}
	for _, c := range cases {
		if got := isCSAClientHook(c.hook); got != c.want {
			t.Errorf("%s: isCSAClientHook = %v, want %v", c.name, got, c.want)
		}
	}
}

// TestSweepExcludesCurrentSessionAndCaps guards two cost regressions: the sweep must NOT ship the
// session whose SessionStart just fired (its SessionEnd will), and one invocation must ship at most
// sweepChunk so a big --backfill history drains across restarts instead of one 240s burst.
func TestSweepExcludesCurrentSessionAndCaps(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	defer func() { ownerID, cfID, cfSecret, authToken, watchRoot, redactMode = "", "", "", "", "", "" }()

	var mu sync.Mutex
	var shipped []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ingest" { // the sweep also polls /api/client-config
			fmt.Fprint(w, `{"ok":true}`)
			return
		}
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		shipped = append(shipped, body["session_id"])
		mu.Unlock()
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer srv.Close()
	count := func() int { mu.Lock(); defer mu.Unlock(); return len(shipped) }
	has := func(sid string) bool {
		mu.Lock()
		defer mu.Unlock()
		for _, s := range shipped {
			if s == sid {
				return true
			}
		}
		return false
	}

	proj := filepath.Join(home, ".claude", "projects", "-p")
	os.MkdirAll(proj, 0o755)
	for i := 0; i < sweepChunk+5; i++ { // more sessions than one sweep may ship
		os.WriteFile(filepath.Join(proj, fmt.Sprintf("s%02d.jsonl", i)),
			[]byte(`{"message":{"role":"user","content":"work"}}`+"\n"), 0o644)
	}
	// backfill accepted (no SweepFrom floor) so all are eligible; "current" is the starting session.
	saveConfig(configPath(), &config{Server: srv.URL, Identity: "a@b.c", Backfill: true,
		Consent: &consentRecord{PolicyVersion: requiredPolicyVersion, AcceptedAt: time.Now().Format(time.RFC3339),
			Method: "flag:--yes", IdentityAtConsent: "a@b.c"}})

	// SessionStart delivers the starting session's id on stdin.
	runSweep(strings.NewReader(`{"hook_event_name":"SessionStart","session_id":"s00","transcript_path":"x"}`))
	if has("s00") {
		t.Fatal("sweep shipped the CURRENT (just-started) session — its SessionEnd will double-ship it")
	}
	if n := count(); n != sweepChunk {
		t.Fatalf("one sweep shipped %d sessions, want the cap of %d (unpaced burst otherwise)", n, sweepChunk)
	}
}

// TestSweepShipsCrashedSessionsOnce covers the whole point of the sweep and its one dangerous edge:
// a crashed session (no SessionEnd, so no marker) must ship exactly once, and history the user did
// NOT consent to must never ship at all.
func TestSweepShipsCrashedSessionsOnce(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	defer func() { ownerID, cfID, cfSecret, authToken, watchRoot, redactMode = "", "", "", "", "", "" }()

	var mu sync.Mutex
	var shipped []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ingest" { // the sweep also polls /api/client-config
			fmt.Fprint(w, `{"ok":true}`)
			return
		}
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		shipped = append(shipped, body["session_id"])
		mu.Unlock()
		fmt.Fprint(w, `{"ok":true,"hidden_context":1,"entities":1}`)
	}))
	defer srv.Close()
	got := func() []string { mu.Lock(); defer mu.Unlock(); return append([]string{}, shipped...) }

	proj := filepath.Join(home, ".claude", "projects", "-Users-test-demo")
	os.MkdirAll(proj, 0o755)
	writeAt := func(sid string, age time.Duration) {
		p := filepath.Join(proj, sid+".jsonl")
		os.WriteFile(p, []byte(`{"message":{"role":"user","content":"work"}}`+"\n"), 0o644)
		when := time.Now().Add(-age)
		os.Chtimes(p, when, when)
	}
	writeAt("old-history", 72*time.Hour) // predates setup: the user declined backfill
	writeAt("crashed", 0)                // this session died — SessionEnd never fired

	setupAt := time.Now().Add(-time.Hour)
	cfg := &config{Server: srv.URL, Identity: "test@example.com",
		SweepFrom: setupAt.Format(time.RFC3339),
		Consent: &consentRecord{PolicyVersion: requiredPolicyVersion,
			AcceptedAt: setupAt.Format(time.RFC3339), Method: "flag:--yes", IdentityAtConsent: "test@example.com"}}
	if err := saveConfig(configPath(), cfg); err != nil {
		t.Fatal(err)
	}

	runSweep(strings.NewReader(""))
	if s := got(); len(s) != 1 || s[0] != "crashed" {
		t.Fatalf("sweep shipped %v, want exactly [crashed] — history must NOT ship without backfill", s)
	}
	// Idempotent: the marker means a second sweep is silent.
	runSweep(strings.NewReader(""))
	if s := got(); len(s) != 1 {
		t.Fatalf("second sweep re-shipped: %v", s)
	}

	// With backfill accepted (no SweepFrom floor), history is fair game.
	cfg.SweepFrom = ""
	cfg.Backfill = true
	saveConfig(configPath(), cfg)
	runSweep(strings.NewReader(""))
	s := got()
	if len(s) != 2 || s[1] != "old-history" {
		t.Fatalf("sweep with backfill shipped %v, want history too", s)
	}
}

// TestSweepRefusesWithoutConsent: the sweep must be gated exactly as hard as SessionEnd shipping.
func TestSweepRefusesWithoutConsent(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	posted := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ingest" { // the sweep also polls /api/client-config
			fmt.Fprint(w, `{"ok":true}`)
			return
		}
		posted++
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer srv.Close()
	proj := filepath.Join(home, ".claude", "projects", "-p")
	os.MkdirAll(proj, 0o755)
	os.WriteFile(filepath.Join(proj, "s1.jsonl"), []byte("{}\n"), 0o644)

	runSweep(strings.NewReader("")) // no config at all
	// Config with a STALE consent policy version -> still must not ship.
	saveConfig(configPath(), &config{Server: srv.URL, Identity: "a@b.c",
		Consent: &consentRecord{PolicyVersion: requiredPolicyVersion - 1}})
	runSweep(strings.NewReader(""))
	if posted != 0 {
		t.Fatalf("sweep shipped without current consent: %d POST(s)", posted)
	}
}

// A RE-SETUP MUST NEVER NARROW THE HISTORY FLOOR.
//
// The migration off the shared credential asks every holder to re-run `csa-client setup`, and the
// client itself prints that instruction on the first 401 (the ambient hook and doctor both do).
// Before this, re-running setup stamped SweepFrom=now unconditionally — and `ship --sweep` skips
// everything older, so any transcript queued-but-unshipped was silently destroyed. A failed ship
// writes no marker, so the backlog is largest during exactly the outage that triggers the
// instruction.
//
// This pins the decision table rather than the implementation: the floor may widen or stay put, and
// must never move forward.
func TestReSetupNeverNarrowsTheSweepFloor(t *testing.T) {
	now := time.Now()
	older := now.Add(-72 * time.Hour)

	floorAfterResetup := func(prev *config, backfill bool) string {
		got := sweepFloor(backfill, prev)
		if t, err := time.Parse(time.RFC3339, got); err == nil && !t.Before(now.Truncate(time.Second)) {
			return now.Format(time.RFC3339) // a fresh stamp, normalised to the instant the cases expect
		}
		return got
	}
	consented := func(sweep string) *config {
		return &config{SweepFrom: sweep, Backfill: sweep == "", Consent: &consentRecord{PolicyVersion: requiredPolicyVersion}}
	}

	for _, tc := range []struct {
		name     string
		prev     *config
		backfill bool
		want     string
	}{
		{"an existing floor is KEPT, not moved forward", consented(older.Format(time.RFC3339)), false,
			older.Format(time.RFC3339)},
		{"no previous floor means history was included — do not invent one", consented(""), false, ""},
		{"a fresh machine gets today's floor", nil, false, now.Format(time.RFC3339)},
		{"--backfill still clears it deliberately", consented(older.Format(time.RFC3339)), true, ""},
		{"an unparseable stored floor falls back to now rather than trusting it",
			consented("not-a-time"), false, now.Format(time.RFC3339)},
		{"a watcher-era config that declined history gets a floor, not the whole history",
			&config{Consent: &consentRecord{PolicyVersion: requiredPolicyVersion}}, false, now.Format(time.RFC3339)},
	} {
		if got := floorAfterResetup(tc.prev, tc.backfill); got != tc.want {
			t.Errorf("%s: floor = %q, want %q", tc.name, got, tc.want)
		}
	}

	// The regression itself: a laptop that has been unable to ship keeps its backlog eligible.
	prev := consented(older.Format(time.RFC3339))
	if got := floorAfterResetup(prev, false); got != prev.SweepFrom {
		t.Fatalf("re-setup moved the floor to %q — every transcript between %q and now would be "+
			"dropped from the sweep, silently, on the command the client tells the user to run",
			got, prev.SweepFrom)
	}
}

func TestSetupServerFallsBackToConfiguredServer(t *testing.T) {
	t.Setenv("CSA_SERVER", "")
	prev := &config{Server: "https://csa.example.com"}
	if got := setupServer("", prev); got != "https://csa.example.com" {
		t.Fatalf("no flag, existing config: got %q", got)
	}
	if got := setupServer("https://other.example.com/", prev); got != "https://other.example.com" {
		t.Fatalf("flag must win over config: got %q", got)
	}
	t.Setenv("CSA_SERVER", "https://env.example.com")
	if got := setupServer("", prev); got != "https://env.example.com" {
		t.Fatalf("CSA_SERVER must win over config: got %q", got)
	}
	t.Setenv("CSA_SERVER", "")
	if got := setupServer("", nil); got != "" {
		t.Fatalf("fresh install with no flag must have no server: got %q", got)
	}
}

// A machine that ships through setup's SessionEnd hook must not also get a login watcher: both
// would ship every session. A recall-only install (init's ambient toggle) still may.
func TestInstallAutostartRefusesWhenShipHookWired(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	settings := filepath.Join(home, ".claude", "settings.json")

	if shipHookWired(settings) {
		t.Fatal("no settings file must read as not wired")
	}
	if err := installHooksAt(settings, "/Users/x/.csa/bin/csa-client", csaHooks()[:1]); err != nil {
		t.Fatal(err)
	}
	if shipHookWired(settings) {
		t.Fatal("the recall hook alone is not a ship hook")
	}
	if err := installHooksAt(settings, "/Users/x/.csa/bin/csa-client", csaHooks()); err != nil {
		t.Fatal(err)
	}
	if !shipHookWired(settings) {
		t.Fatal("setup's three hooks must read as wired")
	}
	installed, err := installAutostart()
	if installed || err == nil || !strings.Contains(err.Error(), "twice") {
		t.Fatalf("installAutostart() = %v, %v; want a refusal naming the double ship", installed, err)
	}
	if _, err := os.Stat(filepath.Join(home, "Library", "LaunchAgents", launchdLabel+".plist")); !os.IsNotExist(err) {
		t.Fatalf("refused install still wrote a plist: %v", err)
	}
}

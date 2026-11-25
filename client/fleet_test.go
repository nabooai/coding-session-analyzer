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

// fleetServer is a stand-in for the real one: it serves a directive, counts ingests, and records
// acks. Routing on the path (rather than answering everything) is what makes it able to tell a
// shipped session from a poll — see the mocks in setup_test.go.
type fleetServer struct {
	mu      sync.Mutex
	shipped []string
	acks    []map[string]any
	config  clientConfig
	srv     *httptest.Server
}

func newFleetServer(t *testing.T, cc clientConfig) *fleetServer {
	t.Helper()
	f := &fleetServer{config: cc}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/client-config":
			f.mu.Lock()
			cfg := f.config
			f.mu.Unlock()
			json.NewEncoder(w).Encode(cfg)
		case "/api/client-config/ack":
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			f.mu.Lock()
			f.acks = append(f.acks, body)
			f.mu.Unlock()
			fmt.Fprint(w, `{"ok":true}`)
		case "/api/ingest":
			var body map[string]string
			json.NewDecoder(r.Body).Decode(&body)
			f.mu.Lock()
			f.shipped = append(f.shipped, body["session_id"])
			f.mu.Unlock()
			fmt.Fprint(w, `{"ok":true}`)
		default:
			fmt.Fprint(w, `{"ok":true}`)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fleetServer) got() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string{}, f.shipped...)
}

func (f *fleetServer) ackList() []map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]map[string]any{}, f.acks...)
}

// fleetHome sets up a machine with `n` sessions of the given age, and a config that declined
// backfill an hour ago. Returns the project dir.
func fleetHome(t *testing.T, server string, backfill bool, consentedDays int) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Cleanup(func() {
		ownerID, cfID, cfSecret, authToken, watchRoot, redactMode = "", "", "", "", "", ""
	})
	proj := filepath.Join(home, ".claude", "projects", "-Users-test-demo")
	os.MkdirAll(proj, 0o755)
	setupAt := time.Now().Add(-time.Hour)
	cfg := &config{Server: server, Identity: "test@example.com", Backfill: backfill,
		SweepFrom: setupAt.Format(time.RFC3339),
		Consent: &consentRecord{PolicyVersion: requiredPolicyVersion,
			AcceptedAt: setupAt.Format(time.RFC3339), Method: "flag:--yes",
			IdentityAtConsent: "test@example.com", ArchiveDaysAtConsent: consentedDays}}
	if err := saveConfig(configPath(), cfg); err != nil {
		t.Fatal(err)
	}
	return proj
}

func writeSession(t *testing.T, proj, sid string, age time.Duration) {
	t.Helper()
	p := filepath.Join(proj, sid+".jsonl")
	os.WriteFile(p, []byte(`{"message":{"role":"user","content":"work"}}`+"\n"), 0o644)
	when := time.Now().Add(-age)
	os.Chtimes(p, when, when)
}

// A directive is the remote answer to "we lost two weeks of your sessions, re-send them": history
// that the local sweep floor would have skipped ships, and the machine reports what it did.
func TestDirectiveLowersTheSweepFloor(t *testing.T) {
	f := newFleetServer(t, clientConfig{Directives: []struct {
		ID    string `json:"id"`
		Since string `json:"since"`
	}{{ID: "d1", Since: time.Now().Add(-96 * time.Hour).Format(time.RFC3339)}}})
	proj := fleetHome(t, f.srv.URL, true, 0)
	writeSession(t, proj, "ancient", 200*time.Hour) // older than the directive: still out of scope
	writeSession(t, proj, "old-history", 72*time.Hour)
	writeSession(t, proj, "recent", 0)

	runSweep(strings.NewReader(""))

	got := f.got()
	if !contains(got, "old-history") {
		t.Fatalf("shipped %v — the directive's floor must reach history the local floor skipped", got)
	}
	if contains(got, "ancient") {
		t.Fatalf("shipped %v — a directive is a floor, not 'send everything'", got)
	}
	acks := f.ackList()
	if len(acks) != 1 || acks[0]["status"] != "applied" || acks[0]["id"] != "d1" {
		t.Fatalf("acks = %v, want one applied ack for d1", acks)
	}
	if n, _ := acks[0]["shipped"].(float64); int(n) != len(got) {
		t.Fatalf("ack reported %v shipped, the server saw %d", acks[0]["shipped"], len(got))
	}
}

// A directive may only ever WIDEN a sweep. On a machine with no floor at all (backfill accepted),
// `since` is the zero time, and treating that as "unset" made the directive's date the new floor —
// so asking a machine for history made it ship LESS. Found by a cold-read code review.
func TestDirectiveNeverNarrowsAMachineWithNoFloor(t *testing.T) {
	f := newFleetServer(t, clientConfig{Directives: []struct {
		ID    string `json:"id"`
		Since string `json:"since"`
	}{{ID: "d7", Since: time.Now().Add(-96 * time.Hour).Format(time.RFC3339)}}})
	proj := fleetHome(t, f.srv.URL, true, 0)
	// No floor: this machine already ships everything it has.
	cfg, _ := loadConfig(configPath())
	cfg.SweepFrom = ""
	saveConfig(configPath(), cfg)
	writeSession(t, proj, "ancient", 500*time.Hour) // far older than the directive's date

	runSweep(strings.NewReader(""))

	if got := f.got(); !contains(got, "ancient") {
		t.Fatalf("shipped %v — a directive must not stop a machine shipping what it already would", got)
	}
}

// The decision that outranks the admin: a machine whose owner declined backfill refuses, and says
// so, rather than quietly uploading the history they said they did not want to send.
func TestDirectiveRespectsBackfillDecline(t *testing.T) {
	f := newFleetServer(t, clientConfig{Directives: []struct {
		ID    string `json:"id"`
		Since string `json:"since"`
	}{{ID: "d2", Since: time.Now().Add(-96 * time.Hour).Format(time.RFC3339)}}})
	proj := fleetHome(t, f.srv.URL, false, 0) // backfill DECLINED at setup
	writeSession(t, proj, "old-history", 72*time.Hour)

	runSweep(strings.NewReader(""))

	if got := f.got(); contains(got, "old-history") {
		t.Fatalf("shipped %v — an admin directive must not override a local backfill decline", got)
	}
	acks := f.ackList()
	if len(acks) != 1 || acks[0]["status"] != "declined" {
		t.Fatalf("acks = %v, want a single 'declined' ack — a refusal the console can see", acks)
	}
}

// Retention is consent-gated: a server that starts keeping transcripts for longer than this person
// agreed to gets nothing until a human re-reads the notice.
func TestShippingStopsWhenRetentionExceedsConsent(t *testing.T) {
	f := newFleetServer(t, clientConfig{ArchiveDays: 30})
	proj := fleetHome(t, f.srv.URL, true, 0) // consented when the server kept nothing
	writeSession(t, proj, "fresh", 0)

	runSweep(strings.NewReader(""))

	if got := f.got(); len(got) != 0 {
		t.Fatalf("shipped %v — nothing may ship once the server keeps transcripts longer than consented", got)
	}
	// ...and the refusal survives the process: the value is cached, so SessionEnd honours it too
	// without a fetch of its own.
	cfg, err := loadConfig(configPath())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ServerArchiveDays != 30 {
		t.Fatalf("server_archive_days = %d, want the polled 30 cached for the SessionEnd path", cfg.ServerArchiveDays)
	}
	if !retentionPaused(cfg) {
		t.Fatal("the cached window must pause this machine")
	}
	before := len(f.got())
	runShip(strings.NewReader(`{"session_id":"fresh","transcript_path":"` +
		filepath.Join(proj, "fresh.jsonl") + `"}`))
	if len(f.got()) != before {
		t.Fatal("SessionEnd shipped while paused: it reads the cached window, and must honour it")
	}

	// Consent that COVERS the window ships normally — and a server lowering retention needs
	// nobody's permission.
	cfg.Consent.ArchiveDaysAtConsent = 30
	saveConfig(configPath(), cfg)
	runSweep(strings.NewReader(""))
	if got := f.got(); !contains(got, "fresh") {
		t.Fatalf("shipped %v, want the session to ship once consent covers the retention", got)
	}
}

// A paused machine must keep listening. The retention refusal used to live in shipGate, which runs
// FIRST in runSweep — so a paused machine returned before its own poll, never learned the operator
// had lowered retention again, and stayed dark forever while the docs promised the opposite.
// Found by a user-simulation pass.
func TestLoweringRetentionUnpausesAMachine(t *testing.T) {
	f := newFleetServer(t, clientConfig{ArchiveDays: 30})
	proj := fleetHome(t, f.srv.URL, true, 0) // consented when the server kept nothing
	writeSession(t, proj, "fresh", 0)

	runSweep(strings.NewReader(""))
	if got := f.got(); len(got) != 0 {
		t.Fatalf("shipped %v while paused, want nothing", got)
	}

	// The operator turns retention back off. Nobody re-runs anything on the laptop.
	f.mu.Lock()
	f.config = clientConfig{ArchiveDays: 0}
	f.mu.Unlock()

	runSweep(strings.NewReader(""))
	if got := f.got(); !contains(got, "fresh") {
		t.Fatalf("shipped %v — a machine must un-pause on its own once retention drops back", got)
	}
	cfg, err := loadConfig(configPath())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ServerArchiveDays != 0 {
		t.Fatalf("cached window = %d, want the refreshed 0", cfg.ServerArchiveDays)
	}
	if shipGate() == "" {
		t.Fatal("SessionEnd must ship again too, off the refreshed cache")
	}
}

// A paused machine must not act on directives either: complying would ship sessions under terms its
// owner never accepted, which is the whole point of pausing.
func TestPausedMachineIgnoresDirectives(t *testing.T) {
	f := newFleetServer(t, clientConfig{ArchiveDays: 30, Directives: []struct {
		ID    string `json:"id"`
		Since string `json:"since"`
	}{{ID: "d9", Since: ""}}})
	proj := fleetHome(t, f.srv.URL, true, 0)
	writeSession(t, proj, "old-history", 72*time.Hour)

	runSweep(strings.NewReader(""))

	if got := f.got(); len(got) != 0 {
		t.Fatalf("shipped %v while paused on retention", got)
	}
	if acks := f.ackList(); len(acks) != 0 {
		t.Fatalf("acks = %v — a paused machine has not decided anything yet, it is waiting on its human", acks)
	}
}

// sweepChunk caps a pass at 10, but a directive can ask for months. Acking a capped pass would stop
// the server offering the directive and strand the rest, so a capped pass banks progress instead.
func TestCappedDirectivePassDefersItsAck(t *testing.T) {
	f := newFleetServer(t, clientConfig{Directives: []struct {
		ID    string `json:"id"`
		Since string `json:"since"`
	}{{ID: "d3", Since: ""}}}) // "" = all history
	proj := fleetHome(t, f.srv.URL, true, 0)
	for i := 0; i < sweepChunk+3; i++ {
		writeSession(t, proj, fmt.Sprintf("s%02d", i), time.Duration(i+1)*time.Hour)
	}

	runSweep(strings.NewReader(""))

	if n := len(f.got()); n != sweepChunk {
		t.Fatalf("first pass shipped %d, want the cap of %d", n, sweepChunk)
	}
	if acks := f.ackList(); len(acks) != 0 {
		t.Fatalf("acks = %v — a capped pass is not done and must not ack (it would strand the rest)", acks)
	}
	if p := directiveProgress("d3"); p != sweepChunk {
		t.Fatalf("progress ledger = %d, want %d banked for the next pass", p, sweepChunk)
	}

	runSweep(strings.NewReader("")) // the remainder fits under the cap
	if n := len(f.got()); n != sweepChunk+3 {
		t.Fatalf("after two passes %d sessions shipped, want all %d", n, sweepChunk+3)
	}
	acks := f.ackList()
	if len(acks) != 1 || acks[0]["status"] != "applied" {
		t.Fatalf("acks = %v, want one applied ack once the work finished", acks)
	}
	if n, _ := acks[0]["shipped"].(float64); int(n) != sweepChunk+3 {
		t.Fatalf("ack reported %v, want the CUMULATIVE %d across both passes", acks[0]["shipped"], sweepChunk+3)
	}
	if p := directiveProgress("d3"); p != 0 {
		t.Fatalf("progress ledger = %d, want it cleared after the ack", p)
	}
}

// A directive id is a path component in the progress ledger, and it comes from the server. Hex
// only: filepath.Base blocks "../" but passes "." and "..", and clearDirectiveProgress(".") would
// then remove the ledger DIRECTORY itself. Found by a cold-read code review.
func TestDirectiveLedgerIdIsPathSafe(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	setDirectiveProgress("deadbeef01234567", 7) // a real id: token_hex(8)
	if p := directiveProgress("deadbeef01234567"); p != 7 {
		t.Fatalf("progress = %d, want 7 for a well-formed id", p)
	}
	dir := filepath.Join(home, ".csa", "directives")
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("the ledger directory should exist: %v", err)
	}

	for _, bad := range []string{"../../escaped", "..", ".", "", "a/b", "NOTHEX", strings.Repeat("a", 65)} {
		setDirectiveProgress(bad, 9)
		if p := directiveProgress(bad); p != 0 {
			t.Fatalf("id %q was accepted (progress %d); only hex ids may become a path", bad, p)
		}
		clearDirectiveProgress(bad)
		if _, err := os.Stat(dir); err != nil {
			t.Fatalf("clearing id %q removed the ledger directory itself: %v", bad, err)
		}
	}
	if _, err := os.Stat(filepath.Join(home, "escaped")); err == nil {
		t.Fatal("a ../ in a directive id escaped ~/.csa/directives")
	}
	if p := directiveProgress("deadbeef01234567"); p != 7 {
		t.Fatalf("progress = %d — a rejected id must not disturb a real one", p)
	}
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

// Acking `applied` retires a directive for that machine forever. A pass where every ship FAILED
// must therefore not ack: with the corpus wedged or over budget, `shipped` is 0 and `capped` is
// false, and the request would be closed with a green tick having moved nothing.
func TestAllShipsFailingDoesNotRetireTheRequest(t *testing.T) {
	f := newFleetServer(t, clientConfig{Directives: []struct {
		ID    string `json:"id"`
		Since string `json:"since"`
	}{{ID: "d8", Since: time.Now().Add(-96 * time.Hour).Format(time.RFC3339)}}})
	// Every ingest 500s; the config poll and ack routes keep working.
	f.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/client-config":
			f.mu.Lock()
			cfg := f.config
			f.mu.Unlock()
			json.NewEncoder(w).Encode(cfg)
		case "/api/client-config/ack":
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			f.mu.Lock()
			f.acks = append(f.acks, body)
			f.mu.Unlock()
			fmt.Fprint(w, `{"ok":true}`)
		default:
			http.Error(w, "boom", 500)
		}
	})
	proj := fleetHome(t, f.srv.URL, true, 0)
	writeSession(t, proj, "old-history", 72*time.Hour)

	runSweep(strings.NewReader(""))

	if acks := f.ackList(); len(acks) != 0 {
		t.Fatalf("acks = %v — nothing shipped, so the request must stay open for the next attempt", acks)
	}
}

// PARTIAL failure must not retire the request either. Bailing only when `shipped == 0` caught a
// totally wedged server and missed the ordinary case: some sessions through, the rest refused, under
// the chunk cap. That acked `applied` and stranded the refused ones forever. Raised by a cold-read
// code review as the residual of an earlier fix.
func TestPartialFailureDoesNotRetireTheRequest(t *testing.T) {
	f := newFleetServer(t, clientConfig{Directives: []struct {
		ID    string `json:"id"`
		Since string `json:"since"`
	}{{ID: "d10", Since: time.Now().Add(-500 * time.Hour).Format(time.RFC3339)}}})

	// The first ingest succeeds; every later one 500s. Under the chunk cap, so `capped` is false.
	var n int
	f.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/client-config":
			f.mu.Lock()
			cfg := f.config
			f.mu.Unlock()
			json.NewEncoder(w).Encode(cfg)
		case "/api/client-config/ack":
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			f.mu.Lock()
			f.acks = append(f.acks, body)
			f.mu.Unlock()
			fmt.Fprint(w, `{"ok":true}`)
		case "/api/ingest":
			f.mu.Lock()
			n++
			first := n == 1
			if first {
				var body map[string]string
				json.NewDecoder(r.Body).Decode(&body)
				f.shipped = append(f.shipped, body["session_id"])
			}
			f.mu.Unlock()
			if first {
				fmt.Fprint(w, `{"ok":true}`)
				return
			}
			http.Error(w, "boom", 500)
		default:
			fmt.Fprint(w, `{"ok":true}`)
		}
	})
	proj := fleetHome(t, f.srv.URL, true, 0)
	for i := 0; i < 4; i++ { // fewer than sweepChunk, so the cap is not what stops the ack
		writeSession(t, proj, fmt.Sprintf("h%02d", i), time.Duration(i+1)*time.Hour)
	}

	runSweep(strings.NewReader(""))

	if got := len(f.got()); got != 1 {
		t.Fatalf("%d shipped, want exactly the one that succeeded", got)
	}
	if acks := f.ackList(); len(acks) != 0 {
		t.Fatalf("acks = %v — some sessions failed, so the request is not done and must stay open", acks)
	}
	if p := directiveProgress("d10"); p != 1 {
		t.Fatalf("progress = %d, want the 1 that did ship banked for the next pass", p)
	}
}

// "Forever" (archive_days = -1) is longer than any number of days: a laptop that agreed to a window,
// or to none, pauses when the server switches to it; one that agreed to forever ships under any
// window, since less is kept than was disclosed.
func TestKeepForeverIsLongerThanAnyConsentedWindow(t *testing.T) {
	cases := []struct {
		server, agreed int
		paused         bool
	}{
		{keepForever, 0, true}, {keepForever, 30, true}, {keepForever, keepForever, false},
		{30, keepForever, false}, {0, keepForever, false}, {30, 0, true}, {30, 30, false},
	}
	for _, c := range cases {
		cfg := &config{ServerArchiveDays: c.server,
			Consent: &consentRecord{PolicyVersion: requiredPolicyVersion, ArchiveDaysAtConsent: c.agreed}}
		if got := cfg.retentionExceedsConsent(); got != c.paused {
			t.Errorf("server %d, agreed %d: paused = %v, want %v", c.server, c.agreed, got, c.paused)
		}
	}
	if s := retentionWords(keepForever); strings.Contains(s, "-1") {
		t.Fatalf("a paused notice must not print forever as %q", s)
	}
}

// The policy a server discloses before consent must carry forever through as forever, not clamp it
// to "keeps none": that is the value the consent record stores and the pause compares against.
func TestFetchPolicyKeepsForever(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"redaction_min": "secrets", "archive_days": -1})
	}))
	defer srv.Close()
	p, err := fetchPolicy(srv.URL, "")
	if err != nil {
		t.Fatal(err)
	}
	if p.ArchiveDays == nil || *p.ArchiveDays != keepForever {
		t.Fatalf("archive_days = %v, want keepForever", p.ArchiveDays)
	}
}

// A server that hosts several workspaces cannot say what THIS caller's keeps before sign-in: it
// answers null, which decodes as unknown (nil), never as "keeps none" with a number attached. The
// consent it leads to records none, so the first authenticated poll of a workspace that keeps any
// pauses shipping until the person re-runs setup with a token and sees the number.
func TestFetchPolicyUnknownRetentionIsNil(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"redaction_min": "secrets", "archive_days": nil})
	}))
	defer srv.Close()
	p, err := fetchPolicy(srv.URL, "")
	if err != nil {
		t.Fatal(err)
	}
	if p.ArchiveDays != nil {
		t.Fatalf("archive_days = %d, want nil for a server that cannot say", *p.ArchiveDays)
	}
	if daysOrNone(p.ArchiveDays) != 0 {
		t.Fatal("an unknown window must be recorded as consent to none")
	}
	cfg := &config{ServerArchiveDays: 30,
		Consent: &consentRecord{PolicyVersion: requiredPolicyVersion, ArchiveDaysAtConsent: daysOrNone(p.ArchiveDays)}}
	if !cfg.retentionExceedsConsent() {
		t.Fatal("a workspace that keeps 30 days exceeds a consent given while the window was unknown")
	}
}

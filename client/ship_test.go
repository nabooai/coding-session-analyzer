package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestRunShip is the smallest check that fails if the SessionEnd ship path breaks:
// (a) the POST reaches /api/ingest, (b) a planted secret is redacted before the wire,
// (c) with no consent/config nothing is POSTed, plus the duplicate-firing guard and
// the ignore-glob exclusion.
func TestRunShip(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	defer func() { ownerID, cfID, cfSecret, authToken, watchRoot, redactMode = "", "", "", "", "", "" }()

	var mu sync.Mutex
	var posts []map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ingest" { // the sweep also polls /api/client-config
			fmt.Fprint(w, `{"ok":true}`)
			return
		}
		if r.URL.Path != "/api/ingest" {
			t.Errorf("POST hit %s, want /api/ingest", r.URL.Path)
		}
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("undecodable ingest body: %v", err)
		}
		mu.Lock()
		posts = append(posts, body)
		mu.Unlock()
		fmt.Fprint(w, `{"ok":true,"hidden_context":1,"entities":2}`)
	}))
	defer srv.Close()
	nposts := func() int { mu.Lock(); defer mu.Unlock(); return len(posts) }

	// A transcript with a planted fake secret, under a watcher-shaped project dir.
	proj := filepath.Join(home, ".claude", "projects", "-Users-test-Git-demo-repo")
	if err := os.MkdirAll(proj, 0o755); err != nil {
		t.Fatal(err)
	}
	transcript := filepath.Join(proj, "sess-ship-1.jsonl")
	const planted = "sk-fake1234567890abcdefghijklmnop"
	write := func(s string) {
		if err := os.WriteFile(transcript, []byte(s), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write(`{"message":{"role":"user","content":"my key is ` + planted + `"}}` + "\n")

	hookInput := func() *strings.Reader {
		return strings.NewReader(`{"hook_event_name":"SessionEnd","session_id":"sess-ship-1","transcript_path":` +
			string(mustJSON(t, transcript)) + `,"cwd":"/tmp","reason":"other"}`)
	}

	// (c) No config / no consent -> silently ships NOTHING.
	runShip(hookInput())
	if nposts() != 0 {
		t.Fatalf("shipped without consent: %d POST(s)", nposts())
	}

	// Consent on record -> ships once, redacted, attributed.
	cfg := &config{Server: srv.URL, Identity: "test@example.com", Consent: &consentRecord{
		PolicyVersion: requiredPolicyVersion, AcceptedAt: time.Now().Format(time.RFC3339),
		Method: "flag:--yes", IdentityAtConsent: "test@example.com"}}
	if err := saveConfig(configPath(), cfg); err != nil {
		t.Fatal(err)
	}
	runShip(hookInput())
	if nposts() != 1 {
		t.Fatalf("want 1 POST after consent, got %d", nposts())
	}
	got := posts[0]
	if got["session_id"] != "sess-ship-1" {
		t.Errorf("session_id = %q", got["session_id"])
	}
	if got["owner"] != "test@example.com" {
		t.Errorf("owner = %q", got["owner"])
	}
	if strings.Contains(got["content"], planted) {
		t.Fatalf("planted secret reached the wire: %q", got["content"])
	}
	if !strings.Contains(got["content"], "[REDACTED:openai_key]") {
		t.Errorf("planted secret not redacted as openai_key: %q", got["content"])
	}
	if got["redaction_mode"] != "secrets" {
		t.Errorf("redaction_mode = %q, want secrets", got["redaction_mode"])
	}

	// Duplicate SessionEnd, transcript unchanged -> the size marker suppresses the re-ship.
	runShip(hookInput())
	if nposts() != 1 {
		t.Fatalf("duplicate SessionEnd re-shipped: %d POST(s)", nposts())
	}

	// Transcript grew -> ships again (whole file, transcripts are the unit).
	write(`{"message":{"role":"user","content":"my key is ` + planted + `"}}` + "\n" +
		`{"message":{"role":"user","content":"and more work happened"}}` + "\n")
	runShip(hookInput())
	if nposts() != 2 {
		t.Fatalf("grown transcript did not re-ship: %d POST(s)", nposts())
	}

	// Ignore glob on the project dir -> excluded even though the transcript grew again.
	if err := os.WriteFile(filepath.Join(home, ".csa", "ignore"), []byte("*demo-repo*\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	write(strings.Repeat("x", 4096))
	runShip(hookInput())
	if nposts() != 2 {
		t.Fatalf("ignored project shipped anyway: %d POST(s)", nposts())
	}
}

// TestMain doubles as the detached child. shipDetach re-execs os.Executable(), which under `go test`
// is this test binary, so without this the child would re-run the whole suite. The carried payload
// says "you are the child": do the one ship and exit, exactly as main() would.
func TestMain(m *testing.M) {
	if _, ok := os.LookupEnv(shipInputEnv); ok {
		ev, _ := shipDetach(os.Stdin)
		runShip(ev)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// TestShipHookBinaryDoesNotWaitForTheServer drives the REAL binary, the way Claude Code does, because
// that is the only thing that fails when the fix is undone. Calling shipDetach directly (below) proves
// shipDetach works but says nothing about main()'s dispatch actually using it — revert main.go to
// `runShip(os.Stdin)` and a shipDetach-only test stays green while the stall is fully back.
func TestShipHookBinaryDoesNotWaitForTheServer(t *testing.T) {
	if testing.Short() {
		t.Skip("builds a binary")
	}
	home := t.TempDir()
	bin := filepath.Join(t.TempDir(), "csa-client-underDeadline")
	if out, err := exec.Command("go", "build", "-o", bin, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}

	release := make(chan struct{})
	posted := make(chan struct{}, 1)
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ingest" { // the sweep also polls /api/client-config
			fmt.Fprint(w, `{"ok":true}`)
			return
		}
		posted <- struct{}{}
		<-release // hold it open the way an inline `claude -p` extraction does
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer srv.Close()
	defer unblock()

	transcript := writeTestTranscript(t, home, "sess-bin-1", `{"message":{"role":"user","content":"hi"}}`)
	writeTestConfig(t, home, srv.URL)

	// Bound the hook itself: if the fix regresses, the hook blocks forever on a server that never
	// answers, and without a deadline that surfaces as an unreadable suite-wide timeout instead of
	// "the hook blocked 15s".
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, "ship")
	cmd.Env = append(os.Environ(), "HOME="+home, "CSA_SERVER="+srv.URL)
	cmd.Stdin = strings.NewReader(`{"session_id":"sess-bin-1","transcript_path":` + string(mustJSON(t, transcript)) + `}`)
	start := time.Now()
	err := cmd.Run()
	elapsed := time.Since(start)
	if elapsed > 10*time.Second {
		t.Fatalf("`csa-client ship` blocked %v on a server that never answered — the hook must hand off, "+
			"not wait (is main() still calling shipDetach?)", elapsed)
	}
	if err != nil {
		t.Fatalf("hook exited nonzero after %v: %v", elapsed, err)
	}
	select { // and the detached child still does the work
	case <-posted:
	case <-time.After(30 * time.Second):
		t.Fatal("no POST from the detached child — handing off must not mean dropping the session")
	}
	// Let the child finish before t.TempDir() cleanup runs, or it races the marker write.
	unblock()
	waitForFile(t, filepath.Join(home, ".csa", "shipped", "sess-bin-1"))
}

// waitForFile blocks until path exists, so a detached child's last write can't race TempDir cleanup.
func waitForFile(t *testing.T, path string) {
	t.Helper()
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("background ship never recorded %s", filepath.Base(path))
}

// TestSweepDoesNotDoubleShipAnInFlightSession guards the invariant the detach broke. The shipped marker
// is only written AFTER send() returns (45-75s), and before the detach the SessionEnd hook BLOCKED, so
// Claude Code could not start the next session until the marker existed. Detaching removed that
// accidental serialization: on `/clear` (SessionEnd then SessionStart milliseconds later) the sweep now
// runs while the previous ship is still in flight, sees no marker, and re-ships — two /api/ingest calls,
// two server-side `claude -p` extractions, for one session.
func TestSweepDoesNotDoubleShipAnInFlightSession(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	defer func() { ownerID, cfID, cfSecret, authToken, watchRoot, redactMode = "", "", "", "", "", "" }()

	var mu sync.Mutex
	var posts int
	release := make(chan struct{})
	inFlight := make(chan struct{}, 1)
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ingest" { // the sweep also polls /api/client-config
			fmt.Fprint(w, `{"ok":true}`)
			return
		}
		mu.Lock()
		posts++
		n := posts
		mu.Unlock()
		if n == 1 { // hold ONLY the first POST open, standing in for a slow `claude -p`. A duplicate
			inFlight <- struct{}{} // must answer immediately, or the sweep would block and deadlock
			<-release              // the test instead of reporting the double-ship.
		}
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer srv.Close()
	defer unblock()

	transcript := writeTestTranscript(t, home, "sess-race-1", `{"message":{"role":"user","content":"hi"}}`)
	writeTestConfig(t, home, srv.URL)
	server := shipGate()
	if server == "" {
		t.Fatal("shipGate refused a consented config")
	}

	done := make(chan bool, 1)
	go func() { done <- shipOne(server, "sess-race-1", transcript) }() // the SessionEnd ship, still in flight
	<-inFlight                                                         // it is now parked in send(), no marker yet

	// The next SessionStart fires while that POST is open. It must NOT re-ship the same session.
	runSweep(strings.NewReader(`{"session_id":"brand-new-session"}`))

	unblock()
	<-done
	mu.Lock()
	defer mu.Unlock()
	if posts != 1 {
		t.Fatalf("session shipped %d times; a sweep re-shipped a session whose ship was still in flight "+
			"(each duplicate is a second `claude -p` extraction, billed)", posts)
	}
}

// TestShipHookDoesNotWaitForTheServer covers shipDetach itself; the binary test above covers the wiring.
func TestShipHookDoesNotWaitForTheServer(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	defer func() { ownerID, cfID, cfSecret, authToken, watchRoot, redactMode = "", "", "", "", "", "" }()

	release := make(chan struct{})
	posted := make(chan struct{}, 1)
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) } // srv.Close() waits on in-flight requests
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/ingest" { // the sweep also polls /api/client-config
			fmt.Fprint(w, `{"ok":true}`)
			return
		}
		posted <- struct{}{}
		<-release // hold the POST open the way a real inline extraction does
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer srv.Close()
	defer unblock()

	proj := filepath.Join(home, ".claude", "projects", "-Users-test-Git-demo-repo")
	if err := os.MkdirAll(proj, 0o755); err != nil {
		t.Fatal(err)
	}
	transcript := filepath.Join(proj, "sess-slow-1.jsonl")
	if err := os.WriteFile(transcript, []byte(`{"message":{"role":"user","content":"hi"}}`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := &config{Server: srv.URL, Identity: "test@example.com", Consent: &consentRecord{
		PolicyVersion: requiredPolicyVersion, AcceptedAt: time.Now().Format(time.RFC3339),
		Method: "flag:--yes", IdentityAtConsent: "test@example.com"}}
	if err := saveConfig(configPath(), cfg); err != nil {
		t.Fatal(err)
	}

	// The parent path: hand off and return while the server is still holding the POST open.
	start := time.Now()
	_, parent := shipDetach(strings.NewReader(`{"session_id":"sess-slow-1","transcript_path":` +
		string(mustJSON(t, transcript)) + `}`))
	elapsed := time.Since(start)
	if !parent {
		t.Fatal("shipDetach did not detach — the hook would ship inline and block on the server")
	}
	if elapsed > 5*time.Second {
		t.Fatalf("hook took %v to hand off; it must not wait on the server", elapsed)
	}
	// ...and the work still happens in the background, on a server that never got faster.
	select {
	case <-posted:
	case <-time.After(30 * time.Second):
		t.Fatal("detached child never POSTed — handing off must not mean dropping the session")
	}
	// Let the slow server answer, then wait for the child to record the ship. Waiting here is also
	// what keeps the child from racing t.TempDir()'s cleanup: the marker is its last act.
	unblock()
	waitForFile(t, shippedMarkerPath("sess-slow-1"))
}

// writeTestTranscript plants a transcript under a watcher-shaped project dir and returns its path.
func writeTestTranscript(t *testing.T, home, sid, body string) string {
	t.Helper()
	proj := filepath.Join(home, ".claude", "projects", "-Users-test-Git-demo-repo")
	if err := os.MkdirAll(proj, 0o755); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(proj, sid+".jsonl")
	if err := os.WriteFile(p, []byte(body+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

// writeTestConfig records consent for `home` pointing at `server`, so shipGate() opens.
func writeTestConfig(t *testing.T, home, server string) {
	t.Helper()
	old := os.Getenv("HOME")
	os.Setenv("HOME", home) // configPath() reads HOME; the binary test can't use t.Setenv for a subprocess
	defer os.Setenv("HOME", old)
	cfg := &config{Server: server, Identity: "test@example.com", Consent: &consentRecord{
		PolicyVersion: requiredPolicyVersion, AcceptedAt: time.Now().Format(time.RFC3339),
		Method: "flag:--yes", IdentityAtConsent: "test@example.com"}}
	if err := saveConfig(configPath(), cfg); err != nil {
		t.Fatal(err)
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

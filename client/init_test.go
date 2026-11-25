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
)

// tokenServer answers the four calls `init --yes --token` makes, and only with the right bearer.
func tokenServer(t *testing.T, token string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+token {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/api/health":
			w.Write([]byte(`{"ok":true}`))
		case "/api/policy":
			w.Write([]byte(`{"redaction_min":"secrets","archive_days":0}`))
		case "/api/ingest":
			w.Write([]byte(`{"ok":true}`))
		case "/api/team":
			w.Write([]byte(`[]`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// runInitCaptured runs `init` in-process against a scratch HOME and returns what it printed.
func runInitCaptured(t *testing.T, args ...string) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout := os.Stdout
	os.Stdout = w
	done := make(chan string)
	go func() {
		b, _ := io.ReadAll(r)
		done <- string(b)
	}()
	runInit(args)
	w.Close()
	os.Stdout = stdout
	return <-done
}

func scratchHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("PATH", "") // no `claude`, no `launchctl`: nothing outside HOME is touched
	t.Setenv("CSA_SERVER", "")
	t.Setenv("CSA_AUTH_TOKEN", "")
	return home
}

// The documented token path must end with a machine that ships. It used to wire nothing, print
// "one step left", and leave the machine silent until someone found the watcher on their own.
func TestInitTokenPathWiresEveryShipHook(t *testing.T) {
	home := scratchHome(t)
	srv := tokenServer(t, "s3cret")

	out := runInitCaptured(t, "--yes", "--server", srv.URL, "--token", "s3cret",
		"--email", "bob@example.com", "--backfill=false")

	settings := filepath.Join(home, ".claude", "settings.json")
	b, err := os.ReadFile(settings)
	if err != nil {
		t.Fatalf("init wrote no Claude Code settings: %v\n%s", err, out)
	}
	var root map[string]any
	if err := json.Unmarshal(b, &root); err != nil {
		t.Fatal(err)
	}
	for event, wired := range hookStatus(root) {
		if !wired {
			t.Errorf("%s hook not wired after init --yes --token", event)
		}
	}
	if _, err := os.Stat(filepath.Join(home, ".csa", "bin", "csa-client")); err != nil {
		t.Errorf("the hooks point at a binary init did not install: %v", err)
	}
	if !strings.Contains(out, "Nothing more to run") || strings.Contains(out, "Nothing ships yet") {
		t.Errorf("epilogue must say the machine ships, got:\n%s", out)
	}

	cfg, err := loadConfig(configPath())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.SweepFrom == "" {
		t.Error("--backfill=false must stamp a sweep floor, or the first SessionStart ships the history the user declined")
	}
	if cfg.AuthToken != "s3cret" || cfg.Identity != "bob@example.com" {
		t.Errorf("config = token %q identity %q", cfg.AuthToken, cfg.Identity)
	}
}

// Re-running init on a machine an older init left with the recall hook alone (no watcher) is the
// exact silent shape; the re-run must complete the wiring, not keep it.
func TestReInitCompletesARecallOnlyMachine(t *testing.T) {
	home := scratchHome(t)
	srv := tokenServer(t, "s3cret")
	settings := filepath.Join(home, ".claude", "settings.json")
	if err := installHooksAt(settings, "/old/.csa/bin/csa-client", csaHooks()[:1]); err != nil {
		t.Fatal(err)
	}

	runInitCaptured(t, "--yes", "--server", srv.URL, "--token", "s3cret", "--email", "bob@example.com")

	for _, ev := range []string{"UserPromptSubmit", "SessionEnd", "SessionStart"} {
		if mine, _ := csaEntriesFor(t, settings, ev); mine != 1 {
			t.Errorf("%s: %d CSA entries after re-init, want exactly 1", ev, mine)
		}
	}
	cfg, err := loadConfig(configPath())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.SweepFrom != "" {
		t.Errorf("history was accepted (the init default), so no floor: got %q", cfg.SweepFrom)
	}
}

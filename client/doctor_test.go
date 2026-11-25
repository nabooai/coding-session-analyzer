package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHookStatusAllWired(t *testing.T) {
	dir := t.TempDir()
	settings := filepath.Join(dir, "settings.json")
	if err := installHooksAt(settings, "/Users/x/.csa/bin/csa-client", csaHooks()); err != nil {
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
	present := hookStatus(root)
	for _, spec := range csaHooks() {
		if !present[spec.event] {
			t.Errorf("hookStatus: %s not detected as present after installHooksAt wrote it", spec.event)
		}
	}
}

func TestHookStatusNoneWired(t *testing.T) {
	present := hookStatus(map[string]any{})
	for _, spec := range csaHooks() {
		if present[spec.event] {
			t.Errorf("hookStatus: %s reported present on an empty settings root", spec.event)
		}
	}
}

// TestHookStatusPartial reproduces the exact shape found on a real machine mid-investigation:
// only the ambient-recall hook installed (via `init`, not `setup`), SessionEnd/SessionStart
// missing entirely — the trap this command exists to catch at a glance.
func TestHookStatusPartial(t *testing.T) {
	dir := t.TempDir()
	settings := filepath.Join(dir, "settings.json")
	if err := installHooksAt(settings, "/Users/x/.csa/bin/csa-client", csaHooks()[:1]); err != nil {
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
	present := hookStatus(root)
	if !present["UserPromptSubmit"] {
		t.Error("hookStatus: UserPromptSubmit should be present")
	}
	if present["SessionEnd"] || present["SessionStart"] {
		t.Error("hookStatus: SessionEnd/SessionStart should NOT be present — only the recall hook was installed")
	}
}

// TestHookStatusPluginForm covers the shell-quoted form the plugin channel writes
// (command: `"${CLAUDE_PLUGIN_ROOT}/bin/csa-client" ship`, no args array) — a real installation
// shape distinct from what installHooksAt itself writes.
func TestHookStatusPluginForm(t *testing.T) {
	root := map[string]any{
		"hooks": map[string]any{
			"SessionEnd": []any{
				map[string]any{"hooks": []any{
					map[string]any{"type": "command", "command": `"/opt/csa/bin/csa-client" ship`, "timeout": 240},
				}},
			},
		},
	}
	present := hookStatus(root)
	if !present["SessionEnd"] {
		t.Error("hookStatus: should recognise the shell-quoted plugin form as a CSA hook")
	}
	if present["SessionStart"] || present["UserPromptSubmit"] {
		t.Error("hookStatus: only SessionEnd was installed in this fixture")
	}
}

// TestDoctorHooksSectionWatcherShape reproduces the exact false-alarm found testing on
// lab.example.com: a machine with zero Claude Code hooks but a confirmed-running watcher is
// a legitimate bot/service deployment, not a broken one — it must report info, not three failures.
func TestDoctorHooksSectionWatcherShape(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home) // doctorHooksSection reads ~/.claude/settings.json via os.UserHomeDir()
	settings := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(settings), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(settings, []byte("{}"), 0o644); err != nil { // exists, but wires nothing
		t.Fatal(err)
	}

	sec := doctorHooksSection("/etc/systemd/system/csa-client.service")
	if len(sec.results) != 1 || sec.results[0].level != dInfo {
		t.Fatalf("zero hooks + watcher present: want exactly one dInfo result, got %+v", sec.results)
	}

	sec = doctorHooksSection("")
	if len(sec.results) != 1 || sec.results[0].level != dTodo {
		t.Fatalf("zero hooks + NO watcher: want the one not-shipping step, got %+v", sec.results)
	}
}

// TestDoctorHooksSectionPartialIgnoresWatcher: a PARTIAL hook install (e.g. only the recall hook,
// the exact trap `init` without `setup` leaves behind) must still fail per missing hook even when
// a watcher is present — that combination means double-shipping risk, not a clean bot deployment.
func TestDoctorHooksSectionPartialIgnoresWatcher(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	settings := filepath.Join(home, ".claude", "settings.json")
	if err := installHooksAt(settings, "/Users/x/.csa/bin/csa-client", csaHooks()[:1]); err != nil {
		t.Fatal(err)
	}

	sec := doctorHooksSection("/etc/systemd/system/csa-client.service")
	failCount := 0
	for _, r := range sec.results {
		if r.level == dFail {
			failCount++
		}
	}
	if failCount == 0 {
		t.Errorf("partial hooks + watcher present: still want failures for the missing hooks, got %+v", sec.results)
	}
}

// A machine `init` or `setup` just wired has shipped nothing yet. That is not a problem to send an
// admin: no warning, no failure.
func TestDoctorJustWiredMachineIsQuiet(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	settings := filepath.Join(home, ".claude", "settings.json")
	if err := installHooksAt(settings, "/Users/x/.csa/bin/csa-client", csaHooks()); err != nil {
		t.Fatal(err)
	}
	hooksShip := shipHookWired(settings)
	for _, sec := range []doctorSection{
		doctorHooksSection(""),
		doctorAutostartSection("", hooksShip),
		doctorShippingSection("", hooksShip),
		doctorProjectsSection(),
	} {
		for _, r := range sec.results {
			if r.level == dWarn || r.level == dFail || r.level == dTodo {
				t.Errorf("%s: %q reads as a problem on a just-wired machine", sec.title, r.line)
			}
		}
	}
}

// No settings file and no watcher: one actionable step, not a failure for the admin.
func TestDoctorNoSettingsIsOneStep(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	sec := doctorHooksSection("")
	if len(sec.results) != 1 || sec.results[0].level != dTodo || !strings.Contains(sec.results[0].line, "csa-client setup") {
		t.Fatalf("want one not-shipping step naming the command, got %+v", sec.results)
	}
	for _, sec := range []doctorSection{doctorAutostartSection("", false), doctorShippingSection("", false)} {
		for _, r := range sec.results {
			if r.level != dInfo {
				t.Errorf("%s: %q repeats the not-shipping finding at level %d", sec.title, r.line, r.level)
			}
		}
	}
}

func TestFirstField(t *testing.T) {
	cases := map[string]string{
		"2026/08/20 09:15:03 sent abc123 ok=true hidden_context=0 entities=0": "2026/08/20 09:15:03",
		"":         "",
		"onefield": "onefield",
	}
	for line, want := range cases {
		if got := firstField(line); got != want {
			t.Errorf("firstField(%q) = %q, want %q", line, got, want)
		}
	}
}

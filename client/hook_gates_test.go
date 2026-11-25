package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

type hookGateSamples struct {
	LooksSecret  struct{ Yes, No []string } `json:"looks_secret"`
	ShouldRecall struct{ Yes, No []string } `json:"should_recall"`
	AmbientQuery []struct {
		In  string `json:"in"`
		Out string `json:"out"`
	} `json:"ambient_query"`
	IgnoredBy []struct {
		Pattern string `json:"pattern"`
		Path    string `json:"path"`
		Match   bool   `json:"match"`
	} `json:"ignored_by"`
}

func loadHookGateSamples(t *testing.T) hookGateSamples {
	t.Helper()
	b, err := os.ReadFile("../docs/hook_gate_samples.json")
	if err != nil {
		t.Fatal(err)
	}
	var s hookGateSamples
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}
	return s
}

func TestHookGateSamples(t *testing.T) {
	s := loadHookGateSamples(t)
	for _, p := range s.LooksSecret.Yes {
		if !looksSecret(p) {
			t.Errorf("residual secret not caught: %q", p)
		}
	}
	for _, p := range s.LooksSecret.No {
		if looksSecret(p) {
			t.Errorf("clean prompt read as a secret: %q", p)
		}
	}
	for _, p := range s.ShouldRecall.Yes {
		if !shouldAmbientRecall(p) {
			t.Errorf("should recall for %q", p)
		}
	}
	for _, p := range s.ShouldRecall.No {
		if shouldAmbientRecall(p) {
			t.Errorf("should not recall for %q", p)
		}
	}
	for _, c := range s.AmbientQuery {
		if got := ambientQuery(c.In); got != c.Out {
			t.Errorf("ambientQuery(%q) = %q, want %q", c.In, got, c.Out)
		}
	}
	for _, c := range s.IgnoredBy {
		if got := ignoredBy(c.Path, []string{c.Pattern}) != ""; got != c.Match {
			t.Errorf("ignoredBy(%q, %q) = %v, want %v", c.Path, c.Pattern, got, c.Match)
		}
	}
}

// TestPluginRedactorIsByteEqual runs every shared fixture through the plugin's redact.py and redactReport
// and compares the output bytes and the labels, in order.
func TestPluginRedactorIsByteEqual(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH")
	}
	raw, err := os.ReadFile("../docs/redaction_samples.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []redactFixture `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	scripts, err := filepath.Abs("../csa-plugin/scripts")
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("python3", "-I", "-S", "-B", "-c", `
import json, sys
sys.path.append(sys.argv[1])
import redact
out = [redact.redact(c["input"], c.get("mode") or "secrets", c.get("deny") or (), c.get("allow") or ())
       for c in json.load(sys.stdin)["cases"]]
json.dump(out, sys.stdout)
`, scripts)
	cmd.Stdin = bytes.NewReader(raw)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdout, err := cmd.Output()
	if err != nil {
		t.Fatalf("python3: %v: %s", err, stderr.String())
	}
	var py [][]json.RawMessage
	if err := json.Unmarshal(stdout, &py); err != nil {
		t.Fatal(err)
	}
	if len(py) != len(doc.Cases) {
		t.Fatalf("python returned %d results for %d cases", len(py), len(doc.Cases))
	}
	for i, c := range doc.Cases {
		var pyOut string
		var pyLabels []string
		if err := json.Unmarshal(py[i][0], &pyOut); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(py[i][1], &pyLabels); err != nil {
			t.Fatal(err)
		}
		out, labels := redactReport(redactPolicy{Mode: c.Mode, Deny: c.Deny, Allow: c.Allow}, c.Input)
		if out != pyOut {
			t.Errorf("%s: Go and Python differ:\n go %q\n py %q", c.Name, out, pyOut)
		}
		if strings.Join(labels, ",") != strings.Join(pyLabels, ",") {
			t.Errorf("%s: labels differ: go %v py %v", c.Name, labels, pyLabels)
		}
	}
}

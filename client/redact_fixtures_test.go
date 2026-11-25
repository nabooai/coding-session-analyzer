package main

// Go HALF of the client<->server byte-parity proof. Loads the SAME docs/redaction_samples.json the
// Python server test (server/tests/test_redaction_e2e.py) loads and asserts the SAME must_redact / must_keep
// / labels through redactReport. Both suites green on one shared fixture file == the Go client and the
// Python server redactors are byte-parity mirrors (same labels, same regexes, same deny/allow logic).
// Stdlib only.
//
// RUN THIS WITH `go test -count=1` after editing the JSON. Go's test cache keys on source +
// args, NOT external file reads, so a plain `go test` can report a STALE pass against an old fixture
// count. Upgrade path if this bites: mirror the fixtures into this package dir and //go:embed them
// (go:embed can't reach ../docs), or add a build step — not worth it until the caching actually misleads.

import (
	"encoding/json"
	"os"
	"sort"
	"strings"
	"testing"
)

type redactFixture struct {
	Name       string   `json:"name"`
	Mode       string   `json:"mode"`
	Input      string   `json:"input"`
	Deny       []string `json:"deny"`
	Allow      []string `json:"allow"`
	MustRedact []string `json:"must_redact"`
	MustKeep   []string `json:"must_keep"`
	Labels     []string `json:"labels"`
}

// sameSet reports whether two label slices hold the same set of values (order-independent; both
// redactors dedup, so plain set equality is the right comparison and catches over/under-redaction).
func sameSet(a, b []string) bool {
	ca, cb := append([]string(nil), a...), append([]string(nil), b...)
	sort.Strings(ca)
	sort.Strings(cb)
	if len(ca) != len(cb) {
		return false
	}
	for i := range ca {
		if ca[i] != cb[i] {
			return false
		}
	}
	return true
}

func TestRedactionFixtures(t *testing.T) {
	b, err := os.ReadFile("../docs/redaction_samples.json")
	if err != nil {
		t.Fatalf("read fixtures: %v", err)
	}
	var doc struct {
		Cases []redactFixture `json:"cases"`
	}
	if err := json.Unmarshal(b, &doc); err != nil {
		t.Fatalf("parse fixtures: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("no fixture cases loaded from redaction_samples.json")
	}
	for _, c := range doc.Cases {
		out, found := redactReport(redactPolicy{Mode: c.Mode, Deny: c.Deny, Allow: c.Allow}, c.Input)
		for _, bad := range c.MustRedact {
			if strings.Contains(out, bad) {
				t.Errorf("%s: must_redact %q LEAKED -> %q", c.Name, bad, out)
			}
		}
		for _, good := range c.MustKeep {
			if !strings.Contains(out, good) {
				t.Errorf("%s: must_keep %q MISSING -> %q", c.Name, good, out)
			}
		}
		if !sameSet(found, c.Labels) {
			t.Errorf("%s: fired labels %v != expected %v -> %q", c.Name, found, c.Labels, out)
		}
	}
	t.Logf("redaction fixtures: %d cases passed (Go half of client<->server parity)", len(doc.Cases))
}

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var launchctlCalls []string

func init() {
	launchctl = func(args ...string) ([]byte, error) {
		launchctlCalls = append(launchctlCalls, strings.Join(args, " "))
		return nil, nil
	}
}

func writeAgent(t *testing.T, home, name, label, program string) string {
	t.Helper()
	dir := launchAgentsDir(home)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, name)
	body := `<plist version="1.0"><dict><key>Label</key><string>` + label +
		`</string><key>ProgramArguments</key><array><string>` + program + `</string></array></dict></plist>`
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestOlderAgentsAreFoundAndRemovedAndStrangersAreNot(t *testing.T) {
	home := t.TempDir()
	launchctlCalls = nil
	older := writeAgent(t, home, "org.example.csa-client.plist", "org.example.csa-client", "/x/.csa/bin/csa-client")
	notCSA := writeAgent(t, home, "other.csa-client.plist", "other.csa-client", "/usr/bin/backup")
	stranger := writeAgent(t, home, "com.example.sync.plist", "com.example.sync", "/x/csa-client")

	agents := csaAgents(home)
	if len(agents) != 1 || agents[0].path != older || agents[0].label != "org.example.csa-client" {
		t.Fatalf("agents = %+v", agents)
	}
	if got := removeAgents(home, ""); len(got) != 1 || got[0] != older {
		t.Fatalf("removed %v", got)
	}
	for _, p := range []string{notCSA, stranger} {
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("touched an unrelated agent %s", p)
		}
	}
	if _, err := os.Stat(older); !os.IsNotExist(err) {
		t.Fatal("older agent still there")
	}
	if len(launchctlCalls) != 1 || !strings.HasSuffix(launchctlCalls[0], "/org.example.csa-client") {
		t.Fatalf("launchctl calls %v", launchctlCalls)
	}
}

func TestInstallKeepsTheNewAgentAndRetiresTheOlder(t *testing.T) {
	home := t.TempDir()
	own := writeAgent(t, home, launchdLabel+".plist", launchdLabel, "/x/csa-client")
	older := writeAgent(t, home, "org.example.csa-client.plist", "org.example.csa-client", "/x/csa-client")
	removeAgents(home, own)
	if _, err := os.Stat(own); err != nil {
		t.Fatal("removed the agent being installed")
	}
	if _, err := os.Stat(older); !os.IsNotExist(err) {
		t.Fatal("older agent kept: it would double-ship")
	}
}

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const launchdLabel = "com.csa.client"

// launchctl is the one door to the machine's launchd; tests replace it.
var launchctl = func(args ...string) ([]byte, error) {
	return exec.Command("launchctl", args...).CombinedOutput()
}

type launchAgent struct{ path, label string }

var (
	plistLabel   = regexp.MustCompile(`<key>Label</key>\s*<string>([^<]+)</string>`)
	plistProgram = regexp.MustCompile(`<key>ProgramArguments</key>\s*<array>\s*<string>([^<]+)</string>`)
)

func launchAgentsDir(home string) string { return filepath.Join(home, "Library", "LaunchAgents") }

// csaAgents lists the CSA login agents under home: this client's own plist, and any older install
// whose file is named *.csa-client.plist. A file counts only when it runs a csa-client binary.
// Read-only.
func csaAgents(home string) []launchAgent {
	own := filepath.Join(launchAgentsDir(home), launchdLabel+".plist")
	found, _ := filepath.Glob(filepath.Join(launchAgentsDir(home), "*.csa-client.plist"))
	var out []launchAgent
	for _, p := range append([]string{own}, found...) {
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		prog := plistProgram.FindSubmatch(b)
		label := plistLabel.FindSubmatch(b)
		if prog == nil || label == nil || !strings.HasPrefix(filepath.Base(string(prog[1])), "csa-client") {
			continue
		}
		out = append(out, launchAgent{p, string(label[1])})
	}
	return out
}

// removeAgents stops and deletes the CSA login agents except keep (a plist path; "" removes all).
func removeAgents(home, keep string) []string {
	uid := strconv.Itoa(os.Getuid())
	var removed []string
	for _, a := range csaAgents(home) {
		if a.path == keep {
			continue
		}
		launchctl("bootout", "gui/"+uid+"/"+a.label) // fine if not loaded
		if os.Remove(a.path) == nil {
			removed = append(removed, a.path)
		}
	}
	return removed
}

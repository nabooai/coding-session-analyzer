// doctor.go — `csa-client doctor`: a read-only, end-to-end diagnostic sweep. It exists because
// "my sessions aren't showing up" has turned out to have half a dozen distinct root causes
// (empty/expired token, an `only` allowlist that matches nothing, hooks never wired, a stale
// binary, the server itself down) that otherwise get diagnosed one Slack message at a time by
// whoever's on call. doctor checks all of them in one pass and prints what a human would
// otherwise have to reconstruct by hand across five files and a live server call.
//
// It never writes anything and never ships a session — the fix for a failing check is always a
// command it prints, never an action it takes itself. The report is meant to be copy-pasted to
// whoever administers the server; doctor does not send it anywhere on its own (nothing here
// leaves the machine except the read-only probes explicitly listed below: GET /api/health and
// GET /api/team, both of which the watcher already calls in the normal course of things).
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type doctorLevel int

const (
	dOK doctorLevel = iota
	dWarn
	dFail
	dInfo // a listing, not a pass/fail check — excluded from the summary tally
	dTodo // set up but shipping nothing: one command fixes it, no admin needed
)

const notShippingFix = "`csa-client setup` (or `csa-client init` on a shared-token server)"

type doctorResult struct {
	level doctorLevel
	line  string
}

func (r doctorResult) mark() string {
	switch r.level {
	case dOK:
		return okMark()
	case dWarn:
		return warnMark()
	case dFail:
		return failMark()
	case dTodo:
		return cyan("→")
	default:
		return dim("•") // a listing, not a verdict — no color of judgment
	}
}

func dOKf(format string, a ...any) doctorResult { return doctorResult{dOK, fmt.Sprintf(format, a...)} }
func dWarnf(format string, a ...any) doctorResult {
	return doctorResult{dWarn, fmt.Sprintf(format, a...)}
}
func dFailf(format string, a ...any) doctorResult {
	return doctorResult{dFail, fmt.Sprintf(format, a...)}
}
func dInfof(format string, a ...any) doctorResult {
	return doctorResult{dInfo, fmt.Sprintf(format, a...)}
}

// doctorSection is one named group of checks (Config, Hooks, ...), printed as a heading
// followed by each result indented under it.
type doctorSection struct {
	title   string
	results []doctorResult
}

func runDoctor(args []string) {
	for _, a := range args {
		if a == "--help" || a == "-h" {
			fmt.Println("usage: csa-client doctor")
			fmt.Println("Runs a read-only diagnostic sweep and prints a report to copy-paste to your CSA admin.")
			return
		}
	}

	fmt.Printf("%s  %s\n", bold("CSA Doctor"), dim(time.Now().Format(time.RFC3339)))

	exe, exeErr := os.Executable()
	if exeErr == nil {
		fmt.Printf("csa-client %s  %s\n\n", version, dim(exe))
	} else {
		fmt.Printf("csa-client %s\n\n", version)
	}

	cfg, cfgErr := loadConfig(configPath())
	if cfgErr == nil && cfg.Shipping != "" {
		// The plugin's own /csa:status reports on this config: every check below is about this client.
		fmt.Printf("%s The Claude Code plugin owns this machine's config (%s), so csa-client is not shipping, on purpose.\n"+
			"  Its report is %s. Run %s only to switch back.\n", okMark(), cfg.Shipping, cyan("/csa:status"), cyan("csa-client setup"))
		return
	}
	sections := []doctorSection{
		doctorConfigSection(cfg, cfgErr),
	}
	// Connectivity and auth need a server to talk to — skip cleanly rather than print a wall of
	// "no server configured" repeats when config itself already failed loudly above.
	if cfg != nil && cfg.Server != "" {
		sections = append(sections, doctorConnectivitySection(cfg))
	}
	// Computed once, shared by Hooks/Shipping/Autostart: a bot or server deployment ships via a
	// long-running watcher instead of Claude Code hooks, on purpose — that shape must not read as
	// "broken" just because it has no hooks and no ~/.csa/ship.log (the watcher logs to its own
	// service manager, e.g. journald, not that file).
	watcher := doctorWatcherHint()
	home, _ := os.UserHomeDir()
	hooksShip := shipHookWired(filepath.Join(home, ".claude", "settings.json"))

	sections = append(sections,
		doctorSection{title: "Claude Code plugin", results: []doctorResult{doctorPluginResult(pluginEnabled(), hooksShip)}},
		doctorHooksSection(watcher),
		doctorAllowlistSection(),
		doctorAutostartSection(watcher, hooksShip),
		doctorShippingSection(watcher, hooksShip),
		doctorProjectsSection(),
	)

	nOK, nWarn, nFail, nTodo := 0, 0, 0, 0
	for _, sec := range sections {
		fmt.Println(bold(sec.title))
		for _, r := range sec.results {
			fmt.Printf("  %s %s\n", r.mark(), r.line)
			switch r.level {
			case dOK:
				nOK++
			case dWarn:
				nWarn++
			case dFail:
				nFail++
			case dTodo:
				nTodo++
			}
		}
		fmt.Println()
	}

	sep := strings.Repeat("=", 60)
	fmt.Println(sep)
	notShipping := ""
	if nTodo > 0 {
		notShipping = ", NOT SHIPPING YET"
	}
	fmt.Printf("SUMMARY: %d ok, %d warning(s), %d failure(s)%s\n", nOK, nWarn, nFail, notShipping)
	fmt.Println(sep)
	if nTodo > 0 {
		fmt.Printf("Nothing ships from this machine until you run %s.\n", notShippingFix)
	}
	switch {
	case nFail > 0 || nWarn > 0:
		fmt.Println(dim("Copy everything above (from \"CSA Doctor\" down) and send it to your CSA admin."))
	case nTodo == 0:
		fmt.Println(dim("Everything checks out. If sessions still aren't showing up server-side, send this report anyway — it rules out the client."))
	}
}

func doctorConfigSection(cfg *config, cfgErr error) doctorSection {
	sec := doctorSection{title: "Config (~/.csa/config.json)"}
	if cfgErr != nil || cfg == nil {
		sec.results = append(sec.results, dFailf("no config found — run `csa-client setup --server <url>`"))
		return sec
	}
	if cfg.Server == "" {
		sec.results = append(sec.results, dFailf("config has no server URL — run `csa-client setup --server <url>`"))
	} else {
		sec.results = append(sec.results, dOKf("server: %s", cfg.Server))
	}
	if !cfg.consentOK() {
		sec.results = append(sec.results, dFailf("no current consent on record (need policy v%d) — run `csa-client setup`", requiredPolicyVersion))
	} else {
		sec.results = append(sec.results, dOKf("consent on record: policy v%d, accepted %s", cfg.Consent.PolicyVersion, cfg.Consent.AcceptedAt))
	}
	if cfg.Identity == "" {
		sec.results = append(sec.results, dWarnf("no identity on record — sessions will attribute to the OS username"))
	} else {
		sec.results = append(sec.results, dOKf("identity: %s", cfg.Identity))
	}
	tok := resolveAuthToken(cfg.AuthToken)
	if tok == "" && cfg.CFID == "" {
		sec.results = append(sec.results, dWarnf("no auth token and no CF-Access credential configured — fine only for CSA_AUTH=off/cloudflare-edge servers"))
	} else if tok != "" {
		sec.results = append(sec.results, dOKf("auth credential present (%d chars)", len(tok)))
	}
	live := claudeEmail()
	switch {
	case live == "":
		sec.results = append(sec.results, dInfof("Claude Code isn't logged in with an email — can't check identity drift"))
	case identityDrift(cfg.Identity, live):
		sec.results = append(sec.results, dFailf("identity drift: consented as %s, Claude Code is now logged in as %s — re-run `csa-client setup`", cfg.Identity, live))
	default:
		sec.results = append(sec.results, dOKf("Claude Code identity matches consent (%s)", live))
	}
	return sec
}

func doctorConnectivitySection(cfg *config) doctorSection {
	sec := doctorSection{title: "Connectivity"}
	server := strings.TrimRight(env("CSA_SERVER", cfg.Server), "/")
	authToken = resolveAuthToken(cfg.AuthToken)
	cfID, cfSecret = cfg.CFID, cfg.CFSecret // a Cloudflare Access front door refuses even /api/health without them

	start := time.Now()
	h, err := checkHealth(server)
	if err != nil {
		sec.results = append(sec.results, dFailf("server unreachable: %v", err))
		return sec // no point probing auth against a server we can't even reach
	}
	sec.results = append(sec.results, dOKf("server reachable (%s, %dms)", server, time.Since(start).Milliseconds()))
	if h.Extraction.Stalled {
		sec.results = append(sec.results, dFailf("the server is not extracting your sessions: %s", h.Extraction.Reason))
	}

	status, err := doctorAuthProbe(server)
	switch {
	case err != nil:
		sec.results = append(sec.results, dFailf("credential check failed: %v", err))
	case status == http.StatusUnauthorized:
		sec.results = append(sec.results, dFailf("credential rejected (HTTP 401) — run `csa-client setup` to sign in again"))
	case status >= 200 && status < 300:
		sec.results = append(sec.results, dOKf("credential accepted (GET /api/team -> HTTP %d)", status))
	default:
		sec.results = append(sec.results, dWarnf("unexpected response from GET /api/team: HTTP %d", status))
	}
	return sec
}

// doctorAuthProbe is a read-only GET /api/team used purely as a credential check: it needs a
// valid token to return 200, and returns exactly the status code so 401 (bad credential) can be
// told apart from any other failure. Deliberately not fetchTeam(), which collapses every non-200
// into one generic error string — doctor needs the real code.
func doctorAuthProbe(server string) (int, error) {
	req, err := http.NewRequest("GET", server+"/api/team", nil)
	if err != nil {
		return 0, err
	}
	addAuthHeaders(req)
	c := &http.Client{Timeout: 5 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	return resp.StatusCode, nil
}

// hookStatus reports, for each of csaHooks()'s events, whether a CSA hook is present in an
// already-parsed settings.json root. Pure and separately testable — see doctor_test.go.
func hookStatus(root map[string]any) map[string]bool {
	present := map[string]bool{}
	hooks, _ := root["hooks"].(map[string]any)
	for _, spec := range csaHooks() {
		found := false
		for _, entry := range arrayAt(hooks, spec.event) {
			em, ok := entry.(map[string]any)
			if !ok {
				continue
			}
			for _, h := range arrayAt(em, "hooks") {
				if hm, ok := h.(map[string]any); ok && isCSAClientHook(hm) {
					found = true
				}
			}
		}
		present[spec.event] = found
	}
	return present
}

// doctorHooksSection reads Claude Code hooks. `watcher` is doctorWatcherHint()'s result: when a
// background watcher is already confirmed running, a machine with zero hooks is a legitimate
// bot/service deployment (ships by polling, not by SessionEnd/SessionStart), not a broken one —
// see a shared bot host's systemd units, none of which use hooks at all.
func doctorHooksSection(watcher string) doctorSection {
	sec := doctorSection{title: "Hooks (~/.claude/settings.json)"}
	home, _ := os.UserHomeDir()
	path := filepath.Join(home, ".claude", "settings.json")
	b, err := os.ReadFile(path)
	if err != nil && watcher != "" {
		sec.results = append(sec.results, dInfof("no %s (%v) — shipping via the watcher instead (%s)", path, err, watcher))
		return sec
	}
	if os.IsNotExist(err) {
		sec.results = append(sec.results, doctorNotShipping())
		return sec
	}
	if err != nil {
		sec.results = append(sec.results, dFailf("can't read %s: %v", path, err))
		return sec
	}
	var root map[string]any
	if err := json.Unmarshal(b, &root); err != nil {
		sec.results = append(sec.results, dFailf("can't parse %s: %v", path, err))
		return sec
	}
	present := hookStatus(root)
	nPresent, nTotal := 0, len(csaHooks())
	for _, spec := range csaHooks() {
		if present[spec.event] {
			nPresent++
		}
	}
	if nPresent == 0 && watcher != "" {
		// Zero hooks + a confirmed-running watcher is a real, working deployment shape (a
		// bot/service ships by polling, not by SessionEnd/SessionStart) — one informational line,
		// not a wall of failures. A PARTIAL install (some but not all hooks) still falls through
		// to the per-hook failures below regardless of the watcher: that's always a real gap.
		sec.results = append(sec.results, dInfof("no hooks wired — shipping via the watcher instead (%s)", watcher))
		return sec
	}
	if nPresent == 0 {
		sec.results = append(sec.results, doctorNotShipping())
		return sec
	}
	for _, spec := range csaHooks() {
		if present[spec.event] {
			sec.results = append(sec.results, dOKf("%s -> %s", spec.event, strings.Join(spec.args, " ")))
		} else {
			sec.results = append(sec.results, dFailf("%s not wired", spec.event))
		}
	}
	switch {
	case nPresent == nTotal:
		// all wired, nothing more to say
	default:
		sec.results = append(sec.results, dWarnf("run %s to wire every hook in one shot", notShippingFix))
	}
	return sec
}

func doctorNotShipping() doctorResult {
	return doctorResult{dTodo, "not shipping: no CSA hooks and no watcher — run " + notShippingFix}
}

func doctorAllowlistSection() doctorSection {
	sec := doctorSection{title: "Allowlist / exclusions"}
	only := loadIgnores(onlyPath())
	if len(only) == 0 {
		sec.results = append(sec.results, dOKf("~/.csa/only: empty — indexing every project folder"))
	} else {
		sec.results = append(sec.results, dOKf("~/.csa/only: %s", strings.Join(only, ", ")))
		if bad := unmatchedOnlyPatterns(only); len(bad) > 0 {
			sec.results = append(sec.results, dFailf("%s matches NO project folder CSA currently sees — this is a total-exclusion trap; fix with `csa-client only pick`", strings.Join(bad, ", ")))
		}
	}
	if ign := loadIgnores(ignorePath()); len(ign) == 0 {
		sec.results = append(sec.results, dOKf("~/.csa/ignore: empty"))
	} else {
		sec.results = append(sec.results, dOKf("~/.csa/ignore: %s", strings.Join(ign, ", ")))
	}
	return sec
}

func doctorAutostartSection(watcher string, hooksShip bool) doctorSection {
	sec := doctorSection{title: "Autostart"}
	switch {
	case watcher != "":
		sec.results = append(sec.results, dOKf("background watcher installed (%s)", watcher))
	case hooksShip:
		sec.results = append(sec.results, dInfof("none needed — the hooks above ship this machine"))
	default:
		sec.results = append(sec.results, dInfof("no launchd/systemd watcher installed"))
	}
	return sec
}

// doctorWatcherHint looks for evidence that a background watcher is installed for THIS
// identity, beyond existingWatcherInstalled()'s user-scoped launchd/systemd check (which setup.go
// also uses, for a different purpose — refusing to double-install hooks on top of a watcher — so
// its behavior stays untouched here). It additionally globs Linux SYSTEM-level unit files
// (/etc/systemd/system/csa-client*.service): a shared/bot machine's units run there, often under a
// different HOME than the one doctor is reading (e.g. a bot host's several csa-client
// units, none of which live under a per-user systemd path).
//
// Best-effort file-presence check, not a live-process check — a stopped-but-installed unit still
// counts, same as existingWatcherInstalled. And on a shared box with several units, a name match
// is NOT proof any specific one ships THIS identity's config (unit files don't declare that) — a
// single match is named directly; multiple matches are reported as ambiguous rather than picking
// one at random and overclaiming it's the right one.
func doctorWatcherHint() string {
	if p := existingWatcherInstalled(); p != "" {
		return p
	}
	matches, err := filepath.Glob("/etc/systemd/system/csa-client*.service")
	if err != nil || len(matches) == 0 {
		return ""
	}
	if len(matches) == 1 {
		return matches[0]
	}
	return fmt.Sprintf("one of %d system units, can't tell which: %s", len(matches), strings.Join(matches, ", "))
}

func doctorShippingSection(watcher string, hooksShip bool) doctorSection {
	sec := doctorSection{title: "Shipping activity (~/.csa/ship.log)"}
	b, err := os.ReadFile(shipLogPath())
	if err != nil || len(b) == 0 {
		// ship.log is written by the hook path only (ship.go: a detached ship's stderr) — a
		// watcher-shipped machine legitimately never touches it, and logs to its own service
		// manager instead (journald, launchd's log). Don't read that as "nothing has shipped".
		switch {
		case watcher != "":
			sec.results = append(sec.results, dInfof("no ~/.csa/ship.log — expected when shipping via a watcher; check its own service log instead (e.g. journalctl for %s)", watcher))
		case hooksShip:
			sec.results = append(sec.results, dInfof("nothing shipped yet — each Claude Code session ships when it ends"))
		default:
			sec.results = append(sec.results, dInfof("nothing has shipped from this machine"))
		}
		return sec
	}
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	const tailN = 500
	if len(lines) > tailN {
		lines = lines[len(lines)-tailN:]
	}
	var lastSent, lastFail string
	failCount := 0
	for _, l := range lines {
		switch {
		case strings.Contains(l, " sent ") && strings.Contains(l, "ok=true"):
			lastSent = l
		case strings.Contains(l, "FAILED"):
			lastFail = l
			failCount++
		}
	}
	if lastSent == "" {
		sec.results = append(sec.results, dWarnf("no successful send in the last %d log line(s)", len(lines)))
	} else {
		sec.results = append(sec.results, dOKf("last successful send: %s", firstField(lastSent)))
	}
	if failCount > 0 {
		sec.results = append(sec.results, dWarnf("%d FAILED line(s) in the last %d log line(s); most recent: %s", failCount, len(lines), strings.TrimSpace(lastFail)))
	}
	return sec
}

// firstField returns a log line's leading "YYYY/MM/DD HH:MM:SS" timestamp (Go's log.Printf
// default prefix) — the whole line is noisy for a one-line summary.
func firstField(line string) string {
	f := strings.Fields(line)
	if len(f) >= 2 {
		return f[0] + " " + f[1]
	}
	return line
}

func doctorProjectsSection() doctorSection {
	folders := projectFolders()
	sec := doctorSection{title: fmt.Sprintf("Project folders CSA sees (%d)", len(folders))}
	if len(folders) == 0 {
		sec.results = append(sec.results, dInfof("none yet — Claude Code has not saved a session under ~/.claude/projects (or CSA_ROOT)"))
		return sec
	}
	for _, f := range folders {
		sec.results = append(sec.results, dInfof("%s (%d session%s)", f.name, f.count, plural(f.count)))
	}
	return sec
}

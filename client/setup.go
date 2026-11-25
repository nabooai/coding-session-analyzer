// `csa-client setup`: consent in the terminal, then a browser click mints a per-user token, then
// the Claude Code hooks are wired. Consent comes BEFORE the browser step so that declining leaves
// no credential behind on the server. The only ways past consent are typing "yes" or --yes.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// setupPollInterval/setupPollTimeout: how long we wait for the human to click Authorize. The
// timeout tracks the server's nonce TTL (analyzer/cliauth.py TTL_SECONDS = 15min) — polling past
// the point where the nonce is dead only wastes the user's time.
const (
	setupPollInterval = 2 * time.Second
	setupPollTimeout  = 15 * time.Minute
)

type authReply struct {
	OK        bool         `json:"ok"`
	Nonce     string       `json:"nonce"`
	Code      string       `json:"code"`
	URL       string       `json:"url"`
	ExpiresIn int          `json:"expires_in"`
	Status    string       `json:"status"`
	Token     string       `json:"token"`
	Email     string       `json:"email"`
	Policy    serverPolicy `json:"policy"` // the workspace the token belongs to: always knows archive_days
	Error     string       `json:"error"`
}

// postJSON: a short-timeout POST for the handshake. Deliberately NOT the shared `httpc` (180s,
// sized for ingest): a user waiting on their install must learn in seconds that a server is
// unreachable, not after three minutes of silence. Proxies work via Go's default transport
// (ProxyFromEnvironment) — HTTP_PROXY/HTTPS_PROXY are honored with no extra code.
func postJSON(url string, body any, out *authReply) error {
	b, _ := json.Marshal(body)
	req, err := http.NewRequest("POST", url, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 20 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("HTTP %s", resp.Status)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// openBrowser: best-effort. Never fatal — the URL is always printed too, so a headless box, an SSH
// session, or a missing opener costs nothing but a copy-paste.
func openBrowser(url string) bool {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	cmd.Stdout, cmd.Stderr = nil, nil
	return cmd.Start() == nil
}

func hostname() string {
	h, err := os.Hostname()
	if err != nil || strings.TrimSpace(h) == "" {
		return "this computer"
	}
	return strings.TrimSuffix(strings.TrimSpace(h), ".local")
}

// existingWatcherInstalled: is the launchd/systemd watcher from the OLD install flow still around?
// Hooks + watcher both shipping means every session is ingested twice — double the extraction spend
// server-side (/api/ingest re-extracts on every POST), double the noise. Detect and refuse rather
// than quietly doing it.
func existingWatcherInstalled() string {
	home, _ := os.UserHomeDir()
	if agents := csaAgents(home); len(agents) > 0 {
		return agents[0].path
	}
	candidates := []string{
		filepath.Join(home, ".config", "systemd", "user", "csa-client.service"),
	}
	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

func runSetup(args []string) {
	fs := flag.NewFlagSet("setup", flag.ExitOnError)
	serverF := fs.String("server", "", "CSA server URL (the installer bakes this in)")
	yesF := fs.Bool("yes", false, "accept the consent notice non-interactively (it is still printed)")
	backfillF := fs.Bool("backfill", false, "also ship your EXISTING sessions once (default: only new sessions)")
	noBrowserF := fs.Bool("no-browser", false, "print the authorize URL instead of opening a browser (SSH/headless)")
	fs.Parse(args)
	refuseWhilePluginEnabled()

	// A previous config (an old `init` install, or a re-run of `setup`) is CARRIED FORWARD, never
	// silently reset: an existing user who chose redaction `privacy` must not be downgraded to
	// `secrets` — shipping MORE than before — just because they ran the new installer. Same for CF
	// creds and delegated mode. Nil is fine (fresh install).
	prev, _ := loadConfig(configPath())

	server := setupServer(*serverF, prev)
	if server == "" {
		fatalf(exitConfig, "no server given. Run:  csa-client setup --server https://your-csa-server")
	}
	normalized, err := normalizeServer(server)
	if err != nil {
		fatalf(exitConfig, "invalid --server %q: %v", server, err)
	}
	server = normalized

	// Reachability BEFORE anything is printed about consent: "unreachable" must not look like
	// "broken", and the user should not read a privacy notice for a server that isn't there.
	h, err := checkHealth(server)
	if err != nil {
		fatalf(exitConfig, "%s", unreachableMessage(server, err))
	}

	// Identity is bound in the BROWSER (that's what the token attributes to). claudeEmail() is a
	// strong pre-fill when the user is logged into Claude Code; otherwise leave it "" — the consent
	// notice then names "the email you authorize with" rather than a misleading OS username.
	identity := claudeEmail()

	// Redaction mode: start from the previous config (never downgrade an existing user), then let the
	// org policy floor RAISE it — the notice must state the mode actually used. `/api/policy` is
	// public so this pre-consent fetch works even before we hold a token.
	mode := "secrets"
	if prev != nil && redactLevel(prev.redactionMode()) > redactLevel(mode) {
		mode = prev.redactionMode()
	}
	// A previous token names the workspace, so a re-run sees its own window. A first run on a
	// server that hosts several workspaces gets nil: the admin sets it, sign-in discloses it.
	var archiveDays *int
	prevToken := ""
	if prev != nil {
		prevToken = prev.AuthToken
	}
	if p, err := fetchPolicy(server, prevToken); err == nil {
		if p.RedactionMin != "" && redactLevel(p.RedactionMin) > redactLevel(mode) {
			mode = p.RedactionMin
		}
		archiveDays = p.ArchiveDays // disclosed in the notice below, and recorded in the consent
	}

	// Backfill: an EXPLICIT --backfill flag decides; otherwise inherit what this machine already
	// chose. Same precedence `init` implements via fs.Visit, and it was missing here — so a developer
	// who set up WITH history and re-ran `setup` for any unrelated reason (a new token, a moved
	// server, the retention notice telling them to) silently lost that consent. The next admin fetch
	// then reported a refusal against their name for a decision they never made.
	//
	// Resolved HERE, above the notice, because the notice states which way it went. A consent screen
	// that describes a different choice from the one being written is the one lie this cannot afford.
	backfill := *backfillF
	explicitBackfill := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "backfill" {
			explicitBackfill = true
		}
	})
	if !explicitBackfill && prev != nil {
		backfill = prev.Backfill
	}

	// ---- CONSENT GATE — before any credential exists anywhere ----------------------------------
	printConsentNotice(server, identity, mode, backfill, archiveDays)
	method := "interactive-tty"
	if *yesF {
		method = "flag:--yes"
		fmt.Println("Consent accepted via --yes flag.")
	} else {
		if !stdinIsTTY() {
			fatalf(exitConfig, `stdin is not a terminal, so the consent prompt cannot run.

Run this in a terminal:  csa-client setup --server %s
(or pass --yes to accept the notice above non-interactively).`, server)
		}
		fmt.Print(`Type "yes" to agree (anything else declines): `)
		// stdinIsTTY() uses ModeCharDevice, which /dev/null ALSO satisfies (the classic macOS
		// gotcha) — so a Docker `RUN` / `ssh host '… </dev/null'` reaches here despite having no
		// real terminal, and readLine gets EOF. Treat EOF as an explicit decline with the actionable
		// fix, instead of readLine's generic "aborting init" that names the wrong command.
		ans, eof := readLineEOF()
		if eof {
			fatalf(exitConfig, `no terminal input available for the consent prompt.

For an automated/non-interactive setup, pass --yes:
  csa-client setup --server %s --yes --no-browser`, server)
		}
		if ans != "yes" {
			fmt.Println("Declined. Nothing was written, nothing was authorized, nothing will be shipped.")
			os.Exit(0)
		}
	}

	// ---- browser handshake --------------------------------------------------------------------
	var start authReply
	if err := postJSON(server+"/api/cli-auth/new", map[string]string{"hostname": hostname()}, &start); err != nil {
		fatalf(exitConfig, "could not start authorization with %s: %v", server, err)
	}
	if !start.OK || start.Nonce == "" || start.Code == "" || start.URL == "" {
		fatalf(exitConfig, "%s did not start an authorization (is it running a recent CSA?): %s",
			server, start.Error)
	}

	fmt.Printf("\n  Verification code: %s\n", bold(start.Code))
	fmt.Printf("  %s\n\n", dim("Type this code on the authorize page — it proves the page is yours."))
	if *noBrowserF || env("CSA_SETUP_NO_BROWSER", "") != "" {
		fmt.Printf("→ Open this URL to authorize:\n  %s\n", cyan(start.URL))
	} else if openBrowser(start.URL) {
		fmt.Printf("→ Opened %s in your browser.\n", cyan(start.URL))
	} else {
		fmt.Printf("→ Open this URL to authorize (could not launch a browser):\n  %s\n", cyan(start.URL))
	}
	fmt.Printf("  %s", dim("Waiting for you to confirm..."))

	creds, err := pollForApproval(server, start.Nonce)
	fmt.Println()
	if err != nil {
		fatalf(exitConfig, "%v\n\nNothing was written. Re-run:  csa-client setup --server %s", err, server)
	}

	// The server's verified email wins over the local guess: the token is bound to it, so config
	// must agree or every ship would be attributed to a name the server overrides anyway.
	if creds.Email != "" {
		identity = creds.Email
	}
	if creds.Policy.RedactionMin != "" && redactLevel(creds.Policy.RedactionMin) > redactLevel(mode) {
		mode = creds.Policy.RedactionMin
	}
	fmt.Printf("%s Authorized as %s\n", okMark(), bold(identity))
	fmt.Printf("%s Fetched org policy %s\n", okMark(), dim("(redaction floor: "+mode+")"))

	// ---- COMMIT POINT — the first and only write ----------------------------------------------
	// Carry forward the previous install's CF-Access creds and delegated mode (an upgrading
	// CF-fronted or bot host must not lose them); the per-user token replaces the old shared token.
	cfg := &config{
		Server: server, Identity: identity, AuthToken: creds.Token,
		Backfill: backfill, Redaction: &redactPolicy{Mode: mode},
		Consent: &consentRecord{
			PolicyVersion:        requiredPolicyVersion,
			AcceptedAt:           time.Now().Format(time.RFC3339),
			Method:               method,
			IdentityAtConsent:    identity,
			ArchiveDaysAtConsent: daysOrNone(archiveDays), // what the notice above actually said
		},
		ServerArchiveDays: daysOrNone(creds.Policy.ArchiveDays), // the workspace's own, from the claim
	}
	if prev != nil {
		cfg.CFID, cfg.CFSecret, cfg.Delegated = prev.CFID, prev.CFSecret, prev.Delegated
	}
	cfg.SweepFrom = sweepFloor(*backfillF, prev)
	retirePluginCredential(prev)
	if err := saveConfig(configPath(), cfg); err != nil {
		fatalf(1, "writing %s: %v", configPath(), err)
	}
	fmt.Printf("%s wrote %s %s\n", okMark(), cyan(configPath()), dim("(mode 0600)"))
	if cfg.retentionExceedsConsent() {
		fmt.Printf(`%s This workspace keeps transcripts %s; you agreed to %s.
    Nothing ships until you re-run  csa-client setup --server %s  and accept that
    (the notice then shows the number).
`, warnMark(), retentionWords(cfg.ServerArchiveDays), retentionWords(cfg.archiveDaysConsented()), server)
	}

	bin, settings := wireHooks()
	mcpRegistered := registerMCP(cfg, h, true)
	if mcpRegistered {
		fmt.Printf("%s MCP server \"csa\" registered with Claude Code\n", okMark())
	}

	printSetupPaths(bin, settings, mcpRegistered)
	fmt.Printf("\n%s All set — csa-client is connected to %s.\n", okMark(), cyan(server))
	fmt.Printf("  %s\n", dim("Scope what gets indexed:  csa-client only pick"))
	fmt.Printf("  %s\n", dim("Undo everything:          csa-client uninstall"))
}

// sweepFloor is the sweep_from a consent writes: "" when history ships, else the instant
// `ship --sweep` ignores older transcripts from. A re-run never moves an existing floor forward,
// since every transcript queued but not yet shipped would silently drop out of the sweep.
func sweepFloor(history bool, prev *config) string {
	if history {
		return ""
	}
	now := time.Now().Format(time.RFC3339)
	if prev == nil || prev.Consent == nil || prev.Shipping != "" { // the plugin's consent is not this one's
		return now
	}
	if prev.SweepFrom == "" {
		if prev.Backfill {
			return "" // history was already included
		}
		return now
	}
	old, err := time.Parse(time.RFC3339, prev.SweepFrom)
	if n, err2 := time.Parse(time.RFC3339, now); err == nil && err2 == nil && old.Before(n) {
		return prev.SweepFrom
	}
	return now
}

// wireHooks installs every CSA hook and retires a login watcher, which would ship each session twice.
func wireHooks() (bin, settings string) {
	home, _ := os.UserHomeDir()
	if existingWatcherInstalled() != "" {
		fmt.Printf("%s migrating off the login watcher (this machine ships through hooks)\n", okMark())
		stopAutostart(home)
	}
	bin, err := ensureCSAClientBinary()
	if err != nil {
		fatalf(1, "installing the hook binary: %v", err)
	}
	settings = filepath.Join(home, ".claude", "settings.json")
	if err := installHooksAt(settings, bin, csaHooks()); err != nil {
		fatalf(1, "installing Claude Code hooks in %s: %v", settings, err)
	}
	fmt.Printf("%s Claude Code hooks installed in %s\n", okMark(), cyan(settings))
	fmt.Printf("    %s\n", dim("recall on prompt (UserPromptSubmit), ship on end (SessionEnd), catch-up (SessionStart)"))
	return bin, settings
}

// printSetupPaths: every path setup touched, Poco-style. This list IS the privacy UX — a user who
// reads nothing else can diff exactly these against their disk, so it must be COMPLETE: the MCP
// registration mutates ~/.claude.json, which is easy to omit and would make the list a quiet lie.
func printSetupPaths(bin, settings string, mcpRegistered bool) {
	home, _ := os.UserHomeDir()
	fmt.Printf("\n%s\n", bold("Everything this wrote:"))
	for _, p := range []string{configPath(), bin, settings} {
		fmt.Printf("  %s\n", cyan(p))
	}
	fmt.Printf("  %s %s\n", cyan(filepath.Join(filepath.Dir(configPath()), "shipped")),
		dim("(created as sessions ship — one marker per session)"))
	if mcpRegistered {
		fmt.Printf("  %s %s\n", cyan(filepath.Join(home, ".claude.json")),
			dim("(the CSA MCP server registration)"))
	}
}

// setupServer is the server a re-run connects to: --server or CSA_SERVER, else the one already in
// config, so re-authorizing a machine never asks for a URL it already has.
func setupServer(flagVal string, prev *config) string {
	server := env("CSA_SERVER", flagVal)
	if server == "" && prev != nil {
		server = prev.Server
	}
	return strings.TrimRight(server, "/")
}

// pollForApproval waits for the Authorize click. The nonce is single-use server-side, so the
// credentials arrive exactly once; anything else (expired, never clicked) ends with an actionable
// message rather than a hang.
func pollForApproval(server, nonce string) (*authReply, error) {
	deadline := time.Now().Add(setupPollTimeout)
	client := &http.Client{Timeout: 20 * time.Second}
	for time.Now().Before(deadline) {
		time.Sleep(setupPollInterval)
		resp, err := client.Get(server + "/api/cli-auth/" + nonce)
		if err != nil {
			continue // transient (laptop asleep, wifi blip) — keep waiting, the nonce is still alive
		}
		var r authReply
		derr := json.NewDecoder(resp.Body).Decode(&r)
		resp.Body.Close()
		if derr != nil {
			continue
		}
		if r.Status == "approved" && r.Token != "" {
			return &r, nil
		}
		fmt.Print(dim("."))
	}
	return nil, fmt.Errorf("timed out after %s waiting for the browser authorization.\n"+
		"The code expires 15 minutes after it is issued — if you were reading the page, that is\n"+
		"all this means", setupPollTimeout)
}

// serverPolicy is what a server discloses about itself BEFORE anyone consents: how much it strips,
// and how long it keeps what is left.
type serverPolicy struct {
	RedactionMin string `json:"redaction_min"`
	ArchiveDays  *int   `json:"archive_days"` // 0 = none; keepForever = until deleted; nil = not known before sign-in
}

// fetchPolicy reads the workspace's redaction floor and retention window. Best-effort by design: a
// server that predates /api/policy, or one that gates it, must not break onboarding, since the floor
// is enforced server-side on every ingest regardless, so the worst case is that the client strips
// less than the org's minimum locally and the server strips the rest.
//
// `token`, when this machine already holds one, names the workspace; without it a server that hosts
// several answers `archive_days: null`, decoded as nil.
func fetchPolicy(server, token string) (serverPolicy, error) {
	req, err := http.NewRequest("GET", server+"/api/policy", nil)
	if err != nil {
		return serverPolicy{}, err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return serverPolicy{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return serverPolicy{}, fmt.Errorf("HTTP %s", resp.Status)
	}
	var p serverPolicy
	if err := json.NewDecoder(resp.Body).Decode(&p); err != nil {
		return serverPolicy{}, err
	}
	if _, ok := redactLevels[p.RedactionMin]; !ok {
		return serverPolicy{}, fmt.Errorf("unknown redaction_min %q", p.RedactionMin)
	}
	if p.ArchiveDays != nil && *p.ArchiveDays < 0 {
		*p.ArchiveDays = keepForever
	}
	return p, nil
}

// `csa-client init` — consent + setup wizard for a shared-token server — and `install`/`uninstall`.
// The consent gate is a trust boundary: nothing touches disk before the user accepts (the
// config write is the commit point), and nothing ships without the record this wizard writes.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
)

var stdinR = bufio.NewReader(os.Stdin)

// stdinIsTTY: interactive prompts are only allowed when stdin is a character device.
func stdinIsTTY() bool {
	return isTerminal(os.Stdin)
}

func isTerminal(f *os.File) bool {
	fi, err := f.Stat()
	return err == nil && fi.Mode()&os.ModeCharDevice != 0
}

// readLine aborts on EOF: /dev/null passes the char-device check, and a wizard that
// loops (or silently consents) on closed stdin is worse than one that refuses.
func readLine() string {
	line, err := stdinR.ReadString('\n')
	if err != nil && line == "" {
		fatalf(exitConfig, "\nstdin closed while waiting for input — aborting init")
	}
	return strings.TrimSpace(line)
}

// readLineEOF is readLine without the hardcoded "aborting init" fatal: it returns (line, eof) so a
// caller can give its OWN message on closed/empty stdin — needed by `setup`, where the fix is
// "pass --yes", not "run init". eof is true when stdin closed before any input (e.g. /dev/null,
// which stdinIsTTY can't detect).
func readLineEOF() (string, bool) {
	line, err := stdinR.ReadString('\n')
	if err != nil && line == "" {
		return "", true
	}
	return strings.TrimSpace(line), false
}

// prompt asks for a value; empty input returns def (shown in brackets when non-empty).
func prompt(label, def string) string {
	if def != "" {
		fmt.Printf("%s %s: ", label, dim("["+def+"]"))
	} else {
		fmt.Printf("%s: ", label)
	}
	if v := readLine(); v != "" {
		return v
	}
	return def
}

// confirm asks a yes/no question; plain Enter means yes.
func confirm(q string) bool {
	return confirmDefault(q, true)
}

// confirmDefault asks a yes/no question; plain Enter takes def (shown as the capital letter).
func confirmDefault(q string, def bool) bool {
	hint := "[Y/n]"
	if !def {
		hint = "[y/N]"
	}
	fmt.Printf("%s %s ", bold(q), dim(hint))
	switch strings.ToLower(readLine()) {
	case "":
		return def
	case "y", "yes":
		return true
	default:
		return false
	}
}

func fatalf(code int, format string, a ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", a...)
	os.Exit(code)
}

func normalizeServer(s string) (string, error) {
	s = strings.TrimSpace(s)
	u, err := url.Parse(s)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", fmt.Errorf("server URL must look like http(s)://host[:port] — got %q", s)
	}
	return strings.TrimRight(s, "/"), nil
}

// redactModeDesc: one-line "keeps vs strips" summary per mode, for the consent notice, the
// init prompt, and `csa-client redact`. Kept in one place so all three stay consistent.
func redactModeDesc(mode string) string {
	switch mode {
	case "privacy":
		return "privacy: secrets, plus emails, connection strings, bearer tokens, internal hostnames"
	case "strict":
		return "strict: privacy, plus your ~/.csa/redact.deny terms (names, codenames, customer hosts)"
	default:
		return "secrets: API keys, tokens, JWTs, private keys, password=... values. Emails, names, hostnames kept"
	}
}

// redactModeStripKeep returns the (stripped, kept) phrasing for the consent notice at a mode.
func redactModeStripKeep(mode string) (stripped, kept string) {
	switch mode {
	case "privacy":
		return "API keys, tokens, JWTs, private keys, credential assignments, AND emails, connection strings, bearer tokens, internal-TLD hostnames",
			"your prompts, code, and file paths"
	case "strict":
		return "everything privacy strips, AND every term in your ~/.csa/redact.deny (names, codenames, hosts)",
			"your prompts, code, and file paths, minus anything on your deny list"
	default:
		return "API keys, tokens, JWTs, private keys, the value in password=... and similar assignments",
			"your prompts, code, file paths, emails, names, hostnames"
	}
}

// printConsentNotice is the full, honest disclosure — shown before anything is written,
// including under --yes. Keep it in sync with requiredPolicyVersion: if what ships changes,
// bump the version so stale consents void and the watcher refuses until re-accept. The redaction
// mode does NOT bump the version: secrets is the default and every other mode strips strictly more.
//
// `backfill` decides ONE bullet, and it must tell the truth: `init` discloses the default-on
// backfill and then asks the question immediately below the notice, so it passes true. `setup` has
// no such prompt and defaults history OFF, so it passes its actual decision. A notice that claimed
// history ships when it doesn't (or vice versa) is the one lie this product cannot afford.
// `archiveDays` is the OTHER half of the honesty: how long the workspace keeps the redacted
// transcript. nil means the server could not say before sign-in, and the bullet says so. What is
// printed here is what the consent records (config.retentionExceedsConsent).
func printConsentNotice(server, identity, mode string, backfill bool, archiveDays *int) {
	stripped, kept := redactModeStripKeep(mode)
	history := `  * Your EXISTING sessions ship once too (backfill, oldest first, paced).
    Opt out with --backfill=false, or at the question that follows when
    running interactively; after that, new/changed sessions only.`
	if !backfill {
		history = `  * Your EXISTING sessions stay on this machine. Only sessions you start from
    now on ship. To include your history, re-run with --backfill.`
	}
	retention := `  * How long the server keeps the redacted transcript is set by your workspace
    admin and shown once you have signed in. If it keeps any, nothing ships
    until you re-run setup and accept that.`
	if archiveDays != nil {
		switch {
		case *archiveDays == keepForever:
			retention = `  * The server KEEPS the redacted transcript until you or a workspace admin
    delete it, readable by you and by a workspace admin. Facts extracted from
    it remain.`
		case *archiveDays > 0:
			retention = fmt.Sprintf(`  * The server KEEPS the redacted transcript for %d days, readable by you and
    by a workspace admin, then deletes it. Facts extracted from it remain.`, *archiveDays)
		default:
			retention = `  * The server keeps NO copy of the transcript itself. It extracts facts from
    your session and deletes the text.`
		}
	}
	// `init` always resolves a real identity before consent, so it names it. `setup` learns the
	// identity in the browser (the authoritative binding), so if it has no confident local guess it
	// passes "" — and the notice must not name a placeholder OS username the run will then override.
	attribution := bold(identity)
	if identity == "" {
		attribution = "the email you authorize with in your browser (next step)"
	}
	fmt.Printf(`
%s

  * Ships FULL transcripts of your Claude Code sessions (~/.claude/projects)
    to %s as they end. Teammates see the facts and quotes drawn from them; you and workspace admins can read a kept transcript.
  * Every project folder ships unless you narrow it with csa-client only pick
    or csa-client ignore '*repo-name*' (not retroactive).
  * Redaction level: %s (%s).
  * Stripped before sending (pattern-based): %s.
  * KEPT on purpose: %s.
  * Sessions are attributed to you: %s.
%s
%s
  * Stop any time with csa-client uninstall. Un-ship a session that already
    reached the server with csa-client forget <session-id>.

`, bold(fmt.Sprintf("csa-client: read before agreeing (policy v%d)", requiredPolicyVersion)),
		cyan(server), bold(mode), redactModeDesc(mode), stripped, kept, attribution, retention, history)
}

// promptRedactMode asks which redaction level to ship at. Default secrets (the unchanged
// shipped-data promise). Re-asks until a known mode is given.
func promptRedactMode(def string) string {
	fmt.Printf(`
%s

  %s  strip API keys, tokens, JWTs, private keys, password=... values (emails, names, hostnames kept)
  %s  also strip emails, connection strings, bearer tokens, internal-TLD hostnames
  %s   privacy, plus your own deny list (%s): names, codenames, customer hosts

`, bold("Redaction level: what gets stripped before a session leaves your machine"),
		bold("secrets"), bold("privacy"), bold("strict"), cyan("~/.csa/redact.deny"))
	for {
		v := strings.ToLower(prompt("Redaction level (secrets/privacy/strict)", def))
		if _, ok := redactLevels[v]; ok {
			return v
		}
		fmt.Println("Please answer secrets, privacy, or strict.")
	}
}

func runInit(args []string) {
	fs := flag.NewFlagSet("init", flag.ExitOnError)
	serverF := fs.String("server", "", "server URL (http(s)://host:port); default $CSA_SERVER")
	emailF := fs.String("email", "", "identity to attribute sessions to (default: ~/.claude.json email)")
	tokenF := fs.String("token", "", "shared bearer secret for token-auth servers (default: $CSA_AUTH_TOKEN); sent as Authorization: Bearer on every request")
	onlyF := fs.String("only", "", "index ONLY project folders matching these comma-separated globs (allowlist; everything else is skipped)")
	cfIDF := fs.String("cf-id", "", "Cloudflare Access client id (optional)")
	cfSecretF := fs.String("cf-secret", "", "Cloudflare Access client secret (optional)")
	yesF := fs.Bool("yes", false, "accept the consent notice non-interactively (it is still printed)")
	installF := fs.Bool("install", false, "ship through a login watcher instead of the Claude Code hooks (bot/CI hosts)")
	ambientF := fs.Bool("ambient", false, "with --install --yes: also wire the recall hook (the hooks path always does)")
	backfillF := fs.Bool("backfill", true, "also ship existing sessions once (chunked, oldest first); --backfill=false to skip")
	delegatedF := fs.Bool("delegated", false, "bot/CI host mode: honor per-session .owner sidecars for attribution (never set this on a personal machine)")
	redactModeF := fs.String("redact-mode", "", "redaction level: secrets|privacy|strict (default secrets; with --yes this is how you pick)")
	fs.Parse(args)
	refuseWhilePluginEnabled()

	tty := stdinIsTTY()
	if !tty && !*yesF {
		fatalf(exitConfig, "stdin is not a terminal, so the consent prompt cannot run.\n"+
			"Interactive: run `csa-client init` in a terminal.\n"+
			"Non-interactive: csa-client init --yes --server https://your-csa-server --token SECRET [--email you@team.com] [--only '*repo*'] [--backfill=false]")
	}

	prev, _ := loadConfig(configPath()) // previous answers become offered defaults on re-init
	prevServer, prevIdentity := "", ""
	if prev != nil {
		prevServer, prevIdentity = prev.Server, prev.Identity
	}

	// CF Access creds before anything talks to the server (a Cloudflare-fronted health
	// check 403s without them). Flags win, else the previous config's (idempotent re-init).
	cfID, cfSecret = *cfIDF, *cfSecretF
	if cfID == "" && prev != nil {
		cfID, cfSecret = prev.CFID, prev.CFSecret
	}

	// Auth token likewise, so the smoke-test ingest carries it. Precedence: --token flag >
	// $CSA_AUTH_TOKEN > previous config.
	authToken = *tokenF
	if authToken == "" {
		authToken = strings.TrimSpace(os.Getenv("CSA_AUTH_TOKEN"))
	}
	if authToken == "" && prev != nil {
		authToken = prev.AuthToken
	}

	// Server URL first so the consent screen names the real destination, not a placeholder —
	// and CHECKED the moment it's supplied, so a dead URL fails before consent, not after.
	// No default is ever silently applied: flag > $CSA_SERVER > interactive prompt (with retry).
	explicit := *serverF != "" || os.Getenv("CSA_SERVER") != ""
	server := *serverF
	if server == "" {
		server = os.Getenv("CSA_SERVER")
	}
	if server == "" && !tty {
		fatalf(exitConfig, "no server URL. Pass --server https://your-csa-server (or set CSA_SERVER).")
	}
	var h *healthReply
	for {
		if server == "" {
			s, err := normalizeServer(prompt("Server URL (e.g. https://csa.example.com)", prevServer))
			if err != nil {
				fmt.Println(err)
				continue
			}
			server = s
		} else {
			s, err := normalizeServer(server)
			if err != nil {
				fatalf(exitConfig, "%v", err)
			}
			server = s
		}
		fmt.Printf("Checking %s ... ", cyan(server))
		stop := spin()
		t0 := time.Now()
		hh, err := checkHealth(server)
		stop()
		if err == nil {
			if hh.Sessions > 0 {
				fmt.Printf("%s reachable (%s) · %d sessions indexed\n", okMark(), fmtLatency(time.Since(t0)), hh.Sessions)
			} else {
				fmt.Printf("%s reachable (%s)\n", okMark(), fmtLatency(time.Since(t0)))
			}
			h = hh
			break
		}
		fmt.Printf("%s %v\n", failMark(), err)
		if !tty || explicit {
			os.Exit(1) // an explicitly-given URL that fails is an error, not a conversation
		}
		prevServer, server = server, "" // offer the failed URL as the editable default
	}

	// Identity BEFORE consent: the notice says "attributed to you: X" — X must be the
	// confirmed truth, not a fallback guess the user only gets to correct after agreeing.
	identity := *emailF
	if identity == "" {
		if e := claudeEmail(); e != "" {
			if *yesF || confirm(fmt.Sprintf("Sessions will be attributed to %s — correct?", e)) {
				identity = e
			}
		}
		if identity == "" {
			if !tty {
				fatalf(exitConfig, "no account email found in ~/.claude.json — pass --email you@team.com")
			}
			// Normally auto-filled from the Claude Code login; say WHY we're asking when it isn't.
			fmt.Println(dim("Couldn't auto-detect your email (no Claude Code login in ~/.claude.json on this machine)."))
			for identity == "" {
				identity = prompt("Email to attribute your sessions to", prevIdentity)
			}
		}
	}
	if !strings.Contains(identity, "@") {
		fmt.Printf("note: %q is not an email; sessions will be attributed to it verbatim\n", identity)
	}

	// Redaction level BEFORE consent: the notice must state what actually gets stripped, so the
	// mode is decided first. Precedence mirrors backfill: explicit --redact-mode > previous config
	// > "secrets" default. Interactively, whichever wins is only the OFFERED default; --yes takes
	// it as final (a sensible default of secrets when nothing is specified).
	mode := *redactModeF
	if mode == "" && prev != nil {
		mode = prev.redactionMode()
	}
	if mode == "" {
		mode = "secrets"
	}
	if _, ok := redactLevels[mode]; !ok {
		fatalf(exitConfig, "unknown --redact-mode %q — use secrets, privacy, or strict", mode)
	}
	if !*yesF {
		mode = promptRedactMode(mode)
	}

	// CONSENT GATE — the only ways past are typing exactly "yes", or the explicit --yes flag
	// (which still shows the full notice). Declining writes nothing and exits 0.
	// The retention window comes from the server, best-effort: an unreachable server reads as
	// unknown, and a known 0 is the truth about a server that keeps no transcripts.
	var archiveDays *int
	if pol, err := fetchPolicy(server, authToken); err == nil {
		archiveDays = pol.ArchiveDays
	}
	printConsentNotice(server, identity, mode, true, archiveDays) // init: history ships by default, and asks below
	method := "interactive-tty"
	if *yesF {
		method = "flag:--yes"
		fmt.Println("Consent accepted via --yes flag.")
	} else {
		fmt.Print(`Type "yes" to agree (anything else declines): `)
		if readLine() != "yes" {
			fmt.Println("Declined. Nothing was written; nothing will be shipped.")
			os.Exit(0)
		}
	}

	// Backfill choice — part of WHAT ships (the notice above discloses it), so it's decided
	// with consent, before the commit point. Default yes: a team memory that starts empty
	// answers nothing; history is where the gold is. Precedence: an explicit --backfill flag
	// beats the previous config's answer beats the yes default — and interactively, whatever
	// wins is only the OFFERED default, never a silent override of the user's answer.
	explicitBackfill := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "backfill" {
			explicitBackfill = true
		}
	})
	backfill := *backfillF
	if !explicitBackfill && prev != nil {
		backfill = prev.Backfill
	}
	if !*yesF {
		backfill = confirmDefault("Also index your existing sessions? (one-time, oldest first, a few per scan)", backfill)
	}
	// A changed server is a new destination: reset the backfill ledger so history ships there too.
	// Same-server re-init keeps it, so nothing uploads twice.
	if backfill && prev != nil && prev.Server != "" && prev.Server != server {
		if err := os.Remove(backfilledPath()); err == nil {
			fmt.Println(dim("Server changed — backfill ledger reset; existing sessions will ship to the new server."))
		}
	}

	// Delegated mode is deliberately flag-only, no interactive question: it exists for bot/CI
	// hosts set up by an operator, and a personal user should never be nudged toward it.
	// Precedence mirrors backfill: explicit flag > previous config > off.
	delegated := *delegatedF
	explicitDelegated := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "delegated" {
			explicitDelegated = true
		}
	})
	if !explicitDelegated && prev != nil {
		delegated = prev.Delegated
	}

	// COMMIT POINT — the first and only write. Everything before could exit clean.
	cfg := &config{
		Server: server, Identity: identity, AuthToken: authToken, CFID: cfID, CFSecret: cfSecret, Backfill: backfill, Delegated: delegated,
		Redaction: &redactPolicy{Mode: mode},
		Consent: &consentRecord{
			PolicyVersion:        requiredPolicyVersion,
			AcceptedAt:           time.Now().Format(time.RFC3339),
			Method:               method,
			IdentityAtConsent:    identity,
			ArchiveDaysAtConsent: daysOrNone(archiveDays), // what the notice above actually said
		},
		ServerArchiveDays: daysOrNone(archiveDays),
		SweepFrom:         sweepFloor(backfill, prev),
	}
	retirePluginCredential(prev)
	if err := saveConfig(configPath(), cfg); err != nil {
		fatalf(1, "writing %s: %v", configPath(), err)
	}
	fmt.Printf("%s wrote %s %s\n", okMark(), cyan(configPath()), dim("(mode 0600)"))

	// --only: seed the indexed-folders allowlist noninteractively (same ~/.csa/only file the
	// `only` subcommand writes), so a scripted install can scope what ships in one command.
	if *onlyF != "" {
		applyOnlyAllowlist(*onlyF)
	} else if tty && !*yesF {
		// The friendly form of --only: let a fresh user scope what ships at setup, without
		// learning glob syntax or munged folder names. Default indexes everything (unchanged).
		if !confirmDefault("Index all your project folders? (No lets you pick specific ones)", true) {
			runOnlyPick()
		}
	}

	connected := registerMCP(cfg, h, *yesF)

	fmt.Print("Testing the send path (dry run — nothing gets indexed) ... ")
	stopSmoke := spin()
	smokeErr := smokeTest(cfg)
	stopSmoke()
	if smokeErr != nil {
		fmt.Printf("%s %v\n  %s\n", warnMark(), smokeErr,
			dim("non-fatal: config is written; the watcher retries by design"))
	} else {
		fmt.Printf("%s server accepted it\n", okMark())
	}

	// How this machine ships: the hooks, unless --install asks for the login watcher or a system
	// watcher unit already ships it (both at once would ship every session twice).
	shipsVia := "hooks"
	if *installF {
		installAmbientIf(*yesF, *ambientF)
		shipsVia = ""
		if installed, err := installAutostart(); err != nil {
			fmt.Println("Autostart install failed (run 'csa-client install' later):", err)
		} else if installed {
			shipsVia = "login watcher"
		}
	} else if unit := doctorWatcherHint(); unit != "" && existingWatcherInstalled() == "" {
		shipsVia = unit
	} else {
		wireHooks()
	}
	home, _ := os.UserHomeDir()
	if shipHookWired(filepath.Join(home, ".claude", "settings.json")) {
		shipsVia = "hooks"
	}
	epilogue(cfg, shipsVia, h.DemoQuery)

	// Who else is already on the team — live if reachable, else the last cached roster (so a
	// dead server / no WiFi still shows something). Silent only when truly cold-offline.
	if team, live, at, _ := teamRoster(cfg.Server); team != nil {
		renderTeam(team, cfg.Identity, offlineNote(live, at), cfg.Server)
	}

	// Offer the demo only interactively, with team memory connected, and when the server vouched
	// that demo_query is warm.
	if tty && !*yesF && connected && h.DemoQuery != "" {
		if _, err := exec.LookPath("claude"); err == nil {
			fmt.Printf("\n  Watch it answer a real team question (opens Claude Code; short query,\n  uses your Claude account).\n\n")
			if confirm("Try it now?") {
				launchDemo(h.DemoQuery)
			}
		}
	}
}

// installAmbientIf wires the recall hook alone on a watcher machine: with --yes the flag decides,
// interactively the user does.
func installAmbientIf(yes, flagVal bool) {
	ambient := flagVal
	if !yes {
		fmt.Printf(`
%s

  Say yes, and Claude Code will automatically add relevant CSA context when
  your prompt sounds like a past decision, teammate, or "has anyone hit this?"
  question. This sends the matched prompt text to the same CSA server, with
  client-side secret redaction, and does nothing for ordinary coding prompts.

`, bold("Ambient team memory (recommended)"))
		ambient = confirm("Turn on ambient team memory?")
	}
	if !ambient {
		return
	}
	if err := installAmbientRecallHook(); err != nil {
		fmt.Println("Ambient memory hook install failed (re-run 'csa-client init --install' to retry):", err)
		return
	}
	fmt.Printf("%s ambient team memory enabled in Claude Code\n", okMark())
}

// applyOnlyAllowlist writes ~/.csa/only from a comma-separated glob list — the noninteractive
// form of `csa-client only`. It REPLACES any existing allowlist so a scripted re-init is
// idempotent (not append, which the interactive command does). Bad globs abort before the file
// is touched, matching runOnly's refuse-at-add-time rule.
func applyOnlyAllowlist(spec string) {
	var pats []string
	for _, p := range strings.Split(spec, ",") {
		if p = strings.TrimSpace(p); p == "" {
			continue
		}
		if _, err := filepath.Match(p, "x"); err != nil {
			fatalf(1, "bad --only pattern %q: %v — nothing written", p, err)
		}
		pats = append(pats, p)
	}
	if len(pats) == 0 {
		return
	}
	if bad := warnUnmatchedOnly(pats); len(bad) > 0 {
		fatalf(1, "nothing written — fix --only and retry, or run %s afterward", cyan(selfInvocation()+" only pick"))
	}
	if err := writeOnly(pats); err != nil {
		fatalf(1, "%v", err)
	}
	fmt.Printf("%s indexing ONLY %s %s\n", okMark(), strings.Join(pats, ", "), dim("("+onlyPath()+")"))
}

type healthReply struct {
	OK        bool   `json:"ok"`
	MCPPort   int    `json:"mcp_port"`
	MCPPath   string `json:"mcp_path"`
	MCPURL    string `json:"mcp_url"`    // absolute override (tunnel/proxy path-routing); wins over port+path
	Sessions  int    `json:"sessions"`   // indexed-session count (optional; proves it's the real, populated server)
	DemoQuery string `json:"demo_query"` // warm, gold-backed topic the server vouches for (absent when cold)
	DemoFacts int    `json:"demo_facts"`

	Extraction struct {
		Stalled bool   `json:"stalled"`
		Reason  string `json:"reason"`
	} `json:"extraction"`
}

// checkHealth GETs /api/health with a short timeout and translates the common failure
// modes into hints a human can act on.
func checkHealth(server string) (*healthReply, error) {
	req, err := http.NewRequest("GET", server+"/api/health", nil)
	if err != nil {
		return nil, err
	}
	addAuthHeaders(req)
	c := &http.Client{Timeout: 5 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		msg := err.Error()
		switch {
		case certError(err):
			return nil, &untrustedCertError{certAdvice(err)}
		case strings.Contains(msg, "connection refused"):
			return nil, fmt.Errorf("%v\n  -> is the server running, and bound beyond 127.0.0.1?", err)
		case strings.Contains(msg, "no such host"):
			return nil, fmt.Errorf("%v\n  -> hostname doesn't resolve — check the URL and your VPN", err)
		case strings.Contains(msg, "Client.Timeout") || strings.Contains(msg, "deadline exceeded"):
			return nil, fmt.Errorf("%v\n  -> no answer within 5s — check VPN/hostname", err)
		}
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET /api/health returned HTTP %s — wrong URL, or a proxy in the way?", resp.Status)
	}
	var h healthReply
	if err := json.NewDecoder(resp.Body).Decode(&h); err != nil || !h.OK {
		return nil, fmt.Errorf(`/api/health did not reply {"ok":true} — is this really a CSA server?`)
	}
	return &h, nil
}

// mcpEndpoint resolves the MCP URL the server advertises; "" when it advertises none.
// The absolute mcp_url override wins (Cloudflare-style path routing), else scheme+host
// from the server URL with the advertised port and path.
func mcpEndpoint(server string, h *healthReply) string {
	if h.MCPURL != "" {
		return h.MCPURL
	}
	u, err := url.Parse(server)
	if err != nil || h.MCPPort == 0 {
		return ""
	}
	return fmt.Sprintf("%s://%s:%d%s", u.Scheme, u.Hostname(), h.MCPPort, h.MCPPath)
}

// registerMCP wires the team recall tool into Claude Code. Never blocks init: any failure
// prints the exact manual command and moves on.
func registerMCP(cfg *config, h *healthReply, autoYes bool) bool {
	mcpURL := mcpEndpoint(cfg.Server, h)
	if mcpURL == "" {
		fmt.Println("Skipping team recall setup: the server did not advertise an MCP endpoint.")
		return false
	}
	// X-CSA-User: without it every recall and flag from Claude Code lands as anonymous "mcp".
	args := []string{"mcp", "add", "--transport", "http", "csa", mcpURL, "-s", "user",
		"--header", "X-CSA-User: " + cfg.Identity}
	// The origin's token-auth credential: the same bearer the client sends on every HTTP
	// request must ride the MCP calls too, or recall/flag from Claude Code 401s.
	if cfg.AuthToken != "" {
		args = append(args, "--header", "Authorization: Bearer "+cfg.AuthToken)
	}
	if cfg.CFID != "" {
		args = append(args, "--header", "CF-Access-Client-Id: "+cfg.CFID,
			"--header", "CF-Access-Client-Secret: "+cfg.CFSecret)
	}
	manual := "claude " + strings.Join(quoteArgs(maskedHeaders(args)), " ")
	fmt.Printf(`
%s

  Say yes, and Claude can answer questions like "what did we decide about X?"
  or "has anyone hit this bug before?" from your whole team's past sessions —
  right inside any conversation. It only reads from your team's CSA server;
  nothing on your machine changes.

`, bold("Team memory for Claude Code (optional)"))
	if !autoYes && !confirm("Connect Claude Code to the team's memory?") {
		fmt.Println("Skipped — Claude Code works as usual, just without team memory. To connect later:\n  " + cyan(manual))
		return false
	}
	if _, err := exec.LookPath("claude"); err != nil {
		fmt.Printf("%s Couldn't connect automatically — `claude` is not on PATH. Once it is:\n  %s\n", warnMark(), cyan(manual))
		return false
	}
	fmt.Print("Connecting ... ")
	stop := spin()
	exec.Command("claude", "mcp", "remove", "csa", "-s", "user").Run() // idempotent re-init; fine if absent
	out, err := exec.Command("claude", args...).CombinedOutput()
	stop()
	if err != nil {
		fmt.Printf("%s Couldn't connect automatically (%v): %s\n  Connect by hand:  %s\n",
			failMark(), err, strings.TrimSpace(string(out)), cyan(manual))
		return false
	}
	fmt.Printf("%s Connected — try asking Claude: \"what did we decide about X?\"\n  %s\n",
		okMark(), dim(`(technical: registered MCP server "csa", user scope)`))
	return true
}

// runDemo (`csa-client demo`): re-runnable form of the onboarding demo — launches a Claude Code
// session seeded with the server's warm, gold-backed topic. Refuses politely when the corpus is cold.
func runDemo() {
	server := os.Getenv("CSA_SERVER")
	var cfgToken string
	if cfg, err := loadConfig(configPath()); err == nil {
		if server == "" {
			server = cfg.Server
		}
		cfID, cfSecret = cfg.CFID, cfg.CFSecret
		cfgToken = cfg.AuthToken
	}
	authToken = resolveAuthToken(cfgToken)
	if server == "" {
		fatalf(exitConfig, "no server configured. Run 'csa-client setup --server <url>' first.")
	}
	h, err := checkHealth(strings.TrimRight(server, "/"))
	if err != nil {
		fatalf(1, "server unreachable: %v", err)
	}
	if h.DemoQuery == "" {
		fmt.Println("The team corpus isn't warm enough for a demo yet (needs a few gold-flagged facts).\n" +
			"Just ask in any Claude Code session: \"what did we decide about <topic>?\"")
		return
	}
	if _, err := exec.LookPath("claude"); err != nil {
		fatalf(1, "`claude` is not on PATH — install Claude Code first")
	}
	fmt.Printf("Asking your team's memory about %s (uses your Claude account) ...\n", cyan(h.DemoQuery))
	launchDemo(h.DemoQuery)
}

// fmtLatency: ms under a second, one decimal of seconds above (a 5s timeout reads "5.0s").
func fmtLatency(d time.Duration) string {
	if d < time.Second {
		return fmt.Sprintf("%dms", d.Milliseconds())
	}
	return fmt.Sprintf("%.1fs", d.Seconds())
}

// quoteArgs makes a copy-pasteable shell command; only args with whitespace get quoted.
func quoteArgs(args []string) []string {
	out := make([]string, len(args))
	for i, a := range args {
		if strings.ContainsAny(a, " \t") {
			out[i] = strconv.Quote(a)
		} else {
			out[i] = a
		}
	}
	return out
}

// smokeTest POSTs a dry_run ingest: proves the full write path (auth included) without
// triggering LLM extraction or indexing server-side. Content must be valid Claude Code
// JSONL with a user message — the parser rejects plain text.
func smokeTest(cfg *config) error {
	line, _ := json.Marshal(map[string]any{
		"timestamp": time.Now().Format(time.RFC3339),
		"cwd":       "/tmp",
		"message":   map[string]string{"role": "user", "content": "init smoke test"},
	})
	body, _ := json.Marshal(map[string]any{
		"session_id": "init-smoke",
		"content":    string(line),
		"owner":      cfg.Identity,
		"dry_run":    true,
	})
	req, err := http.NewRequest("POST", cfg.Server+"/api/ingest", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	addAuthHeaders(req)
	c := &http.Client{Timeout: 15 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	var r struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return fmt.Errorf("HTTP %s, unreadable reply: %v", resp.Status, err)
	}
	if resp.StatusCode != http.StatusOK || !r.OK {
		return fmt.Errorf("HTTP %s ok=%v error=%q", resp.Status, r.OK, r.Error)
	}
	return nil
}

// selfInvocation: how to run THIS binary, which may be named csa-client-darwin-arm64 in
// ~/Downloads rather than csa-client on PATH.
func selfInvocation() string {
	if p, err := os.Executable(); err == nil {
		return quoteArgs([]string{p})[0]
	}
	return os.Args[0]
}

// launchDemo hands the terminal to a Claude Code session seeded with the server's warm topic.
func launchDemo(topic string) {
	cmd := exec.Command("claude", fmt.Sprintf(
		"What did we decide about %s? Cite who and when. Then tell me I can ask my own question.", topic))
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Printf("%s couldn't launch claude (%v) — paste the first question above instead.\n", warnMark(), err)
	}
}

// epilogue says how the machine ships: "hooks", "login watcher", the path of a system watcher
// unit, or "" when nothing ships until the watcher is started by hand.
func epilogue(cfg *config, shipsVia, demoQ string) {
	self := selfInvocation()
	switch shipsVia {
	case "hooks":
		fmt.Printf(`
%s Setup complete — each Claude Code session ships when it ends. Nothing more to run.

  Check it:       %s
  Stop:           %s
`, okMark(), cyan("csa-client doctor"), cyan(self+" uninstall"))
	case "login watcher":
		status := "launchctl list " + launchdLabel + "    (shows a PID when running)"
		if runtime.GOOS != "darwin" {
			status = "systemctl --user status csa-client"
		}
		fmt.Printf(`
%s Setup complete — csa-client is already running in the background
  (and will start again at every login).

  Nothing more to run.
  Watch it work:  %s
  Status:         %s
  Stop:           %s
`, okMark(), cyan("tail -f ~/.csa/client.log"), cyan(status), cyan(self+" uninstall"))
	case "":
		fmt.Printf(`
%s Nothing ships yet — start the watcher:

  Run it now:     %s
                  (foreground; Ctrl-C stops it — nothing ships while it's not running)
`, warnMark(), cyan(self))
	default:
		fmt.Printf(`
%s Setup complete — the system watcher %s ships this machine.
  No hooks were wired: both would ship every session twice.
`, okMark(), cyan(shipsVia))
	}
	// Secondary tips use the short name — only "Run it now" must be paste-executable.
	shipsWhat := "full transcripts of new/changed sessions (secrets stripped)"
	if cfg.Backfill {
		shipsWhat = "full transcripts, existing history once + new/changed (secrets stripped)"
	}
	fmt.Printf(`  Ships:          %s
                  -> %s, attributed to %s
  Keep private:   %s
  Un-ship:        %s (one) · %s (a whole folder)
  Index only:     %s  (or %s to choose from a list)
  Tip:            when recall returns something wrong or golden, tell Claude —
                  flags rerank future recalls for the whole team.
`, shipsWhat, cyan(cfg.Server), bold(cfg.Identity),
		cyan("csa-client ignore '*repo-name*'"),
		cyan("csa-client forget <id>"), cyan("csa-client forget --project '*repo*'"),
		cyan("csa-client only '*repo-name*'"), cyan("csa-client only pick"))
	if demoQ != "" {
		fmt.Printf("  First question: paste into any Claude session ->\n                  %s\n",
			cyan(fmt.Sprintf("\"what did we decide about %s?\"", demoQ)))
	}
}

// ---- team roster (who else is already on CSA) ----

type teamMember struct {
	User       string `json:"user"`
	Sessions   int    `json:"sessions"`
	LastActive string `json:"last_active"`
}

// fetchTeam GETs the server's per-owner rollup (GET /api/team): everyone whose sessions the
// corpus holds, most-active first. Read-only — no new contract, just a nicety the onboarding
// and the `team` command both read. err != nil means the server wasn't reached.
func fetchTeam(server string) ([]teamMember, error) {
	req, err := http.NewRequest("GET", server+"/api/team", nil)
	if err != nil {
		return nil, err
	}
	addAuthHeaders(req)
	c := &http.Client{Timeout: 5 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET /api/team returned HTTP %s", resp.Status)
	}
	var team []teamMember
	if err := json.NewDecoder(resp.Body).Decode(&team); err != nil {
		return nil, err
	}
	return team, nil
}

// relDay renders an ISO-8601 timestamp as friendly recency (today / yesterday / Nd ago),
// falling back to the plain date past a week or when it won't parse.
func relDay(ts string) string {
	t, err := time.Parse(time.RFC3339, ts)
	if err != nil {
		if len(ts) >= 10 {
			return ts[:10] // "2026-07-12T..." -> "2026-07-12"
		}
		return ""
	}
	return relDayTime(t)
}

func relDayTime(t time.Time) string {
	switch days := int(time.Since(t).Hours() / 24); {
	case days <= 0:
		return "today"
	case days == 1:
		return "yesterday"
	case days < 7:
		return fmt.Sprintf("%dd ago", days)
	default:
		return t.Format("2006-01-02")
	}
}

// ---- offline cache: the roster survives a dead server / no WiFi ----

func teamCachePath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "team.json")
}

type teamCache struct {
	FetchedAt string       `json:"fetched_at"`
	Team      []teamMember `json:"team"`
}

// saveTeamCache records the last roster we saw so `team`/`init` can still show it when the
// server is later unreachable. Best-effort: a write failure is never surfaced (only emails
// and counts — 0600 anyway, for parity with the rest of ~/.csa).
func saveTeamCache(team []teamMember) {
	b, err := json.Marshal(teamCache{FetchedAt: time.Now().Format(time.RFC3339), Team: team})
	if err != nil {
		return
	}
	if os.MkdirAll(filepath.Dir(teamCachePath()), 0o700) == nil {
		os.WriteFile(teamCachePath(), b, 0o600)
	}
}

// loadTeamCache returns the last cached roster and when it was fetched; ok=false if none.
func loadTeamCache() (team []teamMember, at time.Time, ok bool) {
	b, err := os.ReadFile(teamCachePath())
	if err != nil {
		return nil, time.Time{}, false
	}
	var c teamCache
	if json.Unmarshal(b, &c) != nil {
		return nil, time.Time{}, false
	}
	at, _ = time.Parse(time.RFC3339, c.FetchedAt)
	return c.Team, at, true
}

// teamRoster resolves the roster to show: the live server (cached on success), else the last
// cached roster (offline). live reports which; err is the live-fetch failure (nil when live).
// team is nil only when the server is unreachable AND nothing was ever cached — cold offline.
func teamRoster(server string) (team []teamMember, live bool, cachedAt time.Time, err error) {
	t, err := fetchTeam(server)
	if err == nil {
		saveTeamCache(t)
		return t, true, time.Time{}, nil
	}
	if ct, at, ok := loadTeamCache(); ok {
		return ct, false, at, err
	}
	return nil, false, time.Time{}, err
}

// offlineNote is the dim staleness banner appended to the roster header when it came from the
// cache rather than a live fetch.
func offlineNote(live bool, at time.Time) string {
	if live {
		return ""
	}
	if at.IsZero() {
		return "offline — cached roster"
	}
	return "offline — last synced " + relDayTime(at)
}

// renderTeam prints who's already shipping sessions to the team server — the onboarding
// "you're not alone" moment, and the whole body of `csa-client team`. self is marked "(you)".
// Padding is measured on the un-colored text so the columns line up through the ANSI codes.
func renderTeam(team []teamMember, self, note, server string) {
	others := 0
	for _, m := range team {
		if !strings.EqualFold(m.User, self) {
			others++
		}
	}
	header := bold("Your team on CSA")
	if note != "" {
		header += "  " + dim("("+note+")")
	}
	fmt.Printf("\n%s\n", header)
	if others == 0 {
		fmt.Printf("  No teammate has shipped sessions yet. A teammate connects with one line:\n    %s\n",
			cyan("curl -sSL "+strings.TrimRight(server, "/")+"/install | sh"))
		return
	}
	first := ""
	for _, m := range team {
		tag := ""
		if strings.EqualFold(m.User, self) {
			tag = " (you)"
		} else if first == "" {
			first = m.User
		}
		plain := m.User + tag
		pad := ""
		if n := 26 - len(plain); n > 0 {
			pad = strings.Repeat(" ", n)
		}
		meta := fmt.Sprintf("%d session%s", m.Sessions, plural(m.Sessions))
		if last := relDay(m.LastActive); last != "" {
			meta += " · last active " + last
		}
		fmt.Printf("  %s%s%s  %s\n", m.User, dim(tag), pad, dim(meta))
	}
	if first != "" {
		fmt.Printf("  %s\n", dim(fmt.Sprintf("Ask Claude %q to read their sessions.",
			"what has "+shortName(first)+" been working on?")))
	}
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// shortName is the local-part of an email (alice@team.com -> alice), else the value as-is.
func shortName(u string) string {
	if i := strings.IndexByte(u, '@'); i > 0 {
		return u[:i]
	}
	return u
}

// runTeam (`csa-client team`): re-runnable roster of who's already on the team server. Same
// server resolution as `demo` — $CSA_SERVER wins, else the configured one, with its CF creds.
func runTeam() {
	server := os.Getenv("CSA_SERVER")
	self := ""
	var cfgToken string
	if cfg, err := loadConfig(configPath()); err == nil {
		if server == "" {
			server = cfg.Server
		}
		self = cfg.Identity
		cfID, cfSecret = cfg.CFID, cfg.CFSecret
		cfgToken = cfg.AuthToken
	}
	authToken = resolveAuthToken(cfgToken)
	if server == "" {
		fatalf(exitConfig, "no server configured. Run 'csa-client setup --server <url>' first.")
	}
	if self == "" {
		self = owner()
	}
	team, live, at, err := teamRoster(strings.TrimRight(server, "/"))
	if team == nil { // unreachable AND never cached — nothing to show
		fatalf(1, "can't reach %s (%v) and no roster is cached yet.\n  -> connect once (server up, CF-Access creds set) to populate the offline cache.", server, err)
	}
	renderTeam(team, self, offlineNote(live, at), strings.TrimRight(server, "/"))
}

// ---- install / uninstall (login autostart) ----

func runInstall() {
	refuseWhilePluginEnabled()
	cfg, err := loadConfig(configPath())
	if err != nil || !cfg.consentOK() {
		fatalf(exitConfig, "no consent on record. Run 'csa-client init' first.")
	}
	if _, err := installAutostart(); err != nil {
		fatalf(1, "install failed: %v", err)
	}
}

// installAutostart makes the client start at login. Returns whether it actually installed
// something (darwin only); on Linux it prints ready-to-paste systemd steps instead of
// writing unit files we can't verify here, and Windows is not supported yet.
// Refuses when the SessionEnd hook is wired: hooks and a watcher would ship every session twice.
func installAutostart() (bool, error) {
	home, _ := os.UserHomeDir()
	if shipHookWired(filepath.Join(home, ".claude", "settings.json")) {
		return false, fmt.Errorf("this machine already ships through the CSA Claude Code hooks; " +
			"a login watcher would ship every session twice. Nothing installed ('csa-client hook off' removes the hooks)")
	}
	switch runtime.GOOS {
	case "darwin":
		err := installDarwin()
		return err == nil, err
	case "windows":
		fmt.Printf("Autostart on Windows isn't supported yet — run %s in a terminal.\n", cyan(selfInvocation()))
		return false, nil
	default:
		printSystemdInstructions()
		return false, nil
	}
}

// shipHookWired reports whether a CSA SessionEnd hook is present in a Claude Code settings file.
func shipHookWired(settingsPath string) bool {
	b, err := os.ReadFile(settingsPath)
	if err != nil {
		return false
	}
	var root map[string]any
	if json.Unmarshal(b, &root) != nil {
		return false
	}
	return hookStatus(root)["SessionEnd"]
}

func installDarwin() error {
	home, _ := os.UserHomeDir()
	bin, err := ensureCSAClientBinary()
	if err != nil {
		return err
	}

	logPath := filepath.Join(home, ".csa", "client.log")
	plist := filepath.Join(home, "Library", "LaunchAgents", launchdLabel+".plist")
	xml := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace // paths land in XML text nodes
	// ThrottleInterval: KeepAlive respawns on any exit — without it, a stale-consent exit(78)
	// loop respawns every ~10s forever; 300s keeps crash-recovery but caps the noise.
	content := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
	<key>Label</key><string>%s</string>
	<key>ProgramArguments</key><array><string>%s</string></array>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ThrottleInterval</key><integer>300</integer>
	<key>StandardOutPath</key><string>%s</string>
	<key>StandardErrorPath</key><string>%s</string>
</dict></plist>
`, launchdLabel, xml(bin), xml(logPath), xml(logPath))
	if err := os.MkdirAll(filepath.Dir(plist), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(plist, []byte(content), 0o644); err != nil {
		return err
	}

	uid := strconv.Itoa(os.Getuid())
	for _, p := range removeAgents(home, plist) {
		fmt.Printf("%s replaced the older login agent (%s)\n", okMark(), p)
	}
	launchctl("bootout", "gui/"+uid+"/"+launchdLabel) // fine if not loaded
	if out, err := launchctl("bootstrap", "gui/"+uid, plist); err != nil {
		if out2, err2 := launchctl("load", "-w", plist); err2 != nil {
			return fmt.Errorf("launchctl bootstrap: %v (%s); launchctl load: %v (%s)",
				err, bytes.TrimSpace(out), err2, bytes.TrimSpace(out2))
		}
	}
	fmt.Printf("%s autostart installed and running (launchd label %s)\n  binary: %s\n  log:    %s\n",
		okMark(), launchdLabel, cyan(bin), cyan(logPath))
	return nil
}

func printSystemdInstructions() {
	fmt.Printf(`No launchd here — set up a systemd user service instead (copy-paste):

  mkdir -p ~/.csa/bin && cp %s ~/.csa/bin/csa-client
  mkdir -p ~/.config/systemd/user`, selfInvocation())
	fmt.Print(`
  cat > ~/.config/systemd/user/csa-client.service <<'EOF'
[Unit]
Description=CSA client - ships Claude Code sessions to the team server

[Service]
ExecStart=%h/.csa/bin/csa-client
Restart=always

[Install]
WantedBy=default.target
EOF
  systemctl --user enable --now csa-client
`)
}

// unregisterMCP removes the team-recall MCP server from Claude Code (user scope).
// Best-effort and idempotent: reports false (nothing done) if the `claude` CLI is absent.
func unregisterMCP() bool {
	if _, err := exec.LookPath("claude"); err != nil {
		return false
	}
	exec.Command("claude", "mcp", "remove", "csa", "-s", "user").Run() // fine if never registered
	return true
}

// stopAutostart removes the login watcher (launchd/systemd) and stops it if running. Idempotent.
// `setup` calls it too: a machine that ships through hooks must not also run the watcher.
func stopAutostart(home string) {
	switch runtime.GOOS {
	case "darwin":
		for _, p := range removeAgents(home, "") {
			fmt.Printf("%s autostart stopped and removed (%s)\n", okMark(), p)
		}
	case "windows":
		// nothing persistent to remove on Windows
	default:
		if _, err := os.Stat(filepath.Join(home, ".config", "systemd", "user", "csa-client.service")); err == nil {
			fmt.Print("Autostart: remove the systemd user service:\n" +
				"  systemctl --user disable --now csa-client\n" +
				"  rm ~/.config/systemd/user/csa-client.service\n")
		}
	}
}

// runUninstall removes everything this client installed on the machine: the hooks, the MCP
// server, the login watcher and the installer's binary, and revokes this machine's credential.
// --purge also removes ~/.csa. Sessions already on the server stay; that is `forget`.
func runUninstall(args []string) {
	purge := false
	for _, a := range args {
		if a == "--purge" || a == "-purge" {
			purge = true
		}
	}
	home, _ := os.UserHomeDir()

	// First, while the config still holds the token: "removed" must not leave a live credential.
	uninstallCredential(purge)

	// 1. every Claude Code hook we install: ambient recall, ship (SessionEnd), sweep (SessionStart).
	// All three, or an "uninstalled" client keeps shipping sessions on every SessionEnd.
	if err := uninstallClaudeHook(); err != nil {
		fmt.Println("Could not remove hooks:", err)
	} else {
		fmt.Printf("%s hooks removed from ~/.claude/settings.json %s\n", okMark(),
			dim("(recall, ship, sweep)"))
	}

	// 2. MCP registration
	if unregisterMCP() {
		fmt.Printf("%s MCP server \"csa\" deregistered (user scope)\n", okMark())
	} else {
		fmt.Println("MCP: `claude` CLI not found — skip (remove manually if needed: claude mcp remove csa -s user)")
	}

	// 3. autostart + running agent
	stopAutostart(home)

	// 4. what the one-line installer wrote (~/.local/...). Removed even without --purge: this is our
	// binary, not the user's data — and leaving a csa-client on PATH after "uninstall" invites a
	// re-run that silently re-installs everything. Only OUR symlink is touched: if that path is
	// something else the user put there, it stays.
	libDir := filepath.Join(home, ".local", "lib", "csa")
	link := filepath.Join(home, ".local", "bin", "csa-client")
	if target, err := os.Readlink(link); err == nil && strings.HasPrefix(target, libDir) {
		if os.Remove(link) == nil {
			fmt.Printf("%s removed %s\n", okMark(), cyan(link))
		}
	}
	if _, err := os.Stat(libDir); err == nil && os.RemoveAll(libDir) == nil {
		fmt.Printf("%s removed %s\n", okMark(), cyan(libDir))
	}

	// 5. local config, consent, binary
	csaDir := filepath.Join(home, ".csa")
	if purge {
		if err := os.RemoveAll(csaDir); err != nil {
			fmt.Println("Could not remove", csaDir, ":", err)
		} else {
			fmt.Printf("%s purged %s (config, consent, folder rules, deny and allow lists, shipped markers, binary, logs)\n", okMark(), csaDir)
		}
	} else {
		fmt.Printf("Config, consent record, and binary kept at %s — add %s to remove them too.\n",
			cyan(csaDir), cyan("--purge"))
	}

	fmt.Printf("%s csa-client uninstalled. Sessions already on the team server remain; a workspace admin can delete them.\n", okMark())
}

// runIgnore: `csa-client ignore '<glob>'` appends pattern(s) to ~/.csa/ignore; with no args
// it prints the current list. Bad globs are refused at add time — a pattern that can never
// match would leave the user believing a project is private when it isn't.
func runIgnore(args []string) {
	path := ignorePath()
	if len(args) == 0 {
		pats := loadIgnores(path)
		fmt.Printf("%s — %d pattern(s):\n", path, len(pats))
		for _, p := range pats {
			fmt.Println("  " + p)
		}
		fmt.Println("Add one with: " + cyan(selfInvocation()+" ignore '*repo-name*'"))
		return
	}
	for _, p := range args {
		if _, err := filepath.Match(p, "x"); err != nil {
			fatalf(1, "bad pattern %q: %v — nothing added", p, err)
		}
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		fatalf(1, "%v", err)
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		fatalf(1, "%v", err)
	}
	for _, p := range args {
		fmt.Fprintln(f, p)
	}
	f.Close()
	fmt.Printf("%s added %s to %s — a running watcher applies it on its next scan (no restart).\n",
		okMark(), strings.Join(args, ", "), cyan(path))
	fmt.Println(dim("Not retroactive: sessions that already shipped stay on the server."))
}

// unmatchedOnlyPatterns returns the subset of patterns that match no project folder, session
// file, or path CSA currently sees (via ignoredBy — the same matcher the watcher applies at
// ship time). A syntactically valid glob can still be unmatchable in practice: the most common
// case is a real filesystem path pasted in (e.g. /Users/you/Desktop/repo) instead of the munged
// folder name (-Users-you-Desktop-repo) that only/ignore globs actually match against. For
// `only` specifically, an unmatchable pattern is a total-exclusion trap — an allowlist with a
// pattern that matches nothing ships NOTHING, silently. Returns nil (no check performed, with a
// warning) when there are no sessions yet to test against — every pattern would look "unmatched"
// then, and refusing would block a legitimate first-ever `only` on a brand new machine.
func unmatchedOnlyPatterns(patterns []string) []string {
	home, _ := os.UserHomeDir()
	root := env("CSA_ROOT", filepath.Join(home, ".claude", "projects"))
	sessions := entries(root)
	if len(sessions) == 0 {
		fmt.Printf("%s no sessions under %s yet — can't verify %s matches anything; skipping the check.\n",
			warnMark(), root, strings.Join(patterns, ", "))
		return nil
	}
	var bad []string
	for _, p := range patterns {
		matched := false
		for _, s := range sessions {
			if ignoredBy(s.path, []string{p}) != "" {
				matched = true
				break
			}
		}
		if !matched {
			bad = append(bad, p)
		}
	}
	return bad
}

// warnUnmatchedOnly prints the shared diagnostic for pattern(s) unmatchedOnlyPatterns flagged —
// the flagged list, the munged-name hint, and the current folder list — leaving the caller's
// fatalf to add its own "nothing added/written" phrasing and next step. Returns the flagged
// patterns (nil means clean: nothing to refuse).
func warnUnmatchedOnly(patterns []string) []string {
	bad := unmatchedOnlyPatterns(patterns)
	if len(bad) == 0 {
		return nil
	}
	fmt.Printf("%s %s — matches no project folder, session file, or path CSA currently sees.\n",
		failMark(), strings.Join(bad, ", "))
	fmt.Println(dim("only/ignore globs match the munged folder name below (e.g. -Users-you-Desktop-repo), not a real filesystem path."))
	listProjectFolders()
	return bad
}

// runOnly: `csa-client only '<glob>'` appends pattern(s) to ~/.csa/only, the indexed-folders
// allowlist. While it holds any pattern, the watcher ships ONLY sessions whose project folder
// (or path) matches one — everything else is skipped, exactly like an ignore. With no args it
// prints the current allowlist plus the project folders it currently sees, so you know what to
// match. Empty allowlist = index every folder (the default). Bad globs are refused at add time,
// and so is a glob that matches none of the folders CSA currently sees (unmatchedOnlyPatterns).
func runOnly(args []string) {
	if len(args) > 0 && args[0] == "pick" {
		runOnlyPick()
		return
	}
	path := onlyPath()
	if len(args) == 0 {
		pats := loadIgnores(path) // same one-glob-per-line format as ~/.csa/ignore
		if len(pats) == 0 {
			fmt.Printf("%s empty — indexing every project folder (the default).\n", path)
		} else {
			fmt.Printf("%s — indexing ONLY %d folder glob(s):\n", path, len(pats))
			for _, p := range pats {
				fmt.Println("  " + p)
			}
		}
		listProjectFolders()
		fmt.Println("Restrict to one with: " + cyan(selfInvocation()+" only '*repo-name*'"))
		return
	}
	for _, p := range args {
		if _, err := filepath.Match(p, "x"); err != nil {
			fatalf(1, "bad pattern %q: %v — nothing added", p, err)
		}
	}
	if bad := warnUnmatchedOnly(args); len(bad) > 0 {
		fatalf(1, "nothing added — pick a folder above with %s, or fix the glob and retry", cyan(selfInvocation()+" only pick"))
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		fatalf(1, "%v", err)
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		fatalf(1, "%v", err)
	}
	for _, p := range args {
		fmt.Fprintln(f, p)
	}
	f.Close()
	fmt.Printf("%s added %s to %s — the watcher now indexes ONLY matching folders (applied on its next scan).\n",
		okMark(), strings.Join(args, ", "), cyan(path))
	fmt.Println(dim("Not retroactive: sessions that already shipped stay on the server ('forget <id>' removes one)."))
}

type projFolder struct {
	name  string
	count int
}

// projectFolders lists the project folders under the watch root that hold sessions, sorted by
// name, each with its session count. These are the munged cwd names (projectDir)
// that only/ignore globs — and the folder picker — match against.
func projectFolders() []projFolder {
	home, _ := os.UserHomeDir()
	root := env("CSA_ROOT", filepath.Join(home, ".claude", "projects"))
	counts := map[string]int{}
	for _, s := range entries(root) {
		counts[projectDir(s.path)]++
	}
	names := make([]string, 0, len(counts))
	for n := range counts {
		names = append(names, n)
	}
	sort.Strings(names)
	out := make([]projFolder, len(names))
	for i, n := range names {
		out[i] = projFolder{n, counts[n]}
	}
	return out
}

// listProjectFolders prints the project folders under the watch root that currently hold
// sessions, with counts — these are the raw names 'only'/'ignore' globs match against.
func listProjectFolders() {
	folders := projectFolders()
	if len(folders) == 0 {
		return
	}
	home, _ := os.UserHomeDir()
	root := env("CSA_ROOT", filepath.Join(home, ".claude", "projects"))
	fmt.Printf("\n%d project folder(s) under %s:\n", len(folders), root)
	for _, f := range folders {
		fmt.Printf("  %s %s\n", f.name, dim(fmt.Sprintf("(%d)", f.count)))
	}
	fmt.Println()
}

// writeOnly REPLACES ~/.csa/only with pats (one per line). Empty pats REMOVES the file, which
// means "index every folder" (an empty allowlist is the default). Shared by the folder picker
// and the scripted --only path.
//
// The write is ATOMIC (temp file + rename): the watcher is a separate process that reloads this
// file every scan tick and reads an empty allowlist as "index every folder", so a plain
// truncate-then-write could expose an unpicked folder for one tick while the list is being
// TIGHTENED — the wrong direction for a privacy control. os.Remove (the "index everything" path)
// is already atomic.
func writeOnly(pats []string) error {
	path := onlyPath()
	if len(pats) == 0 {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "only-*.tmp") // same dir so the rename is a same-filesystem move
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op once the rename succeeds; cleans up on any early return
	if _, err := tmp.WriteString(strings.Join(pats, "\n") + "\n"); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o644); err != nil { // CreateTemp is 0600; match the old file mode
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// parseFolderSelection maps a picker response to a selection over n listed folders.
// "all"/"a"/"*" -> (nil, true): index everything (clear the allowlist). Otherwise it reads
// space/comma-separated 1-based indices, keeping the in-range ones deduped in listed order;
// out-of-range or non-numeric tokens (and a blank response) are skipped. The caller treats an
// empty, not-all result as cancel. Pure; tested.
func parseFolderSelection(resp string, n int) (idxs []int, all bool) {
	switch strings.ToLower(strings.TrimSpace(resp)) {
	case "all", "a", "*":
		return nil, true
	}
	seen := map[int]bool{}
	for _, tok := range strings.FieldsFunc(resp, func(r rune) bool { return r == ',' || r == ' ' || r == '\t' }) {
		i, err := strconv.Atoi(tok)
		if err != nil || i < 1 || i > n || seen[i] {
			continue
		}
		seen[i] = true
		idxs = append(idxs, i)
	}
	return idxs, false
}

// runOnlyPick (`csa-client only pick`): interactive folder chooser — the friendly form of
// `only <glob>`. Lists the project folders that hold sessions, numbered, and REPLACES ~/.csa/only
// with the ones you pick, so you "choose specific folders" without hand-writing globs against
// Claude Code's munged cwd names. 'all' clears the allowlist (index everything); blank cancels.
// Picked names are written literally into the allowlist. Every munged cwd name Claude Code
// produces is [A-Za-z0-9-] (it replaces path separators with '-'), so filepath.Match sees no
// glob metacharacter and matches each name exactly — no escaping machinery needed. (A project
// path that literally held '*', '?', or '[' is the only way a picked entry could mis-match, and
// it's too rare to justify escaping.)
func runOnlyPick() {
	if !stdinIsTTY() {
		fatalf(exitConfig, "`only pick` needs a terminal — noninteractive: %s only '<glob>' (or init --only '<glob>')", selfInvocation())
	}
	folders := projectFolders()
	if len(folders) == 0 {
		fmt.Println("No project folders with sessions yet under the watch root — nothing to pick.")
		return
	}
	cur := map[string]bool{}
	for _, p := range loadIgnores(onlyPath()) {
		cur[p] = true // marks folders already allowlisted by EXACT name (a glob entry won't mark)
	}
	fmt.Printf("%s\n\n", bold("Choose the folders to index — everything else stays private:"))
	for i, f := range folders {
		mark := "   "
		if cur[f.name] {
			mark = okMark() + " "
		}
		fmt.Printf("  %s%2d  %s %s\n", mark, i+1, f.name, dim(fmt.Sprintf("(%d session%s)", f.count, plural(f.count))))
	}
	fmt.Printf("\n%s\n", dim("Numbers separated by space/comma (e.g. 1 3 4) · 'all' to index every folder · blank to cancel"))
	idxs, all := parseFolderSelection(prompt("Index folders", ""), len(folders))
	if all {
		if err := writeOnly(nil); err != nil {
			fatalf(1, "%v", err)
		}
		fmt.Printf("%s indexing every folder — %s cleared.\n", okMark(), cyan(onlyPath()))
		return
	}
	if len(idxs) == 0 {
		fmt.Println("Nothing selected — no change.")
		return
	}
	picked := make([]string, len(idxs))
	for i, n := range idxs {
		picked[i] = folders[n-1].name
	}
	if err := writeOnly(picked); err != nil {
		fatalf(1, "%v", err)
	}
	fmt.Printf("%s indexing ONLY %d folder(s) %s — the watcher applies it on its next scan.\n",
		okMark(), len(picked), dim("("+onlyPath()+")"))
	fmt.Println(dim("Not retroactive: sessions that already shipped stay on the server ('forget <id>' removes one)."))
}

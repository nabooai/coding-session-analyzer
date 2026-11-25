package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const hookTimeout = 3

// Ambient recall fires on EVERY substantive prompt (not a phrase allowlist) — measured on real
// traffic, a regex allowlist stayed silent on most work-in-progress prompts that had gold. The
// server's recall abstains when nothing solid matches, so it (not the trigger) is the relevance
// gate. We only skip here what recall can never help: contentless / pronoun-only prompts ("ok",
// "how do I disable it?"), which were the measured noise source. A prompt qualifies with >=2
// "content tokens": alnum words >=4 chars that aren't common instruction/filler words.
var contentToken = regexp.MustCompile(`[a-z0-9][a-z0-9_\-./]{3,}`)

var contentStop = map[string]bool{
	"about": true, "what": true, "when": true, "where": true, "which": true, "would": true,
	"could": true, "should": true, "there": true, "their": true, "that": true, "this": true,
	"these": true, "those": true, "with": true, "from": true, "into": true, "your": true,
	"will": true, "does": true, "done": true, "have": true, "been": true, "here": true,
	"then": true, "they": true, "them": true, "please": true, "gonna": true, "wanna": true,
	"just": true, "like": true, "make": true, "made": true, "need": true, "want": true,
	"show": true, "give": true, "tell": true, "using": true, "used": true, "also": true,
	"some": true, "thing": true, "stuff": true, "really": true, "actually": true, "maybe": true,
	"yourself": true, "yourslef": true,
}

var ambientQueryRewrites = []*regexp.Regexp{
	regexp.MustCompile(`(?i)^\s*use\s+csa\s+to\s+answer:?\s*["']?(.+?)["']?\s*$`),
	regexp.MustCompile(`(?i)^\s*what\s+did\s+we\s+decide\s+about\s+(.+?)\??\s*$`),
	regexp.MustCompile(`(?i)^\s*what\s+was\s+decided\s+about\s+(.+?)\??\s*$`),
	regexp.MustCompile(`(?i)^\s*what\s+do\s+we\s+know\s+about\s+(.+?)\??\s*$`),
	regexp.MustCompile(`(?i)^\s*has\s+any(?:one|body)\s+hit\s+(.+?)\s+before\??\s*$`),
	regexp.MustCompile(`(?i)^\s*did\s+any(?:one|body)\s+(?:hit|see|work\s+on|touch)\s+(.+?)\s+before\??\s*$`),
	regexp.MustCompile(`(?i)^\s*why\s+did\s+(.+?)\s+decide(?:d)?\s+about\s+(.+?)\??\s*$`),
	regexp.MustCompile(`(?i)^\s*why\s+(.+?)\s+decided?\s+about\s+(.+?)\??\s*$`),
}

// residualSecret: patterns that indicate a secret SURVIVED redaction. The shared redactor catches the
// high-precision token shapes (sk-…, ghp_…, AKIA…, JWTs, private keys), but adversarial testing showed
// AWS *secret* access keys, `Authorization: Bearer …` tokens, and `scheme://user:pass@host` connection
// strings still reach the wire raw. Ambient recall is a new per-prompt egress path, so we fail SAFE: if
// any of these survive on the redacted query, skip the recall entirely (no injection, nothing sent) rather
// than ship a partially-redacted prompt. Checked AFTER redaction, so a properly-redacted token won't trip it.
var residualSecret = []*regexp.Regexp{
	regexp.MustCompile(`(?i)\bbearer\s+[a-z0-9._~+/\-]{16,}`), // Bearer <token>
	regexp.MustCompile(`://[^/\s:@]+:[^/\s@]{6,}@`),           // user:password@host
	// labeled secret with a value: `password=x`, `password: x`, `the password is x` — but NOT the safe
	// redaction placeholder (`token [REDACTED:…]`), because a `= : is was` connector must precede the value.
	regexp.MustCompile(`(?i)(passw(or)?d|pwd|secret|api[_-]?key|access[_-]?key|token|credential)s?\s*(is|was|=|:)\s*\S{8,}`),
	regexp.MustCompile(`\b[A-Za-z0-9+/]{40}\b`), // 40-char high-entropy (AWS secret key)
}

func looksSecret(s string) bool {
	for _, re := range residualSecret {
		if re.MatchString(s) {
			return true
		}
	}
	return false
}

func shouldAmbientRecall(prompt string) bool {
	p := strings.ToLower(strings.TrimSpace(prompt))
	// A prompt opening with '<' is a machine-injected tag block, not a person typing: <task-notification>,
	// <local-command-stdout>, <command-name>, <system-reminder>. All 35 of these in the first 448 logged
	// prod hook queries were injected into and none of them could ever have helped a human. Rejecting the
	// whole '<' class rather than a prefix list is deliberate — the tag vocabulary is the harness's to
	// change, and a human prompt starting with '<' (a pasted closing tag) loses nothing by staying silent.
	if len(p) < 20 || len(p) > 4000 || strings.HasPrefix(p, "/") || strings.HasPrefix(p, "<") {
		return false
	}
	content := 0
	for _, tok := range contentToken.FindAllString(p, -1) {
		if contentStop[tok] {
			continue
		}
		if content++; content >= 2 {
			return true
		}
	}
	return false
}

func ambientQuery(prompt string) string {
	q := strings.TrimSpace(prompt)
	for i := 0; i < 2; i++ {
		changed := false
		for _, re := range ambientQueryRewrites {
			if m := re.FindStringSubmatch(q); len(m) > 0 {
				if len(m) == 3 {
					q = strings.TrimSpace(m[1] + " " + m[2])
				} else {
					q = strings.TrimSpace(m[1])
				}
				q = strings.Trim(q, " \t\r\n\"'`“”‘’?.!")
				changed = true
				break
			}
		}
		if !changed {
			break
		}
	}
	q = strings.Trim(q, " \t\r\n\"'`“”‘’?.!")
	if q == "" {
		return strings.TrimSpace(prompt)
	}
	return q
}

// runHook is the UserPromptSubmit hook; `hook off` removes every CSA hook. There is no
// `hook install`: recall without the ship hooks looks healthy and never ships, so `setup` and `init` wire all three.
func runHook(args []string) {
	if len(args) > 0 {
		if args[0] != "off" && args[0] != "uninstall" {
			fatalf(2, "unknown 'hook %s'. Hooks are installed by 'csa-client setup' (or 'csa-client init'); 'csa-client hook off' removes them.", args[0])
		}
		if err := uninstallClaudeHook(); err != nil {
			fatalf(1, "removing Claude Code hooks: %v", err)
		}
		fmt.Printf("%s CSA hooks removed from Claude Code (recall, ship, sweep). Re-add them with 'csa-client setup' (or 'csa-client init').\n", okMark())
		return
	}
	cfg, err := loadConfig(configPath())
	if err != nil || !cfg.consentOK() || strings.TrimSpace(cfg.Server) == "" {
		return
	}
	cfID, cfSecret = cfg.CFID, cfg.CFSecret
	authToken = resolveAuthToken(cfg.AuthToken)

	// A local file stat, not a network call — safe to run on EVERY prompt regardless of content,
	// unlike the recall query below (gated on shouldAmbientRecall). Whichever authenticated call
	// discovers a dead credential first — this one, or the detached `send` a session-end hook ran
	// with no user-visible channel of its own — the very next prompt surfaces it here.
	if authWarnDue() {
		emitAuthWarning()
		return
	}

	var in struct {
		SessionID string `json:"session_id"`
		Prompt    string `json:"prompt"`
		Cwd       string `json:"cwd"` // Claude Code sends the current working dir on every hook event
	}
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil || !shouldAmbientRecall(in.Prompt) {
		return
	}
	query := redact(ambientQuery(in.Prompt))
	if looksSecret(query) {
		return // fail-safe: a secret survived redaction (Bearer/AWS-secret/conn-string) — do NOT POST this prompt
	}
	body, _ := json.Marshal(map[string]any{
		"query":    query,
		"session":  in.SessionID,
		"asked_by": cfg.Identity,
		"cwd":      in.Cwd, // raw: the server normalizes it (worktree -> repo) and scopes on that
		"k":        6,
	})
	req, err := http.NewRequest("POST", strings.TrimRight(cfg.Server, "/")+"/api/ask", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	addAuthHeaders(req)
	c := &http.Client{Timeout: envDur("CSA_HOOK_TIMEOUT", 1800*time.Millisecond)}
	resp, err := c.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized {
		markAuthBroken() // next prompt's authWarnDue() check surfaces this — see above
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return
	}
	clearAuthBroken() // any 2xx here proves the credential works
	var out struct {
		OK            bool   `json:"ok"`
		Found         bool   `json:"found"`
		Context       string `json:"context"`
		SystemMessage string `json:"system_message"`
	}
	if json.NewDecoder(resp.Body).Decode(&out) != nil || !out.OK || !out.Found || out.Context == "" {
		return
	}
	emitHookContext(out.Context, out.SystemMessage)
}

// emitAuthWarning surfaces a dead credential via the same systemMessage channel emitHookContext
// uses — a genuinely user-visible line in a UserPromptSubmit hook's JSON reply, not just context
// fed to the model. No additionalContext here: this isn't recall, it's a client-status notice,
// and shouldn't get relayed as if it were something the model found.
func emitAuthWarning() {
	out := map[string]any{
		"systemMessage": "⚠️ CSA: this machine's credential is no longer accepted. Run `csa-client setup` " +
			"to sign in again — nothing is lost, sessions keep queueing locally and ship once you do.",
	}
	b, _ := json.Marshal(out)
	fmt.Println(string(b))
}

func emitHookContext(context, systemMessage string) {
	msg := "CSA ambient team memory found relevant past-session context for this prompt.\n" +
		"Use it as background evidence when it helps. Preserve the provenance in the CSA lines; " +
		"do not claim a person personally made a decision unless the evidence says that.\n\n" + context
	out := map[string]any{
		"hookSpecificOutput": map[string]any{
			"hookEventName":     "UserPromptSubmit",
			"additionalContext": msg,
		},
	}
	if systemMessage != "" {
		out["systemMessage"] = systemMessage // user-visible line: they SEE what CSA surfaced (and can flag it)
	}
	b, _ := json.Marshal(out)
	fmt.Println(string(b))
}

// csaHookSpec: one Claude Code hook entry this client owns.
//
// The `args` (exec) form is deliberate: with `args` present Claude Code execs the binary directly,
// with no shell and no quoting to get wrong.
type csaHookSpec struct {
	event   string
	args    []string
	timeout int // seconds
}

// shipHookTimeout: /api/ingest runs `claude -p` server-side, so a ship can legitimately take
// minutes — the client's own HTTP timeout is 180s (main.go). hookTimeout (3s) is sized for ambient
// recall, which must never stall a prompt; reusing it here would kill every ship hook mid-POST.
const shipHookTimeout = 240

// csaHooks: every hook `setup` and `init` install. This slice is the single source of truth for
// install (additively), uninstall (remove all of them), and the E2E's "exactly three entries" check.
func csaHooks() []csaHookSpec {
	return []csaHookSpec{
		{"UserPromptSubmit", []string{"hook"}, hookTimeout},            // ambient recall on the way in
		{"SessionEnd", []string{"ship"}, shipHookTimeout},              // ship this session when it ends
		{"SessionStart", []string{"ship", "--sweep"}, shipHookTimeout}, // catch up sessions that CRASHED
	}
}

// installAmbientRecallHook wires the recall hook ALONE, which is `init --install`'s ambient-memory
// toggle and nothing else: that machine ships through the watcher, not through SessionEnd. Do not
// reach for this as "the hook installer" — `setup` and `init` install all of csaHooks(), and the standalone
// `hook install` command that used to call this is gone, because a machine that recalled but never
// shipped looked healthy from the inside.
func installAmbientRecallHook() error {
	bin, err := ensureCSAClientBinary()
	if err != nil {
		return err
	}
	home, _ := os.UserHomeDir()
	return installAmbientRecallHookAt(filepath.Join(home, ".claude", "settings.json"), bin)
}

func installAmbientRecallHookAt(settingsPath, command string) error {
	return installHooksAt(settingsPath, command, csaHooks()[:1])
}

// installHooksAt writes `specs` into a Claude Code settings file ADDITIVELY: third-party hooks
// (Poco's, the user's own) are never touched, and CSA's own prior entries are replaced rather than
// appended to — so re-running the installer leaves exactly one entry per event, not two.
func installHooksAt(settingsPath, bin string, specs []csaHookSpec) error {
	var root map[string]any
	if b, err := os.ReadFile(settingsPath); err == nil && len(bytes.TrimSpace(b)) > 0 {
		if err := json.Unmarshal(b, &root); err != nil {
			return fmt.Errorf("parsing %s: %w", settingsPath, err)
		}
	} else {
		root = map[string]any{}
	}
	hooks := objectAt(root, "hooks")
	for _, spec := range specs {
		args := make([]any, len(spec.args))
		for i, a := range spec.args {
			args[i] = a
		}
		hook := map[string]any{"type": "command", "command": bin, "args": args, "timeout": spec.timeout}
		entry := map[string]any{"hooks": []any{hook}}
		if spec.event == "UserPromptSubmit" {
			entry["matcher"] = "" // what this client has always written here; left as-is
		}
		hooks[spec.event] = append(removeCSAHooks(arrayAt(hooks, spec.event)), entry)
	}
	root["hooks"] = hooks
	b, err := json.MarshalIndent(root, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(settingsPath), 0o755); err != nil {
		return err
	}
	return os.WriteFile(settingsPath, append(b, '\n'), 0o644)
}

func uninstallClaudeHook() error {
	home, _ := os.UserHomeDir()
	return uninstallClaudeHookAt(filepath.Join(home, ".claude", "settings.json"))
}

// uninstallClaudeHookAt strips EVERY hook this client installs (ambient recall, ship, sweep) from a
// Claude Code settings file, preserving every unrelated hook, and prunes now-empty containers.
// No-op (nil) if the file is absent, empty, or carries no CSA hook — safe to run repeatedly.
//
// It sweeps every event in csaHooks(), not just UserPromptSubmit: an uninstall that left SessionEnd
// behind would keep shipping the user's sessions after they asked us to stop. That is the one
// failure this command exists to prevent.
func uninstallClaudeHookAt(settingsPath string) error {
	b, err := os.ReadFile(settingsPath)
	if err != nil || len(bytes.TrimSpace(b)) == 0 {
		return nil // nothing installed
	}
	var root map[string]any
	if err := json.Unmarshal(b, &root); err != nil {
		return fmt.Errorf("parsing %s: %w", settingsPath, err)
	}
	hooks, _ := root["hooks"].(map[string]any)
	if hooks == nil {
		return nil
	}
	for _, spec := range csaHooks() {
		kept := removeCSAHooks(arrayAt(hooks, spec.event))
		if len(kept) == 0 {
			delete(hooks, spec.event)
		} else {
			hooks[spec.event] = kept
		}
	}
	if len(hooks) == 0 {
		delete(root, "hooks")
	} else {
		root["hooks"] = hooks
	}
	out, err := json.MarshalIndent(root, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(settingsPath, append(out, '\n'), 0o644)
}

func ensureCSAClientBinary() (string, error) {
	home, _ := os.UserHomeDir()
	bin := filepath.Join(home, ".csa", "bin", "csa-client")
	exe, err := os.Executable()
	if err == nil {
		exe, err = filepath.EvalSymlinks(exe)
	}
	if err != nil {
		return "", fmt.Errorf("locating running binary: %w", err)
	}
	if exe == bin {
		return bin, nil
	}
	b, err := os.ReadFile(exe)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(bin), 0o755); err != nil {
		return "", err
	}
	_ = os.Remove(bin)
	if err := os.WriteFile(bin, b, 0o755); err != nil {
		return "", err
	}
	return bin, nil
}

func objectAt(m map[string]any, k string) map[string]any {
	if v, ok := m[k].(map[string]any); ok {
		return v
	}
	n := map[string]any{}
	m[k] = n
	return n
}

func arrayAt(m map[string]any, k string) []any {
	if v, ok := m[k].([]any); ok {
		return v
	}
	return nil
}

func removeCSAHooks(entries []any) []any {
	var kept []any
	for _, entry := range entries {
		em, ok := entry.(map[string]any)
		if !ok {
			kept = append(kept, entry)
			continue
		}
		hs, _ := em["hooks"].([]any)
		var keptHooks []any
		for _, h := range hs {
			hm, ok := h.(map[string]any)
			if !ok || !isCSAClientHook(hm) {
				keptHooks = append(keptHooks, h)
			}
		}
		if len(keptHooks) == 0 {
			continue
		}
		em["hooks"] = keptHooks
		kept = append(kept, em)
	}
	return kept
}

// hookBinary extracts the executable from a hook entry, handling both on-disk forms:
//   - exec form (what we write): command IS the bare path — which may contain spaces
//     (`/Users/john smith/.csa/bin/csa-client`) — and the arguments live in a separate `args` array.
//   - shell form (what the plugin writes): command is `"/path/csa-client" ship`, quoted, args inline.
//
// `hasArgs` distinguishes them: with an args array present the whole command is the binary (never
// split on a space that belongs to the path); without it, parse the quoted-or-first-token binary.
func hookBinary(cmd string, hasArgs bool) string {
	s := strings.TrimSpace(cmd)
	if s == "" {
		return ""
	}
	if hasArgs {
		return s // exec form: command is exactly the executable path, spaces and all
	}
	if s[0] == '"' {
		if i := strings.Index(s[1:], `"`); i >= 0 {
			return s[1 : 1+i]
		}
		return strings.Trim(s, `"`)
	}
	if i := strings.IndexAny(s, " \t"); i >= 0 {
		return s[:i]
	}
	return s
}

// isCSAClientHook: is this hook entry OURS (any subcommand, any form)?
//
// Identify by the BINARY, not by its args. The old check was `args == ["hook"]`, which recognised
// only the ambient hook — so a `ship`/`ship --sweep` entry (or a shell-form entry like the plugin
// writes) would not be recognised, and every re-run of setup would append a duplicate instead of
// replacing it: double-shipping, double recall, and double extraction spend server-side. Matching
// the binary also means `uninstall` can actually find everything it installed.
//
// The suffix is `/csa-client` (not bare `csa-client`) so a third-party binary named
// `.../acme-csa-client` is NOT mistaken for ours and deleted on uninstall.
func isCSAClientHook(h map[string]any) bool {
	cmd, _ := h["command"].(string)
	args, hasArgs := h["args"].([]any)
	bin := hookBinary(cmd, hasArgs && len(args) > 0)
	return bin == "csa-client" || bin == "csa-client.exe" ||
		strings.HasSuffix(bin, "/csa-client") || strings.HasSuffix(bin, "/csa-client.exe") ||
		strings.HasSuffix(bin, `\csa-client.exe`) // windows path separator
}

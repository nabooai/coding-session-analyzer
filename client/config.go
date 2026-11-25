// Config + consent record for csa-client: ~/.csa/config.json, mode 0600.
// The consent gate lives here as pure, testable predicates. Only `setup` and `init` write a
// consent record, and nothing ships without a current one.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// requiredPolicyVersion — bump this when WHAT IS SHIPPED changes (new data classes, weaker
// redaction, retention changes...). A consent recorded against an older version is void:
// shipping stops (the watcher exits 78) until the user re-runs setup or init and accepts again.
// v2: backfill — existing session history may ship once (chosen at init, default yes),
// replacing v1's "nothing retroactive" promise.
const requiredPolicyVersion = 2

// exitConfig = EX_CONFIG from sysexits.h: missing/unusable configuration (incl. no consent).
// Distinct from a crash so supervisors and humans can tell the two apart.
const exitConfig = 78

// exitTLS: the server's certificate is not trusted, so nothing can ship.
const exitTLS = 75

type consentRecord struct {
	PolicyVersion     int    `json:"policy_version"`
	AcceptedAt        string `json:"accepted_at"` // RFC3339
	Method            string `json:"method"`      // "interactive-tty" | "flag:--yes" | "browser" (the plugin)
	IdentityAtConsent string `json:"identity_at_consent"`
	// ArchiveDaysAtConsent is how long the server said it would KEEP the redacted transcript of a
	// session, at the moment this person agreed. 0 = "it keeps none", which is every consent
	// recorded before retention existed and remains the default.
	//
	// Retention is exactly the kind of change requiredPolicyVersion exists for — but a bare version
	// bump voids consent on every laptop in the fleet, including all the ones shipping to servers
	// that retain nothing, and a voided consent means exit 78 and a machine that silently stops
	// shipping. So the disclosure is value-carrying instead: shipGate refuses only when the server
	// now keeps transcripts LONGER than the number this user actually saw and accepted. Nothing
	// changes for anyone until an operator turns retention up, and then exactly the affected
	// machines ask their humans again.
	ArchiveDaysAtConsent int `json:"archive_days_at_consent,omitempty"`
}

type config struct {
	Server    string `json:"server"`
	Identity  string `json:"identity"`
	AuthToken string `json:"auth_token,omitempty"` // shared bearer secret for token-auth servers; sent as Authorization: Bearer on every request
	CFID      string `json:"cf_access_client_id"`
	CFSecret  string `json:"cf_access_client_secret"`
	Backfill  bool   `json:"backfill"`  // ship pre-existing sessions once (chosen at init)
	Delegated bool   `json:"delegated"` // honor per-session .owner sidecars (bot/CI hosts only)
	// SweepFrom (RFC3339) bounds `ship --sweep` to sessions modified at/after this instant. Set by
	// `setup` when backfill was DECLINED; empty means "no floor" (backfill accepted, or a config
	// written by `init`, whose backfill ledger already governs history). Without it the first sweep
	// on a fresh install would ship the user's whole ~/.claude/projects — nothing has a marker yet.
	SweepFrom string `json:"sweep_from,omitempty"`
	// ServerArchiveDays caches what the server last told us it retains (/api/client-config, polled
	// by the SessionStart sweep). Cached rather than fetched per ship because SessionEnd must not
	// grow a network round-trip: the sweep refreshes it at least as often as sessions start, so the
	// exposure of a retention increase is bounded by one session rather than being unbounded.
	ServerArchiveDays int            `json:"server_archive_days,omitempty"`
	Redaction         *redactPolicy  `json:"redaction,omitempty"` // redaction mode chosen at init; nil -> secrets
	Consent           *consentRecord `json:"consent,omitempty"`
	// Shipping is "plugin" or "plugin-manual" when the Claude Code plugin owns this config, and absent
	// when this client does. Each client ships only its own, so neither can resume on the other's consent.
	Shipping string `json:"shipping,omitempty"`
}

// redactionMode is the effective redaction mode: the configured one, or "secrets" when unset.
// Secrets is the shipped-data default, so a nil field is NOT a policy change — which is why
// requiredPolicyVersion does not bump for this feature (see docs/redaction.md).
func (c *config) redactionMode() string {
	if c != nil && c.Redaction != nil && c.Redaction.Mode != "" {
		return c.Redaction.Mode
	}
	return "secrets"
}

// keepForever is the server's "kept until its owner or a workspace admin deletes it" retention
// (archive_days = -1): longer than any number of days.
const keepForever = -1

// daysOrNone reads a disclosed window that may be unknown (nil: the server could not name the
// workspace before sign-in) as the number a consent records. Unknown is consent to none.
func daysOrNone(p *int) int {
	if p == nil {
		return 0
	}
	return *p
}

// archiveDaysConsented is the retention this user accepted; 0 for every pre-retention consent.
func (c *config) archiveDaysConsented() int {
	if c != nil && c.Consent != nil {
		return c.Consent.ArchiveDaysAtConsent
	}
	return 0
}

// retentionExceedsConsent reports whether the server now keeps transcripts for longer than this
// user agreed to. It is deliberately a > and not a !=: an operator LOWERING retention (or turning
// it off) needs nobody's permission — less is kept than was disclosed, which cannot surprise anyone.
func (c *config) retentionExceedsConsent() bool {
	if c == nil {
		return false
	}
	server, agreed := c.ServerArchiveDays, c.archiveDaysConsented()
	if agreed == keepForever {
		return false
	}
	return server == keepForever || server > agreed
}

// retentionWords is how a retention window reads in a notice: "for 30 days", or forever.
func retentionWords(days int) string {
	switch {
	case days == keepForever:
		return "until its owner or a workspace admin deletes it"
	case days > 0:
		return fmt.Sprintf("for %d days", days)
	}
	return "none"
}

func configPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "config.json")
}

func loadConfig(path string) (*config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, err
	}
	return &c, nil
}

// saveConfig writes the config 0600 (it can hold CF Access credentials), creating the
// containing directory 0700. Chmod backstops WriteFile, which only sets mode on create.
func saveConfig(path string, c *config) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	// Atomic: write a sibling, then rename. The sweep now rewrites this file whenever the server's
	// retention window changes, from a DETACHED background process on a laptop that can be closed
	// mid-write. A truncated config makes loadConfig fail, and every gate here reads "no config" as
	// "do not ship" — so a half-written file is a machine that goes silent forever. The rename is
	// the same idiom the server uses for its queue and extraction caches.
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o600); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o600); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}

// consentOK is the trust boundary: nil config, missing consent, a consent recorded against an older
// policy version, or a config the plugin owns all mean NO — this client must not ship.
func (c *config) consentOK() bool {
	return c != nil && c.Consent != nil && c.Consent.PolicyVersion >= requiredPolicyVersion && c.Shipping == ""
}

// identityDrift: true when the configured identity and the live Claude account email are
// BOTH real emails and disagree — the machine changed accounts; consent doesn't transfer.
// An OS-username fallback identity (no "@") never counts as drift (warn-and-continue case).
func identityDrift(configured, live string) bool {
	return strings.Contains(configured, "@") && strings.Contains(live, "@") && configured != live
}

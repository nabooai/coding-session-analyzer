// authstatus.go — a small persisted marker so the ambient recall hook (which runs on every
// prompt, synchronously, with a real reply channel back to the user) can warn about a dead
// credential that was actually discovered by `send` (the detached SessionEnd/SessionStart hook,
// which has NO user-visible channel by design — see ship.go). Neither writer needs the other to
// be present; whichever authenticated call fails first marks it, and the very next prompt's hook
// invocation can surface it without an extra network round trip of its own.
//
// Self-healing: any authenticated call that succeeds clears the marker, so there is no separate
// "dismiss this warning" step — fixing the credential (`csa-client setup`) is what clears it.
package main

import (
	"os"
	"path/filepath"
	"time"
)

// authWarnCooldown bounds how often the ambient hook re-surfaces the warning once a credential
// is known broken: once per prompt would bury every real answer under a banner for however long
// the user takes to notice; once ever would let them forget. An hour is a compromise, not a
// measured value.
const authWarnCooldown = time.Hour

func authBrokenPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "auth_broken")
}

// markAuthBroken records that an authenticated request was just rejected (401). Idempotent by
// design: only the transition from "fine" to "broken" writes the file, so a burst of 401s (every
// hook fires this while the credential is dead) does not keep resetting authWarnDue's cooldown
// clock underneath it. Best-effort — a write failure here is not worth surfacing; there is
// already a real problem.
func markAuthBroken() {
	p := authBrokenPath()
	if _, err := os.Stat(p); err == nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return
	}
	_ = os.WriteFile(p, nil, 0o600) // empty content: broken, never warned yet
}

// clearAuthBroken removes the marker after ANY authenticated call succeeds. Call this on every
// 2xx from an authenticated endpoint, not just the ones that also mattered for other reasons —
// proving the credential works is the only fact this file tracks.
func clearAuthBroken() {
	_ = os.Remove(authBrokenPath())
}

// authWarnDue reports whether the ambient hook should surface a re-auth warning right now: the
// marker must exist (some authenticated call really did see a 401), and either it has never
// warned yet or authWarnCooldown has elapsed since the last time it did. A "yes" answer also
// stamps the marker with the current time, so the caller does not need a second write — one
// read-if-due-then-write, not read-then-separately-write.
func authWarnDue() bool {
	p := authBrokenPath()
	b, err := os.ReadFile(p)
	if err != nil {
		return false // no marker: nothing has reported a broken credential
	}
	if len(b) > 0 {
		if last, err := time.Parse(time.RFC3339, string(b)); err == nil && time.Since(last) < authWarnCooldown {
			return false
		}
	}
	_ = os.WriteFile(p, []byte(time.Now().UTC().Format(time.RFC3339)), 0o600)
	return true
}

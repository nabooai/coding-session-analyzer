package main

import (
	"os"
	"testing"
	"time"
)

func TestAuthWarnDueLifecycle(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	if authWarnDue() {
		t.Fatal("authWarnDue() with no marker at all: want false")
	}

	markAuthBroken()
	if !authWarnDue() {
		t.Fatal("authWarnDue() right after markAuthBroken(): want true (never warned yet)")
	}
	if authWarnDue() {
		t.Fatal("authWarnDue() called again immediately: want false — the first call should have stamped the cooldown")
	}

	clearAuthBroken()
	if authWarnDue() {
		t.Fatal("authWarnDue() after clearAuthBroken(): want false — the marker is gone")
	}
}

func TestMarkAuthBrokenDoesNotResetAnExistingWarnTimestamp(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	markAuthBroken()
	authWarnDue() // consumes the "never warned yet" state, stamps now()

	// Simulate a burst of further 401s (e.g. every send while the token stays dead) — none of
	// them should push the cooldown clock forward, or a busy machine would re-warn constantly.
	markAuthBroken()
	markAuthBroken()

	if authWarnDue() {
		t.Fatal("authWarnDue() should still be within cooldown — markAuthBroken() must not reset an already-set warn timestamp")
	}
}

func TestAuthWarnDueRespectsCooldownExpiry(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	markAuthBroken()
	// Pre-date the marker past the cooldown window, as if it was warned an hour+ ago.
	stale := time.Now().UTC().Add(-authWarnCooldown - time.Minute).Format(time.RFC3339)
	if err := os.WriteFile(authBrokenPath(), []byte(stale), 0o600); err != nil {
		t.Fatal(err)
	}

	if !authWarnDue() {
		t.Fatal("authWarnDue() with a stale (past-cooldown) timestamp: want true")
	}
}

func TestClearAuthBrokenIsSafeWhenNothingIsMarked(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	clearAuthBroken() // must not panic or error on a no-op
	if authWarnDue() {
		t.Fatal("authWarnDue() with nothing ever marked: want false")
	}
}

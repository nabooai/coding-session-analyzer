//go:build linux || darwin

package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestShipSweepByHandDoesNotWaitOnTheTerminal(t *testing.T) {
	if testing.Short() {
		t.Skip("builds a binary")
	}
	bin := filepath.Join(t.TempDir(), "csa-client")
	if out, err := exec.Command("go", "build", "-o", bin, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, "ship", "--sweep")
	cmd.Env = append(os.Environ(), "HOME="+t.TempDir())
	cmd.Stdin = openPTY(t)
	if err := cmd.Run(); err != nil {
		t.Fatalf("`csa-client ship --sweep` on a terminal: %v (ctx: %v)", err, ctx.Err())
	}
}

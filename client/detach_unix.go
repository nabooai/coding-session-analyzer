//go:build !windows

package main

import "syscall"

// detachAttr gives the backgrounded ship its own session. Without it the child stays in the hook's
// process group, and SessionEnd fires exactly when Claude Code (or the closing terminal, via SIGHUP)
// is tearing that group down — which would kill a ship mid-POST.
func detachAttr() *syscall.SysProcAttr { return &syscall.SysProcAttr{Setsid: true} }

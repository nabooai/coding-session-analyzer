//go:build windows

package main

import "syscall"

// No setsid on Windows, and a released child already outlives its parent there. If Claude
// Code ever runs hooks inside a job object on Windows, a mid-POST ship dies with the session —
// revisit (CREATE_BREAKAWAY_FROM_JOB) when a Windows user actually reports it.
func detachAttr() *syscall.SysProcAttr { return nil }

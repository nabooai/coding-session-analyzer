// ANSI styling for the interactive commands. The watcher's log lines stay plain: launchd logs
// must not carry escape codes. Everything degrades to plain strings when stdout isn't a terminal
// or NO_COLOR is set (https://no-color.org).
package main

import (
	"fmt"
	"os"
	"sync"
	"time"
)

// styled gates every escape code below: NO_COLOR (any non-empty value) wins, then the
// same ModeCharDevice check the wizard already uses for stdin, applied to stdout.
var styled = styleOK(os.Getenv("NO_COLOR"), os.Stdout)

func styleOK(noColor string, out *os.File) bool {
	if noColor != "" {
		return false
	}
	fi, err := out.Stat()
	return err == nil && fi.Mode()&os.ModeCharDevice != 0
}

func paint(code, s string) string {
	if !styled {
		return s
	}
	return "\x1b[" + code + "m" + s + "\x1b[0m"
}

func bold(s string) string { return paint("1", s) }
func dim(s string) string  { return paint("2", s) }
func cyan(s string) string { return paint("36", s) } // URLs and copy-pasteable commands

func okMark() string   { return paint("32", "✓") }
func warnMark() string { return paint("33", "!") }
func failMark() string { return paint("31", "✗") }

// spin animates a braille spinner at the current cursor position until stop() is called.
// It draws nothing for the first 300ms (fast steps stay clean) and nothing at all when
// styling is off. stop() is idempotent and returns only after the glyph is erased, so
// callers can print the step result immediately.
func spin() (stop func()) {
	if !styled {
		return func() {}
	}
	done, cleared := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(cleared)
		frames := []rune("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
		tick := time.NewTicker(80 * time.Millisecond)
		defer tick.Stop()
		start, drew := time.Now(), false
		for i := 0; ; i++ {
			select {
			case <-done:
				if drew {
					fmt.Print(" \b") // erase the glyph; the rest of the line stays
				}
				return
			case <-tick.C:
				if time.Since(start) < 300*time.Millisecond {
					continue
				}
				fmt.Print(string(frames[i%len(frames)]) + "\b") // draw, park cursor on it
				drew = true
			}
		}
	}()
	var once sync.Once
	return func() { once.Do(func() { close(done); <-cleared }) }
}

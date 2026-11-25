// `csa-client ship`: the SessionEnd hook. Claude Code pipes {session_id, transcript_path, ...} on
// stdin; ship applies the same consent gate and ignore/only exclusions as the watcher, then redacts
// and POSTs /api/ingest via send(). A hook must never break the user's session: every failure path
// logs to stderr (stdout is the hook protocol) and exits 0.
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// shipInputEnv carries the hook's JSON across the re-exec in shipDetach. Its presence is also how
// the child knows it is the child.
const shipInputEnv = "CSA_SHIP_INPUT"

// shipLogPath: where a detached ship's stderr lands. Append-only, no rotation: a couple of lines
// per session.
func shipLogPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "ship.log")
}

// shipEvent is the part of the Claude Code hook JSON the ship paths use: SessionEnd delivers both
// fields, SessionStart only session_id (which the sweep excludes as the session starting now).
type shipEvent struct {
	SessionID      string `json:"session_id"`
	TranscriptPath string `json:"transcript_path"`
}

// shipDetach hands this ship/sweep to a background copy of ourselves, returning the reader the work
// should read its event from and whether the caller is the parent (and must therefore exit now).
//
// This is what keeps shipping off the user's critical path. /api/ingest runs `claude -p` server-side
// and legitimately takes minutes, and Claude Code waits for a hook's process AND its stdout pipe — so
// a hook that POSTs inline stalls the user for exactly as long as the server is slow. That is not a
// server bug to tune away: the client must never make a person wait on shipping, however busy the
// server is. The child holds neither pipe (stdio -> /dev/null) and gets its own session, so it also
// outlives SessionEnd, where Claude Code is exiting and a process-group kill would otherwise take a
// mid-POST ship down with it.
//
// Failure to re-exec returns (payload, false) — the caller ships inline. Slow beats silently dropped.
func shipDetach(stdin io.Reader) (io.Reader, bool) {
	if v, ok := os.LookupEnv(shipInputEnv); ok {
		return strings.NewReader(v), false // we ARE the child — do the work
	}
	// Decode in the parent, and pass the event by value: Decode returns at the closing brace without
	// waiting for stdin's EOF (io.ReadAll would hang whenever Claude Code holds the pipe open), and a
	// pipe to the child would need this process to stay alive to feed it — the very thing we avoid.
	// A terminal on stdin is a person running `ship --sweep` by hand, not a hook: no event will ever
	// arrive, and decoding would block until they type one.
	var ev shipEvent
	if f, ok := stdin.(*os.File); !ok || !isTerminal(f) {
		_ = json.NewDecoder(stdin).Decode(&ev) // absent/garbage stdin -> zero value; the runners gate on it
	}
	payload, _ := json.Marshal(ev) // two strings: cannot fail (invalid UTF-8 is coerced, not errored)
	inline := strings.NewReader(string(payload))
	exe, err := os.Executable()
	if err != nil {
		return inline, false
	}
	null, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		return inline, false
	}
	defer null.Close()
	cmd := exec.Command(exe, os.Args[1:]...)
	// stdout -> /dev/null: that is the hook protocol channel Claude Code waits on, and holding it open
	// would stall the prompt even though this process exits. stderr -> ~/.csa/ship.log, NOT /dev/null:
	// this file's contract is that every failure path logs to stderr, and a detached child's stderr has
	// nowhere else to go. Without it identity drift and every send() failure would be undiagnosable.
	errOut := null
	if lf, err := os.OpenFile(shipLogPath(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600); err == nil {
		defer lf.Close()
		errOut = lf
	}
	cmd.Stdin, cmd.Stdout, cmd.Stderr = null, null, errOut
	cmd.Env = append(os.Environ(), shipInputEnv+"="+string(payload))
	cmd.SysProcAttr = detachAttr()
	if err := cmd.Start(); err != nil {
		return inline, false
	}
	_ = cmd.Process.Release() // never Wait: waiting is the whole thing we are getting rid of
	return nil, true
}

// shippedMarkerPath: per-session dedup marker (~/.csa/shipped/<sid>) holding the transcript
// byte size at the last successful ship. Read only by exact session id — never globbed — so the
// .inflight sibling claimShip writes beside it cannot be mistaken for a marker.
func shippedMarkerPath(sid string) string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "shipped", sid)
}

// shipClaimTTL bounds how long one ship may hold its in-flight claim. send()'s own HTTP timeout is 180s
// (main.go), so a claim older than this belongs to a killed child, not a slow one — take it over rather
// than strand that session forever.
const shipClaimTTL = 5 * time.Minute

// claimShip takes an exclusive, cross-process claim on shipping `sid`; the loser skips. The shipped
// marker cannot do this job: it is written only AFTER send() returns (45-75s of server-side `claude -p`).
// While ship ran inline that was safe by accident — the SessionEnd hook BLOCKED, so Claude Code could
// not start the next session until the marker existed. Detaching the ship removed that serialization:
// on `/clear`, SessionStart fires milliseconds after SessionEnd, so the sweep looks for a marker that
// the still-in-flight ship has not written yet and re-ships the same session. Two POSTs, two billed
// extractions (the server's incremental SKIP can't save us either — it needs the prior record on disk,
// which is written after extraction, and the two POSTs are concurrent).
//
// O_EXCL is the atomicity. Returns (path, true) when claimed — release it with releaseShip.
func claimShip(sid string) (string, bool) {
	path := shippedMarkerPath(sid) + ".inflight"
	if os.MkdirAll(filepath.Dir(path), 0o700) != nil {
		return "", true // can't claim: prefer a possible duplicate over a dropped session
	}
	take := func() bool {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err != nil {
			return false
		}
		f.Close()
		return true
	}
	if take() {
		return path, true
	}
	if fi, err := os.Stat(path); err == nil && time.Since(fi.ModTime()) > shipClaimTTL {
		// Steal by RENAME, not Remove. os.Remove(path) here would be a TOCTOU: it deletes whatever sits
		// at the path *now*, which may be a fresh claim another racer took between our Stat and our
		// Remove — freeing it while that ship is mid-POST, so a third ship could claim the same session.
		// Rename is atomic on the inode: exactly one racer wins it, and whoever wins owns the cleanup.
		// The loser falls through to take(), where O_EXCL settles it — one winner, no double ship.
		if os.Rename(path, path+".stale") == nil {
			_ = os.Remove(path + ".stale")
		}
		if take() {
			return path, true
		}
	}
	return "", false // another ship of this session is in flight right now
}

func releaseShip(path string) {
	if path != "" {
		_ = os.Remove(path)
	}
}

// shipGate: the config/consent/identity checks every shipping path must clear, plus the request
// context send() reads from globals. Returns the server URL, or "" when this machine must not ship.
// Extracted from runShip so `ship --sweep` cannot accidentally ship on weaker terms than SessionEnd
// does — there is exactly one gate, and both callers go through it.
func shipGate() string {
	cfg, err := loadConfig(configPath())
	if err != nil || !cfg.consentOK() {
		return "" // no current consent on record — same trust boundary as runWatcher, but silent
	}
	if live := claudeEmail(); identityDrift(cfg.Identity, live) {
		log.Printf("ship: identity changed (%s at consent, %s now). Not shipping until you re-run 'csa-client setup'.", cfg.Identity, live)
		return ""
	}
	// NOTE: the retention gate is deliberately NOT here. It lives at the two call sites, because
	// runSweep has to refresh the cached window BEFORE it can honour it — see retentionPaused.
	server := strings.TrimRight(env("CSA_SERVER", cfg.Server), "/")
	if server == "" {
		return ""
	}
	// Same request context runWatcher sets before send(): owner, auth, redaction policy,
	// and (delegated hosts only) the sidecar watch root.
	ownerID = cfg.Identity
	if ownerID == "" {
		ownerID = owner()
	}
	cfID, cfSecret = cfg.CFID, cfg.CFSecret
	authToken = resolveAuthToken(cfg.AuthToken)
	redactMode = cfg.redactionMode()
	if cfg.Delegated {
		home, _ := os.UserHomeDir()
		watchRoot = env("CSA_ROOT", filepath.Join(home, ".claude", "projects"))
	}
	return server
}

// shipOne ships a single transcript if it has grown past its marker. Returns true if it shipped.
// Callers must have cleared shipGate() first.
func shipOne(server, sid, path string) bool {
	if reason := excludedReason(path, loadIgnores(ignorePath()), loadIgnores(onlyPath())); reason != "" {
		log.Printf("ship: skipping %s (%s)", sid, reason)
		return false
	}
	fi, err := os.Stat(path)
	if err != nil {
		log.Printf("ship: stat %s: %v", path, err)
		return false
	}
	// Duplicate-SessionEnd guard: a size high-water mark per session instead of
	// mtime/hash bookkeeping. SessionEnd can fire more than once (clear/resume/logout); ship
	// only when the transcript GREW past the last shipped size. The sweep reuses the same marker,
	// so a session that already shipped on SessionEnd is never re-sent.
	marker := shippedMarkerPath(sid)
	shipped := func() bool {
		b, err := os.ReadFile(marker)
		if err != nil {
			return false
		}
		last, _ := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
		return fi.Size() <= last
	}
	if shipped() {
		return false
	}
	// The marker only closes the window AFTER send() returns; claimShip closes it DURING (see there).
	claim, ok := claimShip(sid)
	if !ok {
		return false // another ship of this session is mid-POST — it will write the marker
	}
	defer releaseShip(claim)
	if shipped() { // re-check under the claim: the ship we raced may have finished between the two
		return false
	}
	if ok, _ := send(server, sid, path); !ok {
		return false // send() already logged the failure; exit 0 — best-effort shipping
	}
	if err := os.MkdirAll(filepath.Dir(marker), 0o700); err == nil {
		_ = os.WriteFile(marker, []byte(strconv.FormatInt(fi.Size(), 10)), 0o600)
	}
	return true
}

// runShip handles one SessionEnd event, best-effort. Silent return (exit 0) on every gate:
// unconfigured, unconsented, identity drift, bad input, excluded, unchanged, or transport
// failure. It never prompts and never writes stdout.
func runShip(stdin io.Reader) {
	server := shipGate()
	if server == "" {
		return
	}
	var in struct {
		SessionID      string `json:"session_id"`
		TranscriptPath string `json:"transcript_path"`
	}
	if err := json.NewDecoder(stdin).Decode(&in); err != nil || in.SessionID == "" || in.TranscriptPath == "" {
		return
	}
	if cfg, err := loadConfig(configPath()); err == nil && retentionPaused(cfg) {
		return // paused on retention; the SessionStart sweep is what re-checks and can un-pause
	}
	shipOne(server, in.SessionID, in.TranscriptPath)
}

// retentionPaused reports (and logs) whether this machine must hold off because the server keeps
// transcripts for longer than its owner agreed to. One string for both call sites: a developer who
// greps ship.log must find ONE explanation of one condition, not two near-identical ones.
//
// The check is against the CACHED window, which is only meaningful because runSweep refreshes that
// cache BEFORE calling this (see the ordering there). An earlier version refused inside shipGate,
// which runs first in runSweep — so a paused machine never reached its own poll, never learned the
// operator had lowered retention again, and stayed dark forever. Lowering retention needs nobody's
// permission, and that is only true if a paused machine keeps listening.
func retentionPaused(cfg *config) bool {
	if !cfg.retentionExceedsConsent() {
		return false
	}
	log.Printf("ship: paused. This server keeps transcripts %s and you agreed to %s. "+
		"Nothing is being sent and nothing is lost. Run `csa-client setup --backfill` to see the new "+
		"terms and accept or decline them. Shipping resumes on its own if the server lowers it again.",
		retentionWords(cfg.ServerArchiveDays), retentionWords(cfg.archiveDaysConsented()))
	return true
}

// sweepChunk bounds how many catch-up sessions one sweep ships. A SessionStart hook must return
// fast (its stdout is injected into the session, and it holds up the prompt), and a --backfill user
// with hundreds of sessions must not ship them all inside one 240s hook. The marker makes the sweep
// resumable — each shipped session is skipped next time — so a large history drains over successive
// SessionStarts instead of one burst.
const sweepChunk = 10

// --- fleet directives (server -> this machine) -------------------------------------------------
// Configuration used to flow one way only: a laptop decided what it shipped and the server had no
// say. So "we lost two weeks of your sessions, please re-send them" had no remote answer. A
// DIRECTIVE is one admin instruction — ship everything since <date> — fetched here, at SessionStart,
// which is both the natural beat (it already runs on every session) and the only place that knows
// how to sweep. See server/analyzer/fleet.py for the plane on the other side.

// clientConfig is what the server wants this machine to do.
type clientConfig struct {
	ArchiveDays int `json:"archive_days"` // how long IT keeps transcripts — gated against consent
	Directives  []struct {
		ID    string `json:"id"`
		Since string `json:"since"` // RFC3339 floor; "" = all history
	} `json:"directives"`
}

// fetchClientConfig polls /api/client-config. Best-effort with a short timeout: this runs inside a
// SessionStart hook, which holds up someone's prompt, so a slow or down server must cost a couple of
// seconds and nothing else. (nil, false) means "no instructions" — never an error the user sees.
func fetchClientConfig(server string) (*clientConfig, bool) {
	req, err := http.NewRequest("GET", server+"/api/client-config", nil)
	if err != nil {
		return nil, false
	}
	addAuthHeaders(req)
	c := &http.Client{Timeout: 8 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		return nil, false
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, false // incl. an older server that 404s this route
	}
	var cc clientConfig
	if json.NewDecoder(resp.Body).Decode(&cc) != nil {
		return nil, false
	}
	return &cc, true
}

// ackDirective reports what this machine did. Best-effort: a lost ack costs the operator a stale
// "waiting" row in the console, and the directive is simply offered again on the next sweep — which
// is the right failure direction. Never retries inline (this is hook-time).
func ackDirective(server, id, status string, shipped int, detail string) {
	body, _ := json.Marshal(map[string]any{"id": id, "status": status, "shipped": shipped, "detail": detail})
	req, err := http.NewRequest("POST", server+"/api/client-config/ack", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	addAuthHeaders(req)
	c := &http.Client{Timeout: 8 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		log.Printf("fleet: could not tell the server what happened (%v). It will ask again.", err)
		return
	}
	resp.Body.Close()
}

// directiveLedgerPath is a running count of what a directive has shipped so far, in the same
// marker-file idiom as ~/.csa/shipped/<sid> and ~/.csa/backfilled.
//
// It exists because sweepChunk caps a sweep at 10 sessions while a directive can ask for months.
// The server stops offering a directive once a machine ACKS it, so acking after a capped pass would
// strand the rest of the history forever. Instead: ack only when a pass finishes UNDER the cap (the
// work is done), and until then accumulate the count here so the number the operator finally sees is
// the true total rather than the last chunk's.
// safeDirectiveID: hex only. filepath.Base blocks "../" but passes "." and "..", and the id comes
// from the server — `clearDirectiveProgress(".")` would then os.Remove the directives DIRECTORY.
// Real ids are token_hex(8), so this rejects nothing a well-behaved server sends.
func safeDirectiveID(id string) string {
	if id == "" || len(id) > 64 {
		return ""
	}
	for _, c := range id {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return ""
		}
	}
	return id
}

func directiveLedgerPath(id string) string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "directives", safeDirectiveID(id))
}

func directiveProgress(id string) int {
	if safeDirectiveID(id) == "" {
		return 0
	}

	b, err := os.ReadFile(directiveLedgerPath(id))
	if err != nil {
		return 0
	}
	n, _ := strconv.Atoi(strings.TrimSpace(string(b)))
	if n < 0 {
		return 0
	}
	return n
}

func setDirectiveProgress(id string, n int) {
	if safeDirectiveID(id) == "" {
		return
	}

	p := directiveLedgerPath(id)
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err == nil {
		_ = os.WriteFile(p, []byte(strconv.Itoa(n)), 0o600)
	}
}

func clearDirectiveProgress(id string) {
	if safeDirectiveID(id) == "" {
		return
	}
	_ = os.Remove(directiveLedgerPath(id))
}

// applyDirectives folds the server's instructions into this sweep. Returns the (possibly earlier)
// floor to sweep from, and the directives that were actually accepted.
//
// A directive NEVER overrides the local decision. `cfg.Backfill == false` is a person who was asked
// "also index your existing sessions?" and said no; an admin asking for history does not overturn
// that, it just gets told. The consent record is the authority on what may leave this machine —
// that is the whole point of having one.
func applyDirectives(server string, cfg *config, cc *clientConfig, since time.Time) (time.Time, []string) {
	var accepted []string
	for _, d := range cc.Directives {
		if !cfg.Backfill {
			log.Printf("fleet: an administrator asked for your sessions from %s. This machine is set to "+
				"keep its history private, so nothing was sent and they have been told. "+
				"Run `csa-client setup --backfill` if you want to share it.", orAll(d.Since))
			// Phrased as the machine's SETTING, never as the person's decision: `setup` can turn this
			// off without being asked to (it does not inherit the previous choice), and an admin
			// console reading "declined" next to a name would blame a human for that.
			ackDirective(server, d.ID, "declined", 0, "history sharing is off on this machine")
			continue
		}
		if d.Since == "" {
			log.Printf("fleet: an administrator asked for every session on this machine. Sending them now.")
		} else {
			log.Printf("fleet: an administrator asked for your sessions from %s. Sending them now.", d.Since[:10])
		}
		if d.Since == "" { // all history: no floor at all
			since = time.Time{}
			accepted = append(accepted, d.ID)
			continue
		}
		t, err := time.Parse(time.RFC3339, d.Since)
		if err != nil {
			// The server validates `since` at creation, so this is a server we cannot understand.
			// Refuse rather than guess: a misread floor is either a silent no-op or an unintended
			// full-history upload, and both are worse than saying so.
			log.Printf("fleet: ignoring a request from the server: it carries a date this client "+
				"cannot read (%q). Nothing was sent.", d.Since)
			ackDirective(server, d.ID, "error", 0, "unreadable since")
			continue
		}
		// ONLY EVER WIDEN. `since.IsZero()` means there is no floor at all — this machine already
		// ships its whole history — which is the LOWEST possible floor, not the absence of one.
		// Treating it as "unset" and assigning the directive's date raised the floor instead, so
		// asking a machine for history made it ship LESS than it did before, and an "all history"
		// directive could be undone by a dated one later in the same response while both acked
		// `applied`. A directive can only ever reach further back.
		if !since.IsZero() && t.Before(since) {
			since = t
		}
		accepted = append(accepted, d.ID)
	}
	return since, accepted
}

// orAll renders a directive's floor for a human: a bare date, or "the beginning" for all history.
// Never the raw RFC3339 instant — "2026-08-19T00:00:00+00:00" in a log line a developer is meant to
// act on is noise standing where a date should be.
func orAll(s string) string {
	if s == "" {
		return "the beginning"
	}
	if len(s) >= 10 {
		return s[:10]
	}
	return s
}

// sessionAlreadyShipped reports whether shipOne returned false merely because there was nothing to
// do — the session is at or below its marker. Distinguishing that from a real failure is what lets a
// pass tell "everything is already sent" (ack it, the directive is done) from "the server is
// refusing us" (do not ack, or the request is retired having moved nothing).
func sessionAlreadyShipped(sid string, s session) bool {
	b, err := os.ReadFile(shippedMarkerPath(sid))
	if err != nil {
		return false
	}
	last, _ := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
	fi, err := os.Stat(s.path)
	if err != nil {
		return true // unreadable now; not a shipping failure
	}
	return fi.Size() <= last
}

// runSweep is the catch-up path: SessionEnd never fires for a CRASHED session, so those
// transcripts would sit on disk forever (this is the gap that kept the watcher daemon alive).
// Wired as a SessionStart hook by `setup`, it re-checks transcripts against their markers and ships
// the ones that grew. Reuses shipOne, so every gate — consent, identity, ignore/only, redaction,
// the marker — applies identically. `stdin` carries the SessionStart hook JSON (or is empty for a
// manual run).
//
// It never ships history the user did not consent to: cfg.SweepFrom is stamped at setup time when
// backfill was declined, and anything last modified before then is skipped. Without that, the first
// sweep on a fresh install would upload the user's entire ~/.claude/projects — nothing has a marker
// yet. `init --backfill` / `setup --backfill` leave SweepFrom empty, which means "history included".
func runSweep(stdin io.Reader) {
	server := shipGate()
	if server == "" {
		return
	}
	cfg, err := loadConfig(configPath())
	if err != nil {
		return
	}
	var since time.Time
	if cfg.SweepFrom != "" {
		if t, err := time.Parse(time.RFC3339, cfg.SweepFrom); err == nil {
			since = t
		} else {
			log.Printf("ship: unreadable sweep_from %q — sweeping nothing (re-run 'csa-client setup')", cfg.SweepFrom)
			return // a corrupt stamp must not be read as "ship everything"
		}
	}
	// Ask the server what it wants: the retention it now enforces, and any history it is asking for.
	// Best-effort — no answer means no instructions, and the sweep proceeds exactly as it always did.
	//
	// This runs BEFORE the retention gate on purpose. It is the only thing that refreshes the cached
	// window, so a machine that is currently paused must still get here; otherwise lowering retention
	// on the server could never un-pause anyone.
	cc, polled := fetchClientConfig(server)
	if polled && cc.ArchiveDays != cfg.ServerArchiveDays {
		cfg.ServerArchiveDays = cc.ArchiveDays // cached so SessionEnd honours it without a fetch
		if err := saveConfig(configPath(), cfg); err != nil {
			log.Printf("ship: could not record the server's retention window: %v", err)
		}
	}
	if retentionPaused(cfg) {
		return // nothing ships, and no directive is acted on, under terms this person never accepted
	}
	var accepted []string
	if polled {
		since, accepted = applyDirectives(server, cfg, cc, since)
	}
	// The session that is STARTING right now (SessionStart delivers its id on stdin) is excluded: its
	// SessionEnd hook will ship it when it ends. Without this, the sweep ships it near-empty, then
	// SessionEnd ships it full — two /api/ingest calls, two server-side extractions, per session.
	current := ""
	if stdin != nil {
		var in struct {
			SessionID string `json:"session_id"`
		}
		if json.NewDecoder(stdin).Decode(&in) == nil {
			current = in.SessionID
		}
	}
	home, _ := os.UserHomeDir()
	root := env("CSA_ROOT", filepath.Join(home, ".claude", "projects"))
	// Filter here rather than letting shipOne log a line per exclusion. The sweep walks EVERY transcript
	// on disk (1000+ is normal), and sweepChunk caps only successful ships, not skips — so with an
	// `only`/`ignore` list set (csa-client only pick) that is one log line per transcript per
	// SessionStart. Cheap when it went to /dev/null; ~92 KB a pop now that stderr is a real file.
	ign, only := loadIgnores(ignorePath()), loadIgnores(onlyPath())
	shipped, excluded, failed := 0, 0, 0
	for sid, s := range entries(root) {
		if shipped >= sweepChunk {
			break // resumable via markers — the rest catches up on the next SessionStart
		}
		if sid == current {
			continue // its own SessionEnd will ship it; don't double-ship
		}
		if !since.IsZero() && s.mtime.Before(since) {
			continue // predates this install's consent — not ours to ship
		}
		if excludedReason(s.path, ign, only) != "" {
			excluded++ // counted, not logged: one line each would be the whole disk budget
			continue
		}
		if shipOne(server, sid, s.path) {
			shipped++
		} else if !sessionAlreadyShipped(sid, s) {
			failed++ // a real transport/server failure, not "already sent" or "excluded"
			if tlsFailed {
				break // every further send fails the same handshake and would log the advice again
			}
		}
	}
	if shipped > 0 || excluded > 0 {
		log.Printf("ship: swept %d catch-up session(s), %d excluded", shipped, excluded)
	}
	// Report on the admin's directives. A pass that hit the chunk cap is NOT done — acking it would
	// stop the server offering it and strand the rest of the history — so it only banks its progress
	// and reports on a later, uncapped pass.
	// Acking `applied` RETIRES a directive: the server stops offering it to this machine, forever.
	// So a pass may only ack when it has nothing left to do — which means BOTH that it did not hit
	// the chunk cap AND that nothing failed.
	//
	// An earlier version bailed only when `shipped == 0`, which caught a totally wedged server and
	// missed the ordinary case: 3 sessions through, 20 refused, under the cap. That acked `applied`
	// with a green tick and stranded the 20 permanently. Partial success is not success; it is
	// "come back later", exactly like hitting the cap, and it banks its progress the same way.
	//
	// A session that fails PERMANENTLY therefore keeps the request open rather than closing it as
	// done. That is the honest reading — those sessions really have not arrived — and the console
	// says which machine it is still waiting on. Closing it would be a lie that loses the data
	// silently, which is the failure this whole ack exists to prevent.
	incomplete := shipped >= sweepChunk || failed > 0
	for _, id := range accepted {
		total := directiveProgress(id) + shipped
		if incomplete {
			setDirectiveProgress(id, total)
			if failed > 0 {
				log.Printf("fleet: sent %d session(s) for the administrator's request; %d could not be "+
					"sent. Keeping the request open and trying the rest at your next session start.",
					total, failed)
			} else {
				log.Printf("fleet: sending your history. %d session(s) so far, more the next time you "+
					"start a session.", total)
			}
			continue
		}
		ackDirective(server, id, "applied", total, "")
		clearDirectiveProgress(id)
		log.Printf("fleet: finished the administrator's request. %d session(s) sent.", total)
	}
	if failed > 0 && len(accepted) == 0 {
		log.Printf("fleet: %d session(s) could not be sent this time; they will be retried.", failed)
	}
}

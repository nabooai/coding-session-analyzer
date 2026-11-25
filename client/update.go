// Self-update for csa-client. Downloads GET <server>/latest/<asset> from the CSA server this
// client is configured against, the same files install.sh pulls, not from GitHub: the release
// repo is private, and the server is the one host every client already reaches.
//
// Discovery is a CHECKSUM COMPARE, not a version compare — checksums.txt is already
// published, so this needs no new endpoint and no server deploy. The ceiling: a hash says
// "different", not "newer", so a server pinned to an older build than yours reads as an update
// and `update` would walk you backwards. Publish a version marker next to checksums.txt and
// compare semver if that ever bites. The user-facing wording says "different", never "newer".
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Read caps for anything the server sends. Neither body is trusted before it is verified, and an
// unbounded io.ReadAll on a hostile or misconfigured server is an OOM. checksums.txt is ~450
// bytes today; the binary is ~6MB.
const (
	maxChecksumsBytes = 64 << 10
	maxAssetBytes     = 96 << 20
)

// updateHTTP refuses to follow a redirect to a different host. addAuthHeaders puts CF-Access
// service-token headers on these requests, and Go forwards CUSTOM headers across every redirect
// hop — it strips only Authorization/Cookie/WWW-Authenticate. A server (or an on-path attacker)
// answering /latest/ with a 302 to attacker.example would otherwise harvest the tenant's CF
// credentials. A binary download has no business redirecting off-origin anyway.
var updateHTTP = &http.Client{
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if req.URL.Host != via[0].URL.Host {
			return fmt.Errorf("refusing cross-host redirect to %s", req.URL.Host)
		}
		if len(via) >= 5 {
			return fmt.Errorf("too many redirects")
		}
		return nil
	},
}

// assetName is this platform's release asset (matches the release workflow's naming and the
// exact set _ASSET_RE allows the server to serve).
func assetName() string {
	n := fmt.Sprintf("csa-client-%s-%s", runtime.GOOS, runtime.GOARCH)
	if runtime.GOOS == "windows" {
		n += ".exe"
	}
	return n
}

// stampedBuild reports whether a build stamped version (a server image's onprem-<sha>, a release's
// v0.1.17) rather than leaving the "dev" of a plain `go build`. Only stamped builds get the passive
// notice: a local build never matches the published hash, so notifying on it would fire on every
// invocation forever. `update` itself ignores this — ask explicitly and you get to update.
func stampedBuild(v string) bool {
	return v != "" && v != "dev"
}

// isSHA256Hex is the shape gate on anything we treat as a checksum: exactly 64 hex digits.
// Without it a truncated, md5sum-generated or hand-edited checksums.txt yields a short string
// that callers slice for display and panic on, and a garbage value silently becomes the thing
// verifySum compares against. Pure; tested.
func isSHA256Hex(s string) bool {
	if len(s) != 64 {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

// parseChecksums reads sha256sum format ("<hex>  <filename>" per line) into asset -> lowercase
// hex. Lines that are blank, malformed, or don't carry a well-formed sha256 are skipped rather
// than failing the whole file — a bad line must never enter the map, because everything
// downstream treats a present entry as a usable checksum. Pure; tested.
func parseChecksums(body []byte) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(string(body), "\n") {
		f := strings.Fields(line)
		if len(f) != 2 {
			continue
		}
		if sum := strings.ToLower(f[0]); isSHA256Hex(sum) {
			out[f[1]] = sum
		}
	}
	return out
}

// verifySum confirms data hashes to want. A mismatch is a hard failure: this is the gate between
// "downloaded some bytes" and "replaced the running binary". Pure; tested.
func verifySum(data []byte, want string) error {
	if !isSHA256Hex(strings.ToLower(want)) {
		return fmt.Errorf("no usable published checksum to verify against")
	}
	got := hex.EncodeToString(sha256Sum(data))
	if got != strings.ToLower(want) {
		return fmt.Errorf("checksum mismatch (want %s, got %s)", want, got)
	}
	return nil
}

func sha256Sum(b []byte) []byte {
	h := sha256.Sum256(b)
	return h[:]
}

// cleartextRemote reports whether base is plain HTTP to somewhere other than this machine — the
// case where an on-path attacker rewrites the binary AND its checksum together, so the sha256
// proves nothing. server/install.sh warns on exactly this; the stake is higher here because we
// are replacing an already-trusted, already-installed binary. Pure; tested.
func cleartextRemote(base string) bool {
	u, err := url.Parse(base)
	if err != nil || u.Scheme != "http" {
		return false
	}
	switch u.Hostname() {
	case "127.0.0.1", "localhost", "::1", "":
		return false
	}
	return true
}

// updateServer resolves the server to update from and populates the auth globals addAuthHeaders
// reads, mirroring how `team` bootstraps. /latest/ is public at the origin, but a deployment can
// still sit behind Cloudflare Access, so the edge creds go on the request regardless.
func updateServer() string {
	server := strings.TrimSpace(os.Getenv("CSA_SERVER"))
	cfgToken := ""
	if cfg, err := loadConfig(configPath()); err == nil {
		if server == "" {
			server = cfg.Server
		}
		cfID, cfSecret = cfg.CFID, cfg.CFSecret
		cfgToken = cfg.AuthToken
	}
	authToken = resolveAuthToken(cfgToken)
	return strings.TrimRight(server, "/")
}

func httpGetBytes(ctx context.Context, url string, limit int64) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	addAuthHeaders(req)
	resp, err := updateHTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s -> HTTP %d", url, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("%s is larger than the %d-byte cap — refusing", url, limit)
	}
	return body, nil
}

// selfSum is the sha256 of the running binary, resolved through any symlink so we hash (and later
// replace) the real file rather than the link.
func selfSum() (path, sum string, err error) {
	exe, err := os.Executable()
	if err != nil {
		return "", "", err
	}
	if resolved, rerr := filepath.EvalSymlinks(exe); rerr == nil {
		exe = resolved
	}
	b, err := os.ReadFile(exe)
	if err != nil {
		return exe, "", err
	}
	return exe, hex.EncodeToString(sha256Sum(b)), nil
}

// publishedSum fetches checksums.txt from the server and returns this platform's entry.
func publishedSum(ctx context.Context, base string) (string, error) {
	body, err := httpGetBytes(ctx, base+"/latest/checksums.txt", maxChecksumsBytes)
	if err != nil {
		return "", err
	}
	return parseChecksums(body)[assetName()], nil
}

// swapBinary replaces exe with the verified bytes at tmpPath. POSIX renames happily over a
// running binary — the process keeps its inode — but Windows locks the running image and fails
// the implicit delete, so move the old one aside first and put it back if the swap fails.
func swapBinary(tmpPath, exe string) error {
	if runtime.GOOS != "windows" {
		return os.Rename(tmpPath, exe)
	}
	old := exe + ".old"
	os.Remove(old) // a previous update's leftover, still locked back then; ignore failure
	if err := os.Rename(exe, old); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, exe); err != nil {
		os.Rename(old, exe) // better a working old binary than none
		return err
	}
	return nil // `old` is still locked by this process; the next run's os.Remove clears it
}

// notifyIfUpdate prints one line if the server publishes a different build than the one running —
// best-effort and silent on any failure (no server configured, offline, dev/lab build).
//
// Gated on `styled` (stdout is a TTY) so the scripted callers of `csa-client version` — the brew
// formula's assert_match and release.yml — never pay a network round-trip or read an extra line.
// Reuses the color signal rather than adding a second TTY check; NO_COLOR therefore
// also silences the notice, which is fine — those users can run `update` on their own schedule.
func notifyIfUpdate() {
	if !stampedBuild(version) || !styled {
		return
	}
	base := updateServer()
	if base == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	want, err := publishedSum(ctx, base)
	if err != nil || want == "" {
		return
	}
	_, mine, err := selfSum()
	if err != nil || mine == want {
		return
	}
	// "different", not "newer" — a hash cannot tell direction (see the file header).
	fmt.Printf("%s %s publishes a different csa-client than the one running  (run %s)\n",
		warnMark(), base, cyan(selfInvocation()+" update"))
}

// runUpdate is `csa-client update`: compare this binary against the one the server publishes,
// ask Y/N, then download + checksum-verify + swap it in place, and restart the login agent so
// the new inode is what's actually running.
func runUpdate(args []string) {
	yes := false
	for _, a := range args {
		switch a {
		case "--yes", "-y":
			yes = true
		default:
			fatalf(2, "unknown flag for update: %s (accepts --yes)", a)
		}
	}

	base := updateServer()
	if base == "" {
		fatalf(exitConfig, "%s no server configured — run %s first", failMark(), cyan(selfInvocation()+" setup"))
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	fmt.Print("Checking " + base + " for updates ... ")
	stop := spin()
	want, err := publishedSum(ctx, base)
	stop()
	if err != nil {
		fatalf(1, "%s couldn't reach %s (%v)\n  Is the server up, and are your CF-Access creds current?", failMark(), base, err)
	}
	if want == "" {
		fatalf(1, "%s %s publishes no usable checksum for %s (see docs/public-release/install-server.md)",
			failMark(), base, assetName())
	}
	exe, mine, err := selfSum()
	if err != nil {
		fatalf(1, "can't read my own binary at %s: %v", exe, err)
	}
	if mine == want {
		fmt.Printf("%s already up to date (%s)\n", okMark(), version)
		return
	}
	fmt.Printf("A different csa-client is published on %s.\n  running: %s (%s…)\n  server:  %s…\n",
		base, bold(version), mine[:12], want[:12])
	if cleartextRemote(base) {
		fmt.Printf("  %s %s is plain HTTP — an on-path attacker can rewrite BOTH the binary and its\n"+
			"  checksum, so the sha256 below proves nothing. Only continue on a trusted network.\n",
			warnMark(), base)
	}
	if !yes && !confirm("Update now?") {
		fmt.Println("Skipped. Run " + cyan(selfInvocation()+" update") + " any time.")
		return
	}

	asset := assetName()
	fmt.Printf("Downloading %s ... ", asset)
	stop = spin()
	dctx, dcancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer dcancel()
	body, derr := httpGetBytes(dctx, base+"/latest/"+asset, maxAssetBytes)
	stop()
	if derr != nil {
		fatalf(1, "%s download failed (%v)", failMark(), derr)
	}
	if err := verifySum(body, want); err != nil {
		fatalf(1, "%s %v — refusing to install an unverified binary", failMark(), err)
	}

	// Write beside the target so the rename is atomic (same filesystem), then swap. fatalf is
	// os.Exit, which does NOT run deferred functions — so every failure path below removes the
	// temp file explicitly, or a failed update leaves a stray 0755 binary next to the real one
	// and accumulates another on every retry.
	tmp, err := os.CreateTemp(filepath.Dir(exe), ".csa-update-")
	if err != nil {
		fatalf(1, "can't write next to %s: %v", exe, err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(body); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		fatalf(1, "write failed: %v", err)
	}
	tmp.Close()
	if err := os.Chmod(tmpPath, 0o755); err != nil {
		os.Remove(tmpPath)
		fatalf(1, "chmod: %v", err)
	}
	if err := swapBinary(tmpPath, exe); err != nil {
		os.Remove(tmpPath)
		fatalf(1, "%s couldn't replace %s: %v", failMark(), exe, err)
	}
	fmt.Printf("%s installed to %s\n", okMark(), cyan(exe))
	restartAgent()
}

// restartAgent kicks the login agent so the freshly-swapped binary is the one running. launchd
// holds the old inode until the process restarts; kickstart -k does exactly that. Elsewhere we
// can only tell the user how to restart.
func restartAgent() {
	switch runtime.GOOS {
	case "darwin":
	case "windows":
		fmt.Println("  Restart csa-client to run the new version.")
		return
	default:
		fmt.Printf("  Restart the client to run the new version (e.g. %s).\n",
			cyan("systemctl --user restart csa-client"))
		return
	}
	home, _ := os.UserHomeDir()
	agents := csaAgents(home)
	if len(agents) == 0 {
		agents = []launchAgent{{label: launchdLabel}}
	}
	uid := strconv.Itoa(os.Getuid())
	for _, a := range agents {
		target := "gui/" + uid + "/" + a.label
		if out, err := launchctl("kickstart", "-k", target); err != nil {
			fmt.Printf("  %s installed, but couldn't restart the background agent (%v: %s).\n"+
				"  It runs the new version at next login, or restart now: %s\n",
				warnMark(), err, strings.TrimSpace(string(out)), cyan("launchctl kickstart -k "+target))
			return
		}
	}
	fmt.Printf("%s background agent restarted on the new version.\n", okMark())
}

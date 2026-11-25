package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestParseChecksums(t *testing.T) {
	// Real shape of the file the server publishes at /latest/checksums.txt.
	body := `0da6842ef4da40342e8ab57b92de50e7a798d3bdfb68dd662fe047657e20bd36  csa-client-darwin-amd64
e0eaa161a2e48dcb1545c9550311d7f599a2de92f51435892b79612db2d48270  csa-client-darwin-arm64

not-a-checksum-line
83dbf11e959c9945771b85134b4b2ce64b2d390b6b99a6b8e484a6fea62d2925  csa-client-linux-amd64
`
	got := parseChecksums([]byte(body))
	if len(got) != 3 {
		t.Fatalf("want 3 usable entries, got %d: %v", len(got), got)
	}
	if got["csa-client-darwin-arm64"] != "e0eaa161a2e48dcb1545c9550311d7f599a2de92f51435892b79612db2d48270" {
		t.Errorf("darwin-arm64 = %q", got["csa-client-darwin-arm64"])
	}
	if _, ok := got["csa-client-windows-amd64.exe"]; ok {
		t.Error("invented an entry that is not in the file")
	}
}

// Regression: a short/garbage checksum used to reach runUpdate's display, which slices want[:12]
// and panicked with "slice bounds out of range [:12] with length 8". A malformed line must never
// enter the map at all — everything downstream treats a present entry as a usable checksum.
func TestParseChecksumsRejectsMalformedSums(t *testing.T) {
	real := hex.EncodeToString(sha256Sum([]byte("x")))
	for _, bad := range []string{
		"deadbeef",               // truncated — the panic repro
		strings.Repeat("a", 32),  // md5-length
		strings.Repeat("a", 63),  // one short
		strings.Repeat("a", 65),  // one long
		strings.Repeat("zz", 32), // right length, not hex
	} {
		got := parseChecksums([]byte(bad + "  " + assetName() + "\n"))
		if sum, ok := got[assetName()]; ok {
			t.Errorf("accepted malformed checksum %q -> %q", bad, sum)
		}
	}
	// ...and a well-formed one still lands, including upper-case input.
	got := parseChecksums([]byte(strings.ToUpper(real) + "  " + assetName() + "\n"))
	if got[assetName()] != real {
		t.Errorf("well-formed checksum did not survive: %q", got[assetName()])
	}
	// Whatever survives is always safe to slice for display, which is what panicked.
	for _, sum := range got {
		_ = sum[:12]
	}
}

// A binary swap over plain HTTP is the case where an attacker rewrites the payload AND its
// checksum together, so the sha256 proves nothing — it must be called out, but only when the
// server is actually remote.
func TestCleartextRemote(t *testing.T) {
	for in, want := range map[string]bool{
		"http://csa.example.com":  true,
		"http://10.0.0.5:8766":    true,
		"https://csa.example.com": false,
		"http://127.0.0.1:8766":   false,
		"http://localhost:8766":   false,
		"":                        false,
	} {
		if got := cleartextRemote(in); got != want {
			t.Errorf("cleartextRemote(%q) = %v, want %v", in, got, want)
		}
	}
}

// verifySum is the gate between "downloaded some bytes" and "replaced the running binary", so
// every way it can fail must stay a hard error — a silent pass installs an unverified binary.
func TestVerifySumRefusals(t *testing.T) {
	payload := []byte("payload")
	good := hex.EncodeToString(sha256Sum(payload))

	if err := verifySum(payload, good); err != nil {
		t.Errorf("matching checksum should pass, got %v", err)
	}
	if err := verifySum(payload, strings.ToUpper(good)); err != nil {
		t.Errorf("checksum compare must be case-insensitive, got %v", err)
	}
	tampered := hex.EncodeToString(sha256Sum([]byte("tampered")))
	if err := verifySum(payload, tampered); err == nil {
		t.Error("mismatched checksum MUST fail")
	}
	if err := verifySum(payload, ""); err == nil {
		t.Error("absent checksum MUST fail, not pass by default")
	}
	if err := verifySum(payload, "deadbeef"); err == nil {
		t.Error("a malformed (non-sha256) checksum MUST fail, not be compared")
	}
}

// The CF-Access service-token headers ride on these requests, and Go copies CUSTOM headers across
// every redirect hop. A server answering /latest/ with a redirect elsewhere must not harvest them.
func TestRefusesCrossHostRedirect(t *testing.T) {
	evil := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("followed a cross-host redirect and sent headers to the other host: %v", r.Header.Get("CF-Access-Client-Secret"))
	}))
	defer evil.Close()

	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, evil.URL+"/latest/checksums.txt", http.StatusFound)
	}))
	defer redirector.Close()

	if _, err := publishedSum(context.Background(), redirector.URL); err == nil {
		t.Error("a cross-host redirect MUST be refused, not followed")
	}
}

// An unbounded io.ReadAll on an untrusted server is an OOM; the cap must reject, not truncate
// (a truncated body would fail the checksum, but silently reading GBs first is the problem).
func TestOversizedBodyRefused(t *testing.T) {
	big := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write(make([]byte, maxChecksumsBytes+1024))
	}))
	defer big.Close()

	if _, err := publishedSum(context.Background(), big.URL); err == nil {
		t.Error("a body over the cap MUST be refused")
	} else if !strings.Contains(err.Error(), "cap") {
		t.Errorf("want a cap error, got %v", err)
	}
}

// The server image stamps its own version, so its builds get the passive notice too; a plain
// `go build` never matches a published hash and must not nag on every invocation.
func TestStampedBuild(t *testing.T) {
	for in, want := range map[string]bool{
		"v0.1.17":        true,
		"onprem-9185248": true,
		"lab-9185248":    true,
		"1.0.0":          true,
		"dev":            false,
		"":               false,
	} {
		if got := stampedBuild(in); got != want {
			t.Errorf("stampedBuild(%q) = %v, want %v", in, got, want)
		}
	}
}

func TestAssetNameStem(t *testing.T) {
	// Must match the release workflow's naming AND _ASSET_RE on the server, or /latest/<asset> 404s.
	if got := assetName(); !strings.HasPrefix(got, "csa-client-") {
		t.Errorf("assetName() = %q, want a csa-client-<os>-<arch> stem", got)
	}
}

// End-to-end over real HTTP against a stub that speaks the server's /latest/ contract: the
// checksum published there is what publishedSum returns, and a 404 is an error, not "".
func TestPublishedSumOverHTTP(t *testing.T) {
	want := hex.EncodeToString(sha256Sum([]byte("binary-bytes")))
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/latest/checksums.txt" {
			w.WriteHeader(404)
			return
		}
		w.Write([]byte(want + "  " + assetName() + "\n"))
	}))
	defer srv.Close()

	got, err := publishedSum(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("publishedSum: %v", err)
	}
	if got != want {
		t.Errorf("publishedSum = %q, want %q", got, want)
	}

	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
	}))
	defer bad.Close()
	if _, err := publishedSum(context.Background(), bad.URL); err == nil {
		t.Error("a 404 from the server MUST be an error, not an empty checksum")
	}
}

// Guard the hash the whole flow turns on: selfSum must hash the file on disk, byte for byte.
func TestSelfSumMatchesFileOnDisk(t *testing.T) {
	path, sum, err := selfSum()
	if err != nil {
		t.Fatalf("selfSum: %v", err)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	h := sha256.Sum256(b)
	if hex.EncodeToString(h[:]) != sum {
		t.Errorf("selfSum(%s) does not match sha256 of the file on disk", path)
	}
}

package main

import (
	"bytes"
	"crypto/x509"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func untrustedServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"ok":true}`)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestUntrustedCertificateNamesTheCertificateAndTheFix(t *testing.T) {
	srv := untrustedServer(t)
	_, err := checkHealth(srv.URL)
	var cert *untrustedCertError
	if !errors.As(err, &cert) {
		t.Fatalf("checkHealth error = %v, want *untrustedCertError", err)
	}
	for _, want := range []string{"TLS certificate is not trusted", "operating system certificate store", "System keychain", "Claude Code hooks do not inherit"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("advice lacks %q: %s", want, err)
		}
	}
	msg := unreachableMessage(srv.URL, err)
	if strings.Contains(msg, "VPN") || strings.Contains(msg, "server is up") {
		t.Errorf("a certificate failure must not blame the network: %s", msg)
	}
}

func TestUnreachableMessageKeepsNetworkAdviceForOtherFailures(t *testing.T) {
	msg := unreachableMessage("https://x", errors.New("connection refused"))
	if !strings.Contains(msg, "VPN/proxy") {
		t.Errorf("plain network failure lost its advice: %s", msg)
	}
}

func TestShipOnCertificateErrorLogsCertificateAndFlagsExit(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	tlsFailed = false
	defer func() {
		tlsFailed = false
		ownerID, cfID, cfSecret, authToken, watchRoot, redactMode = "", "", "", "", "", ""
	}()
	path := filepath.Join(home, "s.jsonl")
	if err := os.WriteFile(path, []byte(`{"type":"user"}`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	ok, down := send(untrustedServer(t).URL, "s", path)

	if ok || !down {
		t.Errorf("send = (%v, %v), want (false, true)", ok, down)
	}
	if !tlsFailed {
		t.Error("tlsFailed not set, so ship would exit 0")
	}
	out := buf.String()
	if !strings.Contains(out, "certificate") || strings.Contains(out, "server down?") {
		t.Errorf("ship.log must say certificate, not server down: %s", out)
	}
	if !strings.Contains(tlsBrokenAdvice(), "certificate") {
		t.Errorf("the parent hook has nothing to exit on: %q", tlsBrokenAdvice())
	}
}

func TestACertificateMarkerClearsAfterATrustedSend(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	markTLSBroken("certificate advice")
	if tlsBrokenAdvice() != "certificate advice" {
		t.Fatalf("marker not readable: %q", tlsBrokenAdvice())
	}
	clearTLSBroken()
	if tlsBrokenAdvice() != "" {
		t.Error("marker survived clearTLSBroken")
	}
}

func TestAHostnameMismatchIsFixedOnTheServer(t *testing.T) {
	err := x509.HostnameError{Host: "csa.example"}
	if strings.Contains(certAdvice(err), "Trust the CA") || !strings.Contains(certAdvice(err), "reissue") {
		t.Errorf("a hostname failure is fixed on the server, not in the keychain: %s", certAdvice(err))
	}
}

func TestRegisterMCPCommandNeverShowsTheToken(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	args := []string{"mcp", "add", "csa", "https://x/mcp",
		"--header", "Authorization: Bearer sekret-token-123",
		"--header", "CF-Access-Client-Secret: cfsekret"}
	shown := strings.Join(maskedHeaders(args), " ")
	if strings.Contains(shown, "sekret") {
		t.Errorf("printed command leaks a secret: %s", shown)
	}
	if !strings.Contains(shown, configPath()) {
		t.Errorf("printed command must point at the config file: %s", shown)
	}
	if args[5] != "Authorization: Bearer sekret-token-123" {
		t.Error("maskedHeaders must not mutate the real args")
	}
}

func TestTeamWithNoShippersDoesNotClaimFirst(t *testing.T) {
	r, w, _ := os.Pipe()
	old := os.Stdout
	os.Stdout = w
	renderTeam([]teamMember{{User: "me@x.com", Sessions: 1}}, "me@x.com", "", "https://csa")
	w.Close()
	os.Stdout = old
	var b bytes.Buffer
	b.ReadFrom(r)
	if strings.Contains(b.String(), "first") || !strings.Contains(b.String(), "No teammate has shipped sessions yet") {
		t.Errorf("unexpected team text: %s", b.String())
	}
}

func TestDoctorReportsStalledExtraction(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/health" {
			fmt.Fprint(w, `{"ok":true,"extraction":{"stalled":true,"reason":"the model provider rejected the API key"}}`)
			return
		}
		fmt.Fprint(w, `[]`)
	}))
	defer srv.Close()
	sec := doctorConnectivitySection(&config{Server: srv.URL})
	var got string
	for _, r := range sec.results {
		if r.level == dFail {
			got = r.line
		}
	}
	if !strings.Contains(got, "not extracting") || !strings.Contains(got, "rejected the API key") {
		t.Errorf("doctor did not surface the stall: %+v", sec.results)
	}
}

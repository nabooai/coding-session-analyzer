package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// certError reports whether err is the server's TLS certificate failing verification.
func certError(err error) bool {
	var unknown x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	var host x509.HostnameError
	var verify *tls.CertificateVerificationError
	return errors.As(err, &unknown) || errors.As(err, &invalid) || errors.As(err, &host) || errors.As(err, &verify)
}

// certAdvice names the certificate problem and the one fix that reaches Claude Code hooks.
func certAdvice(err error) string {
	var unknown x509.UnknownAuthorityError
	if !errors.As(err, &unknown) {
		return fmt.Sprintf("the server's TLS certificate failed verification (%v)\n"+
			"  -> The certificate itself is wrong for this address or expired; ask whoever runs the server to reissue it.", err)
	}
	return fmt.Sprintf("the server's TLS certificate is not trusted (%v)\n"+
		"  -> Trust the CA that signed it in the operating system certificate store "+
		"(macOS: add it to the System keychain and set it to Always Trust; Linux: update-ca-certificates).\n"+
		"  -> SSL_CERT_FILE and NODE_EXTRA_CA_CERTS only affect the shell they are set in; Claude Code hooks do not inherit them.", err)
}

func tlsBrokenPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".csa", "tls_broken")
}

func markTLSBroken(advice string) {
	if err := os.MkdirAll(filepath.Dir(tlsBrokenPath()), 0o700); err == nil {
		_ = os.WriteFile(tlsBrokenPath(), []byte(advice), 0o600)
	}
}

func clearTLSBroken() { _ = os.Remove(tlsBrokenPath()) }

// tlsBrokenAdvice is what the last detached ship learned about the certificate, or "" when it was fine.
func tlsBrokenAdvice() string {
	b, err := os.ReadFile(tlsBrokenPath())
	if err != nil {
		return ""
	}
	return string(b)
}

// untrustedCertError is checkHealth's answer to a certificate failure, carrying the advice.
type untrustedCertError struct{ advice string }

func (e *untrustedCertError) Error() string { return e.advice }

// unreachableMessage is what setup prints when the health check fails; a certificate failure
// names the certificate instead of blaming the network.
func unreachableMessage(server string, err error) string {
	var cert *untrustedCertError
	if errors.As(err, &cert) {
		return fmt.Sprintf("cannot connect to %s\n  %v\n\nNothing was changed.", server, err)
	}
	return fmt.Sprintf("cannot reach %s\n  %v\n\nCheck the URL, your VPN/proxy, and that the server is up. Nothing was changed.", server, err)
}

// maskedHeaders replaces credential header values so a printed command never carries a secret;
// the real values live in the client config.
func maskedHeaders(args []string) []string {
	out := append([]string(nil), args...)
	for i, a := range out {
		switch {
		case strings.HasPrefix(a, "Authorization: Bearer "):
			out[i] = "Authorization: Bearer <auth_token from " + configPath() + ">"
		case strings.HasPrefix(a, "CF-Access-Client-Secret: "):
			out[i] = "CF-Access-Client-Secret: <cf_secret from " + configPath() + ">"
		}
	}
	return out
}

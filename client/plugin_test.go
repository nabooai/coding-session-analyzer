package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPluginEnabledFrom(t *testing.T) {
	cases := []struct {
		name           string
		list, settings string
		want           bool
	}{
		{"user scope", `[{"id":"csa@csa","enabled":true}]`, ``, true},
		{"project scope", `[{"id":"csa@csa","enabled":false,"projectEnabled":true}]`, ``, true},
		{"disabled", `[{"id":"csa@csa","enabled":false}]`, `{"enabledPlugins":{"csa@csa":true}}`, false},
		{"another plugin", `[{"id":"other@csa","enabled":true}]`, ``, false},
		{"not installed", `[]`, `{"enabledPlugins":{"csa@csa":true}}`, false},
		{"no list, settings on", ``, `{"enabledPlugins":{"csa@csa":true}}`, true},
		{"no list, settings off", ``, `{"enabledPlugins":{"csa@csa":false}}`, false},
		{"garbage list, settings on", `not json`, `{"enabledPlugins":{"csa@csa":true}}`, true},
		{"nothing readable", ``, ``, false},
	}
	for _, c := range cases {
		if got := pluginEnabledFrom([]byte(c.list), []byte(c.settings)); got != c.want {
			t.Errorf("%s: got %v, want %v", c.name, got, c.want)
		}
	}
}

func TestShippingRoundTripsAndParksThisClient(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	cfg := &config{Shipping: "plugin-manual", Consent: &consentRecord{PolicyVersion: requiredPolicyVersion}}
	if err := saveConfig(path, cfg); err != nil {
		t.Fatal(err)
	}
	got, err := loadConfig(path)
	if err != nil || got.Shipping != "plugin-manual" {
		t.Fatalf("shipping did not survive a round trip: %+v, %v", got, err)
	}
	if got.consentOK() {
		t.Fatal("a config the plugin owns must not ship through this client's hooks")
	}
	got.Shipping = ""
	if !got.consentOK() {
		t.Fatal("no shipping field is this client's own config")
	}
	raw, _ := os.ReadFile(path)
	if err := saveConfig(path, got); err != nil {
		t.Fatal(err)
	}
	if raw, _ = os.ReadFile(path); strings.Contains(string(raw), "shipping") {
		t.Fatalf("an unset field must stay out of the file, the plugin's shell guard reads it: %s", raw)
	}
}

func TestDoctorPluginResult(t *testing.T) {
	cases := []struct {
		enabled, hooks bool
		level          doctorLevel
		has            string
	}{
		{true, true, dFail, "Pick one"},
		{true, false, dWarn, "/csa:connect"},
		{false, true, dOK, "not enabled"},
		{false, false, dOK, "not enabled"},
	}
	for _, c := range cases {
		r := doctorPluginResult(c.enabled, c.hooks)
		if r.level != c.level || !strings.Contains(r.line, c.has) {
			t.Errorf("%+v: got %+v", c, r)
		}
	}
}

func TestRetirePluginCredential(t *testing.T) {
	var seen string
	s := revokeServer(t, 200, map[string]any{"ok": true, "revoked": true}, &seen)
	retirePluginCredential(&config{Server: s.URL, AuthToken: "tok"})
	if seen != "" {
		t.Fatalf("this client's own config must not be revoked by a re-run of setup: %q", seen)
	}
	retirePluginCredential(nil)
	retirePluginCredential(&config{Server: s.URL, AuthToken: "tok", Shipping: "plugin"})
	if seen != "POST /api/token/revoke Bearer tok" {
		t.Fatalf("the plugin's credential was left live: %q", seen)
	}
}

func revokeServer(t *testing.T, status int, reply map[string]any, seen *string) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = r.Method + " " + r.URL.Path + " " + r.Header.Get("Authorization")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(reply)
	}))
	t.Cleanup(s.Close)
	return s
}

func TestUninstallCredential(t *testing.T) {
	cases := []struct {
		name        string
		shipping    string
		purge       bool
		status      int
		reply       map[string]any
		wantRequest bool
		wantToken   string
	}{
		{"revoked, config stays", "", false, 200, map[string]any{"ok": true, "revoked": true}, true, ""},
		{"already dead", "", false, 401, map[string]any{"ok": false}, true, ""},
		{"server refuses", "", false, 409, map[string]any{"ok": false, "error": "last admin"}, true, "tok"},
		{"plugin owns the config and stays", "plugin", false, 200, map[string]any{"ok": true}, false, "tok"},
		{"purge revokes the plugin's too", "plugin", true, 200, map[string]any{"ok": true, "revoked": true}, true, "tok"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Setenv("HOME", t.TempDir())
			t.Setenv("CSA_SERVER", "")
			var seen string
			s := revokeServer(t, c.status, c.reply, &seen)
			if err := saveConfig(configPath(), &config{Server: s.URL, AuthToken: "tok", Shipping: c.shipping}); err != nil {
				t.Fatal(err)
			}
			uninstallCredential(c.purge)
			if c.wantRequest && seen != "POST /api/token/revoke Bearer tok" {
				t.Fatalf("revoke request: %q", seen)
			}
			if !c.wantRequest && seen != "" {
				t.Fatalf("expected no request, got %q", seen)
			}
			got, err := loadConfig(configPath())
			if err != nil {
				t.Fatal(err)
			}
			if got.AuthToken != c.wantToken {
				t.Fatalf("auth_token after: %q, want %q", got.AuthToken, c.wantToken)
			}
		})
	}
}

func TestUninstallCredentialSaysSoWhenTheServerIsGone(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("CSA_SERVER", "")
	if err := saveConfig(configPath(), &config{Server: "http://127.0.0.1:1", AuthToken: "tok"}); err != nil {
		t.Fatal(err)
	}
	uninstallCredential(false)
	if got, _ := loadConfig(configPath()); got.AuthToken != "tok" {
		t.Fatal("a credential that could not be revoked must not be forgotten")
	}
}

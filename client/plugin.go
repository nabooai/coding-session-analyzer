// The Claude Code plugin (csa-plugin/) is the other client for this machine. It shares ~/.csa, so the
// two must never both ship: this client refuses to connect while the plugin is enabled, and the
// plugin stands down while this client's hooks or watcher exist.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

const pluginID = "csa@csa"

// pluginEnabledFrom reads `claude plugin list --json`, which sees every scope; when that is not
// readable, the user-scope enabledPlugins in settings.json is the best remaining answer.
func pluginEnabledFrom(list, settings []byte) bool {
	var plugins []struct {
		ID             string `json:"id"`
		Enabled        bool   `json:"enabled"`
		ProjectEnabled bool   `json:"projectEnabled"`
	}
	if len(list) > 0 && json.Unmarshal(list, &plugins) == nil {
		for _, p := range plugins {
			if p.ID == pluginID && (p.Enabled || p.ProjectEnabled) {
				return true
			}
		}
		return false
	}
	var root struct {
		EnabledPlugins map[string]bool `json:"enabledPlugins"`
	}
	return json.Unmarshal(settings, &root) == nil && root.EnabledPlugins[pluginID]
}

func pluginEnabled() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	list, err := exec.CommandContext(ctx, "claude", "plugin", "list", "--json").Output()
	if err != nil {
		list = nil
	}
	home, _ := os.UserHomeDir()
	settings, _ := os.ReadFile(filepath.Join(home, ".claude", "settings.json"))
	return pluginEnabledFrom(list, settings)
}

func refuseWhilePluginEnabled() {
	if pluginEnabled() {
		fatalf(exitConfig, "the CSA plugin is enabled in Claude Code, so csa-client would ship every session a "+
			"second time. Nothing was changed.\n  Keep the plugin, and skip this.\n  Or switch: claude plugin "+
			"disable %s, then run this again.", pluginID)
	}
}

// revokeOwnToken asks the server to end this machine's credential, and says plainly when it could not:
// an uninstall that reports success with a live token is the failure this exists to end.
func revokeOwnToken(cfg *config) (revoked bool) {
	server := env("CSA_SERVER", cfg.Server)
	if server == "" || cfg.AuthToken == "" {
		return false
	}
	req, err := http.NewRequest("POST", server+"/api/token/revoke", bytes.NewReader([]byte("{}")))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.AuthToken)
	c := &http.Client{Timeout: 10 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		fmt.Printf("%s could not reach %s to revoke this machine's credential (%v). It stays live until it idles out; an admin can revoke it from Team.\n",
			warnMark(), server, err)
		return false
	}
	defer resp.Body.Close()
	var r struct {
		OK      bool   `json:"ok"`
		Revoked bool   `json:"revoked"`
		Error   string `json:"error"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&r)
	switch {
	case resp.StatusCode == http.StatusUnauthorized:
		fmt.Printf("%s this machine's credential was already dead on %s\n", okMark(), server)
		return true
	case resp.StatusCode == 200 && r.OK:
		if r.Revoked {
			fmt.Printf("%s this machine's credential revoked on %s\n", okMark(), server)
		}
		return true
	}
	why := r.Error
	if why == "" {
		why = resp.Status
	}
	fmt.Printf("%s could not revoke this machine's credential on %s: %s\n", warnMark(), server, why)
	return false
}

// retirePluginCredential ends the plugin's token when this client takes the config over: nothing would
// present it again, and it would stay live until it idled out.
func retirePluginCredential(prev *config) {
	if prev != nil && prev.Shipping != "" {
		revokeOwnToken(prev)
	}
}

// uninstallCredential revokes unless the plugin owns the config and it is staying; a revoked token
// is dropped from a config that stays, so nothing later presents a dead one.
func uninstallCredential(purge bool) {
	cfg, err := loadConfig(configPath())
	if err != nil || (cfg.Shipping != "" && !purge) || !revokeOwnToken(cfg) || purge {
		return
	}
	cfg.AuthToken = ""
	if err := saveConfig(configPath(), cfg); err != nil {
		fmt.Printf("%s could not update %s: %v\n", warnMark(), configPath(), err)
	}
}

// doctorPluginResult reads a config this client owns: runDoctor hands a plugin-owned one to /csa:status.
func doctorPluginResult(enabled, hooksWired bool) doctorResult {
	switch {
	case enabled && hooksWired:
		return dFailf("the plugin is enabled and csa-client hooks are wired: the plugin stands down and warns. Pick one: claude plugin disable %s, or csa-client uninstall", pluginID)
	case enabled:
		return dWarnf("enabled, not connected: nothing ships through it until /csa:connect")
	}
	return dOKf("not enabled")
}

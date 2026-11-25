#!/bin/sh
# Guided CSA server installer. POSIX sh, safe to run again in the same install directory.
set -eu

REPO_RAW=${CSA_INSTALL_REPO_RAW:-https://raw.githubusercontent.com/nabooai/coding-session-analyzer/master/server}
INSTALL_DIR=${CSA_INSTALL_DIR:-$HOME/csa-server}
SOURCE_DIR=${CSA_INSTALL_SOURCE_DIR:-}
INPUT=${CSA_INSTALL_INPUT:-/dev/tty}

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
ask() {
    prompt=$1 default=$2
    printf '%s [%s]: ' "$prompt" "$default" >&2
    IFS= read -r answer < "$INPUT" || answer=
    [ -n "$answer" ] || answer=$default
    printf '%s' "$answer"
}
secret() {
    prompt=$1
    printf '%s: ' "$prompt" >&2
    old_stty=$(stty -g < "$INPUT" 2>/dev/null || true)
    [ -z "$old_stty" ] || stty -echo < "$INPUT"
    IFS= read -r answer < "$INPUT" || answer=
    [ -z "$old_stty" ] || { stty "$old_stty" < "$INPUT"; printf '\n' >&2; }
    printf '%s' "$answer"
}
json_escape() { printf '%s' "$1" | awk 'BEGIN{ORS=""} {if(NR>1) printf "\\n"; gsub(/\\/,"\\\\"); gsub(/\"/,"\\\""); printf "%s",$0}'; }
env_value() { printf '%s' "$1"; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required. Install it and run this command again."; }

check_host() {
    case $(uname -s 2>/dev/null || true) in Darwin|Linux) ;; *) die "CSA server installation supports macOS and Linux.";; esac
    need docker
    docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop or the Docker daemon."
    docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required. Install the Docker Compose plugin."
    compose_version=$(docker compose version --short 2>/dev/null | tr -d 'v' || true)
    major=$(printf '%s' "$compose_version" | cut -d. -f1); minor=$(printf '%s' "$compose_version" | cut -d. -f2)
    case $major$minor in ''|*[!0-9]*) ;; *) [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 20 ]; } || die "Docker Compose v2.20 or newer is required (found $compose_version).";; esac
    [ -n "$SOURCE_DIR" ] || need curl
}

fetch_kit() {
    mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data" "$INSTALL_DIR/certs"
    for pair in docker-compose.trial.yml:compose.yaml Caddyfile:Caddyfile; do
        source_file=${pair%%:*}
        target_file=${pair#*:}
        [ ! -f "$INSTALL_DIR/$target_file" ] || continue
        if [ -n "$SOURCE_DIR" ]; then
            cp "$SOURCE_DIR/$source_file" "$INSTALL_DIR/$target_file"
        else
            curl -fsSL "$REPO_RAW/$source_file" -o "$INSTALL_DIR/$target_file.tmp" || die "could not download $source_file from GitHub. Check network access, then rerun."
            mv "$INSTALL_DIR/$target_file.tmp" "$INSTALL_DIR/$target_file"
        fi
    done
}

write_bootstrap() {
    if [ -f "$INSTALL_DIR/.env" ] && [ -f "$INSTALL_DIR/config/config.json" ]; then
        say "Existing installation found at $INSTALL_DIR. Preserving its configuration and credentials."
        return
    fi
    if [ -f "$INSTALL_DIR/.env" ] || [ -f "$INSTALL_DIR/config/config.json" ]; then
        die "incomplete existing installation at $INSTALL_DIR. Restore the missing .env or config/config.json, then rerun; nothing was overwritten"
    fi
    [ -r "$INPUT" ] || die "guided setup needs a terminal. Download the installer first, then run: sh csa-install.sh"
    workspace=$(ask "Workspace name" "My workspace")
    admin=$(ask "Admin email" "you@example.com")
    public_url=$(ask "Public URL" "http://localhost:8766")
    case $public_url in *[!A-Za-z0-9:./_-]*) die "public URL contains unsupported characters";; esac
    case $public_url in http://localhost:8766|http://127.0.0.1:8766|https://?*) ;; *) die "use https:// for a network server, or http://localhost:8766 for a solo install";; esac
    case $public_url in */) die "public URL must not end in /";; esac
    case $admin in *@*.*) ;; *) die "admin email must be a complete email address";; esac

    say ""
    say "Model authentication:"
    say "  1. Anthropic API key. Tested now over the HTTPS Messages API."
    say "  2. OpenAI API key. Configure and test it in the welcome AI Models step."
    say "  3. None yet. Browsing works; extraction waits without losing sessions."
    say "Claude subscription login is only for a person's private local server and requires a privately built image."
    say "OpenAI subscription login is not available in this release. Team servers use API credentials."
    provider=$(ask "Choose 1, 2, or 3" "3")
    anthropic_key=
    openai_key=
    openai_main=
    openai_small=
    case $provider in
        1) anthropic_key=$(secret "Anthropic API key (input is hidden)"); [ -n "$anthropic_key" ] || die "an Anthropic API key cannot be empty";;
        2) openai_key=$(secret "OpenAI API key (input is hidden)"); [ -n "$openai_key" ] || die "an OpenAI API key cannot be empty"
           openai_main=$(ask "OpenAI main model" "gpt-5")
           openai_small=$(ask "OpenAI small model" "gpt-5-mini")
           case $openai_main$openai_small in *[!A-Za-z0-9._:/@-]*) die "model IDs may contain only letters, numbers, dot, underscore, colon, slash, @, and hyphen";; esac;;
        3) ;;
        *) die "choose 1, 2, or 3";;
    esac

    old_umask=$(umask); umask 077
    {
        printf 'CSA_PUBLIC_URL=%s\n' "$(env_value "$public_url")"
        case $public_url in http://localhost:8766) printf 'CSA_MCP_URL=http://localhost:58768/mcp\n';; http://127.0.0.1:8766) printf 'CSA_MCP_URL=http://127.0.0.1:58768/mcp\n';; esac
        [ -z "$anthropic_key" ] || printf 'ANTHROPIC_API_KEY=%s\n' "$(env_value "$anthropic_key")"
        if [ -n "$openai_key" ]; then
            printf 'OPENAI_API_KEY=%s\n' "$(env_value "$openai_key")"
            printf 'OPENAI_MAIN_MODEL=%s\n' "$(env_value "$openai_main")"
            printf 'OPENAI_SMALL_MODEL=%s\n' "$(env_value "$openai_small")"
        fi
    } > "$INSTALL_DIR/.env.tmp"
    mv "$INSTALL_DIR/.env.tmp" "$INSTALL_DIR/.env"
    printf '{"workspace":{"name":"%s","adminEmail":"%s"}}\n' "$(json_escape "$workspace")" "$(json_escape "$admin")" > "$INSTALL_DIR/config/config.json.tmp"
    mv "$INSTALL_DIR/config/config.json.tmp" "$INSTALL_DIR/config/config.json"
    chmod 600 "$INSTALL_DIR/.env" "$INSTALL_DIR/config/config.json"
    umask "$old_umask"
}

start() {
    cd "$INSTALL_DIR"
    public_url=$(awk -F= '$1=="CSA_PUBLIC_URL" {sub(/^[^=]*=/,""); print; exit}' .env)
    [ -n "$public_url" ] || die "$INSTALL_DIR/.env has no CSA_PUBLIC_URL"
    case $public_url in https://*) profile="--profile proxy";; *) profile="";; esac
    # shellcheck disable=SC2086
    docker compose $profile up -d || die "startup failed. Inspect with: cd $INSTALL_DIR && docker compose logs migrate api etl"
    say "Waiting for server health..."
    tries=0
    while :; do
        if docker compose exec -T api /app/.venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/api/health', timeout=2)" >/dev/null 2>&1; then break; fi
        tries=$((tries + 1)); [ "$tries" -lt 60 ] || die "server did not become healthy. Run: cd $INSTALL_DIR && docker compose ps -a && docker compose logs migrate api"
        sleep 2
    done

    if grep -Eq '^(ANTHROPIC_API_KEY|OPENAI_API_KEY)=.' .env; then
        provider_name=Anthropic
        grep -q '^OPENAI_API_KEY=.' .env && provider_name=OpenAI
        say "Testing the $provider_name credential and main model..."
        if ! docker compose exec -T etl /app/.venv/bin/python -c "from analyzer import llm; text,_,_=llm.complete('Reply only OK','main'); assert text.strip() == 'OK', text" >/dev/null; then
            die "$provider_name test failed. Check the key, model ID, endpoint, network, account usage terms, and provider logs with: cd $INSTALL_DIR && docker compose logs etl"
        fi
        say "$provider_name model test passed."
    fi

    link=$(docker compose logs migrate 2>/dev/null | awk '/admin sign-in link/ {sub(/^.*admin sign-in link/,"admin sign-in link"); print; exit}')
    say ""
    say "CSA is running."
    [ -z "$link" ] || say "$link"
    say "Browser: $public_url"
    say "Connect Claude Code with the readable plugin (installed disabled):"
    say "  claude plugin marketplace add nabooai/coding-session-analyzer#plugin-v0.2.0 --sparse .claude-plugin csa-plugin"
    say "  claude plugin install csa@csa"
    say "  claude plugin enable csa@csa"
    say "Then run in Claude Code: /csa:connect $public_url"
    say "Install directory: $INSTALL_DIR"
    say "Stop: cd $INSTALL_DIR && docker compose down"
    say "Troubleshoot: cd $INSTALL_DIR && docker compose ps -a && docker compose logs migrate api etl"
}

main() { check_host; fetch_kit; write_bootstrap; start; }
main "$@"

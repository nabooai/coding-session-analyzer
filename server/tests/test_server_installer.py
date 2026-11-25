"""The public guided server installer, exercised without Docker or real credentials."""
import os
import stat
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[2] / "install.sh"
SERVER = Path(__file__).resolve().parents[1]


def _run(tmp_path, answers, *, existing_env=None):
    install = tmp_path / "install"
    install.mkdir()
    if existing_env is not None:
        (install / ".env").write_text(existing_env)
        (install / "config").mkdir()
        (install / "config/config.json").write_text('{"workspace":{"name":"Kept"}}\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  info) exit 0 ;;
  'compose version') echo 'Docker Compose version v2.24.1' ;;
  'compose version --short') echo '2.24.1' ;;
  *'logs migrate'*) echo 'migrate | applied: admin sign-in link for admin@example.com, valid 7 days: http://localhost:8766/join/redacted' ;;
esac
exit 0
""")
    docker.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CSA_INSTALL_DIR": str(install),
        "CSA_INSTALL_SOURCE_DIR": str(SERVER),
        "CSA_INSTALL_INPUT": "/dev/stdin",
        "DOCKER_LOG": str(docker_log),
    }
    result = subprocess.run(["sh", str(INSTALLER)], input=answers, text=True, capture_output=True,
                            env=env, timeout=20)
    return result, install, docker_log


def test_clean_install_writes_private_bootstrap_and_starts(tmp_path):
    secret = "sk-ant-never-print-this"
    result, install, docker_log = _run(tmp_path, f"Engineering\nadmin@example.com\n\n1\n{secret}\n")
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert (install / ".env").read_text() == (
        "CSA_PUBLIC_URL=http://localhost:8766\n"
        "CSA_MCP_URL=http://localhost:58768/mcp\n"
        f"ANTHROPIC_API_KEY={secret}\n"
    )
    assert stat.S_IMODE((install / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((install / "config/config.json").stat().st_mode) == 0o600
    assert (install / "compose.yaml").read_text() == (SERVER / "docker-compose.trial.yml").read_text()
    assert '"name":"Engineering"' in (install / "config/config.json").read_text()
    assert "Anthropic model test passed" in result.stdout
    assert "claude plugin marketplace add nabooai/coding-session-analyzer#plugin-v0.2.0" in result.stdout
    assert "/csa:connect http://localhost:8766" in result.stdout
    assert "compose up -d" in docker_log.read_text()


def test_rerun_preserves_configuration_and_does_not_prompt(tmp_path):
    env = "CSA_PUBLIC_URL=http://localhost:8766\nANTHROPIC_API_KEY=keep-me\n"
    result, install, _ = _run(tmp_path, "", existing_env=env)
    assert result.returncode == 0, result.stderr
    assert (install / ".env").read_text() == env
    assert (install / "config/config.json").read_text() == '{"workspace":{"name":"Kept"}}\n'
    assert "Preserving its configuration and credentials" in result.stdout
    assert "keep-me" not in result.stdout + result.stderr


def test_provider_matrix_is_truthful_and_tests_openai_without_printing_the_key(tmp_path):
    secret = "sk-openai-never-print-this"
    result, install, _ = _run(tmp_path, f"Team\nadmin@example.com\n\n2\n{secret}\n\n\n")
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "OpenAI subscription login is not available" in output
    assert "privately built image" in output
    assert "OpenAI model test passed" in output
    assert secret not in output
    assert (install / ".env").read_text().endswith(
        f"OPENAI_API_KEY={secret}\nOPENAI_MAIN_MODEL=gpt-5\nOPENAI_SMALL_MODEL=gpt-5-mini\n"
    )
    assert "ANTHROPIC_API_KEY" not in (install / ".env").read_text()


def test_network_http_url_is_rejected_before_start(tmp_path):
    result, install, docker_log = _run(tmp_path, "Team\nadmin@example.com\nhttp://csa.example.com\n")
    assert result.returncode != 0
    assert "use https://" in result.stderr
    assert not (install / ".env").exists()
    assert "compose up" not in docker_log.read_text()

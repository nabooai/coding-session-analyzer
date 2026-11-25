"""Provider seam for the pipeline's model calls. Every 'expensive text' step (extraction, fact
judging, scope routing, recall synthesis) goes through complete(), so the provider is chosen in ONE
place.

A caller names a role, `main` or `small`; analyzer.providers resolves it to a provider the admin
registered on AI Models and a model it serves, per call. Anthropic, OpenAI and Azure OpenAI are one
HTTPS POST. Bedrock and Vertex run through the Claude Code CLI (`claude -p`) with that provider's
credential in the subprocess environment. With nothing registered, a role runs on the Claude credential
the environment carries (the Helm secret): through the CLI when the image has it, else an API key goes
over HTTPS and anything else fails with needs_cli()'s sentence. A subscription token only ever reaches
the CLI. Stdlib only (urllib). Logs to stderr only (no print on any import path).
"""
import json, os, shutil, subprocess, tempfile, urllib.error, urllib.parse, urllib.request
from analyzer import improve, netguard, providers, tracer

# What the pinned CLI sends for each alias (measured by server/e2e/onprem-egress.sh), so a call means the same
# model with or without it. The Messages API takes no alias.
_API_MODELS = {"sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001", "opus": "claude-opus-5"}


def provider_summary():
    """One line on what the `main` role runs on, logged at boot so a missing credential is obvious."""
    try:
        t = providers.target("main")
    except Exception as e:
        return f"main model unavailable: {e}"
    missing = needs_cli(t)
    if missing:
        return f"NONE: {missing}"
    if t.provider:
        return f"{providers.KINDS[t.provider.kind]} '{t.provider.name}' ({t.model}), set on AI Models"
    if install_credential():
        if _env_credential()[0] == "OPENAI_API_KEY":
            model = (os.environ.get("OPENAI_MAIN_MODEL") or "gpt-5").strip()
            return f"OpenAI via an API key from the environment ({model}); nothing registered on AI Models"
        return f"Claude via {install_credential()} from the environment ({t.model}); nothing registered on AI Models"
    return "NONE: register a provider on AI Models (or set ANTHROPIC_API_KEY/OPENAI_API_KEY); extraction and recall fail until then"


def _env_credential():
    """The variable and, in words, the Claude credential the environment carries, or ('', '')."""
    for var, what in (("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"), ("CLAUDE_CODE_USE_VERTEX", "Google Vertex"),
                      ("ANTHROPIC_API_KEY", "an Anthropic API key"), ("OPENAI_API_KEY", "an OpenAI API key"),
                      ("CLAUDE_CODE_OAUTH_TOKEN", "a login token")):
        if (os.environ.get(var, "") or "").strip() not in ("", "0", "false"):
            return var, what
    return "", ""


def install_credential():
    """What Claude credential the environment carries that can run on this image, in words, or '' for none."""
    return "" if needs_cli(providers.Target(None, "")) else _env_credential()[1]


def unavailable_kinds():
    """The provider kinds this image cannot run."""
    return [] if shutil.which("claude") else sorted(providers.CLI_KINDS)


def needs_cli(t):
    """The sentence saying `t` needs the Claude Code CLI and this image has none, or ''."""
    if shutil.which("claude"):
        return ""
    if t.provider:
        what = providers.KINDS[t.provider.kind] if t.provider.kind in providers.CLI_KINDS else ""
    else:
        var, what = _env_credential()
        what = "" if var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") else what
    return what and (f"{what} needs Claude Code, which this image does not include: set ANTHROPIC_API_KEY or add an "
                     f"Anthropic, OpenAI or Azure provider on AI Models, or build with --target with-claude-code")


def blocked():
    """needs_cli() for the first role that cannot run on this image, or ''."""
    if shutil.which("claude"):
        return ""
    try:
        return next(filter(None, (needs_cli(providers.target(r)) for r in providers.ENV_DEFAULT)), "")
    except Exception:
        return ""


def model_ready():
    """Whether the `main` role has something it can call: a registered provider or the install credential."""
    try:
        t = providers.target("main")
        return not needs_cli(t) and (t.provider is not None or bool(install_credential()))
    except Exception:
        return False


def complete(prompt, model="main", timeout=300):
    """Run `prompt`, return (text, cost_usd, usage). `model` is a role (`main` / `small`), a
    "<provider>/<model>" ref, or a bare Claude model name (the CLI's --model override). `usage` uses the
    cost ledger's Anthropic key shape; the OpenAI paths normalize into it. Raises on failure."""
    t = providers.target(model)
    kind = t.provider.kind if t.provider else ("openai" if _env_credential()[0] == "OPENAI_API_KEY" else "anthropic")
    with tracer.span("llm", "LLM", **{"llm.provider": kind, "llm.model_name": t.model,
                                      "csa.prompt_chars": len(prompt)}) as sp:
        text, cost_usd, usage = _run(t, prompt, timeout)
        usage = dict(usage or {})
        usage["model"] = usage.get("model") or t.model      # the real id when the provider reports it
        sp.set(**{"output.value": (text or "")[:1000], "csa.cost_usd": cost_usd,
                  "csa.num_turns": usage.get("num_turns"),
                  "llm.model_name": usage["model"],
                  "llm.token_count.prompt": usage.get("input_tokens"),
                  "llm.token_count.completion": usage.get("output_tokens"),
                  "llm.token_count.prompt_details.cache_read": usage.get("cache_read_input_tokens"),
                  "llm.token_count.prompt_details.cache_write": usage.get("cache_creation_input_tokens")})
        return text, cost_usd, usage


def _run(t, prompt, timeout):
    p = t.provider
    if p and p.kind == "openai":
        return _endpoint(_openai, prompt, t.model, timeout, p.credential, p.endpoint or "https://api.openai.com/v1")
    if p and p.kind == "azure_openai":
        return _endpoint(_azure, prompt, t.model, timeout, p)
    if p and p.kind == "anthropic":
        return _endpoint(_anthropic, prompt, t.model, timeout, p.credential, p.endpoint or "https://api.anthropic.com")
    if shutil.which("claude"):
        return _claude(prompt, t.model, timeout, p)
    missing = needs_cli(t)
    if missing:
        raise RuntimeError(missing)
    env_kind = _env_credential()[0]
    if env_kind == "ANTHROPIC_API_KEY":
        base = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip() or "https://api.anthropic.com"
        return _endpoint(_anthropic, prompt, _API_MODELS.get(t.model, t.model), timeout,
                         os.environ["ANTHROPIC_API_KEY"].strip(), base, _open_operator)
    if env_kind == "OPENAI_API_KEY":
        defaults = {
            "sonnet": (os.environ.get("OPENAI_MAIN_MODEL") or "gpt-5").strip(),
            "haiku": (os.environ.get("OPENAI_SMALL_MODEL") or "gpt-5-mini").strip(),
        }
        base = (os.environ.get("OPENAI_BASE_URL") or "").strip() or "https://api.openai.com/v1"
        return _endpoint(_openai, prompt, defaults.get(t.model, t.model), timeout,
                         os.environ["OPENAI_API_KEY"].strip(), base)
    raise RuntimeError("no model is set up: set ANTHROPIC_API_KEY/OPENAI_API_KEY or add a provider on AI Models")


def _endpoint(call, *args):
    """One HTTPS model call. A ValueError quarantines the session, and whatever this raises (a bad address, a
    body that is not JSON) is the endpoint's fault, not the session's, so it leaves as a systemic failure."""
    try:
        return call(*args)
    except ValueError as e:
        raise RuntimeError(f"the model endpoint failed: {e}") from None


# Every CSA model call is a pure text -> text transform: the subprocess inherits nothing from the
# machine it runs on (tests/test_llm_argv.py). Each flag closes a hole that leaked in production:
#   --safe-mode              the only flag that stops CLAUDE.md and auto-memory being injected
#                            (`--setting-sources ""` does not; `--bare` breaks OAuth-token auth)
#   --strict-mcp-config      no MCP servers, so extraction cannot answer from the corpus it populates
#   --tools ""               no built-in tools: every caller sends a prompt and parses text back
#   --no-session-persistence no transcript in ~/.claude/projects, which ingest would read back
# No --permission-mode: with zero tools there is nothing to permit, and `plan` changes the prompt.
_ISOLATION = ["--safe-mode", "--strict-mcp-config", "--tools", "", "--no-session-persistence"]


def _claude_argv(model):
    return ["claude", "-p", "--model", model, "--output-format", "json"] + _ISOLATION


def _parse_claude(returncode, stdout, stderr):
    """Classify a `claude -p --output-format json` result -> (text, cost, usage), or raise.

    RuntimeError is systemic (auth, rate, transport): a non-zero exit, or exit 0 with "is_error": true,
    which is how the CLI reports an expired token. The drain keeps the session queued. ValueError is
    content poison (exit 0 but no JSON): the drain quarantines the session. The reason is read from
    stdout's `result` and from stderr, never left empty."""
    parsed, out = False, {}
    if stdout.strip():
        try:
            out, parsed = json.loads(stdout), True
        except ValueError:
            pass                                         # non-JSON body: handled as content poison below
    if returncode != 0 or (parsed and out.get("is_error")):
        # never fall back to an un-isolated call
        if "unknown option" in stderr or "is invalid" in stderr:
            raise RuntimeError(f"claude CLI rejected an isolation flag, refusing to run un-isolated "
                               f"(upgrade the claude CLI): {stderr.strip()[:200]}")
        reason = str((parsed and out.get("result")) or stderr or stdout or "no output").strip()[:300]
        raise RuntimeError(f"claude failed: {reason}")
    if not parsed:
        raise ValueError("no JSON object in claude output")
    usage = dict(out.get("usage") or {})     # extra keys are inert to record_cost; the LLM span reads them
    if out.get("num_turns") is not None:
        usage["num_turns"] = out["num_turns"]
    if out.get("modelUsage"):                # the real model id, not the alias
        usage["model"] = next(iter(out["modelUsage"]))
    return out.get("result", ""), out.get("total_cost_usd"), usage


def _claude(prompt, model, timeout, provider=None):
    # a neutral cwd: the CLI injects the cwd's git status into the prompt, and no flag strips it
    env, keyfile = providers.claude_env(provider)
    try:
        p = subprocess.run(_claude_argv(model), input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=tempfile.gettempdir(), env=env)
    finally:
        if keyfile:
            os.unlink(keyfile)
    return _parse_claude(p.returncode, p.stdout, p.stderr)


def _open(req, timeout):
    """An admin-typed endpoint is called through netguard: public https addresses only, checked at connect."""
    return netguard.opener().open(req, timeout=timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _open_operator(req, timeout):
    """ANTHROPIC_BASE_URL is the operator's own: a private address, plain http and a proxy are fine. A redirect
    is not, it would carry the key to wherever it points."""
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _chat(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={**headers, "Content-Type": "application/json"})
    with _open(req, timeout) as r:
        out = json.loads(r.read())
    choice = (out.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    if not text and choice.get("finish_reason") not in ("content_filter", "length"):
        raise RuntimeError("the model endpoint answered with no text")
    u = out.get("usage") or {}
    usage = {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens"),
             "cache_read_input_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
             "model": out.get("model") or body.get("model")}
    cached = usage["cache_read_input_tokens"] or 0
    return text, improve.list_cost(usage["model"], max(0, (usage["input_tokens"] or 0) - cached), cached, 0,
                                   usage["output_tokens"]), usage


def _openai(prompt, model, timeout, key, base):
    return _chat(f"{base.rstrip('/')}/chat/completions", {"Authorization": f"Bearer {key}"},
                 {"model": model, "messages": [{"role": "user", "content": prompt}]}, timeout)


def _anthropic(prompt, model, timeout, key, base, opener=None):
    """The Messages API directly, not the CLI, which prefers a login stored on the machine over a key it
    is handed."""
    if key.startswith("sk-ant-oat"):
        raise RuntimeError("that key is a subscription token, which only Claude Code may use: use an API key")
    req = urllib.request.Request(f"{base.rstrip('/')}/v1/messages", method="POST", data=json.dumps(
        {"model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with (opener or _open)(req, timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:                              # systemic, never content poison
        # only a parsed API error message: the endpoint may be any internal host
        try:
            why = json.loads(e.read()[:2000])["error"]["message"][:200]
        except (ValueError, KeyError, TypeError):
            why = "the endpoint did not return a recognizable API error"
        raise RuntimeError(f"Anthropic said {e.code}, {why}") from None
    text = "".join(b.get("text", "") for b in out.get("content") or [] if b.get("type") == "text")
    if not text and out.get("stop_reason") not in ("refusal", "max_tokens"):
        raise RuntimeError("the model endpoint answered with no text")
    u = out.get("usage") or {}
    served = out.get("model") or model
    cost = improve.list_cost(served, u.get("input_tokens") or 0, u.get("cache_read_input_tokens"),
                             u.get("cache_creation_input_tokens"), u.get("output_tokens"))
    return text, cost, {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                              "cache_creation_input_tokens")} | {"model": served}


def _azure(prompt, deployment, timeout, p):
    """Azure OpenAI addresses a model by its deployment name, in the path."""
    q = urllib.parse.urlencode({"api-version": p.api_version or providers.AZURE_API_VERSION})
    url = f"{p.endpoint}/openai/deployments/{urllib.parse.quote(deployment, safe='')}/chat/completions?{q}"
    return _chat(url, {"api-key": p.credential}, {"messages": [{"role": "user", "content": prompt}]}, timeout)


def probe(ref, timeout=60, provider=None):
    """None when `ref` answers a one-line prompt, else a sentence saying why not. Run before a model
    choice or a provider is stored, so a name the endpoint does not know is refused up front. A
    `provider` not yet saved is probed directly."""
    try:
        t = providers.Target(provider, ref) if provider else providers.target(ref)
        _run(t, "Reply with the word OK.", timeout)
    except Exception as e:
        reason = str(e).strip()[:200].rstrip(".") or type(e).__name__
        return f"{ref} did not answer: {reason}. Nothing was saved."
    return None


if __name__ == "__main__":
    argv = _claude_argv("sonnet")
    for flag in ("--safe-mode", "--strict-mcp-config", "--tools", "--no-session-persistence"):
        assert flag in argv, f"isolation flag {flag} missing from argv: {argv}"
    assert argv[argv.index("--tools") + 1] == "", "--tools must be followed by '' (disable all tools)"
    assert "--mcp-config" not in argv, "--strict-mcp-config only isolates while no --mcp-config is passed"
    assert "--bare" not in argv, "--bare forces API-key-only auth; prod uses CLAUDE_CODE_OAUTH_TOKEN"
    assert "--dangerously-skip-permissions" not in argv and "bypassPermissions" not in argv

    # _parse_claude: the 2026-07-25 outage classes. An auth/rate failure must raise a NON-ValueError
    # (systemic => the drain leaves the backlog queued, never quarantines a good session) AND surface the
    # reason — never a bare "claude failed:".
    _auth = json.dumps({"type": "result", "is_error": True,
                        "result": "Failed to authenticate: OAuth session expired", "total_cost_usd": 0})
    try:                                                 # exit 0 + is_error: the shape that USED to poison
        _parse_claude(0, _auth, "")
        assert False, "is_error:true must raise"
    except ValueError:
        assert False, "auth outage must NOT be a ValueError — that would QUARANTINE a good session"
    except RuntimeError as e:
        assert "OAuth session expired" in str(e), f"reason must be surfaced, got: {e}"
    try:                                                 # what actually happened: non-zero + empty stderr
        _parse_claude(1, "", "")
        assert False
    except RuntimeError as e:
        assert "no output" in str(e), f"empty stderr must not yield an empty reason, got: {e}"
    try:                                                 # non-zero but the reason is in stdout, not stderr
        _parse_claude(1, _auth, "")
        assert False
    except RuntimeError as e:
        assert "OAuth" in str(e)
    try:                                                 # exit 0, unparseable body => CONTENT poison (ValueError)
        _parse_claude(0, "not json at all", "")
        assert False
    except ValueError:
        pass
    try:                                                 # a rejected isolation flag still wins, with its reason
        _parse_claude(1, "", "error: unknown option '--safe-mode'")
        assert False
    except RuntimeError as e:
        assert "isolation flag" in str(e) and "safe-mode" in str(e)
    try:                                                 # is_error with a non-string result must not crash on .strip()
        _parse_claude(0, json.dumps({"is_error": True, "result": {"code": "rate_limited"}}), "")
        assert False
    except RuntimeError as e:
        assert "rate_limited" in str(e), f"non-string reason must survive, got: {e}"
    _ok = json.dumps({"result": "hi", "total_cost_usd": 0.01, "usage": {"input_tokens": 3}})
    txt, cost, usage = _parse_claude(0, _ok, "")         # the happy path is unchanged
    assert txt == "hi" and cost == 0.01 and usage["input_tokens"] == 3, (txt, cost, usage)
    assert "num_turns" not in usage, "absent num_turns must not invent a key"
    _ok_t = json.dumps({"result": "hi", "total_cost_usd": 0.01, "usage": {"input_tokens": 3}, "num_turns": 2})
    assert _parse_claude(0, _ok_t, "")[2]["num_turns"] == 2, "num_turns must ride the usage dict"

    print("OK llm: isolation argv + _parse_claude(outage=>systemic w/ reason, malformed=>poison)")

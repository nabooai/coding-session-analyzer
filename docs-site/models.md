# Model setup

CSA uses a **main model** for richer session understanding and answers, and a **small model** for extraction,
judging, and routing. The installer tests credentials before declaring setup complete.

| Provider | API key | Login/subscription |
|---|---|---|
| Anthropic / Claude | Guided and tested | Private local servers only; requires a separately built image |
| OpenAI | Guided and tested; defaults to `gpt-5` / `gpt-5-mini` | Not supported |
| Azure OpenAI | Settings → AI Models | Not applicable |
| OpenAI-compatible HTTPS | Settings → AI Models | Not applicable |
| Bedrock / Vertex | Requires the `with-claude-code` image | Provider credentials only |

A team server should use organization-controlled API/provider credentials, not an employee's subscription.
The public image does not contain proprietary subscription credentials or Claude Code.

## Change or add a provider

Open **Settings → AI Models**. Enter the endpoint, credential, and model IDs, then run the test action. CSA
never returns a stored credential to the browser.

A failed test identifies the likely class: invalid credential, unsupported model ID, endpoint/network issue,
missing CLI, or provider usage mode. If no model is ready, accepted sessions remain queued and search remains
available for facts already extracted.

## Environment bootstrap

The guided installer writes one of these to a mode-0600 `.env` file:

```text
ANTHROPIC_API_KEY=...
```

or:

```text
OPENAI_API_KEY=...
OPENAI_MAIN_MODEL=gpt-5
OPENAI_SMALL_MODEL=gpt-5-mini
```

Do not put credentials in URLs, shell history, Git, screenshots, or support logs.

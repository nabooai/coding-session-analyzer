# Operations

The guided installer creates `~/csa-server` and uses Docker Compose. Routine checks do not require knowledge of
CSA internals:

```sh
cd ~/csa-server
docker compose ps -a
docker compose logs migrate api etl
curl -fsS http://localhost:8766/api/health
```

Stop without deleting data:

```sh
docker compose down
```

Never add `--volumes` during routine recovery. Back up the mounted `data/` directory using the documented
snapshot process before upgrades. Keep `.env`, model credentials, TLS private keys, logs, corpora, and backups
out of Git and container images.

## Network deployment

- Use a stable HTTPS origin with a trusted certificate.
- Keep API and MCP origin ports private behind the proxy/tunnel.
- Verify data routes return `401` without a token after every deployment.
- Keep public onboarding and health routes intentionally narrow.
- Use organization-controlled provider credentials and identity settings.

For SSO, backup, restore, upgrades, corporate CAs, and private egress, follow the repository's
[on-prem runbook](https://github.com/nabooai/coding-session-analyzer/blob/master/docs/public-release/install-server.md).

## Upgrade discipline

Build and release server images and plugin tags from the same reviewed `master` commit. Record image digests,
SBOMs, signatures, schema health, anonymous installation results, and rollback image tags. Test migrations against
a copied corpus—not the only production copy.

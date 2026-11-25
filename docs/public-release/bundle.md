# The offline bundle

`csa-onprem-<version>.tar.gz` is attached to every GitHub release tagged `server-v<version>`.
It holds everything a box with no registry access needs, and the signatures that prove where it
came from. The `bundle` job in `.github/workflows/server-release.yml` builds it.

## Contents

| Path | What | Covered by |
|---|---|---|
| `csa-server-slim-<version>-amd64.tar`, `csa-server-slim-<version>-arm64.tar` | the server image without Claude Code, the same one the release pushed to `ghcr.io/nabooai/csa-server-slim:<version>`, pulled back one platform at a time (`docker save` exports one platform). Loads as `csa-server-slim:<version>` | its own `.sigstore.json`; `SHA256SUMS` |
| `csa-server-slim-<version>-<arch>.tar.sigstore.json` | the Sigstore bundle for each image tarball: signature, signing certificate, transparency-log proof | |
| `caddy-<caddy version>-amd64.tar`, `caddy-<caddy version>-arm64.tar` | the proxy image, saved from the digest `docker-compose.trial.yml` pins. Loads as `caddy:<caddy version>` | `SHA256SUMS` |
| `docker-compose.bundle.yml` | a one-service override that points `proxy` at the loaded Caddy tag (why: under Load) | `SHA256SUMS` |
| `csa-<version>.tgz` | the Helm chart, packaged with chart version and appVersion set to the release; `image.tag` defaults to that appVersion, so `helm install csa csa-<version>.tgz` pulls `ghcr.io/nabooai/csa-server-slim:<version>` (or the loaded `csa-server-slim:<version>` tarball with `image.repository=csa-server-slim`) | `SHA256SUMS` |
| `docker-compose.trial.yml`, `Caddyfile`, `.env.example` | the compose kit | `SHA256SUMS` |
| `install-server.md`, `install-client.md`, `egress.md`, `data-at-rest.md`, `bundle.md` | the two runbooks and the two security pages | `SHA256SUMS` |
| `csa-server-slim-<version>-amd64.spdx.json` | the SBOM of the amd64 image: SPDX 2.3 JSON from syft | `SHA256SUMS` |
| `MANIFEST.txt` | server version, source commit, client version (the server's), Caddy version and digest, the Claude Code version the `with-claude-code` target would add (none is in the bundle), build time and workflow run, the signing identity | `SHA256SUMS` |
| `sigstore-trusted-root.json` | the Sigstore public-good trusted root (the Fulcio and Rekor keys) copied at build time, for verification on a box that cannot reach sigstore.dev | `SHA256SUMS`; the tarball signature (step 1 under Verify) |
| `SHA256SUMS`, `SHA256SUMS.sigstore.json` | a sha256 for every other file, and the signature over that list | |

Beside the tarball the release also carries `csa-onprem-<version>.tar.gz.sigstore.json` (the
signature over the whole tarball), the SBOM, the Helm chart, the three `.sigstore.json` files, `SHA256SUMS` and
`MANIFEST.txt`, so each can be fetched on its own, and `csa-server-<version>-amd64.spdx.json`, the SBOM of the
image built with Claude Code: the same packages plus one entry for the binary
(`@anthropic-ai/claude-code-linux-x64`, version and sha256 taken from the Dockerfile), which syft sees only as a file.

The bundle has no Claude Code in it. AWS Bedrock, Google Vertex and subscription tokens need an image you build
with `--target with-claude-code` and load on the box yourself (`install-server.md` §7).

## Versions inside one bundle

The server image is built from the tagged commit, and so are the client binaries inside it: the
image serves them at `/install` and `/latest/`, stamped with the server's version, and
`csa-client update` fetches from the server's `/latest/`, never from GitHub. So:

- `csa-client version` on a laptop installed from this server prints the server's version.
- Upgrading the server image is what ships a newer client; people then run `csa-client update`
  (their next interactive `csa-client version` shows the notice).
- The Caddy image is whatever digest `docker-compose.trial.yml` pinned at the tagged commit.

## Verify

The image tarballs and `SHA256SUMS` are signed keyless with Sigstore: the signing identity is the
GitHub Actions workflow run itself, and the certificate names it. Verification needs cosign 3
(`cosign version`); the `.sigstore.json` files are Sigstore's bundle format.

```bash
IDENTITY='^https://github\.com/nabooai/coding-session-analyzer/\.github/workflows/server-release\.yml@refs/tags/server-v'
ISSUER=https://token.actions.githubusercontent.com
```

1. On a machine that can reach the internet, verify the tarball and fetch the trusted root:

   ```bash
   cosign verify-blob --bundle csa-onprem-<version>.tar.gz.sigstore.json \
     --certificate-identity-regexp "$IDENTITY" --certificate-oidc-issuer "$ISSUER" csa-onprem-<version>.tar.gz
   cosign initialize     # writes ~/.sigstore/root/tuf-repo-cdn.sigstore.dev/targets/trusted_root.json
   ```

   Expected: `Verified OK`. That signature covers everything in the tarball, the bundled
   `sigstore-trusted-root.json` included. Sigstore updates its public root now and then, so the
   `trusted_root.json` written here may be newer than the bundled copy; either one verifies the
   bundle's signatures, since both carry the keys that were current at signing time.

2. On the box, extract and check the sums:

   ```bash
   tar -xzf csa-onprem-<version>.tar.gz && cd csa-onprem-<version>
   sha256sum -c SHA256SUMS                  # every line: OK
   ```

3. Verify the signed files offline, against that root:

   ```bash
   for f in SHA256SUMS csa-server-slim-<version>-amd64.tar csa-server-slim-<version>-arm64.tar; do
     cosign verify-blob --bundle "$f.sigstore.json" --trusted-root sigstore-trusted-root.json \
       --certificate-identity-regexp "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$f"
   done
   ```

   Expected: `Verified OK` three times. Each bundle carries its transparency-log inclusion
   proof, so no connection to Rekor is made; the trusted root supplies the keys that proof and
   the certificate chain to. These are cosign 3's flags (3.1.3, checked 2026-09-15), and the
   build runs this same command on its own output before uploading. cosign 2.4 and later read
   this bundle format with `--new-bundle-format`; that was not tested.

The identity is what Fulcio writes into the certificate for a GitHub Actions run:
`https://github.com/<owner>/<repo>/<workflow path>@<ref>`, here
`https://github.com/nabooai/coding-session-analyzer/.github/workflows/server-release.yml@refs/tags/server-v<version>`.
The build verifies its own signatures with the same regexp before uploading anything. Keyless
signing publishes that certificate to the public Rekor log, so the repository name, the workflow
path and the tag are public record.

## Load

Pick the box's architecture (`uname -m`: `x86_64` is `amd64`, `aarch64` is `arm64`):

```bash
docker load -i csa-server-slim-<version>-<arch>.tar     # Loaded image: csa-server-slim:<version>
docker load -i caddy-<caddy version>-<arch>.tar    # Loaded image: caddy:<caddy version>
```

Then in `.env` set `CSA_SERVER_IMAGE=csa-server-slim:<version>`, and add the override for the proxy:

```bash
export COMPOSE_FILE=docker-compose.trial.yml:docker-compose.bundle.yml COMPOSE_PROFILES=proxy
docker compose config --services       # migrate, api, etl, mcp, proxy; nothing pulled
```

Why the override: `docker-compose.trial.yml` pins the proxy as `caddy@sha256:<digest>`. A digest
names a registry manifest, and `docker load` restores an image's layers and tag but no manifest
digest, so the loaded image never matches the pin and `docker compose up` would try Docker Hub.
The override names the tag the tarball carries; `MANIFEST.txt` and the override's own comment
record the digest it was saved from. Measured on Docker 27.4.0 with the overlay2 image store on
2026-09-15.

From here the runbook is `install-server.md`, §1 onwards. Upgrades and rollback:
`install-server.md` §8.

## Registry alternative

The bundle's image tarballs are pulled from `ghcr.io/nabooai/csa-server-slim:<version>` (multi-arch),
so a box that may reach GHCR needs no bundle; `.env` then names that image and the compose pulls Caddy
from Docker Hub by digest.

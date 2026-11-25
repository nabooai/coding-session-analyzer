# Built from the repository root. Two outcomes from one set of layers:
#   docker build -f server/Dockerfile -t csa-server:local .                                  no Claude Code (the default)
#   docker build -f server/Dockerfile --target with-claude-code -t csa-server:local .        adds Claude Code
# ui/dist is gitignored and no workflow builds it, so the image builds its own (prod's bind mount still overrides it).
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS ui
WORKDIR /ui
COPY server/ui/package.json server/ui/package-lock.json ./
RUN npm ci
COPY server/ui/ ./
RUN npm run build

# The laptop client served at /install and /latest/, from this same commit and stamped with this image's version.
# On the build platform, so every image architecture carries byte-identical binaries and one checksums.txt.
FROM --platform=$BUILDPLATFORM golang:1.26.7-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS client
WORKDIR /src
COPY client/ ./
ARG CSA_VERSION=dev
RUN set -eu; mkdir /dist; \
    for t in darwin/arm64 darwin/amd64 linux/amd64 linux/arm64 windows/amd64; do \
      os=${t%/*}; arch=${t#*/}; ext=; [ "$os" = windows ] && ext=.exe; \
      CGO_ENABLED=0 GOOS=$os GOARCH=$arch \
        go build -trimpath -ldflags="-s -w -X main.version=$CSA_VERSION" -o "/dist/csa-client-$os-$arch$ext" .; \
    done; \
    cd /dist && sha256sum csa-client-* > checksums.txt

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS base
COPY --from=ghcr.io/astral-sh/uv:0.7.17@sha256:68a26194ea8da0dbb014e8ae1d8ab08a469ee3ba0f4e2ac07b8bb66c0f8185c1 /uv /usr/local/bin/uv
# Fixed uid/gid so bind mounts are chowned once (chown -R 10001:10001 data); a real HOME because the
# CLI writes ~/.claude.json and ~/.claude/.
RUN groupadd -g 10001 csa && useradd -m -u 10001 -g 10001 -s /usr/sbin/nologin csa \
    && install -d -o 10001 -g 10001 /app
WORKDIR /app
USER 10001
ENV HOME=/home/csa
COPY --chown=10001:10001 server/pyproject.toml server/uv.lock ./
# As the runtime user: a root-built .venv fails or rebuilds under uid 10001. Every group is installed so that the
# composes' `uv run` finds the venv in sync and never resolves at container start (offline boxes would hang).
RUN uv sync --frozen --no-cache
COPY --chown=10001:10001 server/ .
COPY --from=ui --chown=10001:10001 /ui/dist ./ui/dist
COPY --from=client /dist ./client-dist
ARG CSA_VERSION=dev
LABEL org.opencontainers.image.version=$CSA_VERSION
RUN echo "$CSA_VERSION" > /app/VERSION
# Auth: ANTHROPIC_API_KEY (+ ANTHROPIC_BASE_URL for a gateway), or a provider on AI Models. with-claude-code adds
# Bedrock, Vertex and CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` on the host.
CMD ["/app/.venv/bin/python", "apiserver.py"]

FROM base AS with-claude-code
ARG TARGETARCH
USER 0
# Claude Code 2.1.270 as the native binary from its npm platform package: the npm wrapper needs node>=22 and
# bookworm ships 18. sha256 per tarball computed 2026-09-14, cross-checked against npm's dist.integrity (sha512).
RUN set -eu; case "$TARGETARCH" in \
      amd64) arch=x64;   sum=a2c0b69773f9da730b0616c19771094de51ffd23f0dfaa7c34ccead34eb76dd0 ;; \
      arm64) arch=arm64; sum=9c2c52fc53e97cc8e5e20848865c53425193251e6764c2abe975c6642587e9f3 ;; \
      *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;; \
    esac; \
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-$arch/-/claude-code-linux-$arch-2.1.270.tgz" /tmp/claude.tgz; \
    echo "$sum  /tmp/claude.tgz" | sha256sum -c -; \
    tar -xzf /tmp/claude.tgz -C /tmp package/claude; \
    install -m 0755 /tmp/package/claude /usr/local/bin/claude; \
    rm -rf /tmp/claude.tgz /tmp/package
USER 10001
# Opt-outs the 2.1.270 binary reads (`strings` over it, 2026-09-14; each name occurs 3 to 18 times). Third-party
# variables, not CSA knobs. What they do not stop is measured by server/e2e/onprem-egress.sh.
ENV DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_AUTOUPDATER=1 DISABLE_BUG_COMMAND=1 \
    DISABLE_FEEDBACK_SURVEY=1 DISABLE_INSTALLATION_CHECKS=1 DISABLE_COST_WARNINGS=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
    CLAUDE_CODE_DISABLE_ORG_MEMORY=1 CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1
LABEL com.csa.claude_code=2.1.270

# The last stage is what a plain `docker build` makes.
FROM base AS slim

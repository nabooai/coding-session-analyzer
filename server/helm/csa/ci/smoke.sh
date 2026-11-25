#!/usr/bin/env bash
# helm lint + template over every ci/*-values.yaml, plus the refusals an operator must see.
# Needs only helm. Run from anywhere: bash server/helm/csa/ci/smoke.sh
set -euo pipefail
chart="$(cd "$(dirname "$0")/.." && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
has() { grep -qF -- "$2" "$1" || fail "$(basename "$1") lacks: $2"; }
lacks() { ! grep -qF -- "$2" "$1" || fail "$(basename "$1") has: $2"; }
refused() {   # refused <expected message> <helm template args...>
  local want="$1"; shift
  if helm template csa "$@" >"$out/refused" 2>&1; then fail "rendered, expected: $want"; fi
  grep -qF -- "$want" "$out/refused" || fail "expected '$want', got: $(cat "$out/refused")"
}

helm lint --strict "$chart" >/dev/null 2>&1 || helm lint --strict "$chart"
for v in "$chart"/ci/*-values.yaml; do
  name="$(basename "$v" -values.yaml)"
  helm lint --strict "$chart" -f "$v" >/dev/null || helm lint --strict "$chart" -f "$v"
  helm template csa "$chart" -n csa -f "$v" >"$out/$name.yaml"
  f="$out/$name.yaml"
  [ "$(grep -c 'readOnlyRootFilesystem: true' "$f")" = 4 ] || fail "$name: not every container is read-only"
  [ "$(grep -c 'secretRef: {name: csa-llm, optional: true}' "$f")" = 4 ] || fail "$name: a keyless install would not start"
  [ "$(grep -c 'livenessProbe:' "$f")" = 3 ] && [ "$(grep -c 'startupProbe:' "$f")" = 3 ] || fail "$name: api, mcp and etl need a startup and a liveness probe"
  has "$f" "enableServiceLinks: false"
  has "$f" "automountServiceAccountToken: false"
  has "$f" "seccompProfile: {type: RuntimeDefault}"
  has "$f" "app.kubernetes.io/instance: csa"
  has "$f" 'CSA_PUBLIC_URL: "https://csa.example.com"'
  echo "ok  $name"
done

has "$out/ambassador.yaml" "kind: Mapping"
lacks "$out/ambassador.yaml" "kind: Ingress"
lacks "$out/ambassador.yaml" "kind: NetworkPolicy"
has "$out/nginx.yaml" "ingressClassName: nginx"
has "$out/nginx.yaml" "cert-manager.io/cluster-issuer: letsencrypt"
has "$out/nginx.yaml" "kind: NetworkPolicy"
has "$out/nginx.yaml" "kubernetes.io/metadata.name: ingress-nginx"
lacks "$out/none.yaml" "kind: Ingress"
lacks "$out/none.yaml" "kind: Mapping"
lacks "$out/none.yaml" "kind: PersistentVolumeClaim"
lacks "$out/none.yaml" "imagePullSecrets"
has "$out/none.yaml" "claimName: csa-restored"

# The release path: a packaged chart pins its image through appVersion, no image.tag needed.
helm package "$chart" --version 0.0.0-ci --app-version 0.0.0-ci --destination "$out" >/dev/null
helm lint --strict "$out/csa-0.0.0-ci.tgz" --set host=csa.example.com >/dev/null
helm template csa "$out/csa-0.0.0-ci.tgz" --set host=csa.example.com >"$out/packaged.yaml"
has "$out/packaged.yaml" 'image: "ghcr.io/nabooai/csa-server-slim:0.0.0-ci"'
helm template csa "$out/csa-0.0.0-ci.tgz" --set host=csa.example.com,image.repository=registry.example.com/csa-server,image.tag=mine >"$out/explicit.yaml"
has "$out/explicit.yaml" 'image: "registry.example.com/csa-server:mine"'
has "$out/explicit.yaml" 'name: ghcr'
lacks "$out/explicit.yaml" "csa-server-slim"
tar -tzf "$out/csa-0.0.0-ci.tgz" >"$out/packaged.list"
lacks "$out/packaged.list" "csa/ci/"
echo "ok  packaged"

refused "host is required" "$chart" --set image.tag=ci
refused "image.tag is required" "$chart" --set host=csa.example.com
refused "networkPolicy.ingressFrom is required" "$chart" --set host=csa.example.com,image.tag=ci,networkPolicy.enabled=true
refused "host" "$chart" --set host=https://csa.example.com,image.tag=ci
refused "frontDoor" "$chart" --set host=csa.example.com,image.tag=ci,frontDoor=traefik
echo "ok  refusals"

#!/usr/bin/env bash
set -euo pipefail

manifest_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
namespace=fcapsule-atlas-test
image_ref=${COLLECTIVE_IMAGE_REF:-${ESTIMA_IMAGE_REF:-}}
source_ref=${FCAPSULE_SOURCE_REF:-${1:-}}

if [[ ! "$image_ref" =~ ^ghcr\.io/hi-io/(collective|estima):sha-[0-9a-f]{40}$ \
  && ! "$image_ref" =~ ^ghcr\.io/hi-io/(collective|estima)@sha256:[0-9a-f]{64}$ ]]; then
  printf '%s\n' "Set COLLECTIVE_IMAGE_REF (or ESTIMA_IMAGE_REF) to a Collective Git-SHA tag or sha256 digest." >&2
  exit 1
fi
if [[ ! "$source_ref" =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' "Usage: COLLECTIVE_IMAGE_REF=<image> FCAPSULE_SOURCE_REF=<40-character-commit-sha> $0" >&2
  exit 1
fi

kubectl apply -f "$manifest_dir/namespace.yaml"
kubectl -n "$namespace" get secret fcapsule-atlas-runtime >/dev/null
kubectl -n "$namespace" get secret fcapsule-atlas-postgres >/dev/null
kubectl -n "$namespace" get service atlas-postgres >/dev/null
kubectl -n "$namespace" get pvc atlas-postgres-data >/dev/null
kubectl -n "$namespace" create configmap fcapsule-atlas-test-source \
  --from-literal="FCAPSULE_SOURCE_REF=$source_ref" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$namespace" apply -f "$manifest_dir/config.yaml"

# Keep the existing test Atlas Service active until Collective passes readiness.
sed "s|ghcr.io/hi-io/estima:0.1.0|$image_ref|" "$manifest_dir/estima-deployment.yaml" \
  | kubectl -n "$namespace" apply -f -
kubectl -n "$namespace" rollout status deployment/estima --timeout=180s
kubectl -n "$namespace" apply -f "$manifest_dir/estima-services.yaml"
kubectl -n "$namespace" apply -f "$manifest_dir/fcapsule-dev.yaml"
kubectl -n "$namespace" rollout restart deployment/fcapsule-dev-a deployment/fcapsule-dev-b
kubectl -n "$namespace" rollout status deployment/fcapsule-dev-a --timeout=600s
kubectl -n "$namespace" rollout status deployment/fcapsule-dev-b --timeout=600s

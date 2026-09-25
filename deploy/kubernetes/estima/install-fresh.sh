#!/usr/bin/env bash
set -euo pipefail

manifest_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
namespace=fcapsule-atlas
image_ref=${ESTIMA_IMAGE_REF:-}

if [[ ! "$image_ref" =~ ^ghcr\.io/hi-io/estima:sha-[0-9a-f]{40}$ \
  && ! "$image_ref" =~ ^ghcr\.io/hi-io/estima@sha256:[0-9a-f]{64}$ ]]; then
  printf '%s\n' "Set ESTIMA_IMAGE_REF to an Estima Git-SHA tag or sha256 digest." >&2
  exit 1
fi
if grep -q 'storageClassName: replace-with-storage-class' "$manifest_dir/storage.yaml"; then
  printf '%s\n' "Set the approved durable StorageClass in storage.yaml before installation." >&2
  exit 1
fi

kubectl apply -f "$manifest_dir/namespace.yaml"
kubectl -n "$namespace" get secret atlas-runtime >/dev/null
kubectl -n "$namespace" get secret estima-postgres >/dev/null
if kubectl -n "$namespace" get service atlas-postgres >/dev/null 2>&1 \
  || kubectl -n "$namespace" get pvc atlas-postgres-data >/dev/null 2>&1; then
  printf '%s\n' "Atlas database resources exist; use the in-place migration path instead." >&2
  exit 1
fi
if kubectl -n "$namespace" get pvc estima-postgres-data >/dev/null 2>&1; then
  printf '%s\n' "PVC estima-postgres-data already exists; use the update path, not fresh install." >&2
  exit 1
fi

kubectl -n "$namespace" apply -f "$manifest_dir/storage.yaml"
kubectl -n "$namespace" apply -f "$manifest_dir/postgres.yaml"
kubectl -n "$namespace" rollout status deployment/estima-postgres --timeout=300s
sed "s|ghcr.io/hi-io/estima:0.1.0|$image_ref|" "$manifest_dir/api-deployment.yaml" \
  | kubectl -n "$namespace" apply -f -
kubectl -n "$namespace" rollout status deployment/estima --timeout=600s
kubectl -n "$namespace" apply -f "$manifest_dir/api-services.yaml"

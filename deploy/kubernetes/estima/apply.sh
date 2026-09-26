#!/usr/bin/env bash
set -euo pipefail

manifest_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
namespace=fcapsule-atlas
image_ref=${COLLECTIVE_IMAGE_REF:-${ESTIMA_IMAGE_REF:-}}

if [[ ! "$image_ref" =~ ^ghcr\.io/hi-io/(collective|estima):sha-[0-9a-f]{40}$ \
  && ! "$image_ref" =~ ^ghcr\.io/hi-io/(collective|estima)@sha256:[0-9a-f]{64}$ ]]; then
  printf '%s\n' "Set COLLECTIVE_IMAGE_REF (or ESTIMA_IMAGE_REF) to a Collective Git-SHA tag or sha256 digest." >&2
  exit 1
fi

kubectl apply -f "$manifest_dir/namespace.yaml"
kubectl -n "$namespace" get secret atlas-runtime >/dev/null
kubectl -n "$namespace" get secret atlas-postgres >/dev/null
kubectl -n "$namespace" get service atlas-postgres >/dev/null
kubectl -n "$namespace" get pvc atlas-postgres-data >/dev/null

# Stage the API independently: do not switch the legacy DNS alias until the new pod is ready.
sed "s|ghcr.io/hi-io/estima:0.1.0|$image_ref|" "$manifest_dir/api-deployment.yaml" \
  | kubectl -n "$namespace" apply -f -
kubectl -n "$namespace" rollout status deployment/estima --timeout=600s
kubectl -n "$namespace" apply -f "$manifest_dir/api-services.yaml"

printf '%s\n' "Collective is ready. The estima and atlas Services remain compatibility aliases."

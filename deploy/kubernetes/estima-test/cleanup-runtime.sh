#!/usr/bin/env bash
set -euo pipefail

namespace=fcapsule-atlas-test

# Keep the namespace, Secrets, PVCs, PVs, and synthetic PostgreSQL data.
kubectl -n "$namespace" delete deployment \
  estima atlas-postgres fcapsule-dev-a fcapsule-dev-b --ignore-not-found
kubectl -n "$namespace" delete service \
  estima atlas atlas-postgres fcapsule-dev-a fcapsule-dev-b --ignore-not-found

#!/usr/bin/env bash
set -euo pipefail

namespace=fcapsule-atlas

# Workload-only stop. Persistent volumes, Secrets, PostgreSQL, and namespace remain.
kubectl -n "$namespace" delete deployment estima --ignore-not-found
kubectl -n "$namespace" delete service estima atlas --ignore-not-found

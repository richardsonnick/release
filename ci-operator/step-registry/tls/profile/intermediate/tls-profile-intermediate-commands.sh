#!/bin/bash
set -euo pipefail

export KUBECONFIG=${SHARED_DIR}/kubeconfig

if test -f "${SHARED_DIR}/proxy-conf.sh"; then
  # shellcheck disable=SC1090
  source "${SHARED_DIR}/proxy-conf.sh"
fi

# Intermediate is the cluster default, but set it explicitly so the negative
# canary stays deterministic even if the default profile ever changes.
oc patch apiservers/cluster --type=merge -p '{"spec":{"tlsSecurityProfile":{"type":"Intermediate","intermediate":{}}}}'

oc adm wait-for-stable-cluster

tls_profile=$(oc get apiserver/cluster -ojson | jq -r .spec.tlsSecurityProfile.type)
if [[ "$tls_profile" != "Intermediate" ]]; then
  echo "Error: TLS Security Profile is '$tls_profile', expected 'Intermediate'"
  exit 1
fi

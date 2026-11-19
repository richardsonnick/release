#!/bin/bash
set -euo pipefail

[[ ${CLUSTER_PROFILE_NAME:?} == gcp-openshift-gce-devel-ci-2 ]] || exit 1
cat > "${SHARED_DIR}/jobs2-login.sh" <<'LOGIN'
[[ -s ${CLOUDSDK_AUTH_ACCESS_TOKEN_FILE:?} && ! -e ${CLUSTER_PROFILE_DIR}/gce.json ]] || exit 1
GOOGLE_PROJECT_ID=$(<"${CLUSTER_PROFILE_DIR}/openshift_gcp_project")
[[ "$GOOGLE_PROJECT_ID" == openshift-gce-devel-ci-2 ]] || exit 1
export GOOGLE_PROJECT_ID CLOUDSDK_CORE_PROJECT="$GOOGLE_PROJECT_ID"
jobs2_ssh_source() {
  local ip
  ip=$(curl -fsS --retry 3 https://checkip.amazonaws.com) || return 1
  python3 -c 'import ipaddress,sys; sys.exit(0 if ipaddress.IPv4Address(sys.argv[1]).is_global else 1)' "$ip" || return 1
  printf '%s/32\n' "$ip"
}
LOGIN

source "${SHARED_DIR}/jobs2-login.sh"
prefix="${NAMESPACE}-${UNIQUE_HASH}"
[[ "$prefix" =~ ^ci-op-[a-z0-9-]+$ ]] || { echo "Rehearsal identity or configuration check failed" >&2; exit 1; }
# Arm cleanup only after confirming that the names are unused.
cat > "${SHARED_DIR}/jobs2-resources.sh" <<'RESOURCES'
vm_zones() { gcloud compute instances list --filter="name=('${prefix}-buildhost')" --format='value(zone.basename())'; }
firewalls() { gcloud compute firewall-rules list --filter="name=('${prefix}-buildhost-ingress-allow')" --format='value(name)'; }
deployments() { gcloud deployment-manager deployments list --filter="name=('${prefix}-vpc')" --format='value(name)'; }
networks() { gcloud compute networks list --filter="name=('${prefix}-network')" --format='value(name)'; }
RESOURCES
source "${SHARED_DIR}/jobs2-resources.sh"
for query in vm_zones firewalls deployments networks; do
  existing=$("$query")
  [[ -z "$existing" ]] || { echo "A resource already exists for $prefix"; exit 1; }
done
printf '%s\n' "$prefix" > "${SHARED_DIR}/jobs2-prefix"

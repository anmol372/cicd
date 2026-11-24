# Shared build inputs for the reviewed fork workflow. Sourced by globals.sh.
if [[ "${CI_REHEARSAL:-}" == 1 ]]; then
  case "${GITHUB_REPOSITORY:-}:${GITHUB_EVENT_NAME:-}:${GITHUB_REF:-}" in
    anmol372/opflex:workflow_dispatch:refs/heads/anmrasto/gha-rehearsal-20261119|\
    anmol372/aci-containers:workflow_dispatch:refs/heads/anmrasto/gha-rehearsal-20261119|\
    anmol372/acc-provision:workflow_dispatch:refs/heads/anmrasto/gha-rehearsal-20261119) ;;
    *) echo "Rehearsal requires the reviewed test-fork workflow" >&2; return 1 ;;
  esac
  if [[ "${GITHUB_ACTIONS:-}" != true || ! "${TRAVIS_TAG:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ||
        ! "${UPSTREAM_ID:-}" =~ ^[a-f0-9]+$ ]]; then
    echo "Rehearsal requires a four-part base release and a hex upstream ID" >&2
    return 1
  fi
  RELEASE_TAG=$TRAVIS_TAG
fi

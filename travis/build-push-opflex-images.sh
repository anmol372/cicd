#!/bin/bash
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  set -Eeuo pipefail
else
  set -x
fi
SCRIPTS_DIR=$(dirname ${BASH_SOURCE[0]})
source "$SCRIPTS_DIR/globals.sh" || exit 1
export -n CI_QUAY_USERNAME CI_QUAY_TOKEN

git show --summary

IMAGE_BUILD_REGISTRY="${QUAY_REGISTRY}"
IMAGE_BUILD_TAG=${IMAGE_TAG}
OTHER_IMAGE_TAGS="${TRAVIS_TAG_WITH_UPSTREAM_ID},${TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER}"
RELEASE_TAG_WITH_UPSTREAM_ID=${RELEASE_TAG}.${UPSTREAM_ID}

if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  : "${GHA_DATE_TAG:?GHA_DATE_TAG is required for a GitHub Actions build}"
  : "${GHA_OPFLEX_BUILD_PHASE:?GHA_OPFLEX_BUILD_PHASE is required for a GitHub Actions build}"
  : "${TRAVIS_BUILD_NUMBER:?TRAVIS_BUILD_NUMBER is required for a GitHub Actions build}"
  : "${CI_ARTIFACT_DIR:?CI_ARTIFACT_DIR is required for a GitHub Actions build}"

  if [[ "${CI_REHEARSAL:-}" == 1 ]]; then
    [[ "${GITHUB_REPOSITORY}" == anmol372/opflex ]] || exit 1
  else
    : "${GHA_TRIGGER_TAG:?GHA_TRIGGER_TAG is required for a GitHub Actions build}"
    [[ "${GITHUB_REPOSITORY:-}" == noironetworks/opflex &&
       "${GITHUB_REF:-}" == "refs/tags/${GHA_TRIGGER_TAG}" ]] || {
      echo "GitHub Actions build requires a tag in noironetworks/opflex" >&2; exit 1;
    }
    case "${GHA_OPFLEX_BUILD_PHASE}:${GHA_TRIGGER_TAG}" in
      "base:${RELEASE_TAG}-opflex-build-base"|"runtime:${RELEASE_TAG}") ;;
      *) echo "Tag does not select the requested OpFlex build phase" >&2; exit 1 ;;
    esac
  fi
  [[ "${TRAVIS_TAG:-}" == "${RELEASE_TAG}" ]] || {
    echo "GitHub Actions must use release tag ${RELEASE_TAG} for image naming" >&2
    exit 1
  }
  [[ "${GHA_DATE_TAG}" =~ ^[0-9]{6}$ ]] || {
    echo "GHA_DATE_TAG must use MMDDYY format" >&2
    exit 1
  }
  [[ "${TRAVIS_BUILD_NUMBER}" =~ ^[1-9][0-9]*$ ]] || {
    echo "TRAVIS_BUILD_NUMBER must be a positive integer" >&2
    exit 1
  }

  case "${GHA_OPFLEX_BUILD_PHASE}" in
    base)
      BUILD_BASE=true
      ;;
    runtime)
      BUILD_BASE=false
      if [[ -z "${EXPECTED_OPFLEX_BASE_DIGEST:-}" ]]; then
        echo "::error::EXPECTED_OPFLEX_BASE_DIGEST is required for runtime phase" >&2
        exit 1
      fi
      ;;
    *)
      echo "::error::GHA_OPFLEX_BUILD_PHASE must be base or runtime" >&2
      exit 1
      ;;
  esac
  IMAGE_BUILD_TAG="${RELEASE_TAG_WITH_UPSTREAM_ID}"
  OTHER_IMAGE_TAGS="${RELEASE_TAG_WITH_UPSTREAM_ID},${RELEASE_TAG_WITH_UPSTREAM_ID}.${GHA_DATE_TAG}.${TRAVIS_BUILD_NUMBER}"
else
  # Travis selects the build-base job from the release tag name.
  if [[ "${TRAVIS_TAG}" == *"opflex-build-base"* ]]; then
    BUILD_BASE=true
    IMAGE_BUILD_TAG=${RELEASE_TAG_WITH_UPSTREAM_ID}
    OTHER_IMAGE_TAGS="${RELEASE_TAG_WITH_UPSTREAM_ID},${RELEASE_TAG_WITH_UPSTREAM_ID}.${DATE_TAG}.${TRAVIS_BUILD_NUMBER},${UPSTREAM_IMAGE_Z_TAG}"
  else
    BUILD_BASE=false
  fi
fi

# The child selects its build phase and immutable runtime input from the environment.
if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
  export GHA_OPFLEX_BUILD_PHASE
  if [[ "${BUILD_BASE}" == false ]]; then
    export EXPECTED_OPFLEX_BASE_DIGEST
  fi
fi

if [[ "${GITHUB_ACTIONS:-false}" == "true" && "${CI_EVIDENCE_MODE:-}" == "prepared-v1" ]]; then
  if [[ "${BUILD_BASE}" == true ]]; then
    PROVENANCE_IMAGES=(opflex-build-base)
  else
    PROVENANCE_IMAGES=(opflex opflex-build)
  fi
  for IMAGE in "${PROVENANCE_IMAGES[@]}"; do
    PROVENANCE_ARGS=(--build-arg "DOCKER_HUB_ID=${IMAGE_BUILD_REGISTRY}" --build-arg "DOCKER_TAG=${IMAGE_BUILD_TAG}")
    if [[ "${IMAGE}" == opflex-build ]]; then
      : "${EXPECTED_OPFLEX_BASE_DIGEST:?Expected immutable build base is required}"
      PROVENANCE_ARGS+=(--build-arg "OPFLEX_BUILD_BASE_IMAGE=${IMAGE_BUILD_REGISTRY}/opflex-build-base@${EXPECTED_OPFLEX_BASE_DIGEST}" --expected-digest "${EXPECTED_OPFLEX_BASE_DIGEST}")
    fi
    python3 "$SCRIPTS_DIR/scan_result.py" capture-base \
      --dockerfile "docker/travis/Dockerfile-${IMAGE}" \
      --pull-once-file "${CI_ARTIFACT_DIR}/base-provenance/.pulled.json" \
      "${PROVENANCE_ARGS[@]}" \
      --output "${CI_ARTIFACT_DIR}/base-provenance/${IMAGE}.json"
  done
fi

BUILD_LABEL=
if [[ "${CI_REHEARSAL:-}" == 1 ]]; then
  BUILD_LABEL=--label=quay.expires-after=6h
fi
docker/travis/build-opflex-travis.sh "${IMAGE_BUILD_REGISTRY}" "${IMAGE_BUILD_TAG}" "${BUILD_LABEL}"
docker images

#Fetching Base Image - Common base image for every container so fetching once
BASE_IMAGE=$(grep -E '^FROM' docker/travis/Dockerfile-opflex | awk '{print $2}')
docker pull ${BASE_IMAGE}
docker images


if [[ "${BUILD_BASE}" == true ]]; then
  ALL_IMAGES=("opflex-build-base")
  for IMAGE in "${ALL_IMAGES[@]}"; do
    CI_QUAY_USERNAME="${CI_QUAY_USERNAME:-}" CI_QUAY_TOKEN="${CI_QUAY_TOKEN:-}" \
      "$SCRIPTS_DIR/push-images.sh" "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${BASE_IMAGE}"
  done
else
  # The child checked and used the selected digest before it built. The alias can
  # change after that check without changing the runtime's actual build input.
  ALL_IMAGES=("opflex-build" "opflex")
  for IMAGE in "${ALL_IMAGES[@]}"; do
    CI_QUAY_USERNAME="${CI_QUAY_USERNAME:-}" CI_QUAY_TOKEN="${CI_QUAY_TOKEN:-}" \
      "$SCRIPTS_DIR/push-images.sh" "${IMAGE_BUILD_REGISTRY}" "${IMAGE}" "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${BASE_IMAGE}"
  done

  IMAGE_SHA=$(docker image inspect --format='{{.Id}}' "${IMAGE_BUILD_REGISTRY}/opflex:${IMAGE_BUILD_TAG}")
  if [[ "${GITHUB_ACTIONS:-false}" == "true" ]]; then
    # For GitHub Actions: write metadata for separate status publication step
    IMAGES_METADATA_FILE="${CI_ARTIFACT_DIR}/opflex-images-metadata.jsonl"
    printf '%s\n' "{\"image\":\"opflex\",\"registry\":\"${QUAY_NOIRO_REGISTRY}\",\"tag\":\"${IMAGE_BUILD_TAG}\",\"other_tags\":\"${OTHER_IMAGE_TAGS}\",\"sha\":\"${IMAGE_SHA}\",\"base_image\":\"${BASE_IMAGE}\"}" >> "$IMAGES_METADATA_FILE"
  else
    # For Travis: call status endpoint directly (backward compatibility)
    if [[ "${SKIP_CICD_STATUS:-false}" != "true" ]]; then
      "$SCRIPTS_DIR/push-to-cicd-status.sh" "${QUAY_NOIRO_REGISTRY}" opflex "${IMAGE_BUILD_TAG}" "${OTHER_IMAGE_TAGS}" "${IMAGE_SHA}" "${BASE_IMAGE}" || {
        echo "WARNING: Failed to publish status for opflex, but image push succeeded - continuing"
      }
    fi
  fi
fi

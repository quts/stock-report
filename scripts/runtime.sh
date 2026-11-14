#!/bin/sh
# One image and command contract for Apple Container and Docker.
set -eu
cd "$(dirname "$0")/.."
engine=${CONTAINER_ENGINE:-container}
image=${STOCK_REPORT_IMAGE:-stock-report:phase0}
case "$engine" in container|docker) ;; *) echo 'CONTAINER_ENGINE must be container or docker' >&2; exit 2 ;; esac

# The SEC contact is operator configuration: forwarded to the container only when the
# operator exported it, never defaulted, never derived from login data, never logged.
run_container() {
  if [ -n "${STOCK_REPORT_SEC_CONTACT:-}" ]; then
    exec "$engine" run --rm --env "STOCK_REPORT_SEC_CONTACT=$STOCK_REPORT_SEC_CONTACT" \
      -v "$PWD:/work" -w /work "$image" "$@"
  fi
  exec "$engine" run --rm -v "$PWD:/work" -w /work "$image" "$@"
}

command=${1:-test}
[ "$#" -eq 0 ] || shift
case "$command" in
  build) exec "$engine" build -f Dockerfile -t "$image" . "$@" ;;
  test) run_container python -m pytest "$@" ;;
  cli) run_container python -m stock_report "$@" ;;
  qa) run_container python scripts/qa_pdf.py "$@" ;;
  bundle) run_container python scripts/review_bundle.py "$@" ;;
  demo) run_container python scripts/demo.py "$@" ;;
  smoke) run_container python scripts/source_smoke.py "$@" ;;
  *) echo 'Usage: scripts/runtime.sh build|test|cli|demo|qa|bundle|smoke [arguments]' >&2; exit 2 ;;
esac

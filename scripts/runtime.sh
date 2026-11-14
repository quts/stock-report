#!/bin/sh
# One image and command contract for Apple Container and Docker.
set -eu
cd "$(dirname "$0")/.."
engine=${CONTAINER_ENGINE:-container}
image=${STOCK_REPORT_IMAGE:-stock-report:phase0}
case "$engine" in container|docker) ;; *) echo 'CONTAINER_ENGINE must be container or docker' >&2; exit 2 ;; esac
command=${1:-test}
[ "$#" -eq 0 ] || shift
case "$command" in
  build) exec "$engine" build -f Dockerfile -t "$image" . "$@" ;;
  test) exec "$engine" run --rm -v "$PWD:/work" -w /work "$image" python -m pytest "$@" ;;
  cli) exec "$engine" run --rm -v "$PWD:/work" -w /work "$image" python -m stock_report "$@" ;;
  qa) exec "$engine" run --rm -v "$PWD:/work" -w /work "$image" python scripts/qa_pdf.py "$@" ;;
  bundle) exec "$engine" run --rm -v "$PWD:/work" -w /work "$image" python scripts/review_bundle.py "$@" ;;
  demo) exec "$engine" run --rm -v "$PWD:/work" -w /work "$image" python scripts/demo.py "$@" ;;
  *) echo 'Usage: scripts/runtime.sh build|test|cli|demo|qa|bundle [arguments]' >&2; exit 2 ;;
esac

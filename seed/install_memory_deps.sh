#!/usr/bin/env bash
# Long-term memory dependency installer
# Installs LanceDB (vector + full-text store) and fastembed (local ONNX
# embeddings) into the project venv, then pre-downloads the embedding model so
# the first ingest inside a heartbeat cycle isn't a cold ~130 MB network fetch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Keep these in sync with pyproject.toml and scripts/memory_store.py.
LANCEDB_SPEC="${LANCEDB_SPEC:-lancedb>=0.36}"
FASTEMBED_SPEC="${FASTEMBED_SPEC:-fastembed>=0.8}"
EMBED_MODEL="${EMBED_MODEL:-BAAI/bge-small-en-v1.5}"

print_info()    { echo "  → $*"; }
print_success() { echo "  ✓ $*"; }
print_warning() { echo "  ⚠ $*"; }
print_error()   { echo "  ✗ $*" >&2; }

install_packages() {
    # `uv sync` is the only path that respects uv.lock and the numpy<2.0 pin in
    # pyproject.toml. `uv pip install lancedb fastembed` resolves independently
    # and would happily pull numpy>=2, breaking the rest of the environment —
    # so it is deliberately not used here.
    if command -v uv >/dev/null 2>&1; then
        print_info "Syncing project dependencies from uv.lock"
        (cd "${PROJECT_ROOT}" && uv sync)
        print_success "dependencies synced via uv"
        return 0
    fi

    local project_pip="${PROJECT_ROOT}/.venv/bin/pip"
    if [[ -x "${project_pip}" ]]; then
        print_warning "uv not found — falling back to pip (does not honour uv.lock)"
        "${project_pip}" install --upgrade "${LANCEDB_SPEC}" "${FASTEMBED_SPEC}"
        print_success "installed via pip"
        return 0
    fi

    print_warning "No uv and no project venv — installing into the ambient environment"
    pip install --upgrade "${LANCEDB_SPEC}" "${FASTEMBED_SPEC}"
}

verify_imports() {
    local project_python="${PROJECT_ROOT}/.venv/bin/python3"
    if [[ ! -x "${project_python}" ]]; then
        print_warning "Project venv python not found — skipping verification"
        return 0
    fi

    print_info "Verifying imports..."
    if "${project_python}" -c "import lancedb, fastembed; print('lancedb', lancedb.__version__)"; then
        print_success "lancedb and fastembed import cleanly"
    else
        print_error "import check failed"
        return 1
    fi
}

warm_model() {
    # Downloads the ONNX weights into the fastembed cache. Without this the
    # first ingest pays the download inside a heartbeat cycle, where a slow or
    # unavailable HuggingFace makes the cycle look like a memory failure.
    local project_python="${PROJECT_ROOT}/.venv/bin/python3"
    if [[ ! -x "${project_python}" ]]; then
        print_warning "Project venv python not found — skipping model warm-up"
        return 0
    fi

    print_info "Pre-downloading ${EMBED_MODEL} (first run only, ~130 MB)..."
    if "${project_python}" - <<EOF
import sys
sys.path.insert(0, "${PROJECT_ROOT}")
from scripts import memory_store as store

vec = store.embed_query("warm up the embedding model")
if len(vec) != store.EMBED_DIM:
    print(f"dimension mismatch: got {len(vec)}, expected {store.EMBED_DIM}", file=sys.stderr)
    sys.exit(1)
print(f"embedding OK — {store.EMBED_MODEL}, {len(vec)} dims")
EOF
    then
        print_success "embedding model cached and verified"
    else
        print_warning "model warm-up failed — the first ingest will retry the download"
    fi
}

main() {
    echo "Long-term memory dependency installer"
    echo "  LanceDB:   ${LANCEDB_SPEC}"
    echo "  fastembed: ${FASTEMBED_SPEC}"
    echo "  model:     ${EMBED_MODEL}"
    echo ""
    # Never let an install hiccup skip the model warm-up: paying the 130 MB
    # download inside the first heartbeat cycle is the failure this script
    # exists to prevent, so the later steps run and report on their own.
    install_packages || print_warning "dependency install reported an error; continuing"
    verify_imports || print_warning "import verification failed"
    warm_model
    echo ""
    print_success "Done. Build the index with:"
    echo "      cd ${PROJECT_ROOT} && uv run python scripts/memory_ingest.py --build"
}

main "$@"

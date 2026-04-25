#!/bin/bash
# Memvid SDK Installer
# Installs the published `memvid-sdk-free` wheel from PyPI into the project
# venv. The wheel ships with fastembed support so local embedding models
# (bge-base, etc.) work without an external API key.

set -e

MEMVID_SDK_VERSION="${MEMVID_SDK_VERSION:-2.0.159}"
MEMVID_SDK_PACKAGE="memvid-sdk-free==${MEMVID_SDK_VERSION}"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_success() { echo -e "${GREEN}✔${NC} $1"; }
print_error()   { echo -e "${RED}✖${NC} $1"; }
print_info()    { echo -e "${BLUE}→${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

check_python() {
    if command_exists python3; then
        print_success "python3 already installed ($(python3 --version 2>&1 | cut -d' ' -f2))"
    else
        print_error "python3 not found — please install python3 and re-run"
        exit 1
    fi
}

install_memvid_sdk() {
    local script_dir project_root
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    project_root="$(cd "${script_dir}/.." && pwd)"

    if [[ ! -f "${project_root}/pyproject.toml" ]]; then
        print_warning "No pyproject.toml at ${project_root} — installing with system pip"
        pip install --upgrade "${MEMVID_SDK_PACKAGE}"
        return 0
    fi

    print_info "Installing ${MEMVID_SDK_PACKAGE} into ${project_root}/.venv"

    if command_exists uv; then
        (cd "${project_root}" && uv pip install --upgrade "${MEMVID_SDK_PACKAGE}")
        print_success "${MEMVID_SDK_PACKAGE} installed via uv"
    else
        local project_pip="${project_root}/.venv/bin/pip"
        if [[ ! -x "${project_pip}" ]]; then
            print_info "Creating project venv at ${project_root}/.venv"
            python3 -m venv "${project_root}/.venv"
        fi
        "${project_pip}" install --upgrade "${MEMVID_SDK_PACKAGE}"
        print_success "${MEMVID_SDK_PACKAGE} installed via pip"
    fi
}

verify() {
    local script_dir project_root project_python
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    project_root="$(cd "${script_dir}/.." && pwd)"
    project_python="${project_root}/.venv/bin/python3"

    if [[ ! -x "${project_python}" ]]; then
        project_python="$(command -v python3)"
    fi

    print_info "Verifying installation via ${project_python}..."
    if "${project_python}" -c "import memvid_sdk; assert hasattr(memvid_sdk, 'create') and hasattr(memvid_sdk, 'use'); print('memvid_sdk OK')"; then
        print_success "memvid_sdk imports cleanly and exposes create/use"
    else
        print_error "memvid_sdk import failed"
        exit 1
    fi
}

verify_embedding() {
    local script_dir project_root project_python
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    project_root="$(cd "${script_dir}/.." && pwd)"
    project_python="${project_root}/.venv/bin/python3"

    if [[ ! -x "${project_python}" ]]; then
        print_warning "Project venv python not found — skipping embedding verification"
        return 0
    fi

    print_info "Verifying fastembed embedding in project venv..."
    if "${project_python}" - <<'EOF'
import memvid_sdk, tempfile, os, sys

with tempfile.TemporaryDirectory() as d:
    mv2 = os.path.join(d, "test.mv2")
    try:
        mem = memvid_sdk.create(mv2, enable_vec=True, enable_lex=True)
        mem.put(
            title="fastembed test",
            label="test",
            text="Testing local embeddings with fastembed bge-base model.",
            enable_embedding=True,
            embedding_model="bge-base",
        )
        print("fastembed put() OK")
    except Exception as e:
        print(f"fastembed put() FAILED: {e}", file=sys.stderr)
        sys.exit(1)
EOF
    then
        print_success "fastembed embedding verified — local models work"
    else
        print_warning "fastembed embedding check failed (model download may be needed on first use)"
    fi
}

main() {
    echo "Memvid SDK Installer (PyPI: ${MEMVID_SDK_PACKAGE})"
    echo ""

    check_python
    echo ""

    install_memvid_sdk
    echo ""

    verify
    echo ""

    verify_embedding
    echo ""

    print_success "Installation complete."
    print_info "Local embedding models (bge-base, etc.) are now available."
    print_info "Use embedding_model=\"bge-base\" in put() calls."
}

main

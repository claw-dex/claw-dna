#!/bin/bash
# Memvid SDK Installer for macOS and Linux
# Clones the memvid-sdk repo and builds the Python SDK (Rust extension via
# maturin) using the SDK's own scripts/build_sdk.sh — which now includes the
# `fastembed` Cargo feature so local embedding models (bge-base, etc.) work
# without an external API key. Then builds a wheel and installs it into the
# project venv.

set -e

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

# Detect OS (kept minimal — the SDK's build_sdk.sh handles platform specifics)
detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
    elif [[ -f /etc/os-release ]]; then
        OS="linux"
    else
        print_error "Unable to detect operating system"
        exit 1
    fi
    print_info "Detected OS: $OS"
}

# Check for git
check_git() {
    if command_exists git; then
        print_success "git already installed ($(git --version | cut -d' ' -f3))"
    else
        print_error "git not found — please install git and re-run"
        exit 1
    fi
}

# Check for python3 (build_sdk.sh needs it to create its own .venv)
check_python() {
    if command_exists python3; then
        print_success "python3 already installed ($(python3 --version 2>&1 | cut -d' ' -f2))"
    else
        print_error "python3 not found — please install python3 and re-run"
        exit 1
    fi
}

# Check for Rust (maturin needs it)
check_rust() {
    # Pick up an existing rustup install that isn't on PATH yet
    if ! command_exists rustc && [[ -f "$HOME/.cargo/env" ]]; then
        # shellcheck disable=SC1091
        . "$HOME/.cargo/env"
    fi

    if command_exists rustc && command_exists cargo; then
        print_success "Rust already installed ($(rustc --version | cut -d' ' -f2))"
    else
        print_error "Rust toolchain not found"
        print_info "Install Rust from https://rustup.rs or run the SDK's build-tools helper:"
        print_info "  bash \$MEMVID_SDK_SRC_DIR/scripts/install_build_tools.sh"
        exit 1
    fi
}

# Clone (or update) the SDK source and run its build script
install_memvid_sdk() {
    MEMVID_SDK_REPO="${MEMVID_SDK_REPO:-https://github.com/0xGosu/memvid-sdk.git}"
    MEMVID_SDK_SRC_DIR="${MEMVID_SDK_SRC_DIR:-$HOME/.memvid-sdk-src}"

    print_info "Installing memvid-sdk from source..."
    print_info "Repository: $MEMVID_SDK_REPO"
    print_info "Source directory: $MEMVID_SDK_SRC_DIR"

    if [[ -d "$MEMVID_SDK_SRC_DIR/.git" ]]; then
        print_info "Updating existing clone at $MEMVID_SDK_SRC_DIR..."
        git -C "$MEMVID_SDK_SRC_DIR" fetch --all --prune
        git -C "$MEMVID_SDK_SRC_DIR" pull --ff-only
    else
        print_info "Cloning $MEMVID_SDK_REPO into $MEMVID_SDK_SRC_DIR..."
        rm -rf "$MEMVID_SDK_SRC_DIR"
        git clone "$MEMVID_SDK_REPO" "$MEMVID_SDK_SRC_DIR"
    fi

    local build_script="$MEMVID_SDK_SRC_DIR/scripts/build_sdk.sh"
    if [[ ! -f "$build_script" ]]; then
        print_error "Build script not found at $build_script"
        exit 1
    fi

    print_info "Running $build_script --wheel..."
    bash "$build_script" --wheel

    print_success "memvid-sdk built successfully (with fastembed + wheel)"
}

# Locate the wheel produced by build_sdk.sh --wheel and install it into the
# project venv. No re-compile happens here — we just pip-install the prebuilt
# wheel (uv pip install -e would trigger a fresh maturin build without the
# ORT env vars, which fails with a 504, so we deliberately use the wheel).
install_wheel_to_project_venv() {
    local src_dir="${MEMVID_SDK_SRC_DIR:-$HOME/.memvid-sdk-src}"
    local wheel_dir="${src_dir}/.wheels"

    local wheel
    wheel="$(ls -t "${wheel_dir}"/memvid*.whl 2>/dev/null | head -1)"
    if [[ -z "${wheel}" ]]; then
        print_error "No wheel found in ${wheel_dir} — did build_sdk.sh --wheel succeed?"
        exit 1
    fi
    print_success "Wheel: $(basename "${wheel}")"

    local script_dir project_root
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    project_root="$(cd "${script_dir}/.." && pwd)"

    if [[ ! -f "${project_root}/pyproject.toml" ]]; then
        print_warning "No pyproject.toml at ${project_root} — skipping project venv install"
        print_info "Install the wheel manually: pip install ${wheel}"
        return 0
    fi

    if command_exists uv; then
        (cd "${project_root}" && uv sync --quiet) || true
        if (cd "${project_root}" && uv pip install --force-reinstall "${wheel}"); then
            print_success "memvid_sdk (fastembed) installed into ${project_root}/.venv"
        else
            print_error "uv pip install failed for wheel ${wheel}"
            exit 1
        fi
    else
        local project_pip="${project_root}/.venv/bin/pip"
        if [[ -x "${project_pip}" ]]; then
            "${project_pip}" install --force-reinstall "${wheel}" && \
                print_success "memvid_sdk (fastembed) installed into ${project_root}/.venv"
        else
            print_warning "Neither uv nor project pip found — install the wheel manually:"
            print_info "  pip install ${wheel}"
        fi
    fi
}

# Verify installation by importing the SDK from its own venv
verify() {
    print_info "Verifying installation..."
    local sdk_python="${MEMVID_SDK_SRC_DIR}/.venv/bin/python3"
    if [[ ! -x "$sdk_python" ]]; then
        print_error "SDK venv python not found at $sdk_python"
        exit 1
    fi
    if "$sdk_python" -c "import memvid_sdk; print('memvid_sdk version:', getattr(memvid_sdk, '__version__', 'unknown'))"; then
        print_success "memvid_sdk imports cleanly from SDK venv"
    else
        print_error "memvid_sdk import failed"
        exit 1
    fi
}

# Verify that fastembed embedding works end-to-end in the project venv
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

# Create a tiny temp .mv2 and try to put() with local embeddings
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
    echo "Memvid SDK Installer (with fastembed local embeddings)"
    echo "Checking prerequisites…"
    echo ""

    detect_os
    check_git
    check_python
    check_rust
    echo ""

    # Step 1: Build SDK + wheel (upstream build_sdk.sh --wheel)
    install_memvid_sdk
    echo ""

    # Step 2: Verify build
    verify
    echo ""

    # Step 3: Install the prebuilt wheel into the project venv (no recompile)
    install_wheel_to_project_venv
    echo ""

    # Step 4: Verify fastembed works end-to-end
    verify_embedding
    echo ""

    print_success "Installation complete."
    print_info "Local embedding models (bge-base, etc.) are now available."
    print_info "Use embedding_model=\"bge-base\" in put() calls."
}

main

#!/bin/bash
# Memvid SDK Installer for macOS and Linux
# Clones the memvid-sdk repo and builds the Python SDK (Rust extension via
# maturin) using the SDK's own scripts/build_sdk.sh.

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

    print_info "Running $build_script..."
    bash "$build_script"

    print_success "memvid-sdk built successfully"
    print_info "SDK venv:         $MEMVID_SDK_SRC_DIR/.venv"
    print_info "To use the SDK in another project, either:"
    print_info "  - activate the SDK venv: source $MEMVID_SDK_SRC_DIR/.venv/bin/activate"
    print_info "  - or install editably into your project venv:"
    print_info "      uv pip install -e $MEMVID_SDK_SRC_DIR"
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

# Install the built SDK editably into the claw-dna project venv so that
# `uv run python scripts/memory_*.py` can `import memvid_sdk` directly.
install_into_project_venv() {
    local script_dir project_root
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    project_root="$(cd "${script_dir}/.." && pwd)"

    if [[ ! -f "${project_root}/pyproject.toml" ]]; then
        print_warning "No pyproject.toml at ${project_root} — skipping project venv install"
        return 0
    fi

    print_info "Installing memvid_sdk editably into claw-dna project venv..."
    print_info "  project:  ${project_root}"
    print_info "  source:   ${MEMVID_SDK_SRC_DIR}"

    if ! command_exists uv; then
        print_warning "uv not found on PATH — skipping project venv install"
        print_info "To install manually later:"
        print_info "  cd ${project_root} && uv pip install -e ${MEMVID_SDK_SRC_DIR}"
        return 0
    fi

    # Ensure the project has a venv to install into
    (cd "${project_root}" && uv sync --quiet) || {
        print_warning "uv sync failed at ${project_root} — skipping editable install"
        return 0
    }

    if (cd "${project_root}" && uv pip install -e "${MEMVID_SDK_SRC_DIR}"); then
        print_success "memvid_sdk installed editably into ${project_root}/.venv"
    else
        print_error "Failed to install memvid_sdk into project venv"
        exit 1
    fi

    # Verify import from the project venv
    local project_python="${project_root}/.venv/bin/python3"
    if [[ -x "$project_python" ]] && "$project_python" -c "import memvid_sdk" 2>/dev/null; then
        print_success "memvid_sdk imports cleanly from project venv"
    else
        print_warning "memvid_sdk installed but import check failed — investigate manually"
    fi
}

main() {
    echo "Memvid SDK Installer"
    echo "Checking prerequisites…"
    echo ""

    detect_os
    check_git
    check_python
    check_rust
    echo ""

    install_memvid_sdk
    echo ""

    verify
    echo ""

    install_into_project_venv
    echo ""

    print_success "Installation complete."
}

main

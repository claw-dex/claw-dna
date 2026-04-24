#!/bin/bash
# Memvid Installer for macOS and Linux
# Builds memvid from source using the Rush toolchain

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print success message
print_success() {
    echo -e "${GREEN}✔${NC} $1"
}

# Print error message
print_error() {
    echo -e "${RED}✖${NC} $1"
}

# Print info message
print_info() {
    echo -e "${BLUE}→${NC} $1"
}

# Print warning message
print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

# Detect OS
detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
        PKG_MANAGER="brew"
        
        # Check if Homebrew is installed
        if ! command_exists brew; then
            print_error "Homebrew is not installed"
            print_info "Please install Homebrew first:"
            print_info "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            exit 1
        fi
    elif [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO_NAME="${PRETTY_NAME:-${NAME:-$ID}}"
        
        if [[ "$ID" == "ubuntu" ]] || [[ "$ID" == "debian" ]]; then
            OS="linux"
            PKG_MANAGER="apt"
            DISTRO_FAMILY="debian"
        elif [[ "$ID" == "fedora" ]] || [[ "$ID" == "rhel" ]] || [[ "$ID" == "centos" ]] || [[ "$ID" == "rocky" ]] || [[ "$ID" == "almalinux" ]]; then
            OS="linux"
            PKG_MANAGER="dnf"
            DISTRO_FAMILY="rhel"
        elif [[ "$ID" == "arch" ]] || [[ "$ID" == "manjaro" ]]; then
            OS="linux"
            PKG_MANAGER="pacman"
            DISTRO_FAMILY="arch"
        elif [[ "$ID" == "alpine" ]]; then
            OS="linux"
            PKG_MANAGER="apk"
            DISTRO_FAMILY="alpine"
        else
            print_error "Unsupported Linux distribution: $ID"
            print_info "Supported distributions: Ubuntu, Debian, Fedora, RHEL, CentOS, Arch, Alpine"
            exit 1
        fi
        
        # Check if sudo is available (except for Alpine which might use su)
        if ! command_exists sudo && [[ "$DISTRO_FAMILY" != "alpine" ]]; then
            print_error "sudo is not available"
            print_info "Please install sudo or run as root"
            exit 1
        fi
    else
        print_error "Unable to detect operating system"
        exit 1
    fi
    
    if [[ "$OS" == "linux" ]]; then
        print_info "Detected OS: $OS ($DISTRO_NAME)"
    else
        print_info "Detected OS: $OS"
    fi
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check for Xcode Command Line Tools on macOS
check_xcode_cli_tools() {
    if [[ "$OS" != "macos" ]]; then
        return 0
    fi
    
    # Check if Xcode CLI Tools are installed
    if xcode-select -p &>/dev/null; then
        print_success "Xcode Command Line Tools already installed"
        return 0
    else
        print_error "Xcode Command Line Tools not found"
        print_warning "Xcode Command Line Tools are required for everything to work properly"
        echo ""
        
        # Ask for confirmation
        if [[ -t 0 ]] && [[ -t 1 ]]; then
            read -p "Install Xcode Command Line Tools now? [Y/n] " -n 1 -r
            echo
        elif [[ -c /dev/tty ]]; then
            read -p "Install Xcode Command Line Tools now? [Y/n] " -n 1 -r < /dev/tty
            echo
        else
            REPLY="Y"
        fi
        
        if [[ ! $REPLY =~ ^[Yy]$ ]] && [[ ! $REPLY == "" ]]; then
            print_error "Xcode Command Line Tools are required for everything to work properly"
            print_info "Please install them manually with: xcode-select --install"
            exit 1
        fi
        
        print_info "Installing Xcode Command Line Tools..."
        xcode-select --install
        
        print_info "Please complete the Xcode Command Line Tools installation in the popup window"
        print_info "Then run this installer again"
        exit 0
    fi
}

# Install system dependencies for Linux
install_system_deps() {
    if [[ "$OS" != "linux" ]]; then
        return 0
    fi
    
    print_info "Checking system dependencies..."
    
    NEEDS_DEPS=false
    DEPS_TO_INSTALL=()
    
    # Check and collect missing dependencies
    if [[ "$DISTRO_FAMILY" == "debian" ]]; then
        if ! dpkg -l | grep -q "^ii.*ca-certificates"; then
            NEEDS_DEPS=true
            DEPS_TO_INSTALL+=("ca-certificates")
        fi
    elif [[ "$DISTRO_FAMILY" == "rhel" ]]; then
        if ! rpm -q ca-certificates &>/dev/null; then
            NEEDS_DEPS=true
            DEPS_TO_INSTALL+=("ca-certificates")
        fi
    elif [[ "$DISTRO_FAMILY" == "arch" ]]; then
        if ! pacman -Q ca-certificates &>/dev/null; then
            NEEDS_DEPS=true
            DEPS_TO_INSTALL+=("ca-certificates")
        fi
    elif [[ "$DISTRO_FAMILY" == "alpine" ]]; then
        if ! apk info -e ca-certificates &>/dev/null; then
            NEEDS_DEPS=true
            DEPS_TO_INSTALL+=("ca-certificates")
        fi
    fi
    
    if [[ "$NEEDS_DEPS" == false ]]; then
        print_success "All system dependencies are installed"
        return 0
    fi
    
    print_info "Installing system dependencies: ${DEPS_TO_INSTALL[*]}..."
    
    if [[ "$DISTRO_FAMILY" == "debian" ]]; then
        sudo apt-get update
        sudo apt-get install -y --no-install-recommends "${DEPS_TO_INSTALL[@]}"
    elif [[ "$DISTRO_FAMILY" == "rhel" ]]; then
        sudo dnf install -y "${DEPS_TO_INSTALL[@]}"
    elif [[ "$DISTRO_FAMILY" == "arch" ]]; then
        sudo pacman -S --noconfirm "${DEPS_TO_INSTALL[@]}"
    elif [[ "$DISTRO_FAMILY" == "alpine" ]]; then
        if command_exists sudo; then
            sudo apk add --no-cache "${DEPS_TO_INSTALL[@]}"
        else
            apk add --no-cache "${DEPS_TO_INSTALL[@]}"
        fi
    fi
    
    print_success "System dependencies installed successfully"
}

# Check for git
check_git() {
    if command_exists git; then
        GIT_VERSION=$(git --version | cut -d' ' -f3)
        print_success "git already installed (version $GIT_VERSION)"
        return 0
    else
        print_error "git not found"
        return 1
    fi
}

# Install git
install_git() {
    print_info "Installing git using $PKG_MANAGER..."
    
    if [[ "$OS" == "macos" ]]; then
        brew install git
    elif [[ "$OS" == "linux" ]]; then
        if [[ "$DISTRO_FAMILY" == "debian" ]]; then
            sudo apt-get update
            sudo apt-get install -y git
        elif [[ "$DISTRO_FAMILY" == "rhel" ]]; then
            sudo dnf install -y git
        elif [[ "$DISTRO_FAMILY" == "arch" ]]; then
            sudo pacman -S --noconfirm git
        elif [[ "$DISTRO_FAMILY" == "alpine" ]]; then
            if command_exists sudo; then
                sudo apk add --no-cache git
            else
                apk add --no-cache git
            fi
        fi
    fi
    
    if command_exists git; then
        print_success "git installed successfully"
    else
        print_error "git installation failed"
        exit 1
    fi
}

# Check if memvid is already installed
check_memvid() {
    if command_exists memvid; then
        # Get version - output format is "memvid 2.0.131"
        MEMVID_VERSION=$(memvid --version 2>&1 | awk '{print $2}')
        print_success "memvid already installed ($MEMVID_VERSION)"
        return 0
    else
        print_error "memvid not found"
        return 1
    fi
}

# Install missing tools
install_missing() {
    NEEDS_GIT=false
    NEEDS_MEMVID=false

    if ! check_git; then
        NEEDS_GIT=true
    fi

    if ! check_memvid; then
        NEEDS_MEMVID=true
    fi

    if [[ "$NEEDS_GIT" == false ]] && [[ "$NEEDS_MEMVID" == false ]]; then
        print_info "All dependencies are already installed"
        return 0
    fi

    [[ "$NEEDS_GIT" == true ]] && install_git
    [[ "$NEEDS_MEMVID" == true ]] && install_memvid
}

# Install memvid from source using Rush
install_memvid() {
    MEMVID_REPO="https://github.com/0xGosu/memvid.git"
    MEMVID_SRC_DIR="${MEMVID_SRC_DIR:-$HOME/.memvid-src}"

    print_info "Installing memvid from source..."
    print_info "Repository: $MEMVID_REPO"
    print_info "Source directory: $MEMVID_SRC_DIR"

    if ! command_exists rush; then
        print_error "rush not found in PATH"
        print_info "Rush toolchain is required to build memvid from source"
        exit 1
    fi

    # Clone or update the repo
    if [[ -d "$MEMVID_SRC_DIR/.git" ]]; then
        print_info "Updating existing clone at $MEMVID_SRC_DIR..."
        git -C "$MEMVID_SRC_DIR" fetch --all --prune
        git -C "$MEMVID_SRC_DIR" pull --ff-only
    else
        print_info "Cloning $MEMVID_REPO into $MEMVID_SRC_DIR..."
        rm -rf "$MEMVID_SRC_DIR"
        git clone "$MEMVID_REPO" "$MEMVID_SRC_DIR"
    fi

    # Build using Rush
    (
        cd "$MEMVID_SRC_DIR"
        print_info "Running 'rush install'..."
        rush install
        print_info "Running 'rush build'..."
        rush build
    )

    # Locate the built memvid binary
    CLI_DIR="$MEMVID_SRC_DIR/packages/memvid-cli"
    if [[ ! -d "$CLI_DIR" ]]; then
        CLI_DIR=$(find "$MEMVID_SRC_DIR" -type d -name "memvid-cli" -not -path "*/node_modules/*" | head -n 1)
    fi

    if [[ -z "$CLI_DIR" ]] || [[ ! -f "$CLI_DIR/package.json" ]]; then
        print_error "Could not locate memvid-cli package in $MEMVID_SRC_DIR"
        exit 1
    fi

    # Resolve the bin entry from package.json (expected to be a file path)
    MEMVID_BIN=""
    for candidate in "$CLI_DIR/bin/memvid" "$CLI_DIR/bin/memvid.js" "$CLI_DIR/lib/cli.js" "$CLI_DIR/dist/cli.js"; do
        if [[ -f "$candidate" ]]; then
            MEMVID_BIN="$candidate"
            break
        fi
    done

    if [[ -z "$MEMVID_BIN" ]]; then
        print_error "Could not locate built memvid entry point under $CLI_DIR"
        print_info "Expected one of: bin/memvid, bin/memvid.js, lib/cli.js, dist/cli.js"
        exit 1
    fi

    chmod +x "$MEMVID_BIN" 2>/dev/null || true

    # Install a symlink into a directory on PATH
    TARGET_BIN_DIR="${MEMVID_BIN_DIR:-/usr/local/bin}"
    TARGET_LINK="$TARGET_BIN_DIR/memvid"

    print_info "Linking $MEMVID_BIN -> $TARGET_LINK..."
    if [[ -w "$TARGET_BIN_DIR" ]]; then
        ln -sf "$MEMVID_BIN" "$TARGET_LINK"
    else
        sudo ln -sf "$MEMVID_BIN" "$TARGET_LINK"
    fi

    if command_exists memvid; then
        print_success "memvid installed successfully from source"
    else
        print_error "memvid installation failed (binary not on PATH)"
        print_info "Ensure $TARGET_BIN_DIR is in your PATH, or set MEMVID_BIN_DIR to a directory that is"
        exit 1
    fi
}

# Verify installation
verify() {
    print_info "Verifying installation..."
    
    if command_exists memvid; then
        # Get version - output format is "memvid 2.0.131"
        MEMVID_VERSION=$(memvid --version 2>&1 | awk '{print $2}')

        print_success "memvid is installed and accessible"
        print_info "Version: $MEMVID_VERSION"
        echo ""
        print_success "Installation complete! You can now use 'memvid' command."
    else
        print_error "memvid verification failed"
        print_info "The installation may have completed, but 'memvid' command is not in PATH"
        print_info "Ensure the symlink target directory (default /usr/local/bin) is in your PATH"
        exit 1
    fi
}

# Main execution
main() {
    echo "Memvid Installer"
    echo "Checking system requirements…"
    echo ""
    
    detect_os
    echo ""
    
    # Check Xcode CLI Tools on macOS
    if [[ "$OS" == "macos" ]]; then
        check_xcode_cli_tools
        echo ""
    fi
    
    # Install system dependencies on Linux
    if [[ "$OS" == "linux" ]]; then
        install_system_deps
        echo ""
    fi
    
    install_missing
    echo ""
    
    verify
}

# Run main function
main

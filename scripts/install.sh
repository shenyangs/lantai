#!/usr/bin/env bash
# Lantai installer for macOS and Linux.
set -euo pipefail

if [ -t 1 ]; then
  GREEN="\033[0;32m"
  YELLOW="\033[0;33m"
  RED="\033[0;31m"
  RESET="\033[0m"
else
  GREEN="" YELLOW="" RED="" RESET=""
fi

info() { printf "${GREEN}[lantai]${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}[lantai]${RESET} %s\n" "$*"; }
die() { printf "${RED}[lantai] ERROR:${RESET} %s\n" "$*" >&2; exit 1; }

LANTAI_HOME="${LANTAI_HOME:-$HOME/.lantai}"
LANTAI_VENV="$LANTAI_HOME/venv"
LANTAI_BIN="$LANTAI_HOME/bin"
PYTHON_VERSION="${LANTAI_PYTHON_VERSION:-3.12}"
VERSION=""
FROM_SOURCE=0
SOURCE_DIR=""
EXTRAS=""

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Options:
  --version <ver>       Install a specific PyPI version.
  --from-source [dir]   Install from a local source tree, or from LANTAI_REPO.
  --extras <extras>     Optional extras, for example: local
  -h, --help            Show this help text.

Environment:
  LANTAI_HOME           Install directory. Default: ~/.lantai
  LANTAI_REPO           Git URL used with --from-source when no dir is given.
  LANTAI_PYTHON_VERSION Python version used by uv. Default: 3.12
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version)
      VERSION="${2:-}"
      [ -n "$VERSION" ] || die "--version requires a value"
      shift 2
      ;;
    --from-source)
      FROM_SOURCE=1
      if [ $# -ge 2 ] && [ "${2#--}" = "$2" ]; then
        SOURCE_DIR="$(cd "$2" && pwd)" || die "Directory not found: $2"
        shift 2
      else
        shift
      fi
      ;;
    --extras)
      EXTRAS="${2:-}"
      [ -n "$EXTRAS" ] || die "--extras requires a value"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) die "Unsupported OS. This installer supports Linux and macOS." ;;
esac

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    info "uv found: $(command -v uv)"
    return
  fi

  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      export PATH="$(dirname "$candidate"):$PATH"
      info "uv found: $candidate"
      return
    fi
  done

  info "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv install failed"
}

prepare_console() {
  local repo_dir="$1"
  local console_src="$repo_dir/console/dist"
  local console_dest="$repo_dir/src/lantai/console"

  [ -f "$console_dest/index.html" ] && return
  [ -f "$repo_dir/console/package.json" ] || return
  command -v npm >/dev/null 2>&1 || {
    warn "npm not found; source install will continue without rebuilding console assets"
    return
  }

  info "Building console frontend"
  (cd "$repo_dir/console" && npm ci && npm run build)
  if [ -f "$console_src/index.html" ]; then
    rm -rf "$console_dest"
    mkdir -p "$console_dest"
    cp -R "$console_src/." "$console_dest/"
  fi
}

install_target() {
  local extras_suffix=""
  [ -n "$EXTRAS" ] && extras_suffix="[$EXTRAS]"

  if [ "$FROM_SOURCE" -eq 1 ]; then
    if [ -n "$SOURCE_DIR" ]; then
      prepare_console "$SOURCE_DIR"
      echo "${SOURCE_DIR}${extras_suffix}"
      return
    fi

    [ -n "${LANTAI_REPO:-}" ] || die "Set LANTAI_REPO or pass --from-source <dir>"
    command -v git >/dev/null 2>&1 || die "git is required for source install"
    local clone_dir
    clone_dir="$(mktemp -d)"
    git clone --depth 1 "$LANTAI_REPO" "$clone_dir"
    prepare_console "$clone_dir"
    echo "${clone_dir}${extras_suffix}"
    return
  fi

  if [ -n "$VERSION" ]; then
    echo "lantai==$VERSION${extras_suffix}"
  else
    echo "lantai${extras_suffix}"
  fi
}

info "Installing Lantai into $LANTAI_HOME"
ensure_uv
mkdir -p "$LANTAI_HOME" "$LANTAI_BIN"
uv venv "$LANTAI_VENV" --python "$PYTHON_VERSION" --quiet

TARGET="$(install_target)"
info "Installing $TARGET"
uv pip install "$TARGET" --python "$LANTAI_VENV/bin/python" --prerelease=allow --refresh-package lantai

[ -x "$LANTAI_VENV/bin/lantai" ] || die "lantai CLI not found after install"

cat > "$LANTAI_BIN/lantai" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
LANTAI_HOME="${LANTAI_HOME:-$HOME/.lantai}"
exec "$LANTAI_HOME/venv/bin/lantai" "$@"
WRAPPER
chmod +x "$LANTAI_BIN/lantai"

PATH_LINE='export PATH="$HOME/.lantai/bin:$PATH"'
for profile in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
  if [ -f "$profile" ] && grep -qF ".lantai/bin" "$profile"; then
    continue
  fi
  if [ -f "$profile" ] || [ "$(basename "$profile")" = ".zshrc" ]; then
    printf '\n# Lantai\n%s\n' "$PATH_LINE" >> "$profile"
    info "Updated $profile"
    break
  fi
done

info "Installed successfully"
echo
echo "Run:"
echo "  lantai init"
echo "  lantai app"

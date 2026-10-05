#!/usr/bin/env bash
# Build a self-contained Linux server zip that can be installed without the git tree.
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
arch=${1:?usage: package-server-zip.sh <amd64|arm64> [version]}
version=${2:-0.1.0}

case "$arch" in
  amd64|arm64) ;;
  *) printf 'unsupported arch: %s\n' "$arch" >&2; exit 2 ;;
esac

binary="$root_dir/build/server/$arch/lovemilk-class-broadcaster-server"
unit_src="$root_dir/deploy/systemd/lovemilk-class-broadcaster-server.service"
if [[ ! -f "$binary" ]]; then
  printf 'missing server binary: %s\nRun: make build-server or make server\n' "$binary" >&2
  exit 2
fi
if [[ ! -f "$unit_src" ]]; then
  printf 'missing unit file: %s\n' "$unit_src" >&2
  exit 2
fi

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
pkg_name="lovemilk-class-broadcaster-server-${version}-linux-${arch}"
pkg_dir="$stage/$pkg_name"
mkdir -p "$pkg_dir"

install -m 755 "$binary" "$pkg_dir/lovemilk-class-broadcaster-server"
install -m 644 "$unit_src" "$pkg_dir/lovemilk-class-broadcaster-server.service"

cat > "$pkg_dir/install-user-service.sh" <<'EOF'
#!/usr/bin/env bash
# Install this zip's server binary as a user-level systemd service.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
service_name=lovemilk-class-broadcaster-server.service
source_binary="$here/lovemilk-class-broadcaster-server"
unit_file="$here/$service_name"
app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/lovemilk-class-broadcaster"
bin_dir="$app_dir/bin"
data_dir="$app_dir/data"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ ! -f "$source_binary" ]]; then
  printf 'server binary not found next to this script: %s\n' "$source_binary" >&2
  exit 2
fi
if [[ ! -f "$unit_file" ]]; then
  printf 'unit file not found next to this script: %s\n' "$unit_file" >&2
  exit 2
fi
if ! command -v systemctl >/dev/null 2>&1; then
  printf 'systemctl is required for a user service\n' >&2
  exit 2
fi

install -d -m 700 "$bin_dir" "$data_dir" "$unit_dir"
install -m 755 "$source_binary" "$bin_dir/lovemilk-class-broadcaster-server"
install -m 644 "$unit_file" "$unit_dir/$service_name"

systemctl --user daemon-reload
systemctl --user enable --now "$service_name"
printf 'Enabled %s for user %s\n' "$service_name" "$USER"
printf 'Binary: %s\nData:   %s\n' "$bin_dir/lovemilk-class-broadcaster-server" "$data_dir"
printf 'Logs:   journalctl --user -u %s -f\n' "$service_name"
EOF
chmod 755 "$pkg_dir/install-user-service.sh"

cat > "$pkg_dir/README.txt" <<EOF
lovemilk class broadcaster server (${version}, linux-${arch})

Contents:
  lovemilk-class-broadcaster-server   server ELF (frontend embedded)
  lovemilk-class-broadcaster-server.service
  install-user-service.sh             user-level systemd installer

Install (no root, no git tree required):
  unzip ${pkg_name}.zip
  cd ${pkg_name}
  ./install-user-service.sh

This installs:
  ~/.local/share/lovemilk-class-broadcaster/bin/lovemilk-class-broadcaster-server
  ~/.local/share/lovemilk-class-broadcaster/data/
  ~/.config/systemd/user/lovemilk-class-broadcaster-server.service

Then enables and starts the user service.

Optional (run without login session):
  loginctl enable-linger "\$USER"

Uninstall service (keeps data):
  systemctl --user disable --now lovemilk-class-broadcaster-server.service
  rm -f ~/.config/systemd/user/lovemilk-class-broadcaster-server.service
  systemctl --user daemon-reload
EOF

mkdir -p "$root_dir/bin"
out="$root_dir/bin/${pkg_name}.zip"
rm -f "$out"
(
  cd "$stage"
  zip -qr "$out" "$pkg_name"
)
printf 'wrote %s\n' "$out"

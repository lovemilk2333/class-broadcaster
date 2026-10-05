#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -gt 0 ]]; then
  source_binary=$1
else
  case "$(uname -m)" in
    x86_64|amd64) server_arch=amd64 ;;
    aarch64|arm64) server_arch=arm64 ;;
    *) printf 'unsupported host architecture: %s\n' "$(uname -m)" >&2; exit 2 ;;
  esac
  source_binary="$root_dir/build/server/$server_arch/lovemilk-class-broadcaster-server"
fi
service_name=lovemilk-class-broadcaster-server.service
app_dir="$HOME/.local/share/lovemilk-class-broadcaster"
bin_dir="$app_dir/bin"
data_dir="$app_dir/data"
unit_dir="$HOME/.config/systemd/user"

if [[ ! -f "$source_binary" ]]; then
  printf 'server binary not found: %s\nBuild the matching server target or pass a binary path.\n' "$source_binary" >&2
  exit 2
fi
if ! command -v systemctl >/dev/null 2>&1; then
  printf 'systemctl is required for a user service\n' >&2
  exit 2
fi

install -d -m 700 "$bin_dir" "$data_dir" "$unit_dir"
install -m 755 "$source_binary" "$bin_dir/lovemilk-class-broadcaster-server"
install -m 644 "$root_dir/deploy/systemd/$service_name" "$unit_dir/$service_name"

systemctl --user daemon-reload
systemctl --user enable --now "$service_name"
printf 'Enabled %s for user %s\n' "$service_name" "$USER"

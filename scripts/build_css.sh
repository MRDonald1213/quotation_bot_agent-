#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tool_dir="$repo_root/.tools"
tailwind="$tool_dir/tailwindcss"

if [[ ! -x "$tailwind" ]]; then
  mkdir -p "$tool_dir"
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64) asset="tailwindcss-macos-arm64" ;;
    Darwin:x86_64) asset="tailwindcss-macos-x64" ;;
    Linux:x86_64) asset="tailwindcss-linux-x64" ;;
    Linux:aarch64|Linux:arm64) asset="tailwindcss-linux-arm64" ;;
    *) echo "Unsupported OS/architecture for the Tailwind standalone CLI: $(uname -s)/$(uname -m)" >&2; exit 1 ;;
  esac
  curl --fail --location --silent --show-error \
    "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/$asset" \
    --output "$tailwind"
  chmod +x "$tailwind"
fi

"$tailwind" \
  --input "$repo_root/tools/daisyui/input.css" \
  --output "$repo_root/src/freight_agent/web/static/tailwind.generated.css" \
  --minify

#!/usr/bin/env bash
# Build the Claude Desktop bundle (.mcpb) with the official MCPB CLI.
#
#   packaging/mcpb/build.sh [output-directory]      (default: dist-mcpb/)
#
# The bundle uses the MCPB "uv" server type (manifest 0.4): it carries the project sources,
# pyproject.toml and uv.lock, and the client starts the server with
# `uv run --directory <bundle> --frozen futureweb-intervals-mcp`, so the dependencies are
# installed exactly as locked. Only files tracked by git are packed (no .env, no caches).
#
# Requirements: git, Node.js with npm (for the MCPB CLI). The CLI is installed into a temporary
# directory with its version pinned and its dependency tree resolved as of a fixed date
# (npm --before), so a release published later cannot change what builds the bundle. Bump both
# deliberately: MCPB_CLI_VERSION / MCPB_CLI_BEFORE below or in the environment.
set -euo pipefail

MCPB_CLI_VERSION="${MCPB_CLI_VERSION:-2.1.2}"
MCPB_CLI_BEFORE="${MCPB_CLI_BEFORE:-2026-10-01}"

root="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
out="${1:-${root}/dist-mcpb}"
version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "${root}/pyproject.toml" | head -n 1)"
manifest_version="$(sed -n 's/^  "version": "\(.*\)",$/\1/p' "${root}/packaging/mcpb/manifest.json" | head -n 1)"
if [ -z "${version}" ] || [ "${version}" != "${manifest_version}" ]; then
  echo "error: packaging/mcpb/manifest.json version '${manifest_version}' does not match pyproject.toml '${version}'" >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
stage="${work}/bundle"
tools="${work}/tools"
mkdir -p "${stage}" "${tools}" "${out}"

# Bundle contents: the manifest, what `uv run` needs to install the project, and the license.
cp "${root}/packaging/mcpb/manifest.json" "${stage}/manifest.json"
git -C "${root}" ls-files -z -- pyproject.toml uv.lock README.md LICENSE src/intervals_mcp_server \
  | (cd "${root}" && xargs -0 cp --parents -t "${stage}")

echo "Installing @anthropic-ai/mcpb@${MCPB_CLI_VERSION} (dependencies as of ${MCPB_CLI_BEFORE})"
npm install --prefix "${tools}" --no-save --no-package-lock --no-audit --no-fund --ignore-scripts \
  --before "${MCPB_CLI_BEFORE}" "@anthropic-ai/mcpb@${MCPB_CLI_VERSION}" >/dev/null
mcpb="${tools}/node_modules/.bin/mcpb"
test "$("${mcpb}" --version)" = "${MCPB_CLI_VERSION}"

bundle="${out}/futureweb-intervals-mcp-${version}.mcpb"
rm -f "${bundle}"
"${mcpb}" validate "${stage}/manifest.json"
"${mcpb}" pack "${stage}" "${bundle}"
"${mcpb}" info "${bundle}"
echo "Built ${bundle}"

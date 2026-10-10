#!/usr/bin/env bash
# Install the MCP Registry publisher CLI (mcp-publisher) for linux/amd64 into a directory.
#
#   scripts/install-mcp-publisher.sh <directory>
#
# Version and SHA-256 are pinned here, in one place for CI (validation) and release.yml
# (publishing). Upgrade both together from the release's registry_<version>_checksums.txt:
# https://github.com/modelcontextprotocol/registry/releases
# A too old publisher fails the GitHub OIDC login with "invalid audience".
set -euo pipefail

MCP_PUBLISHER_VERSION="1.8.1"
MCP_PUBLISHER_SHA256="a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc"

dest="${1:?usage: $0 <directory>}"
mkdir -p "${dest}"
archive="$(mktemp)"
trap 'rm -f "${archive}"' EXIT
curl --proto '=https' --tlsv1.2 -sSfL -o "${archive}" \
  "https://github.com/modelcontextprotocol/registry/releases/download/v${MCP_PUBLISHER_VERSION}/mcp-publisher_linux_amd64.tar.gz"
echo "${MCP_PUBLISHER_SHA256}  ${archive}" | sha256sum --check --quiet -
tar -xzf "${archive}" -C "${dest}" mcp-publisher
"${dest}/mcp-publisher" --version

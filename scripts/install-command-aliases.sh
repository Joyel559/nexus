#!/usr/bin/env bash
set -euo pipefail

TARGET_SHELL_FILE="${HOME}/.bashrc"
MARKER_START="# >>> nexus command aliases >>>"
MARKER_END="# <<< nexus command aliases <<<"

if [[ ! -f "${TARGET_SHELL_FILE}" ]]; then
  touch "${TARGET_SHELL_FILE}"
fi

if grep -q "${MARKER_START}" "${TARGET_SHELL_FILE}"; then
  echo "Nexus aliases already installed in ${TARGET_SHELL_FILE}"
  exit 0
fi

cat >> "${TARGET_SHELL_FILE}" <<'EOF'
# >>> nexus command aliases >>>
# Nexus commands
alias server='fcc-server'
alias claudecode='fcc-claude'
# <<< nexus command aliases <<<
EOF

echo "Installed Nexus aliases in ${TARGET_SHELL_FILE}"
echo "Run: source ${TARGET_SHELL_FILE}"

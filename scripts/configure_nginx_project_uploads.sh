#!/usr/bin/env bash
# Run on the VPS as root before deploying clients that upload project files.
set -Eeuo pipefail

NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-enabled/api.conscout.com}"
UPLOAD_LIMIT="${UPLOAD_LIMIT:-256m}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi
if [[ ! -f "${NGINX_SITE}" ]]; then
  echo "Nginx site config not found: ${NGINX_SITE}" >&2
  exit 1
fi

RESOLVED_SITE="$(readlink -f "${NGINX_SITE}")"
BACKUP_SITE="${RESOLVED_SITE}.bak.$(date +%Y%m%d%H%M%S)"
cp --preserve=all "${RESOLVED_SITE}" "${BACKUP_SITE}"

python3 - "${RESOLVED_SITE}" "${UPLOAD_LIMIT}" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
limit = sys.argv[2]
source = path.read_text(encoding="utf-8")
marker = "# ConScout project upload limits"
directives = (
    f"    {marker}\n"
    f"    client_max_body_size {limit};\n"
    "    client_body_timeout 300s;\n"
    "    proxy_read_timeout 300s;\n"
    "    proxy_send_timeout 300s;"
)
pattern = re.compile(
    rf"\s*{re.escape(marker)}\s*\n"
    r"\s*client_max_body_size\s+[^;]+;\s*\n"
    r"\s*client_body_timeout\s+[^;]+;\s*\n"
    r"\s*proxy_read_timeout\s+[^;]+;\s*\n"
    r"\s*proxy_send_timeout\s+[^;]+;"
)
if pattern.search(source):
    updated = pattern.sub("\n" + directives, source, count=1)
else:
    needle = "    server_name api.conscout.com;"
    if needle not in source:
        raise SystemExit("api.conscout.com server block was not found")
    updated = source.replace(needle, needle + "\n\n" + directives, 1)
path.write_text(updated, encoding="utf-8")
PY

if ! nginx -t; then
  cp --preserve=all "${BACKUP_SITE}" "${RESOLVED_SITE}"
  nginx -t
  echo "Invalid nginx configuration; restored ${BACKUP_SITE}." >&2
  exit 1
fi

systemctl reload nginx
echo "Configured ${RESOLVED_SITE} with client_max_body_size ${UPLOAD_LIMIT}."
echo "Backup: ${BACKUP_SITE}"

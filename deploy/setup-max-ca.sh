#!/usr/bin/env bash
# Application-scoped trust for MAX. Does not modify the system CA store.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo bash deploy/setup-max-ca.sh'
  exit 1
fi
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
test -f "$APP_DIR/.env"
test -x "$APP_DIR/.venv/bin/python"
MAX_TMP="$(mktemp -d)"
trap 'rm -f -- "$MAX_TMP/root.crt" "$MAX_TMP/sub.crt" "$MAX_TMP/root.pem" "$MAX_TMP/sub.pem" "$MAX_TMP/bundle.pem"; rmdir -- "$MAX_TMP"' EXIT
curl --fail --silent --show-error --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 60 \
  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -o "$MAX_TMP/root.crt"
curl --fail --silent --show-error --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 60 \
  https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt -o "$MAX_TMP/sub.crt"
openssl x509 -in "$MAX_TMP/root.crt" -out "$MAX_TMP/root.pem"
openssl x509 -in "$MAX_TMP/sub.crt" -out "$MAX_TMP/sub.pem"
openssl x509 -in "$MAX_TMP/root.pem" -checkend 86400 -noout
openssl x509 -in "$MAX_TMP/sub.pem" -checkend 86400 -noout
openssl verify -CAfile "$MAX_TMP/root.pem" "$MAX_TMP/sub.pem"
cat "$MAX_TMP/root.pem" "$MAX_TMP/sub.pem" > "$MAX_TMP/bundle.pem"
# A 401 without a token is expected. The purpose here is verified TLS, not authorization.
curl --silent --show-error --cacert "$MAX_TMP/bundle.pem" --connect-timeout 10 --max-time 20 \
  --output /dev/null --write-out 'MAX TLS OK; HTTP %{http_code}\n' https://platform-api2.max.ru/me
install -m 0644 "$MAX_TMP/bundle.pem" "$APP_DIR/data/max-ca.pem"
"$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
import sys
from pathlib import Path
root=Path(sys.argv[1])
env=root/'.env'
lines=env.read_text(encoding='utf-8').splitlines()
lines=[s for s in lines if not s.strip().startswith(('MAX_CA_BUNDLE=', 'export MAX_CA_BUNDLE='))]
lines.append('MAX_CA_BUNDLE='+str(root/'data/max-ca.pem'))
env.write_text('\n'.join(lines)+'\n',encoding='utf-8')
PY
systemctl restart sushi-web
echo 'MAX_CA_BUNDLE configured. In admin: check MAX token, then register webhook.'

#!/usr/bin/env bash
# Application-scoped bank trust. HTTPS verification stays enabled.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo bash deploy/setup-tbank-ca.sh'
  exit 1
fi
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
test -f "$APP_DIR/.env"
test -x "$APP_DIR/.venv/bin/python"
BANK_TMP="$(mktemp -d)"
trap 'rm -f -- "$BANK_TMP/root.crt" "$BANK_TMP/sub.crt" "$BANK_TMP/root.pem" "$BANK_TMP/sub.pem" "$BANK_TMP/bundle.pem"; rmdir -- "$BANK_TMP"' EXIT
curl --fail --silent --show-error --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 60 \
  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -o "$BANK_TMP/root.crt"
curl --fail --silent --show-error --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 60 \
  https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt -o "$BANK_TMP/sub.crt"
openssl x509 -in "$BANK_TMP/root.crt" -out "$BANK_TMP/root.pem"
openssl x509 -in "$BANK_TMP/sub.crt" -out "$BANK_TMP/sub.pem"
openssl x509 -in "$BANK_TMP/root.pem" -checkend 86400 -noout
openssl x509 -in "$BANK_TMP/sub.pem" -checkend 86400 -noout
openssl verify -CAfile "$BANK_TMP/root.pem" "$BANK_TMP/sub.pem"
cat "$BANK_TMP/root.pem" "$BANK_TMP/sub.pem" > "$BANK_TMP/bundle.pem"
chmod 755 "$BANK_TMP"
chmod 644 "$BANK_TMP/bundle.pem"
# Validate using the service's Python/user, default roots plus the candidate bundle.
# No token or payment request is sent. Any HTTP response proves TLS worked.
sudo -u sushi "$APP_DIR/.venv/bin/python" - "$BANK_TMP/bundle.pem" <<'PY'
import ssl
import sys
import httpx
context = ssl.create_default_context()
context.load_verify_locations(cafile=sys.argv[1])
try:
    response = httpx.get('https://securepay.tinkoff.ru/v2/', verify=context,
                         trust_env=False, follow_redirects=False, timeout=20)
except httpx.HTTPError as exc:
    raise SystemExit(f'T-Bank TLS check failed: {type(exc).__name__}: {exc}')
print(f'T-Bank TLS OK; HTTP {response.status_code}')
PY
install -m 0644 "$BANK_TMP/bundle.pem" "$APP_DIR/data/tbank-ca.pem"
"$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
import re
import sys
from pathlib import Path
root = Path(sys.argv[1])
env = root / '.env'
lines = env.read_text(encoding='utf-8').splitlines()
lines = [s for s in lines if not re.match(r'^\s*(?:export\s+)?TBANK_CA_BUNDLE\s*=', s)]
lines.append('TBANK_CA_BUNDLE=' + str(root / 'data/tbank-ca.pem'))
env.write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY
systemctl restart sushi-web
echo 'TBANK_CA_BUNDLE configured. Create a NEW demo checkout to test payment.'

#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Generate a self-signed TLS key/cert for local HTTPS.

Usage:
  $0 [--san] [--days N] [--key key.pem] [--cert cert.pem] [--cn CN]

Options:
  --san         Include SANs: localhost, 127.0.0.1, ::1
  --days N      Validity days (default: 365)
  --key PATH    Output private key path (default: key.pem)
  --cert PATH   Output certificate path (default: cert.pem)
  --cn CN       Common Name (default: localhost)
EOF
}

DAYS=365
KEY=key.pem
CERT=cert.pem
CN=localhost
SAN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --san) SAN=1; shift ;;
    --days) DAYS=${2:-365}; shift 2 ;;
    --key) KEY=${2:-key.pem}; shift 2 ;;
    --cert) CERT=${2:-cert.pem}; shift 2 ;;
    --cn) CN=${2:-localhost}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

command -v openssl >/dev/null 2>&1 || { echo "openssl not found" >&2; exit 1; }

if [[ "$SAN" -eq 1 ]]; then
  tmpcnf=$(mktemp)
  trap 'rm -f "$tmpcnf"' EXIT
  cat >"$tmpcnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no
[dn]
CN = ${CN}
[v3_req]
subjectAltName = @alt_names
[alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
IP.2 = ::1
EOF
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" -days "$DAYS" \
    -config "$tmpcnf"
else
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" -days "$DAYS" \
    -subj "/CN=${CN}"
fi

echo "Generated: $KEY, $CERT"

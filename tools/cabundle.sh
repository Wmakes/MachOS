#!/bin/sh
# Refresh the Mozilla CA bundle maintained by curl. Never install an empty file
# or replace the current bundle after a failed HTTPS download or parse.
set -eu
cd "$(dirname "$0")/.."
DEST=root/etc/ssl/certs
mkdir -p "$DEST"
TEMP=$(mktemp -d "$DEST/.refresh.XXXXXX")
trap 'rm -rf "$TEMP"' EXIT HUP INT TERM
curl --fail --location --proto '=https' --proto-redir '=https' \
    https://curl.se/ca/cacert.pem -o "$TEMP/bundle.pem"
openssl crl2pkcs7 -nocrl -certfile "$TEMP/bundle.pem" -out /dev/null
grep -q '^-----BEGIN CERTIFICATE-----$' "$TEMP/bundle.pem"
{
    echo '# Mozilla CA list, curated as PEM by curl.'
    echo '# Source: https://curl.se/ca/cacert.pem'
    echo "# Refreshed: $(date -u +%Y-%m-%d) UTC"
    sed -n '/^## Certificate data from Mozilla as of:/p' "$TEMP/bundle.pem"
} > "$TEMP/SOURCE"
chmod 0644 "$TEMP/bundle.pem" "$TEMP/SOURCE"
mv "$TEMP/bundle.pem" "$DEST/ca-certificates.crt"
mv "$TEMP/SOURCE" "$DEST/SOURCE"

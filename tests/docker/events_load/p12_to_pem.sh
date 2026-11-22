#!/bin/sh
# Turn every ACCOUNTS_DIR/<account>/client.p12 (with its password.txt, as `events_load prepare -mode mtls` writes
# them from POST /users/{id}/certificates) into cert.pem and key.pem, the files events_load reads. The key is written
# unencrypted with mode 0600: keep the directory private and delete it after the test.
#
#   sh p12_to_pem.sh ACCOUNTS_DIR
#   docker run --rm -v "$PWD/accounts:/accounts" --entrypoint p12_to_pem.sh events-load /accounts
set -eu
dir="${1:?usage: p12_to_pem.sh ACCOUNTS_DIR}"
umask 077
n=0
for p12 in "$dir"/*/client.p12; do
    [ -f "$p12" ] || continue
    account=$(dirname "$p12")
    openssl pkcs12 -in "$p12" -passin "file:$account/password.txt" -clcerts -nokeys | openssl x509 -out "$account/cert.pem"
    openssl pkcs12 -in "$p12" -passin "file:$account/password.txt" -nocerts -nodes | openssl pkey -out "$account/key.pem"
    n=$((n + 1))
done
echo "converted $n account(s) in $dir"

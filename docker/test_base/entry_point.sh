#!/bin/bash
set -eu

# setup certs
echo "setup certificates for testing..."
mkdir -p /ota-client/certs && 
    cp -av /ota-client/tests/keys/root.pem /ota-client/certs/1.root.pem && 
    cp -av /ota-client/tests/keys/interm.pem /ota-client/certs/1.interm.pem

# exec the input params
echo "execute command..."
exec "$@"
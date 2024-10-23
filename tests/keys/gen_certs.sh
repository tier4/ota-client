#!/bin/bash

# https://stackoverflow.com/questions/52500165/problem-verifying-a-self-created-openssl-root-intermediate-and-end-user-certifi

set -eux

CA_CHAIN_PREFIX=${1:-test}

# Root CA:
openssl ecparam -out root.key -name prime256v1 -genkey
openssl req -new -x509 \
    -days $((365 * 10 + 5)) \
    -key root.key \
    -out ${CA_CHAIN_PREFIX}.root.pem \
    -sha256 \
    -subj "/C=JP/ST=Tokyo/O=Tier4/CN=root.tier4.jp"

# Intermediate
openssl ecparam -out interm.key -name prime256v1 -genkey
openssl req -new \
    -key interm.key \
    -out interm.csr \
    -sha256 \
    -subj "/C=JP/ST=Tokyo/O=Tier4/CN=intermediate.tier4.jp"

CA_INTERM_EXT="
[ v3_intermediate_ca ]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:true, pathlen:0
"
openssl x509 -req \
    -days $((365 * 10 + 5)) \
    -in interm.csr \
    -CA ${CA_CHAIN_PREFIX}.root.pem \
    -CAkey root.key \
    -out ${CA_CHAIN_PREFIX}.interm.pem \
    -sha256 -CAcreateserial \
    -extfile <(echo "${CA_INTERM_EXT}") \
    -extensions v3_intermediate_ca

# Sign cert
SIGN_CERT_EXT="
[ sign_cert ]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
keyUsage = critical, digitalSignature
extendedKeyUsage = codeSigning
basicConstraints = critical, CA:FALSE
"

openssl ecparam -out sign.key -name prime256v1 -genkey
openssl req -new \
    -key sign.key \
    -out sign.csr \
    -sha256 \
    -subj "/C=JP/ST=Tokyo/O=Tier4/CN=sign.tier4.jp"

openssl x509 -req \
    -days 365 \
    -in sign.csr \
    -CA ${CA_CHAIN_PREFIX}.interm.pem \
    -CAkey interm.key \
    -out sign.pem \
    -sha256 -CAcreateserial \
    -extfile <(echo "${SIGN_CERT_EXT}") \
    -extensions sign_cert

rm -f root.key interm.key interm.csr sign.csr *.srl

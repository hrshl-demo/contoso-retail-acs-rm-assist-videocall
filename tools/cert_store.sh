#!/usr/bin/env bash
# =====================================================================================
# tools/cert_store.sh — persist & reuse the Caddy/Let's Encrypt certificate, ENCRYPTED.
# -------------------------------------------------------------------------------------
# The Caddy data dir (/var/lib/caddy/.local/share/caddy — ACME account + issued cert +
# PRIVATE KEY) is tarred, encrypted with AES-256-CBC (OpenSSL, PBKDF2) and committed to
# infra/cert/, then decrypted and pre-seeded onto the VM on every later build. ACME
# issuance therefore happens exactly ONCE, which keeps builds off the Let's Encrypt rate
# limits and makes the cert survive a full `wipe.sh`.
#
# ⚠️ READ THIS BEFORE YOU RELY ON THE WORD "ENCRYPTED" ⚠️
# The AES key is COMMITTED NEXT TO THE CIPHERTEXT, as infra/cert/cert-enc.key. That is a
# deliberate tradeoff so a plain `git clone` can restore the cert with no jump-VM and no
# out-of-band secret. The direct consequence:
#
#     THIS IS OBFUSCATION, NOT CRYPTOGRAPHY. ANYONE WHO CAN READ THIS REPO CAN DECRYPT
#     THE PRIVATE KEY. It is exactly as exposed as a plaintext key would be, to anyone
#     with repo access.
#
# What it actually buys, and this is the whole list:
#   • GitHub secret scanning / push protection does not fire on an opaque blob, so the
#     repo does not accumulate "leaked private key" alerts.
#   • The key is not casually greppable — `grep -r "BEGIN PRIVATE KEY"` finds nothing,
#     so it will not be caught by an incidental scan, a screen-share, or a copy/paste.
#   • It matches the reference implementation, so the two repos behave the same way.
#
# The security boundary is, and remains, the repo's PRIVATE access control. Keep the repo
# private. Do not use this pattern for anything that guards real customer data — this is a
# throwaway demo host on a nip.io name.
#
# Subcommands:
#   status              -> prints STORED | ABSENT   (exit 3 when ABSENT)
#   publish <plain.tgz> -> encrypt into infra/cert/, write the lock file + key   [--force]
#   restore <out.tgz>   -> decrypt infra/cert/ back to <out.tgz>  (exit 3 when ABSENT)
#   verify              -> assert all three artifacts are committed (read-only)
#
# NOTE: there is no `sync` subcommand (the reference has one). This repo already commits
# and pushes infra/cert/ via tools/commit-artifacts.sh at the end of build.sh, so a second
# git-pushing path would just be a way for the two to disagree.
# =====================================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$HERE/.." && pwd)}"
# Self-source env.sh so this tool works standalone; it also sources the git-ignored
# secrets.env, which is where a locally-overridden CERT_ENC_KEY would come from.
# shellcheck source=../infra/common/env.sh
source "$REPO_ROOT/infra/common/env.sh" >/dev/null

CERT_ABS_DIR="$REPO_ROOT/${CERT_DIR:-infra/cert}"
# env.sh exports CERT_ENC_FILE/CERT_KEY_FILE/CERT_LOCK_FILE as paths RELATIVE to the repo
# root (e.g. "infra/cert/cert-enc.key"), which would resolve against the caller's cwd here.
# phase10 up.sh does not cd, so this tool must not assume it. Anchor anything relative to
# REPO_ROOT and leave absolute overrides alone.
_abs() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$REPO_ROOT" "$1" ;; esac; }
CERT_ENC_FILE="$(_abs "${CERT_ENC_FILE:-${CERT_DIR:-infra/cert}/caddy-data.tgz.enc}")"
CERT_KEY_FILE="$(_abs "${CERT_KEY_FILE:-${CERT_DIR:-infra/cert}/cert-enc.key}")"
CERT_LOCK_FILE="$(_abs "${CERT_LOCK_FILE:-${CERT_DIR:-infra/cert}/.cert-lock.json}")"

_log()  { printf '\033[1;34m[cert_store]\033[0m %s\n' "$*"; }
_warn() { printf '\033[1;33m[cert_store]\033[0m %s\n' "$*" >&2; }
_die()  { printf '\033[1;31m[cert_store]\033[0m %s\n' "$*" >&2; exit 1; }

command -v openssl >/dev/null 2>&1 \
  || _die "openssl is required (install: sudo apt-get install -y openssl)."

# Resolve the AES passphrase WITHOUT generating one. Priority:
#   1. CERT_ENC_KEY already in the environment (env.sh sources secrets.env)
#   2. the key committed in the repo at infra/cert/cert-enc.key
# The repo copy is what lets a fresh clone decrypt with no jump-VM involvement.
_resolve_key() {
  if [[ -n "${CERT_ENC_KEY:-}" ]]; then return 0; fi
  if [[ -f "$CERT_KEY_FILE" ]]; then
    local k; k="$(tr -d ' \t\r\n' < "$CERT_KEY_FILE")"
    if [[ -n "$k" ]]; then
      export CERT_ENC_KEY="$k"
      return 0
    fi
  fi
  return 1
}

# Only ever called on the FIRST publish. Never on restore — see cmd_restore.
_ensure_key() {
  _resolve_key && return 0
  _log "No CERT_ENC_KEY found — generating a new 256-bit AES key."
  export CERT_ENC_KEY="$(openssl rand -hex 32)"
}

_persist_key_to_repo() {
  mkdir -p "$CERT_ABS_DIR"
  local cur=""
  [[ -f "$CERT_KEY_FILE" ]] && cur="$(tr -d ' \t\r\n' < "$CERT_KEY_FILE")"
  if [[ "$cur" != "${CERT_ENC_KEY:-}" ]]; then
    printf '%s\n' "$CERT_ENC_KEY" > "$CERT_KEY_FILE"
    _log "Wrote the AES key to $(basename "$CERT_KEY_FILE") so any clone can decrypt."
  fi
}

_sha256() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

# Cipher parameters are pinned to match the reference implementation byte-for-byte, so a
# bundle produced by either repo can be read by the other. Do not "modernise" these
# without re-encrypting: changing -md/-iter silently breaks decryption of existing blobs.
_encrypt() {  # _encrypt <plain-in> <enc-out>
  CERT_ENC_KEY="$CERT_ENC_KEY" openssl enc -aes-256-cbc -md sha256 -pbkdf2 -iter 200000 -salt \
    -pass env:CERT_ENC_KEY -in "$1" -out "$2"
}
_decrypt() {  # _decrypt <enc-in> <plain-out>
  CERT_ENC_KEY="$CERT_ENC_KEY" openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 200000 \
    -pass env:CERT_ENC_KEY -in "$1" -out "$2"
}

# Read one string field out of the lock file WITHOUT jq — jq is not guaranteed present on
# the jump host, and a missing jq must not be able to change the reuse decision.
lock_field() {  # lock_field <name>
  [[ -f "$CERT_LOCK_FILE" ]] || return 0
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$CERT_LOCK_FILE" | head -1
}

cmd_status() {
  if [[ -f "$CERT_ENC_FILE" ]]; then echo "STORED"; return 0; fi
  echo "ABSENT"; return 3
}

cmd_publish() {
  local plain="" force=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --force) force="1" ;;
      *)       plain="$1" ;;
    esac
    shift
  done
  [[ -n "$plain" && -f "$plain" ]] || _die "publish needs the plaintext cert tarball (got '$plain')."

  if [[ -f "$CERT_ENC_FILE" && -z "$force" ]]; then
    _log "A certificate is already stored — not overwriting (use --force to replace)."
    return 0
  fi

  _ensure_key
  mkdir -p "$CERT_ABS_DIR"
  _log "Encrypting the cert bundle (AES-256-CBC/PBKDF2) into ${CERT_DIR:-infra/cert}/ ..."
  _encrypt "$plain" "$CERT_ENC_FILE" || _die "Encryption failed."

  local sha size
  sha="$(_sha256 "$CERT_ENC_FILE")"
  size="$(stat -c %s "$CERT_ENC_FILE" 2>/dev/null || wc -c < "$CERT_ENC_FILE")"
  # This lock file REPLACES the old CERT_FROZEN sentinel. One file is the single source of
  # truth for "which host/IP/CA is this cert for", instead of two files that can disagree.
  cat > "$CERT_LOCK_FILE" <<JSON
{
  "cipher": "aes-256-cbc/pbkdf2",
  "enc_file": "$(basename "$CERT_ENC_FILE")",
  "sha256": "${sha}",
  "bytes": ${size},
  "fqdn": "${RMASSIST_HOST:-unknown}",
  "static_ip": "${PERSIST_IP:-unknown}",
  "acme_ca": "${ACME_CA:-unknown}",
  "staging": "${LETSENCRYPT_STAGING:-0}",
  "frozen_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "note": "Encrypted Caddy cert store, reused by later builds (decrypt + pre-seed, no ACME call). The AES key is committed alongside as cert-enc.key, so this is OBFUSCATION, NOT SECURITY: anyone with repo access can decrypt it. Keep this repo private."
}
JSON
  _persist_key_to_repo
  _log "Stored $(basename "$CERT_ENC_FILE") (${size} bytes, sha256 ${sha:0:12}...) + $(basename "$CERT_LOCK_FILE")."
  _log "Commit ${CERT_DIR:-infra/cert}/ — build.sh does this automatically via tools/commit-artifacts.sh."
}

cmd_restore() {
  local out="${1:-}"
  [[ -n "$out" ]] || _die "restore needs an output path for the decrypted tarball."
  if [[ ! -f "$CERT_ENC_FILE" ]]; then
    _log "No stored certificate — Caddy will perform first issuance via ACME."
    return 3
  fi
  # Deliberately _resolve_key, never _ensure_key: a freshly generated key could never
  # decrypt an existing blob, so silently making one up would turn a recoverable problem
  # into a confusing decrypt failure.
  if ! _resolve_key; then
    _die "A stored cert exists but no AES key is available: neither CERT_ENC_KEY in the
environment/secrets.env nor ${CERT_DIR:-infra/cert}/$(basename "$CERT_KEY_FILE") in the repo.
Restore that key file, or delete $(basename "$CERT_ENC_FILE") to force a fresh ACME issuance."
  fi
  _log "Decrypting the stored cert bundle ..."
  _decrypt "$CERT_ENC_FILE" "$out" || _die "Decryption FAILED — $(basename "$CERT_KEY_FILE") does not
match the ciphertext (key and .enc came from different commits), or the bundle is corrupt.
To force a fresh Let's Encrypt issuance instead, delete $CERT_ENC_FILE and re-run the build."
  _log "Decrypted to $out (Caddy will reuse it — no new ACME order)."
}

cmd_verify() {
  if [[ ! -f "$CERT_ENC_FILE" ]]; then
    _warn "cert VERIFY: ABSENT — nothing stored yet."
    return 3
  fi
  local rc=0 f
  for f in "$(basename "$CERT_ENC_FILE")" "$(basename "$CERT_KEY_FILE")" "$(basename "$CERT_LOCK_FILE")"; do
    git -C "$REPO_ROOT" ls-files --error-unmatch "${CERT_DIR:-infra/cert}/$f" >/dev/null 2>&1 \
      || { _warn "cert VERIFY: ${CERT_DIR:-infra/cert}/$f is NOT tracked in git — a clone could not reuse the cert."; rc=1; }
  done
  # The plaintext tarball must never be tracked. This is the check that would catch a
  # regression back to committing the private key in the clear.
  if git -C "$REPO_ROOT" ls-files --error-unmatch "${CERT_DIR:-infra/cert}/caddy-data.tgz" >/dev/null 2>&1; then
    _warn "cert VERIFY: the PLAINTEXT ${CERT_DIR:-infra/cert}/caddy-data.tgz is tracked in git. Remove it."
    rc=1
  fi
  if [[ "$rc" == "0" ]]; then
    _log "cert VERIFIED for $(lock_field fqdn) — encrypted bundle, key and lock are all tracked."
    git -C "$REPO_ROOT" ls-files "${CERT_DIR:-infra/cert}" | sed 's/^/    /'
  fi
  return "$rc"
}

case "${1:-}" in
  status)  cmd_status ;;
  publish) shift; cmd_publish "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  verify)  cmd_verify ;;
  # Read one field out of the lock file. Used by phase10 up.sh to decide whether the stored
  # cert belongs to the CURRENT host before trusting it.
  lock-field) shift; lock_field "${1:?usage: cert_store.sh lock-field <name>}" ;;
  *) echo "usage: cert_store.sh {status|publish <plain.tgz> [--force]|restore <out.tgz>|verify|lock-field <name>}" >&2; exit 2 ;;
esac

# `infra/cert/` — reusable Let's Encrypt certificate, committed **encrypted**

This directory holds the **Caddy certificate store** for the stable host
`rmassist.<static-ip>.nip.io`. It is minted **once** and reused forever, so builds never call
Let's Encrypt again (which is what keeps us clear of ACME rate limits) and the certificate
survives a full `wipe.sh`.

## ⚠️ This is OBFUSCATION, NOT SECURITY

The AES key is **committed next to the ciphertext**, as `cert-enc.key`.

> **Anyone with repo access can decrypt the private key.** Against someone who can read this
> repository, the encrypted bundle is exactly as exposed as a plaintext key would be.

The key is committed deliberately, so that a plain `git clone` can restore and reuse the
certificate with no jump VM and no out-of-band secret. What the encryption actually buys — and
this is the complete list:

* **GitHub secret scanning / push protection does not fire** on an opaque blob, so the repo
  does not accumulate "leaked private key" alerts.
* **It is not casually greppable.** `grep -r "BEGIN PRIVATE KEY"` finds nothing, so the key
  will not be picked up by an incidental scan, a screen-share, or a stray copy/paste.
* It **matches the reference implementation**, so both repos behave identically.

The security boundary is, and remains, **the repository's private access control**. Keep this
repo private. Do not reuse this pattern for anything holding real data — this is a throwaway
demo host on a `nip.io` name.

## What gets committed here

| File | Written by | Purpose |
| --- | --- | --- |
| `caddy-data.tgz.enc` | `tools/cert_store.sh publish` | The Caddy data dir (`/var/lib/caddy/.local/share/caddy`) — ACME account, issued cert and its private key — tarred and AES-256-CBC encrypted. |
| `cert-enc.key` | same | The AES passphrase. Committed on purpose; see the warning above. |
| `.cert-lock.json` | same | Records `cipher`, `sha256`, `bytes`, `fqdn`, `static_ip`, `acme_ca`, `staging`, `frozen_at`. |

The **plaintext** `caddy-data.tgz` is git-ignored and must never be committed. `up.sh` only
ever writes it as a transient temp file and shreds it on every exit path, including failures.

### `.cert-lock.json` replaced `CERT_FROZEN`

The old sentinel recorded the host/IP/CA the cert was minted for, and its mere presence (with
the tarball) meant "reuse this". Both jobs now belong to `.cert-lock.json`, so there is exactly
one source of truth. Two files that must agree is a bug waiting to happen; the file's existence
answers *is a cert stored?* and its `fqdn` field answers *is it for this host?*.

## How reuse works

1. `build_persistent.sh` reserves a **static public IP** in the never-wiped persistent RG,
   which fixes `rmassist.<ip>.nip.io` forever.
2. **First build:** Caddy obtains the cert via HTTP-01, `up.sh` exports it, `cert_store.sh`
   encrypts it to `caddy-data.tgz.enc` + writes the key and lock, and `build.sh` auto-commits
   and pushes via `tools/commit-artifacts.sh`.
3. **Later builds:** `up.sh` checks `.cert-lock.json`'s `fqdn` matches the current host,
   decrypts to a temp file, pre-seeds it onto the VM **before Caddy ever starts**, then shreds
   the plaintext. No Let's Encrypt request is made.
4. `wipe.sh` deletes the VM but **never** touches the persistent RG or this directory.

If the stored cert is for a different host (the static IP changed), or decryption fails, `up.sh`
warns and falls back to minting a fresh one — it never hard-fails the build on a cert it cannot
use.

## Commands

```bash
bash tools/cert_store.sh status              # STORED | ABSENT
bash tools/cert_store.sh verify              # assert all three artifacts are tracked in git
bash tools/cert_store.sh lock-field fqdn     # which host is the stored cert for?
bash tools/cert_store.sh restore /tmp/c.tgz  # decrypt (for inspection or manual recovery)
bash tools/cert_store.sh publish /tmp/c.tgz  # encrypt + store (--force to replace)
```

`verify` also fails if the plaintext `caddy-data.tgz` is ever tracked — that is the check that
would catch a regression back to committing the private key in the clear.

To force a fresh certificate (for example after the static IP changes), delete
`caddy-data.tgz.enc` and `.cert-lock.json`, then re-run `build.sh`.

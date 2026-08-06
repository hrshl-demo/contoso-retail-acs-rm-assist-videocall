# `infra/cert/` — reusable Let's Encrypt certificate (committed on purpose)

This directory holds the **Caddy certificate store**, minted **once** for the stable host
`rmassist.<static-ip>.nip.io` and then **reused forever** so builds never call Let's Encrypt again
(which avoids ACME rate limits).

## What gets committed here

| File | Written by | Purpose |
| --- | --- | --- |
| `caddy-data.tgz` | `infra/phase10-vmhost/up.sh` (first build only) | The exported Caddy data dir (`/var/lib/caddy/.local/share/caddy`) containing the issued cert, its private key, and the ACME account. |
| `CERT_FROZEN` | same | Sentinel recording the host/IP/CA the cert was minted for. Its presence + `caddy-data.tgz` makes every later build **pre-seed** the cert (no ACME call). |

Both are empty until the **first** `bash build.sh` runs the VM phase (phase 10). On that first
build Caddy obtains the cert via HTTP-01, `up.sh` exports it here, and `build.sh` auto-commits +
pushes it. From then on the cert is pre-seeded onto the VM and reused.

## How reuse works

1. `build_persistent.sh` reserves a **static public IP** in the never-wiped persistent RG, which
   fixes the hostname `rmassist.<ip>.nip.io` forever.
2. First build: Caddy mints the cert → exported here → committed.
3. Later builds: `up.sh` pre-seeds `caddy-data.tgz` onto the VM **before** starting Caddy, so
   Caddy serves the existing cert with **no Let's Encrypt request**.
4. `wipe.sh` deletes the VM but **never** touches the persistent RG or this committed cert, so the
   next build reuses it.

To force a fresh cert (e.g. after the static IP changes), delete `caddy-data.tgz` and `CERT_FROZEN`
and re-run `build.sh`.

> ⚠️ **Security note:** the cert's **private key** is committed here. This is an accepted tradeoff
> for a throwaway demo host on `nip.io`. Do not reuse this pattern for anything holding real data.

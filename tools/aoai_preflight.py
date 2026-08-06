#!/usr/bin/env python3
"""tools/aoai_preflight.py — keyless gpt-5.4 readiness probe (run ON the data-gen VM).

Purpose: after phase10 grants the VM's system-assigned managed identity the Cognitive
Services roles on the Foundry account, Azure RBAC needs time to propagate to the data
plane (typically seconds, sometimes several minutes). This probe makes ONE tiny chat
completion using the SAME auth path as the generation scripts, so the caller can retry
until access is live BEFORE starting the expensive dataset/SOP generation.

Auth path (identical to data/contosobank/enrich_contosobank.py):
  base_url = FOUNDRY_AOAI_ENDPOINT (+ /openai/v1 if missing)
  api_key  = get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")

Exit codes:
  0  -> gpt-5.4 answered; keyless access is live.
  1  -> not ready yet / error (message on stderr). The caller should sleep + retry.
"""
import os
import sys


def main() -> int:
    ep = os.environ.get("FOUNDRY_AOAI_ENDPOINT", "").rstrip("/")
    if not ep:
        print("FOUNDRY_AOAI_ENDPOINT is not set", file=sys.stderr)
        return 1
    if not ep.endswith("/openai/v1"):
        ep = ep + "/openai/v1"
    deployment = os.environ.get("FOUNDRY_CHAT_DEPLOYMENT", "gpt-5-4")
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import OpenAI
    except Exception as exc:  # deps missing on the VM
        print(f"import error ({exc})", file=sys.stderr)
        return 1
    try:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://ai.azure.com/.default"
        )
        client = OpenAI(base_url=ep, api_key=token_provider)
        resp = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": "ping"}],
            max_completion_tokens=1,
        )
        # A well-formed response object is proof the keyless call was authorized.
        _ = resp.choices[0]
        print(f"OK: keyless gpt-5.4 reachable (deployment={deployment})")
        return 0
    except Exception as exc:
        print(f"not ready: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

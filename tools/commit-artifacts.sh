#!/usr/bin/env bash
# tools/commit-artifacts.sh
#
# Auto-commit + push the generated, git-committable artifacts at the end of a build so the repo
# stays the single source of truth. Runs LOCALLY (the machine that ran build.sh is the one that
# is GitHub-authenticated) — no GitHub secret ever lives on the VM.
#
# Staged paths (only these — never a blanket 'git add -A'):
#   data/contosobank/            deterministic dataset + gpt-5.4 enrichment (contosobank_dataset.json)
#   data/contosobank/BASELINE_FROZEN   the freeze sentinel (makes future builds skip generation)
#   docs/sop/contosobank_*.md    gpt-5.4-authored SOP corpus (the curated 01..20 SOPs are untouched)
#   infra/cert/                  the ENCRYPTED Let's Encrypt cert store (minted once, reused):
#                                caddy-data.tgz.enc + cert-enc.key + .cert-lock.json.
#                                The plaintext tarball is git-ignored and never staged.
#
# Safe + idempotent: if nothing changed it is a clean no-op. Disable pushing with GIT_PUSH=0
# (commit only). Override the branch with GIT_PUSH_BRANCH.
#
# Usage:  bash tools/commit-artifacts.sh ["optional commit subject"]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

_c(){ printf '\033[%sm' "$1"; }
log(){ printf '%s[commit-artifacts]%s %s\n' "$(_c '1;34')" "$(_c 0)" "$*"; }
ok(){  printf '%s[commit-artifacts]%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn(){ printf '%s[commit-artifacts]%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { warn "Not a git work tree — skipping."; exit 0; }

SUBJECT="${1:-chore(data): refresh Contoso Bank dataset, SOPs & cert baseline}"
PATHS=( "data/contosobank" "docs/sop" "infra/cert" )

STAGED_ANY=0
for p in "${PATHS[@]}"; do
  [[ -e "$REPO_ROOT/$p" ]] || continue
  git add -- "$p" 2>/dev/null && STAGED_ANY=1
done

# Nothing staged at all, or staged content identical to HEAD -> clean no-op.
if [[ "$STAGED_ANY" == "0" ]] || git diff --cached --quiet; then
  ok "No artifact changes to commit — repo already up to date."
  exit 0
fi

log "Staged artifact changes:"
git --no-pager diff --cached --stat -- "${PATHS[@]}" || true

COMMIT_MSG="$(printf '%s\n\nRegenerated/frozen build artifacts (data + SOPs + cert).\n\nCo-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>\n' "$SUBJECT")"
if git commit -m "$COMMIT_MSG" >/dev/null 2>&1; then
  ok "Committed: $SUBJECT"
else
  warn "git commit failed (nothing to commit, or a hook rejected it)."; exit 0
fi

if [[ "${GIT_PUSH:-1}" != "1" ]]; then
  warn "GIT_PUSH=0 — committed locally but NOT pushing. Push manually with: git push"
  exit 0
fi

BRANCH="${GIT_PUSH_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null)}"
REMOTE="${GIT_PUSH_REMOTE:-origin}"
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  warn "No git remote '$REMOTE' — commit kept locally. Add a remote and 'git push' to publish."
  exit 0
fi
log "Pushing to $REMOTE/$BRANCH ..."
if git push "$REMOTE" "HEAD:$BRANCH" >/dev/null 2>&1; then
  ok "Pushed artifacts to $REMOTE/$BRANCH."
else
  warn "git push failed (auth, protected branch, or non-fast-forward). The commit is safe locally; push manually."
  exit 0
fi

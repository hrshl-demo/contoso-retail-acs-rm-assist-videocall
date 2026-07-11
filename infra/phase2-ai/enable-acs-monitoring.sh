#!/usr/bin/env bash
# infra/phase2-ai/enable-acs-monitoring.sh
#
# Enables Azure Monitor diagnostic logging on the ACS resource, routed to the
# project Log Analytics workspace (NAME_LAW). This is what lets you see WHY an
# outbound call leg did not ring — ACS logs the carrier-side disposition
# (EndReason / ResultCategories) to the ACSCallSummary / ACSCallDiagnostics
# tables, which never reaches the app webhook.
#
# Idempotent and non-destructive. Safe to re-run. Called automatically at the end
# of phase2-ai/up.sh, and runnable standalone.
set -euo pipefail
PHASE="phase2-ai"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"
ensure_az_login
ensure_rg

ACS_ID="$(az communication list -g "$AZ_RG" --query "[?name=='${NAME_ACS}'].id | [0]" -o tsv 2>/dev/null || true)"
[[ -n "$ACS_ID" ]] || { echo "ACS resource $NAME_ACS not found"; exit 1; }

LAW_ID="$(az monitor log-analytics workspace show -g "$AZ_RG" -n "$NAME_LAW" --query id -o tsv 2>/dev/null || true)"
[[ -n "$LAW_ID" ]] || { echo "Log Analytics workspace $NAME_LAW not found (run phase1 first)"; exit 1; }

log "Enabling ACS diagnostic settings -> $NAME_LAW"
# Categories vary by ACS; CallSummary + CallDiagnostics are the call ones. We
# enable the broad set and let Azure ignore any not applicable.
az monitor diagnostic-settings create \
  --name "acs-to-law" \
  --resource "$ACS_ID" \
  --workspace "$LAW_ID" \
  --logs '[
    {"category":"CallSummary","enabled":true},
    {"category":"CallDiagnostics","enabled":true},
    {"category":"CallSummaryUpdates","enabled":true},
    {"category":"CallDiagnosticsUpdates","enabled":true},
    {"category":"CallAutomationMediaSummary","enabled":true},
    {"category":"CallRecordingSummary","enabled":true}
  ]' \
  --metrics '[{"category":"AllMetrics","enabled":true}]' \
  -o none 2>/dev/null \
  || az monitor diagnostic-settings create \
       --name "acs-to-law" --resource "$ACS_ID" --workspace "$LAW_ID" \
       --logs '[{"categoryGroup":"allLogs","enabled":true}]' \
       --metrics '[{"category":"AllMetrics","enabled":true}]' -o none

ok "ACS diagnostic logging enabled (table population begins within ~5-10 min)."
cat <<EOF

To review why a call leg did/didn't connect (after ~10 min of call activity):

  WS="\$(az monitor log-analytics workspace show -g "$AZ_RG" -n "$NAME_LAW" --query customerId -o tsv)"
  az monitor log-analytics query -w "\$WS" --analytics-query \\
    'ACSCallSummary | where TimeGenerated > ago(1h) | project TimeGenerated, CorrelationId, EndpointType, CallType, CallDurationInSeconds, EndReason, ResultCategories | order by TimeGenerated desc | take 20' -o table

EndReason is the carrier/ACS disposition for the leg. For a US toll-free number
dialing +91 India, watch for reasons indicating the destination carrier rejected
or did not deliver the call.
EOF

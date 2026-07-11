// graph.js — Provision a REAL Teams meeting on the RM's calendar via Microsoft Graph
// (application / client-credentials flow). This is the production path: it creates an
// actual calendar event on the RM's Outlook/Teams calendar with an online Teams meeting
// attached and returns the real joinUrl that BOTH the RM and the customer (via ACS
// interop) join. No user is ever in the loop — it is fully automated.
//
// Configure with app-only credentials that have the Calendars.ReadWrite application
// permission (admin-consented) and set RM_USER_ID to the RM's mailbox (UPN or objectId):
//   GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, RM_USER_ID
// Optional: MEETING_TIMEZONE (IANA/Windows tz, default "India Standard Time"),
//           MEETING_DURATION_MINUTES (default 30).
//
// When these are not set the caller falls back to a standing meeting link or a
// synthetic demo link, so the demo still completes offline.

const TENANT = process.env.GRAPH_TENANT_ID || process.env.AZURE_TENANT_ID || null;
const CLIENT_ID = process.env.GRAPH_CLIENT_ID || null;
const CLIENT_SECRET = process.env.GRAPH_CLIENT_SECRET || null;
const RM_USER_ID = process.env.RM_USER_ID || process.env.RM_USER_UPN || null;
const TZ = process.env.MEETING_TIMEZONE || 'India Standard Time';
const DURATION_MIN = Math.max(15, Math.min(180, Number(process.env.MEETING_DURATION_MINUTES || 30)));

export function graphConfigured() {
  return !!(TENANT && CLIENT_ID && CLIENT_SECRET && RM_USER_ID);
}

let _tok = { value: null, exp: 0 };
async function getToken() {
  const now = Date.now();
  if (_tok.value && now < _tok.exp - 60000) return _tok.value;
  const body = new URLSearchParams({
    client_id: CLIENT_ID,
    client_secret: CLIENT_SECRET,
    scope: 'https://graph.microsoft.com/.default',
    grant_type: 'client_credentials',
  });
  const r = await fetch(`https://login.microsoftonline.com/${encodeURIComponent(TENANT)}/oauth2/v2.0/token`, {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body,
  });
  if (!r.ok) throw new Error('graph token ' + r.status + ' ' + (await r.text().catch(() => '')));
  const j = await r.json();
  _tok = { value: j.access_token, exp: now + Number(j.expires_in || 3600) * 1000 };
  return _tok.value;
}

// Graph wants a local dateTime string (no trailing Z) paired with an explicit timeZone.
function graphDate(d) { return new Date(d).toISOString().replace(/\.\d+Z$/, '').replace(/Z$/, ''); }

// Create a calendar event on the RM's calendar with an online Teams meeting attached.
// Returns { joinUrl, eventId, webLink }.
export async function createRmCalendarMeeting({ subject, startIso, endIso, customerName, customerEmail, note } = {}) {
  const token = await getToken();
  const start = startIso ? new Date(startIso) : new Date();
  const end = endIso ? new Date(endIso) : new Date(start.getTime() + DURATION_MIN * 60000);
  const event = {
    subject: subject || `Video banking call · ${customerName || 'Customer'}`,
    start: { dateTime: graphDate(start), timeZone: 'UTC' },
    end: { dateTime: graphDate(end), timeZone: 'UTC' },
    isOnlineMeeting: true,
    onlineMeetingProvider: 'teamsForBusiness',
    body: {
      contentType: 'HTML',
      content: `Customer-initiated video banking call${customerName ? ' with <b>' + customerName + '</b>' : ''}.` +
        (note ? '<br>' + note : ''),
    },
  };
  if (customerEmail) {
    event.attendees = [{ emailAddress: { address: customerEmail, name: customerName || customerEmail }, type: 'required' }];
  }
  const r = await fetch(`https://graph.microsoft.com/v1.0/users/${encodeURIComponent(RM_USER_ID)}/events`, {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json', Prefer: `outlook.timezone="${TZ}"` },
    body: JSON.stringify(event),
  });
  if (!r.ok) throw new Error('graph event ' + r.status + ' ' + (await r.text().catch(() => '')));
  const j = await r.json();
  const joinUrl = j.onlineMeeting && j.onlineMeeting.joinUrl;
  if (!joinUrl) throw new Error('graph event created but no onlineMeeting.joinUrl returned');
  return { joinUrl, eventId: j.id, webLink: j.webLink };
}

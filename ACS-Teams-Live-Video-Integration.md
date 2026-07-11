# Azure Communication Services ↔ Microsoft Teams — Live Video Call Integration

A **self-contained, generic walkthrough** for joining a **Microsoft Teams meeting** from a web page
using the **Azure Communication Services (ACS) Calling SDK**, with **two-way live video** — using
**only a browser, plain HTML/JavaScript, and the Azure CLI**. There is **no Node.js, no npm, no
webpack, and no server to run**.

> This document is **standalone** and has **nothing to do with the RM-Assist demo** — no nudges, no
> customer data, no Foundry/AI. It is purely the *ACS → Teams live video call* plumbing so you can
> show a customer how the interop works and hand them copy-paste code.

---

## ⚠️ Read this first: HTTP vs HTTPS (why we do NOT use `http://<vm-ip>:8080`)

Browsers only grant a page access to the **camera and microphone** when the page runs in a
**secure context**. A secure context is:

- **`https://…`** (any real HTTPS URL), **or**
- **`http://localhost`** / **`http://127.0.0.1`** — localhost is treated as secure, **but only if the
  browser is running on the very same machine** that serves the page.

So your instinct is correct:

| Where you open the page | Secure context? | Camera/mic + Teams video work? |
|-------------------------|-----------------|--------------------------------|
| `http://localhost:8080` **on a machine that has the browser + a webcam** | ✅ yes | ✅ yes |
| `http://<vm-public-ip>:8080` from your laptop (remote, plain HTTP) | ❌ **no** | ❌ **camera blocked** |
| `https://…` (this guide) | ✅ yes | ✅ yes |

An Azure VM typically has **no desktop and no webcam**, and reaching it over `http://<ip>:8080` is
**not** a secure context — so the camera is blocked and the Teams call cannot send video. That is
exactly the problem you anticipated.

**This guide fixes it by hosting the page on real HTTPS** using an **Azure Storage static website**
(`https://<name>.z<nn>.web.core.windows.net`). It is free, needs **no container, no build, and no
npm**, and you deploy it with a single `az storage blob upload`. You then open that HTTPS URL from
**any** machine that has a webcam (your laptop is fine) and the camera works.

---

**What you will build**

```
                 az (Cloud Shell or VM)                         browser on a webcam machine
   ┌───────────────────────────────────────┐        ┌──────────────────────────────────────────┐
   │  az communication create   ──►  ACS    │        │  https://<acct>.web.core.windows.net       │
   │  az storage ... static website ──► HTTPS│  ───►  │  index.html  (ACS Calling Web SDK via CDN) │
   │  az communication identity token issue  │        │   • you paste: token + Teams meeting link  │
   │        └► a short-lived VoIP token ─────┼──paste─►│   • callAgent.join({ meetingLink })        │
   └───────────────────────────────────────┘        └───────────────┬────────────────────────────┘
                                                                     │ joins as anonymous participant
                                                                     ▼
                                                     ┌──────────────────────────────┐
                                                     │  Microsoft Teams meeting     │
                                                     │  (host + Teams participants) │
                                                     └──────────────────────────────┘
```

- An **ACS resource** (created with `az`, deleted at the end so nothing lingers/bills).
- A **Storage static website** that serves one `index.html` over **HTTPS**.
- A **browser client** (ACS Calling Web SDK, loaded from a CDN — no build) that turns on your
  camera/mic and **joins a Teams meeting link**, rendering your local video and the remote Teams
  participants' video.
- A **VoIP token** minted with `az` and pasted into the page (so the page contains **no secrets**).

---

## 0. Prerequisites

| Requirement | Notes |
|-------------|-------|
| Azure subscription | With rights to create a resource group, an ACS resource, and a storage account. |
| Azure CLI (`az`) | Cloud Shell already has it; on a VM: `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash`. |
| A machine with a **webcam + microphone** and a modern browser (Edge/Chrome) | This is where you open the HTTPS page and join the call — your **laptop is perfect**. |
| A **Microsoft Teams meeting link** | Any meeting you can schedule in Teams/Outlook (`https://teams.microsoft.com/l/meetup-join/...`). |
| Teams tenant policy | The meeting must **allow anonymous/external participants to join** (see §2). |

> You do **not** need Node.js, npm, Docker, or a webcam on the machine that runs `az`. The `az` steps
> can run in **Cloud Shell**; the actual call runs in a **browser on any webcam machine**.

---

## 1. Provision ACS + an HTTPS static website (Azure CLI)

Run these in Cloud Shell or on your VM. Everything is variable-driven — **no hardcoded names**, and
the entire list of variables is right here.

```bash
# ─────────────────────── configurable variables (edit these) ───────────────────────
export RG="rg-acs-teams-demo"          # resource group (can be a new one, or your existing RG)
export LOCATION="southindia"            # Azure region for the RG + storage account
export ACS_NAME="acsteams$RANDOM"       # ACS resource name (globally unique)
export ACS_DATA_LOCATION="India"        # ACS data residency: India | United States | Europe | UK | Australia | Asia Pacific ...
export STORAGE_NAME="acsteams$RANDOM"   # storage account name: 3-24 lowercase letters/digits, globally unique
# ────────────────────────────────────────────────────────────────────────────────────

# make sure the CLI can manage ACS (extension auto-installs on first use)
az extension add --name communication --only-show-errors 2>/dev/null || true
az provider register --namespace Microsoft.Communication --wait
az provider register --namespace Microsoft.Storage --wait

# resource group
az group create --name "$RG" --location "$LOCATION" -o none

# the ACS resource (its own location is always "Global"; residency is --data-location)
az communication create \
  --name "$ACS_NAME" \
  --resource-group "$RG" \
  --location "Global" \
  --data-location "$ACS_DATA_LOCATION" \
  -o none

# connection string used only to MINT tokens (never goes into the web page)
export ACS_CONNECTION_STRING="$(az communication list-key \
  --name "$ACS_NAME" --resource-group "$RG" \
  --query primaryConnectionString -o tsv)"

# storage account that will host the page over HTTPS
az storage account create \
  --name "$STORAGE_NAME" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku Standard_LRS --kind StorageV2 --https-only true \
  -o none

# turn on static-website hosting (serves the $web container over HTTPS, anonymously)
az storage blob service-properties update \
  --account-name "$STORAGE_NAME" \
  --static-website --index-document index.html --404-document index.html \
  --only-show-errors -o none

# the public HTTPS URL your browser will open
export SITE_URL="$(az storage account show \
  --name "$STORAGE_NAME" --resource-group "$RG" \
  --query "primaryEndpoints.web" -o tsv)"

echo "ACS_NAME     = $ACS_NAME"
echo "STORAGE_NAME = $STORAGE_NAME"
echo "SITE_URL     = $SITE_URL"
```

Keep `ACS_CONNECTION_STRING` for §4 (token minting). Treat it as a **secret** — it never goes into
the HTML. The static-website endpoint is served anonymously over HTTPS and is **not** affected by the
account's "allow blob public access" flag, so it still works under lock-down policies.

---

## 2. Allow external/anonymous join on the Teams meeting (one-time tenant setting)

ACS users join a Teams meeting as **anonymous/external** participants. This works only if the Teams
tenant allows it. A Teams admin sets it once:

1. **Teams admin center → Meetings → Meeting policies →** (Global or the host's policy).
2. Enable **"Anonymous users can join a meeting."**
3. (Org-wide) **Teams admin center → Meetings → Meeting settings → "Anonymous users can join a
   meeting."**

Changes can take up to a few hours to propagate. As the meeting organiser you can also set
**Meeting options → "Who can bypass the lobby"** so the ACS guest is admitted (or admit them manually
from the lobby).

> No app registration, bot, or Graph permission is required for **meeting-join** interop — just the
> ACS resource above and a meeting link.

---

## 3. The web page — one file, `index.html`

Create a single file named **`index.html`** with the content below. It loads the ACS Calling Web SDK
directly from a **CDN as browser ESM** (jsDelivr `/+esm`, which resolves every transitive dependency),
so there is **nothing to install or build**. It has two inputs — the **Teams meeting link** and the
**ACS token** you mint in §4 — so the deployed file contains **no secrets**.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ACS ↔ Teams — Live Video</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; background:#0f172a; color:#e2e8f0; }
    h1 { font-size: 18px; }
    label { display:block; margin: 10px 0 4px; font-size: 13px; color:#94a3b8; }
    input, textarea, button { font-size: 14px; padding: 8px; border-radius: 6px; border: 1px solid #334155; }
    input, textarea { width: 640px; max-width: 92vw; background:#1e293b; color:#e2e8f0; }
    textarea { height: 64px; font-family: ui-monospace, monospace; }
    button { background:#2563eb; color:#fff; border:none; cursor:pointer; margin-right:8px; }
    button:disabled { background:#475569; cursor:not-allowed; }
    .row { margin: 10px 0; }
    .videos { display:flex; gap:16px; flex-wrap:wrap; margin-top:16px; }
    .tile { background:#000; border:1px solid #334155; border-radius:8px; overflow:hidden; }
    .tile video { width: 460px; height: 258px; object-fit: cover; display:block; }
    .tile h3 { margin:0; padding:6px 10px; font-size:12px; background:#1e293b; }
    #remoteVideos { display:flex; gap:12px; flex-wrap:wrap; }
    small, code { color:#94a3b8; }
  </style>
</head>
<body>
  <h1>Azure Communication Services → Microsoft Teams (live video)</h1>

  <div class="row">
    <label for="displayName">Your display name in the meeting</label>
    <input id="displayName" value="ACS Guest" />
  </div>
  <div class="row">
    <label for="meetingLink">Teams meeting link</label>
    <input id="meetingLink" placeholder="https://teams.microsoft.com/l/meetup-join/..." />
  </div>
  <div class="row">
    <label for="token">ACS access token (paste the value from `az communication identity token issue`)</label>
    <textarea id="token" placeholder="eyJhbGciOi..."></textarea>
  </div>

  <div class="row">
    <button id="joinBtn">Join Teams meeting (video)</button>
    <button id="hangupBtn">Hang up</button>
    <span>State: <b id="callState">idle</b></span>
  </div>

  <div class="videos">
    <div class="tile"><h3>You (local)</h3><div id="localVideo"></div></div>
    <div class="tile"><h3>Teams participants (remote)</h3><div id="remoteVideos"></div></div>
  </div>

  <script type="module">
    // ACS Calling Web SDK, loaded straight from a CDN as browser ESM — no npm, no build.
    // jsDelivr's /+esm resolves @azure/logger and @azure/communication-common automatically.
    import { CallClient, LocalVideoStream, VideoStreamRenderer }
      from "https://cdn.jsdelivr.net/npm/@azure/communication-calling@1.43.1/+esm";
    import { AzureCommunicationTokenCredential }
      from "https://cdn.jsdelivr.net/npm/@azure/communication-common@2.4.0/+esm";

    const $ = (id) => document.getElementById(id);
    const setState = (s) => { $("callState").textContent = s; };

    let callClient, callAgent, deviceManager, call, localRenderer;
    const remoteRenderers = new Map(); // RemoteVideoStream -> VideoStreamRenderer

    // Create the call agent once, from the pasted token. Scope "voip" is what the
    // Calling SDK needs to join a Teams meeting with audio/video.
    async function ensureAgent() {
      if (callAgent) return;
      const token = $("token").value.trim();
      if (!token) throw new Error("Paste an ACS access token first (see the az command in the guide).");
      callClient = new CallClient();
      const credential = new AzureCommunicationTokenCredential(token);
      callAgent = await callClient.createCallAgent(credential, {
        displayName: $("displayName").value || "ACS Guest",
      });
      deviceManager = await callClient.getDeviceManager();
      await deviceManager.askDevicePermission({ video: true, audio: true });
    }

    async function buildLocalVideoStream() {
      const cameras = await deviceManager.getCameras();
      if (!cameras || cameras.length === 0) {
        console.warn("No camera found — joining audio-only.");
        return null;
      }
      return new LocalVideoStream(cameras[0]);
    }

    async function renderLocal(stream) {
      localRenderer = new VideoStreamRenderer(stream);
      const view = await localRenderer.createView();
      $("localVideo").appendChild(view.target);
    }

    async function renderRemote(stream) {
      const renderer = new VideoStreamRenderer(stream);
      const view = await renderer.createView();
      remoteRenderers.set(stream, renderer);
      $("remoteVideos").appendChild(view.target);
    }

    function subscribeParticipant(participant) {
      const handle = (stream) => {
        if (stream.isAvailable) renderRemote(stream);
        stream.on("isAvailableChanged", () => {
          if (stream.isAvailable) renderRemote(stream);
          else {
            const r = remoteRenderers.get(stream);
            if (r) { r.dispose(); remoteRenderers.delete(stream); }
          }
        });
      };
      participant.videoStreams.forEach(handle);
      participant.on("videoStreamsUpdated", (e) => {
        e.added.forEach(handle);
        e.removed.forEach((stream) => {
          const r = remoteRenderers.get(stream);
          if (r) { r.dispose(); remoteRenderers.delete(stream); }
        });
      });
    }

    function subscribeCall(c) {
      c.on("stateChanged", () => setState(c.state));
      c.remoteParticipants.forEach(subscribeParticipant);
      c.on("remoteParticipantsUpdated", (e) => e.added.forEach(subscribeParticipant));
    }

    async function join() {
      const meetingLink = $("meetingLink").value.trim();
      if (!meetingLink) throw new Error("Paste a Teams meeting link first.");
      await ensureAgent();

      const localVideoStream = await buildLocalVideoStream();
      const videoOptions = localVideoStream ? { localVideoStreams: [localVideoStream] } : undefined;

      // THE INTEROP CALL: join a Teams meeting by its link.
      call = callAgent.join({ meetingLink }, { videoOptions, audioOptions: { muted: false } });

      if (localVideoStream) await renderLocal(localVideoStream);
      subscribeCall(call);
    }

    async function hangUp() {
      try { await call?.hangUp(); } catch (_) {}
      localRenderer?.dispose();
      remoteRenderers.forEach((r) => r.dispose());
      remoteRenderers.clear();
      $("localVideo").innerHTML = "";
      $("remoteVideos").innerHTML = "";
      setState("ended");
    }

    $("joinBtn").addEventListener("click", () => join().catch((e) => alert(e.message)));
    $("hangupBtn").addEventListener("click", () => hangUp());
  </script>
</body>
</html>
```

---

## 4. Deploy the page + mint a token, then run the call

### 4.1 Upload `index.html` to the HTTPS static website

From the same shell where you ran §1 (so the variables are still set), in the folder that contains
`index.html`:

```bash
# an account key is enough to upload; the site is then served over HTTPS anonymously
export STORAGE_KEY="$(az storage account keys list \
  --account-name "$STORAGE_NAME" --resource-group "$RG" \
  --query "[0].value" -o tsv)"

az storage blob upload \
  --account-name "$STORAGE_NAME" --account-key "$STORAGE_KEY" \
  --container-name '$web' \
  --file index.html --name index.html \
  --content-type "text/html" --overwrite \
  -o none

echo "Open this in a browser on a webcam machine: $SITE_URL"
```

### 4.2 Mint a short-lived VoIP token

```bash
# creates a throwaway ACS identity and issues a voip-scoped token (valid ~24h)
export ACS_TOKEN="$(az communication identity token issue \
  --scope voip \
  --connection-string "$ACS_CONNECTION_STRING" \
  --query token -o tsv)"

echo "ACS_TOKEN (paste this into the page):"
echo "$ACS_TOKEN"
```

> The token expires in ~24 hours — if the demo is the next day, just re-run this one command and paste
> the fresh value. The token is `voip`-scoped and reveals no account secret.

### 4.3 Join the meeting

On **any machine with a webcam** (your laptop is ideal):

1. Open **`$SITE_URL`** (the `https://…web.core.windows.net/` URL printed above).
2. Paste the **Teams meeting link** and the **ACS token** from §4.2; set a **display name**.
3. Click **Join Teams meeting (video)** and **allow camera + microphone** when prompted.
4. If the meeting has a lobby, the organiser admits the guest.
5. You should see **your local video** and the **Teams participants' video**, with two-way audio.

---

## 5. How the interop works (for explaining to a customer)

- **Identity & token** — `az communication identity token issue --scope voip` mints a disposable ACS
  user + a short-lived token. Minting happens with `az` (server side), so the connection string never
  reaches the browser; the page only ever sees a scoped, expiring token.
- **Call agent** — `new CallClient()` + `new AzureCommunicationTokenCredential(token)` +
  `createCallAgent(credential)` authenticate the browser as that ACS user.
- **Joining Teams** — `callAgent.join({ meetingLink }, options)` **is the entire interop**. The ACS
  user joins the Teams meeting as an **anonymous/external** participant — no bot, no Graph, no app
  registration for meeting-join.
- **Video** — a `LocalVideoStream` (your camera) is passed in `videoOptions`; remote participants'
  `RemoteVideoStream`s are rendered with `VideoStreamRenderer`.
- **HTTPS** — the page is served from the Storage static website over HTTPS, which is a **secure
  context**, so the browser lets the SDK access the camera/mic.

---

## 6. Running several guests in parallel

The guide is fully self-contained (its own RG, ACS resource, and storage account) and shares nothing
with the RM-Assist demo, so you can run it **alongside** the demo build/deploy/wipe with no conflict.

To have **multiple guests** in the same Teams meeting at once, just mint **one token per guest**
(re-run §4.2) and open the same `$SITE_URL` in a **separate browser tab/profile/machine** for each,
pasting a different token into each. Every token is a distinct ACS identity, so they appear as
separate participants. (One webcam can only feed one tab at a time, so use different machines for
genuinely separate video feeds.)

---

## 7. Clean up (delete everything so nothing bills)

```bash
# delete just the two resources...
az communication delete --name "$ACS_NAME" --resource-group "$RG" --yes
az storage account delete --name "$STORAGE_NAME" --resource-group "$RG" --yes

# ...or, if this RG was only for this demo, delete the whole thing in one shot
az group delete --name "$RG" --yes --no-wait
```

ACS and Storage static-website hosting are pay-per-use and idle usage is negligible, but deleting the
resources guarantees a clean, zero-footprint teardown.

---

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Camera prompt never appears / black tile | The page **must** be on `https://…` (the Storage web endpoint) or `http://localhost` **on the same machine**. Plain `http://<ip>` is blocked — that is the whole reason we host on HTTPS. Also check the browser's per-site camera permission. |
| `NotAllowedError` / permission denied | You blocked the camera prompt earlier. Click the padlock in the address bar → allow Camera + Microphone → reload. |
| Stuck in the lobby / never admitted | Organiser must admit the guest, or set **Meeting options → who can bypass lobby**. Ensure **anonymous join** is enabled (§2). |
| `401` / token invalid or expired | Re-mint the token (§4.2) — it lasts ~24h — and paste the fresh value. |
| `403` / cannot join | Tenant blocks anonymous users, or the link is a channel meeting without external join. Use a standard scheduled meeting link. |
| No remote video | The Teams participant may have their camera off, or is still in the lobby. Audio works independently of video. |
| SDK fails to load / blank page | You need internet access to `cdn.jsdelivr.net`. On a locked-down network, allow it, or download the two `/+esm` files and host them beside `index.html`, updating the two `import` URLs to relative paths. |
| `az communication` not found | It auto-installs on first use; if not: `az extension add --name communication`. |
| Static site URL 404s | Wait a few seconds after enabling static hosting, confirm the blob uploaded to the **`$web`** container, and use the **`primaryEndpoints.web`** URL (not the blob URL). |

---

## 9. One-file recap

That is the entire integration — **one `index.html`**, three `az` blocks (provision, upload, token),
and one Teams tenant setting. No Node, no npm, no server, no build step. Everything is created and
destroyed with `az`, and the deployed page holds no secrets.

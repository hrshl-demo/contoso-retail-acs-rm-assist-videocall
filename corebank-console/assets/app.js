/* Contoso Bank — Core Banking & CRM Console
 * Zero-dependency vanilla JS SPA. Loads the committed synthetic dataset
 * (contosobank_dataset.json) and renders it the way an Indian bank's staff console
 * (core banking à la Finacle + CRM à la Dynamics/CRM Next) would, with a tab per
 * Business Unit (Retail / Business Banking / Corporate). Read-only.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------ data URL
  const params = new URLSearchParams(location.search);
  const DATA_URL = params.get("data") || "./data/contosobank_dataset.json";

  const BU_DEFS = [
    { id: "OVERVIEW", label: "Enterprise Overview" },
    { id: "RETAIL", label: "Retail Banking", seg: "RETAIL" },
    { id: "MSME", label: "Business Banking", seg: "MSME" },
    { id: "CORPORATE", label: "Corporate & Institutional", seg: "CORPORATE" },
  ];

  let DATA = null;
  const state = { bu: "OVERVIEW", cust: null, tab: "overview", acct: "ALL" };

  // ------------------------------------------------------------------ helpers
  const $ = (sel, el) => (el || document).querySelector(sel);
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  function inrFull(n) {
    if (n == null || n === "" || isNaN(n)) return "—";
    const neg = n < 0;
    let s = Math.round(Math.abs(n)).toString();
    let last3 = s.slice(-3);
    let rest = s.slice(0, -3);
    if (rest) last3 = "," + last3;
    rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
    return (neg ? "-₹" : "₹") + rest + last3;
  }
  function inrShort(n) {
    if (n == null || n === "" || isNaN(n)) return "—";
    const a = Math.abs(n), sign = n < 0 ? "-" : "";
    if (a >= 1e7) return sign + "₹" + (a / 1e7).toFixed(2) + " Cr";
    if (a >= 1e5) return sign + "₹" + (a / 1e5).toFixed(2) + " L";
    return inrFull(n);
  }
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtDate(s) {
    if (!s) return "—";
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s));
    if (!m) return esc(s);
    return `${m[3]} ${MONTHS[+m[2] - 1]} ${m[1]}`;
  }
  const num = (v) => (v == null || v === "" || isNaN(v) ? 0 : +v);
  const sum = (arr, f) => (arr || []).reduce((a, x) => a + num(f(x)), 0);
  const initials = (name) =>
    String(name || "?").split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();

  function badge(text, kind) {
    return `<span class="badge b-${kind || "gray"}">${esc(text)}</span>`;
  }
  // Map a status-ish string to a badge colour.
  function statusBadge(v) {
    const s = String(v || "").toUpperCase();
    if (/ACTIVE|STANDARD|PASS|RESOLVED|CLOSED_WON|WON|COMPLETED|CLEAR|APPROVED|PAID|VERIFIED|VALID/.test(s)) return badge(v, "green");
    if (/FAIL|BREACH|NPA|OVERDUE|DPD|BREACHED|SLA_BREACH|LOST|CLOSED_LOST|DECLIN|REJECT|SUSPEND|BLOCK|DELINQ|SMA/.test(s)) return badge(v, "red");
    if (/PENDING|DUE|OPEN|IN_PROGRESS|WIP|REVIEW|WATCH|HOLD|PARTIAL|ESCALAT/.test(s)) return badge(v, "amber");
    return badge(v, "gray");
  }

  const custName = (c) => c.profile.entity_name || c.profile.full_name || c.profile.cust_id;
  const custRM = (c) => DATA.rms[c.profile.rm_id];
  const custList = (seg) =>
    Object.values(DATA.customers).filter((c) => (c.profile.segment || "").toUpperCase() === seg);

  const deposits = (c) => sum((c.accounts || []).filter((a) => a.account_type === "DEPOSIT"), (a) => a.current_balance_inr);
  const advances = (c) => sum(c.loans, (l) => l.outstanding_inr) + sum(c.facilities, (f) => f.outstanding_inr);
  const investments = (c) => sum(c.investment_holding, (h) => h.market_value_inr);
  const relValue = (c) => deposits(c) + advances(c) + investments(c);

  // ------------------------------------------------------------------ boot
  async function boot() {
    try {
      const res = await fetch(DATA_URL, { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      DATA = await res.json();
    } catch (e) {
      $("#appRoot").innerHTML =
        `<div class="empty" style="margin:60px 26px">Could not load dataset from <code>${esc(DATA_URL)}</code>.<br>${esc(e.message)}</div>`;
      return;
    }
    readHash();
    renderChrome();
    render();
    window.addEventListener("hashchange", () => { readHash(); render(); });
  }

  function readHash() {
    const h = new URLSearchParams(location.hash.replace(/^#/, ""));
    if (h.get("bu")) state.bu = h.get("bu");
    state.cust = h.get("cust") || null;
    state.tab = h.get("tab") || "overview";
    state.acct = h.get("acct") || "ALL";
  }
  function writeHash() {
    const p = new URLSearchParams();
    p.set("bu", state.bu);
    if (state.cust) p.set("cust", state.cust);
    if (state.cust) p.set("tab", state.tab);
    if (state.cust && state.acct !== "ALL") p.set("acct", state.acct);
    history.replaceState(null, "", "#" + p.toString());
  }
  function go(patch) { Object.assign(state, patch); writeHash(); render(); }

  // ------------------------------------------------------------------ chrome (top bar)
  function renderChrome() {
    $("#asOfDate").textContent = "As of " + fmtDate(DATA.meta.demo_today);
    $("#footerMeta").textContent =
      `${DATA.meta.bank_name} · ${DATA.meta.codename} · ${Object.keys(DATA.customers).length} customers · ${Object.keys(DATA.rms).length} RMs`;
    const nav = $("#buNav");
    nav.innerHTML = BU_DEFS.map((b) => {
      const cnt = b.seg ? custList(b.seg).length : "";
      return `<button class="bu-tab" data-bu="${b.id}">${esc(b.label)}${cnt !== "" ? `<span class="bu-count">${cnt}</span>` : ""}</button>`;
    }).join("");
    nav.querySelectorAll(".bu-tab").forEach((btn) =>
      btn.addEventListener("click", () => go({ bu: btn.dataset.bu, cust: null, tab: "overview", acct: "ALL" }))
    );
  }

  // ------------------------------------------------------------------ router
  function render() {
    document.querySelectorAll(".bu-tab").forEach((b) =>
      b.classList.toggle("active", b.dataset.bu === state.bu)
    );
    const root = $("#appRoot");
    if (state.bu === "OVERVIEW") { root.innerHTML = renderOverview(); wireOverview(); return; }
    const seg = (BU_DEFS.find((b) => b.id === state.bu) || {}).seg;
    root.innerHTML = renderBU(seg);
    wireBU(seg);
  }

  // ------------------------------------------------------------------ Enterprise overview
  function renderOverview() {
    const custs = Object.values(DATA.customers);
    const totDep = sum(custs, deposits), totAdv = sum(custs, advances), totInv = sum(custs, investments);
    const openTickets = sum(custs, (c) => (c.operations?.service_tickets || []).filter((t) => !/RESOLVED|CLOSED/i.test(t.status || "")).length);
    const opps = custs.flatMap((c) => c.crm?.opportunities || []);
    const openOpps = opps.filter((o) => !/WON|LOST|CLOSED/i.test(o.win_loss_status || o.stage || ""));
    const pipeline = sum(openOpps, (o) => o.expected_value_inr);
    const breaches = sum(custs, (c) => (c.covenants || []).filter((v) => /FAIL|BREACH/i.test(v.last_test_result || v.description || "")).length);

    const kpis = [
      { l: "Total Relationships", v: custs.length, f: `${Object.keys(DATA.rms).length} RMs · ${DATA.reference.branches.length} branches`, k: "" },
      { l: "Total Deposits", v: inrShort(totDep), f: "CASA + term", k: "k-blue" },
      { l: "Total Advances", v: inrShort(totAdv), f: "loans + facilities", k: "k-green" },
      { l: "Investments / AUM", v: inrShort(totInv), f: "third-party + MF", k: "" },
      { l: "Open Opportunities", v: openOpps.length, f: `${inrShort(pipeline)} pipeline`, k: "k-amber" },
      { l: "Open Service Tickets", v: openTickets, f: "across all BUs", k: openTickets ? "k-amber" : "k-green" },
      { l: "Covenant Breaches", v: breaches, f: "financial + non-financial", k: breaches ? "k-red" : "k-green" },
    ];

    const buRows = BU_DEFS.filter((b) => b.seg).map((b) => {
      const list = custList(b.seg);
      const rm = Object.values(DATA.rms).find((r) => (r.segment || "").toUpperCase() === b.seg);
      return `<tr>
        <td><b>${esc(b.label)}</b></td>
        <td>${rm ? esc(rm.rm_name) : "—"}<div class="dim" style="font-size:12px">${rm ? esc(rm.role) : ""}</div></td>
        <td class="num">${list.length}</td>
        <td class="num">${inrShort(sum(list, deposits))}</td>
        <td class="num">${inrShort(sum(list, advances))}</td>
        <td class="num">${inrShort(sum(list, relValue))}</td>
        <td><button class="chip" data-open-bu="${b.id}" style="cursor:pointer">Open ›</button></td>
      </tr>`;
    }).join("");

    const prod = DATA.reference.products_catalog;
    const branchRows = DATA.reference.branches.map((br) =>
      `<tr><td class="mono">${esc(br.ifsc_code)}</td><td><b>${esc(br.branch_name)}</b></td><td>${esc(br.city)}, ${esc(br.state)}</td><td>${badge(br.branch_tier, "blue")}</td><td>${esc(br.region)}</td></tr>`
    ).join("");

    return `<div class="page">
      <div class="page-head">
        <div>
          <h1 class="page-title">Enterprise Overview</h1>
          <div class="page-sub">${esc(DATA.reference.bank_name)} — synthetic book across ${custs.length} relationships · window ${fmtDate(DATA.meta.window_start)} – ${fmtDate(DATA.meta.window_end)}</div>
        </div>
      </div>
      <div class="kpi-grid">
        ${kpis.map((k) => `<div class="kpi ${k.k}"><div class="kpi-label">${esc(k.l)}</div><div class="kpi-value">${esc(k.v)}</div><div class="kpi-foot">${esc(k.f)}</div></div>`).join("")}
      </div>
      <div class="cards-row" style="grid-template-columns:1fr">
        <div class="card">
          <div class="card-head"><div class="card-title">Business Units</div><small>relationship value by segment</small></div>
          <div class="tbl-scroll">
            <table class="data">
              <thead><tr><th>Business Unit</th><th>Relationship Manager</th><th class="num">Customers</th><th class="num">Deposits</th><th class="num">Advances</th><th class="num">Rel. Value</th><th></th></tr></thead>
              <tbody>${buRows}</tbody>
            </table>
          </div>
        </div>
      </div>
      <div class="split-2" style="margin-top:16px">
        <div class="card">
          <div class="card-head"><div class="card-title">Branch Network</div><small>${DATA.reference.branches.length} branches</small></div>
          <div class="tbl-scroll"><table class="data"><thead><tr><th>IFSC</th><th>Branch</th><th>Location</th><th>Tier</th><th>Region</th></tr></thead><tbody>${branchRows}</tbody></table></div>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">Product Catalogue</div><small>${prod.length} products</small></div>
          <div class="card-body" style="max-height:320px;overflow:auto">
            ${prod.map((p) => `<div style="padding:8px 0;border-bottom:1px solid var(--line-soft)">
              <div style="display:flex;justify-content:space-between;gap:10px"><b>${esc(p.product_name)}</b>${badge(p.product_family, "blue")}</div>
              <div class="dim" style="font-size:12px;margin-top:2px">${esc(p.segment_applicability)} · ${p.is_third_party ? "third-party" : "own book"} · ticket ${inrShort(p.min_ticket_inr)}–${inrShort(p.max_ticket_inr)}</div>
            </div>`).join("")}
          </div>
        </div>
      </div>
    </div>`;
  }
  function wireOverview() {
    document.querySelectorAll("[data-open-bu]").forEach((b) =>
      b.addEventListener("click", () => go({ bu: b.dataset.openBu, cust: null, tab: "overview", acct: "ALL" }))
    );
  }

  // ------------------------------------------------------------------ BU view
  function renderBU(seg) {
    const list = custList(seg);
    const rm = Object.values(DATA.rms).find((r) => (r.segment || "").toUpperCase() === seg);
    if (!state.cust || !list.find((c) => c.profile.cust_id === state.cust)) {
      state.cust = list.length ? list[0].profile.cust_id : null;
    }
    const sidebar = `<aside class="bu-sidebar">
      ${rm ? rmCard(rm) : ""}
      <div class="cust-list-head">Portfolio — ${list.length} relationship${list.length === 1 ? "" : "s"}</div>
      ${list.map((c) => custListItem(c)).join("")}
    </aside>`;
    const detail = `<section class="bu-detail" id="buDetail">${state.cust ? renderCustomer(DATA.customers[state.cust]) : `<div class="empty" style="margin:40px">No customers in this business unit.</div>`}</section>`;
    return `<div class="bu-layout">${sidebar}${detail}</div>`;
  }

  function rmCard(rm) {
    const ps = rm.portfolio_stats || {};
    return `<div class="rm-card">
      <div class="rm-top">
        <div class="avatar">${initials(rm.rm_name)}</div>
        <div><div class="rm-name">${esc(rm.rm_name)}</div><div class="rm-role">${esc(rm.role)} · ${esc(rm.employee_grade || "")}</div></div>
      </div>
      <div class="rm-stats">
        <div class="rm-stat"><div class="v">${(ps.customer_ids || ps.mapped_households || "—")}</div><div class="l">Customers</div></div>
        <div class="rm-stat"><div class="v">${inrShort(ps.relationship_value_inr)}</div><div class="l">Book Value</div></div>
      </div>
      <div class="rm-kpis">${(rm.kpis || []).map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>
    </div>`;
  }

  function custListItem(c) {
    const active = c.profile.cust_id === state.cust ? " active" : "";
    const rating = c.profile.internal_rating || c.risk_profile?.suitability_band || c.profile.sub_segment || "";
    return `<button class="cust-item${active}" data-cust="${esc(c.profile.cust_id)}">
      <div class="ci-top"><span class="ci-name">${esc(custName(c))}</span><span class="ci-val">${inrShort(relValue(c))}</span></div>
      <div class="ci-sub"><span class="mono">${esc(c.profile.cust_id)}</span>${rating ? `<span>· ${esc(rating)}</span>` : ""}<span>· ${esc(c.profile.city || "")}</span></div>
    </button>`;
  }

  function wireBU() {
    document.querySelectorAll("[data-cust]").forEach((b) =>
      b.addEventListener("click", () => go({ cust: b.dataset.cust, tab: "overview", acct: "ALL" }))
    );
    wireCustomer();
  }

  // ------------------------------------------------------------------ Customer 360
  function customerTabs(c) {
    const t = [{ id: "overview", label: "Overview" }, { id: "accounts", label: "Accounts", n: (c.accounts || []).length }];
    if ((c.transactions || []).length) t.push({ id: "transactions", label: "Transactions", n: c.transactions.length });
    const creditN = (c.facilities || []).length + (c.loans || []).length;
    if (creditN) t.push({ id: "credit", label: "Credit & Facilities", n: creditN });
    if ((c.investment_holding || []).length) t.push({ id: "wealth", label: "Investments", n: c.investment_holding.length });
    if ((c.operations?.trade_finance_events || []).length) t.push({ id: "trade", label: "Trade Finance", n: c.operations.trade_finance_events.length });
    const crmN = ["interactions", "meeting_summaries", "email_threads", "opportunities"].reduce((a, k) => a + (c.crm?.[k] || []).length, 0);
    t.push({ id: "crm", label: "CRM 360", n: crmN });
    if ((c.operations?.service_tickets || []).length) t.push({ id: "service", label: "Service", n: c.operations.service_tickets.length });
    if ((c.operations?.documents || []).length) t.push({ id: "docs", label: "Documents", n: c.operations.documents.length });
    if ((c.six_month_arc || []).length) t.push({ id: "arc", label: "Relationship Arc", n: c.six_month_arc.length });
    return t;
  }

  function renderCustomer(c) {
    const tabs = customerTabs(c);
    if (!tabs.find((t) => t.id === state.tab)) state.tab = "overview";
    const rm = custRM(c);
    const p = c.profile;
    const head = `<div class="cust-header">
      <div class="cust-header-main">
        <div>
          <div class="cust-h-name">${esc(custName(c))}</div>
          <div class="cust-h-meta">
            <span class="mono">${esc(p.cust_id)}</span>
            <span><b>Segment</b> ${esc(p.segment)}${p.sub_segment ? " / " + esc(p.sub_segment) : ""}</span>
            <span><b>RM</b> ${rm ? esc(rm.rm_name) : "—"}</span>
            <span><b>Branch</b> ${esc(branchName(p.home_branch_id))}</span>
            <span><b>Since</b> ${fmtDate(p.relationship_start_date)}</span>
            ${p.is_active ? badge("ACTIVE", "green") : badge("INACTIVE", "gray")}
          </div>
        </div>
        <div class="cust-h-right">
          <div class="big">${inrShort(relValue(c))}</div>
          <div class="lbl">Relationship Value</div>
        </div>
      </div>
      <div class="subtabs">
        ${tabs.map((t) => `<button class="subtab${t.id === state.tab ? " active" : ""}" data-tab="${t.id}">${esc(t.label)}${t.n != null ? `<span class="st-badge">${t.n}</span>` : ""}</button>`).join("")}
      </div>
    </div>`;
    return head + `<div class="tab-body" id="tabBody">${renderTab(c, state.tab)}</div>`;
  }

  function branchName(id) {
    const b = (DATA.reference.branches || []).find((x) => x.branch_id === id);
    return b ? `${b.branch_name}` : id || "—";
  }

  function wireCustomer() {
    document.querySelectorAll("[data-tab]").forEach((b) =>
      b.addEventListener("click", () => go({ tab: b.dataset.tab, acct: "ALL" }))
    );
    const sel = $("#acctFilter");
    if (sel) sel.addEventListener("change", () => go({ acct: sel.value }));
  }

  function renderTab(c, tab) {
    switch (tab) {
      case "overview": return tabOverview(c);
      case "accounts": return tabAccounts(c);
      case "transactions": return tabTransactions(c);
      case "credit": return tabCredit(c);
      case "wealth": return tabWealth(c);
      case "trade": return tabTrade(c);
      case "crm": return tabCRM(c);
      case "service": return tabService(c);
      case "docs": return tabDocs(c);
      case "arc": return tabArc(c);
      default: return tabOverview(c);
    }
  }

  const field = (l, v) => `<div class="field"><div class="fl">${esc(l)}</div><div class="fv">${v == null || v === "" ? "—" : v}</div></div>`;

  // ---- Overview
  function tabOverview(c) {
    const p = c.profile, k = c.kyc || {}, rp = c.risk_profile || {}, fin = c.financials || {};
    const isEntity = p.cust_type === "ENTITY" || p.entity_name;
    const idFields = isEntity
      ? [field("Entity Type", esc(p.sub_segment || p.cust_type)), field("Incorporated", fmtDate(p.date_of_incorporation)),
         field("PAN", `<span class="mono">${esc(p.pan)}</span>`), field("GSTIN", `<span class="mono">${esc(p.gstin || "—")}</span>`),
         field("CIN", `<span class="mono">${esc(p.cin || "—")}</span>`), field("Annual Turnover", inrShort(p.annual_turnover_inr))]
      : [field("Occupation", esc(p.occupation || "—")), field("Age", p.age || "—"),
         field("PAN", `<span class="mono">${esc(p.pan)}</span>`), field("Declared Income", inrShort(p.declared_annual_income_inr)),
         field("Preferred Channel", esc(p.preferred_channel || "—")), field("NRE/NRO", esc(p.nre_nro_flag || "Resident"))];

    const bureau = fin.bureau || {};
    const bScore = bureau.score || bureau.commercial_score;
    const kycDue = k.next_kyc_due_date;
    const contacts = (c.contacts || []).map((ct) =>
      `<tr><td><b>${esc(ct.name)}</b>${ct.is_primary ? " " + badge("PRIMARY", "blue") : ""}</td><td>${esc(ct.contact_type)}${ct.designation ? " · " + esc(ct.designation) : ""}</td><td class="mono">${esc(ct.mobile_masked || "")}</td><td class="mono">${esc(ct.email_masked || "")}</td></tr>`
    ).join("");

    const arc = (c.six_month_arc || [])[c.six_month_arc?.length ? c.six_month_arc.length - 1 : 0];

    return `
    <div class="section">
      <div class="section-title">Relationship Snapshot</div>
      <div class="kpi-grid">
        <div class="kpi k-blue"><div class="kpi-label">Deposits</div><div class="kpi-value">${inrShort(deposits(c))}</div><div class="kpi-foot">${(c.accounts || []).filter((a) => a.account_type === "DEPOSIT").length} account(s)</div></div>
        <div class="kpi k-green"><div class="kpi-label">Advances</div><div class="kpi-value">${inrShort(advances(c))}</div><div class="kpi-foot">${(c.loans || []).length + (c.facilities || []).length} facility/loan</div></div>
        <div class="kpi"><div class="kpi-label">Investments</div><div class="kpi-value">${inrShort(investments(c))}</div><div class="kpi-foot">${(c.investment_holding || []).length} holding(s)</div></div>
        <div class="kpi ${bScore >= 750 ? "k-green" : bScore ? "k-amber" : ""}"><div class="kpi-label">Bureau Score</div><div class="kpi-value">${bScore || "—"}</div><div class="kpi-foot">${bureau.foir_pct != null ? "FOIR " + bureau.foir_pct + "%" : (fin.statements ? "corporate book" : (bureau.commercial_score ? "commercial" : "—"))}</div></div>
      </div>
    </div>

    <div class="split-2">
      <div class="section">
        <div class="section-title">Customer Profile</div>
        <div class="info-grid">${idFields.join("")}
          ${field("City / State", esc((p.city || "") + (p.state ? ", " + p.state : "")))}
          ${field("Internal Rating", p.internal_rating ? badge(p.internal_rating, "blue") : "—")}
          ${field("External Rating", esc(p.external_rating || "—"))}
          ${p.wallet_share_pct != null ? field("Wallet Share", p.wallet_share_pct + "%") : ""}
        </div>
      </div>
      <div class="section">
        <div class="section-title">KYC &amp; Risk</div>
        <div class="info-grid">
          ${field("KYC Status", statusBadge(k.kyc_status))}
          ${field("KYC Risk Category", esc(k.kyc_risk_category || "—"))}
          ${field("Last KYC", fmtDate(k.last_kyc_date))}
          ${field("Next KYC Due", kycDue ? fmtDate(kycDue) : "—")}
          ${field("CKYC ID", `<span class="mono">${esc(k.ckyc_identifier || "—")}</span>`)}
          ${field("PEP", k.pep_flag ? badge("PEP", "amber") : badge("No", "green"))}
          ${field("Sanctions Screen", statusBadge(k.sanctions_screen_status))}
          ${rp.suitability_band ? field("Suitability Band", badge(rp.suitability_band, "blue")) : ""}
          ${rp.investment_risk_appetite ? field("Risk Appetite", esc(rp.investment_risk_appetite)) : ""}
        </div>
      </div>
    </div>

    ${contacts ? `<div class="section"><div class="section-title">Contacts</div>
      <div class="tbl-wrap"><div class="tbl-scroll"><table class="data"><thead><tr><th>Name</th><th>Role</th><th>Mobile</th><th>Email</th></tr></thead><tbody>${contacts}</tbody></table></div></div></div>` : ""}

    ${p.advisor_brief ? `<div class="section"><div class="section-title">Advisor Brief</div><div class="list-card"><div class="lc-body">${esc(p.advisor_brief)}</div></div></div>` : ""}
    ${arc ? `<div class="section"><div class="section-title">Latest Relationship Event</div><div class="list-card"><div class="lc-head"><span class="lc-title">${esc(arc.event_code)}</span><span class="dim">${fmtDate(arc.date)}</span></div><div class="lc-body">${esc(arc.narrative)}</div></div></div>` : ""}
    `;
  }

  // ---- Accounts (Finacle core)
  function tabAccounts(c) {
    const accts = c.accounts || [];
    if (!accts.length) return `<div class="empty">No accounts on file.</div>`;
    const rows = accts.map((a) => `<tr>
      <td class="mono">${esc(a.account_id)}</td>
      <td><b>${esc(a.product_name)}</b><div class="dim" style="font-size:12px">${esc(a.account_type)} · ${esc(a.account_subtype || "")}</div></td>
      <td class="mono">${esc(a.ifsc_code)}</td>
      <td>${fmtDate(a.open_date)}</td>
      <td>${statusBadge(a.account_status)}</td>
      <td class="num"><b>${inrFull(a.current_balance_inr)}</b></td>
      <td>${(c.transactions || []).some((t) => t.account_id === a.account_id) ? `<button class="chip" data-view-txn="${esc(a.account_id)}" style="cursor:pointer">Ledger ›</button>` : ""}</td>
    </tr>`).join("");
    const totDep = sum(accts.filter((a) => a.account_type === "DEPOSIT"), (a) => a.current_balance_inr);
    return `<div class="section">
      <div class="section-title">Accounts &amp; Balances <span style="margin-left:auto;font-weight:600;color:var(--maroon)">CASA + Deposits: ${inrShort(totDep)}</span></div>
      <div class="tbl-wrap"><div class="tbl-scroll"><table class="data">
        <thead><tr><th>Account No.</th><th>Product</th><th>IFSC</th><th>Opened</th><th>Status</th><th class="num">Balance</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div></div>
    </div>`;
  }

  // ---- Transactions ledger
  function tabTransactions(c) {
    const accts = c.accounts || [];
    let txns = (c.transactions || []).slice();
    if (state.acct !== "ALL") txns = txns.filter((t) => t.account_id === state.acct);
    txns.sort((a, b) => (b.txn_timestamp || b.txn_date || "").localeCompare(a.txn_timestamp || a.txn_date || ""));
    const shown = txns.slice(0, 300);
    const opts = `<option value="ALL">All accounts (${(c.transactions || []).length})</option>` +
      accts.filter((a) => (c.transactions || []).some((t) => t.account_id === a.account_id))
        .map((a) => `<option value="${esc(a.account_id)}"${state.acct === a.account_id ? " selected" : ""}>${esc(a.product_name)} · ${esc(a.account_id)}</option>`).join("");
    const rows = shown.map((t) => `<tr>
      <td class="nowrap">${fmtDate(t.txn_date)}</td>
      <td>${badge(t.rail, "blue")}</td>
      <td>${esc(t.narration || t.counterparty_name || "")}<div class="dim" style="font-size:11.5px">${esc(t.counterparty_name || "")}${t.merchant_category_code ? " · MCC " + esc(t.merchant_category_code) : ""}</div></td>
      <td>${t.direction === "DR" ? badge("DR", "red") : badge("CR", "green")}</td>
      <td class="num ${t.direction === "DR" ? "dr" : "cr"}">${t.direction === "DR" ? "-" : "+"}${inrFull(t.amount_inr)}</td>
      <td class="num dim">${inrFull(t.running_balance_inr)}</td>
    </tr>`).join("");
    return `<div class="section">
      <div class="section-title">Transaction Ledger
        <span style="margin-left:auto"><select id="acctFilter" style="font-family:inherit;font-size:13px;padding:6px 10px;border:1px solid var(--line);border-radius:8px">${opts}</select></span>
      </div>
      <div class="tbl-wrap"><div class="tbl-scroll" style="max-height:600px;overflow:auto"><table class="data">
        <thead><tr><th>Date</th><th>Rail</th><th>Narration / Counterparty</th><th>Dr/Cr</th><th class="num">Amount</th><th class="num">Balance</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div></div>
      ${txns.length > 300 ? `<div class="dim" style="margin-top:8px;font-size:12px">Showing latest 300 of ${txns.length} transactions.</div>` : ""}
    </div>`;
  }

  // ---- Credit & facilities
  function tabCredit(c) {
    let html = "";
    const loans = c.loans || [], facs = c.facilities || [], cov = c.covenants || [], col = c.collateral || [];
    if (loans.length) {
      html += `<div class="section"><div class="section-title">Loans</div><div class="tbl-wrap"><div class="tbl-scroll"><table class="data">
        <thead><tr><th>Type</th><th class="num">Sanctioned</th><th class="num">Outstanding</th><th class="num">Rate</th><th class="num">EMI</th><th>Maturity</th><th>DPD</th><th>Class</th></tr></thead>
        <tbody>${loans.map((l) => `<tr>
          <td><b>${esc(l.loan_subtype || "LOAN")}</b><div class="dim" style="font-size:12px">${esc(l.rate_type || "")}</div></td>
          <td class="num">${inrShort(l.sanctioned_amount_inr)}</td>
          <td class="num"><b>${inrShort(l.outstanding_inr)}</b></td>
          <td class="num">${l.interest_rate_pct != null ? l.interest_rate_pct + "%" : "—"}</td>
          <td class="num">${inrFull(l.emi_inr)}</td>
          <td>${fmtDate(l.maturity_date)}</td>
          <td class="num">${num(l.dpd_days)}</td>
          <td>${statusBadge(l.asset_classification)}</td>
        </tr>`).join("")}</tbody></table></div></div></div>`;
    }
    if (facs.length) {
      html += `<div class="section"><div class="section-title">Credit Facilities</div>${facs.map((f) => {
        const util = f.utilisation_pct != null ? Math.round(num(f.utilisation_pct))
          : (f.sanctioned_limit_inr ? Math.min(100, Math.round((num(f.outstanding_inr) / num(f.sanctioned_limit_inr)) * 100)) : 0);
        const cls = util >= 90 ? "bad" : util >= 75 ? "warn" : "";
        return `<div class="list-card">
          <div class="lc-head"><span class="lc-title">${esc(f.facility_type)} · ${esc(f.facility_id)}</span>${statusBadge(f.asset_classification || (f.fund_based_flag ? "FUND-BASED" : "NON-FUND"))}</div>
          <div class="lc-meta"><span><b>Sanctioned</b> ${inrShort(f.sanctioned_limit_inr)}</span><span><b>Drawing Power</b> ${inrShort(f.drawing_power_inr)}</span><span><b>Outstanding</b> ${inrShort(f.outstanding_inr)}</span><span><b>Pricing</b> ${esc(f.benchmark_rate_code || "")}${f.pricing_spread_bps ? " + " + f.pricing_spread_bps + "bps" : ""}</span></div>
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-top:6px"><span class="dim">Utilisation</span><span><b>${util}%</b></span></div>
          <div class="bar ${cls}"><span style="width:${util}%"></span></div>
          <div class="lc-meta" style="margin-top:8px"><span><b>Sanction</b> ${fmtDate(f.sanction_date)}</span><span><b>Expiry</b> ${fmtDate(f.expiry_date)}</span><span><b>Next Review</b> ${fmtDate(f.next_review_date)}</span></div>
        </div>`;
      }).join("")}</div>`;
    }
    if (cov.length) {
      html += `<div class="section"><div class="section-title">Covenants</div><div class="tbl-wrap"><div class="tbl-scroll"><table class="data">
        <thead><tr><th>Metric</th><th>Type</th><th>Facility</th><th>Threshold</th><th class="num">Observed</th><th>Last Test</th><th>Freq</th><th>Waiver</th><th>Result</th></tr></thead>
        <tbody>${cov.map((v) => {
          const fail = /FAIL|BREACH/i.test(v.last_test_result || v.description || "");
          return `<tr>
            <td><b>${esc(v.metric_code || v.covenant_type)}</b><div class="dim" style="font-size:12px;max-width:460px;white-space:normal">${esc(v.description || "")}</div></td>
            <td>${esc(v.covenant_type)}</td><td class="mono">${esc(v.facility_id)}</td>
            <td class="nowrap">${esc(v.threshold_operator || "")} ${esc(v.threshold_value != null ? v.threshold_value : "")}</td>
            <td class="num">${esc(v.observed_value != null ? v.observed_value : "—")}</td>
            <td>${fmtDate(v.last_tested_date)}</td><td>${esc(v.test_frequency || "")}</td>
            <td>${v.waiver_status && v.waiver_status !== "NONE" ? badge(v.waiver_status, "amber") : "—"}</td>
            <td>${fail ? badge("FAIL", "red") : badge("PASS", "green")}${v.breach_count_12m ? ` <span class="dim" style="font-size:11px">${v.breach_count_12m}×/12m</span>` : ""}</td>
          </tr>`;
        }).join("")}</tbody></table></div></div></div>`;
    }
    if (col.length) {
      html += `<div class="section"><div class="section-title">Collateral</div><div class="tbl-wrap"><div class="tbl-scroll"><table class="data">
        <thead><tr><th>Type</th><th class="num">Assessed Value</th><th>Charge</th><th>Valuer</th><th>Valued</th><th>Next Due</th></tr></thead>
        <tbody>${col.map((x) => `<tr>
          <td><b>${esc(x.collateral_type)}</b><div class="dim mono" style="font-size:11.5px">${esc(x.roc_charge_id || "")}</div></td>
          <td class="num">${inrShort(x.assessed_value_inr)}</td><td>${esc(x.charge_type || "")}</td>
          <td>${esc(x.valuer_name || "")}</td><td>${fmtDate(x.valuation_date)}</td><td>${fmtDate(x.next_valuation_due)}</td>
        </tr>`).join("")}</tbody></table></div></div></div>`;
    }
    const rps = c.repayment_schedule || [];
    if (rps.length) {
      html += `<div class="section"><div class="section-title">Repayment Schedule</div><div class="tbl-wrap"><div class="tbl-scroll" style="max-height:340px;overflow:auto"><table class="data">
        <thead><tr><th>#</th><th>Due Date</th><th class="num">Principal</th><th class="num">Interest</th><th class="num">Total</th><th>Paid</th><th>Status</th></tr></thead>
        <tbody>${rps.map((r) => `<tr><td>${esc(r.instalment_no)}</td><td>${fmtDate(r.due_date)}</td><td class="num">${inrFull(r.principal_due_inr)}</td><td class="num">${inrFull(r.interest_due_inr)}</td><td class="num"><b>${inrFull(r.total_due_inr)}</b></td><td>${r.paid_date ? fmtDate(r.paid_date) : "—"}</td><td>${statusBadge(r.payment_status)}</td></tr>`).join("")}</tbody>
      </table></div></div></div>`;
    }
    return html || `<div class="empty">No credit exposure on file.</div>`;
  }

  // ---- Wealth / investments
  function tabWealth(c) {
    const h = c.investment_holding || [];
    if (!h.length) return `<div class="empty">No investment holdings.</div>`;
    const mv = sum(h, (x) => x.market_value_inr), cv = sum(h, (x) => x.cost_value_inr);
    const gain = mv - cv;
    return `<div class="section">
      <div class="kpi-grid">
        <div class="kpi k-blue"><div class="kpi-label">Portfolio Value</div><div class="kpi-value">${inrShort(mv)}</div><div class="kpi-foot">${h.length} holdings</div></div>
        <div class="kpi"><div class="kpi-label">Invested</div><div class="kpi-value">${inrShort(cv)}</div><div class="kpi-foot">cost basis</div></div>
        <div class="kpi ${gain >= 0 ? "k-green" : "k-red"}"><div class="kpi-label">Unrealised P/L</div><div class="kpi-value">${inrShort(gain)}</div><div class="kpi-foot">${cv ? ((gain / cv) * 100).toFixed(1) + "%" : ""}</div></div>
      </div>
      <div class="section-title">Holdings</div>
      <div class="tbl-wrap"><div class="tbl-scroll"><table class="data">
        <thead><tr><th>Scheme</th><th>Type</th><th class="num">Units</th><th class="num">NAV</th><th class="num">Market Value</th><th>SIP</th><th>Suitability</th></tr></thead>
        <tbody>${h.map((x) => `<tr>
          <td><b>${esc(x.scheme_name)}</b><div class="dim mono" style="font-size:11.5px">${esc(x.folio_number || "")}</div></td>
          <td>${esc(x.instrument_type)} ${x.risk_grade ? badge(x.risk_grade, "amber") : ""}</td>
          <td class="num">${esc(x.units)}</td><td class="num">${inrFull(x.nav_inr)}</td>
          <td class="num"><b>${inrFull(x.market_value_inr)}</b></td>
          <td>${x.sip_flag ? badge((x.sip_status || "SIP"), "blue") + `<div class="dim" style="font-size:11px">${inrShort(x.sip_amount_inr)}/mo</div>` : "—"}</td>
          <td>${x.suitability_checked_flag ? badge("CHECKED", "green") : badge("REVIEW", "amber")}</td>
        </tr>`).join("")}</tbody></table></div></div>
    </div>`;
  }

  // ---- Trade finance
  function tabTrade(c) {
    const ev = c.operations?.trade_finance_events || [];
    if (!ev.length) return `<div class="empty">No trade finance activity.</div>`;
    return `<div class="section"><div class="section-title">Trade Finance Events</div>
      <div class="tbl-wrap"><div class="tbl-scroll"><table class="data">
        <thead><tr><th>Instrument</th><th>Reference</th><th class="num">Amount</th><th>Beneficiary</th><th>Country</th><th>Event</th><th>Expiry</th><th>Status</th></tr></thead>
        <tbody>${ev.map((e) => `<tr>
          <td><b>${esc(e.instrument_type)}</b></td><td class="mono">${esc(e.instrument_ref || "")}</td>
          <td class="num">${inrShort(e.amount_inr)}</td><td>${esc(e.beneficiary_name || "")}</td><td>${esc(e.country_code || "")}</td>
          <td>${fmtDate(e.event_date)}</td><td>${fmtDate(e.expiry_date)}</td><td>${statusBadge(e.status)}</td>
        </tr>`).join("")}</tbody></table></div></div></div>`;
  }

  // ---- CRM 360
  function tabCRM(c) {
    const crm = c.crm || {};
    const inter = (crm.interactions || []).slice().sort((a, b) => (b.interaction_date || "").localeCompare(a.interaction_date || ""));
    const opps = crm.opportunities || [];
    const emails = crm.email_threads || [];
    const meetings = crm.meeting_summaries || [];

    const STAGES = ["Identified", "Qualified", "Proposal", "Negotiation", "Closed"];
    const oppCards = opps.map((o) => {
      const won = /WON/i.test(o.win_loss_status || ""), lost = /LOST/i.test(o.win_loss_status || "");
      const si = Math.max(0, STAGES.findIndex((s) => (o.stage || "").toLowerCase().startsWith(s.toLowerCase())));
      const prod = (DATA.reference.products_catalog.find((p) => p.product_id === o.product_id) || {}).product_name || o.product_id;
      return `<div class="list-card">
        <div class="lc-head"><span class="lc-title">${esc(prod)}</span><span class="ci-val">${inrShort(o.expected_value_inr)}</span></div>
        <div class="lc-meta"><span>${won ? badge("WON", "green") : lost ? badge("LOST", "red") : statusBadge(o.stage)}</span><span><b>Prob</b> ${o.probability_pct != null ? o.probability_pct + "%" : "—"}</span><span><b>Close</b> ${fmtDate(o.expected_close_date)}</span><span><b>Source</b> ${esc(o.source || "")}</span></div>
        <div class="stage-bar">${STAGES.map((s, i) => `<div class="stage-pip ${!lost && i <= si ? "on" : ""}"></div>`).join("")}</div>
        ${o.reason || o.loss_reason_text ? `<div class="lc-body">${esc(o.reason || o.loss_reason_text)}</div>` : ""}
        ${o.suitability_note ? `<div class="lc-body dim" style="font-size:12px">Suitability: ${esc(o.suitability_note)}</div>` : ""}
      </div>`;
    }).join("");

    const meetById = {};
    meetings.forEach((m) => { if (m.interaction_id) meetById[m.interaction_id] = m; });
    const timeline = inter.slice(0, 40).map((i) => {
      const m = meetById[i.interaction_id];
      return `<div class="tl-item">
        <div class="tl-date">${fmtDate(i.interaction_date)} · ${esc(i.channel || "")} · ${esc(i.direction || "")}${i.duration_minutes ? " · " + i.duration_minutes + "m" : ""}</div>
        <div class="tl-title">${esc(i.purpose_code || "Interaction")} → ${esc(i.outcome_code || "")} ${sentimentBadge(i.sentiment_score)}</div>
        <div class="tl-body">${esc(i.note || "")}</div>
        ${m ? `<div class="lc-body dim" style="font-size:12px;margin-top:4px"><b>Meeting:</b> ${esc(m.discussion_summary || m.agenda_text || "")}${m.action_items ? " · <b>Actions:</b> " + esc(Array.isArray(m.action_items) ? m.action_items.join("; ") : m.action_items) : ""}</div>` : ""}
        ${i.next_action_code ? `<div class="tl-body" style="font-size:12px;margin-top:3px">↳ Next: ${esc(i.next_action_code)} by ${fmtDate(i.next_action_due_date)}</div>` : ""}
      </div>`;
    }).join("");

    const emailCards = emails.map((t) => `<div class="list-card">
      <div class="lc-head"><span class="lc-title">✉ ${esc(t.subject)}</span>${statusBadge(t.resolution_status)}</div>
      <div class="lc-meta"><span><b>Started</b> ${fmtDate(t.thread_start_date)}</span><span><b>Messages</b> ${t.message_count}</span><span>${esc(Array.isArray(t.participants) ? t.participants.join(", ") : t.participants || "")}</span></div>
      <div class="lc-body">${esc(t.thread_summary || "")}</div>
    </div>`).join("");

    return `
      <div class="split-2">
        <div class="section">
          <div class="section-title">Opportunities <span class="dim" style="font-weight:600;margin-left:auto">${opps.length}</span></div>
          ${oppCards || `<div class="empty">No opportunities.</div>`}
        </div>
        <div class="section">
          <div class="section-title">Email Threads <span class="dim" style="font-weight:600;margin-left:auto">${emails.length}</span></div>
          ${emailCards || `<div class="empty">No email threads.</div>`}
        </div>
      </div>
      <div class="section">
        <div class="section-title">Interaction &amp; Meeting Timeline <span class="dim" style="font-weight:600;margin-left:auto">${inter.length}</span></div>
        <div class="timeline">${timeline || `<div class="empty">No interactions.</div>`}</div>
      </div>`;
  }
  function sentimentBadge(sc) {
    if (sc == null || sc === "") return "";
    const n = +sc;
    if (n >= 0.3) return badge("Positive", "green");
    if (n <= -0.3) return badge("Negative", "red");
    return badge("Neutral", "gray");
  }

  // ---- Service tickets
  function tabService(c) {
    const t = c.operations?.service_tickets || [];
    if (!t.length) return `<div class="empty">No service tickets.</div>`;
    return `<div class="section"><div class="section-title">Service Tickets &amp; Complaints</div>
      ${t.map((x) => `<div class="list-card">
        <div class="lc-head"><span class="lc-title">${esc(x.category)}${x.sub_category ? " · " + esc(x.sub_category) : ""}</span><span>${x.sla_breach_flag ? badge("SLA BREACH", "red") : statusBadge(x.status)}</span></div>
        <div class="lc-meta"><span class="mono">${esc(x.ticket_id)}</span><span><b>Raised</b> ${fmtDate(x.raised_date)} · ${esc(x.channel || "")}</span><span><b>Priority</b> ${esc(x.priority || "")}</span><span><b>Team</b> ${esc(x.assigned_team || "")}</span>${x.reopened_count ? `<span>${badge("Reopened ×" + x.reopened_count, "amber")}</span>` : ""}</div>
        ${x.complaint_narrative ? `<div class="lc-body">${esc(x.complaint_narrative)}</div>` : ""}
      </div>`).join("")}
    </div>`;
  }

  // ---- Documents
  function tabDocs(c) {
    const d = c.operations?.documents || [];
    if (!d.length) return `<div class="empty">No documents.</div>`;
    return `<div class="section"><div class="section-title">Document Vault</div>
      <div class="tbl-wrap"><div class="tbl-scroll" style="max-height:520px;overflow:auto"><table class="data">
        <thead><tr><th>Title</th><th>Type</th><th>Date</th><th class="num">Pages</th><th>Sensitivity</th></tr></thead>
        <tbody>${d.map((x) => `<tr><td><b>${esc(x.doc_title)}</b><div class="dim mono" style="font-size:11px">${esc(x.doc_id)}</div></td><td>${esc(x.doc_type)}</td><td>${fmtDate(x.doc_date)}</td><td class="num">${esc(x.page_count || "")}</td><td>${statusBadge(x.sensitivity_class)}</td></tr>`).join("")}</tbody>
      </table></div></div></div>`;
  }

  // ---- 6-month arc
  function tabArc(c) {
    const arc = (c.six_month_arc || []).slice();
    if (!arc.length) return `<div class="empty">No relationship arc.</div>`;
    return `<div class="section"><div class="section-title">6-Month Relationship Arc</div>
      <div class="timeline">${arc.map((a) => `<div class="tl-item">
        <div class="tl-date">${fmtDate(a.date)} · ${esc(a.month || "")}</div>
        <div class="tl-title">${esc(a.event_code)}</div>
        <div class="tl-body">${esc(a.narrative || "")}</div>
      </div>`).join("")}</div></div>`;
  }

  // Delegated click handler for dynamically-created "Ledger ›" buttons.
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-view-txn]");
    if (b) go({ tab: "transactions", acct: b.dataset.viewTxn });
  });

  boot();
})();

(() => {
  "use strict";

  const POLL_MS = 3000;
  const STAGES = [
    { id: "stage0_duplicate", label: "Duplicate check" },
    { id: "stage1_gemini", label: "AI assessment" },
    { id: "stage2_deep_forensics", label: "Image forensics" },
    { id: "stage3_rules", label: "Cross-checks" },
  ];
  const ANALYZE_BEATS = [
    { id: "stage0_duplicate", ms: 2500 },
    { id: "stage1_gemini", ms: 22000 },
    { id: "stage2_deep_forensics", ms: 3500 },
    { id: "stage3_rules", ms: 2000 },
  ];

  const LABELS = {
    front_bumper: "Front bumper",
    rear_bumper: "Rear bumper",
    bonnet: "Bonnet",
    boot: "Boot",
    front_left_door: "Front left door",
    front_right_door: "Front right door",
    rear_left_door: "Rear left door",
    rear_right_door: "Rear right door",
    front_left_fender: "Front left fender",
    front_right_fender: "Front right fender",
    rear_left_quarter: "Rear left quarter",
    rear_right_quarter: "Rear right quarter",
    headlight_left: "Left headlight",
    headlight_right: "Right headlight",
    taillight_left: "Left taillight",
    taillight_right: "Right taillight",
    windshield: "Windshield",
    rear_windshield: "Rear windshield",
    mirror_left: "Left mirror",
    mirror_right: "Right mirror",
    grille: "Grille",
    roof: "Roof",
    paint_damage: "Paint damage",
    scratch: "Scratch",
    dent: "Dent",
    crack: "Crack",
    shatter: "Shatter",
    detached: "Detached",
    insured_name_claim_form: "Insured name (claim form)",
    owner_name_rc: "Owner name (RC)",
    holder_name_dl: "Licence holder name",
    registration_number_claim_form: "Registration (claim form)",
    registration_number_rc: "Registration (RC)",
    chassis_number: "Chassis number",
    engine_number: "Engine number",
    vehicle_make_model: "Make and model",
    dl_number: "Licence number",
    dl_expiry_date: "Licence expiry",
    policy_number: "Policy number",
    policy_start_date: "Policy start",
    policy_end_date: "Policy end",
    accident_date: "Date of loss",
    accident_time: "Time of loss",
    accident_location: "Location of loss",
    accident_description: "Accident description",
    source_doc: "Source document",
    dl_expired_at_loss: "Licence expired at time of loss",
    name_mismatch: "Name mismatch across documents",
    registration_mismatch: "Registration number mismatch",
    policy_not_active: "Policy not active at date of loss",
    damage_narrative_mismatch: "Damage does not match the narrative",
    no_damage_detected: "No damage detected",
    not_a_vehicle: "Image is not a vehicle",
    duplicate: "Duplicate check",
    ela: "Error level analysis",
    exif: "EXIF metadata",
    clean: "Clean",
    suspicious: "Suspicious",
    fail: "Failed",
    not_applicable: "Not applicable",
    minor: "Minor",
    moderate: "Moderate",
    severe: "Severe",
    HIGH: "High",
    MEDIUM: "Medium",
    analysed: "Analysed",
    rejected: "Rejected",
    error: "Escalated",
    stage0_duplicate: "Duplicate check",
    stage1_gemini: "AI assessment",
    stage2_deep_forensics: "Image forensics",
    stage3_rules: "Cross-checks",
  };

  const store = {
    order: [],
    byId: {},
    selected: null,
    newIds: new Set(),
    analyzing: false,
    analyzeTimer: null,
    analyzeStep: 0,
    docs: [],
    photos: [],
    imgMode: {},
  };

  const $ = (id) => document.getElementById(id);

  function esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function labelOf(key) {
    if (key == null || key === "") return "—";
    const raw = String(key);
    if (LABELS[raw]) return LABELS[raw];
    return raw
      .replace(/[_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function asArray(v) {
    return Array.isArray(v) ? v : [];
  }

  function asObj(v) {
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  }

  function inr(n) {
    const x = Number(n);
    if (!Number.isFinite(x)) return "—";
    return "₹" + Math.round(x).toLocaleString("en-IN");
  }

  function relTime(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 45) return "just now";
    if (s < 90) return "1 min ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 5400) return "1 hr ago";
    if (s < 86400) return Math.round(s / 3600) + " hr ago";
    return Math.round(s / 86400) + " d ago";
  }

  function confPct(c) {
    const n = Number(c);
    if (!Number.isFinite(n)) return null;
    const pct = n <= 1 ? n * 100 : n;
    return Math.round(pct) + "%";
  }

  function showEl(el, on) {
    if (!el) return;
    el.classList.toggle("hidden", !on);
  }

  function setBannerError(msg) {
    const el = $("banner-error");
    if (!msg) {
      showEl(el, false);
      el.textContent = "";
      return;
    }
    el.textContent = msg;
    showEl(el, true);
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      const detail = data && (data.detail || data.message);
      throw new Error(detail ? String(detail) : "HTTP " + res.status + " from " + path);
    }
    return data;
  }

  function estimateSummary(claim) {
    const est = asObj(claim.estimate);
    const low = Number(est.total_low);
    const high = Number(est.total_high);
    if (Number.isFinite(low) && Number.isFinite(high) && (low > 0 || high > 0)) {
      return inr(low) + " – " + inr(high);
    }
    return null;
  }

  function rejectionLine(claim) {
    const stop = claim.stopped_at;
    if (stop === "duplicate") return "Rejected — duplicate photo detected";
    if (stop === "not_a_vehicle") return "Rejected — image is not a vehicle";
    if (stop === "ai_unavailable") return "Analysis unavailable — escalated to human review";
    if (claim.status === "rejected") return "Rejected — independent checks did not pass";
    if (claim.status === "error") return "Analysis unavailable — escalated to human review";
    return null;
  }

  function cardSummary(claim) {
    const reject = rejectionLine(claim);
    if (claim.status === "rejected" || claim.status === "error") {
      return reject || "Stopped before a full assessment";
    }
    return estimateSummary(claim) || reject || "Awaiting priced estimate";
  }

  function hydrateStub(item) {
    return {
      claim_id: item.claim_id,
      created_at: item.created_at,
      status: item.status || "analysed",
      stopped_at: item.stopped_at ?? null,
      timings: asArray(item.timings),
      vehicle_description: item.vehicle_description || "",
      fields: asArray(item.fields),
      damage: asArray(item.damage),
      forensics: asArray(item.forensics),
      flags: asArray(item.flags),
      estimate: asObj(item.estimate),
      _partial: item.status == null,
    };
  }

  async function ensureFull(claimId) {
    const cached = store.byId[claimId];
    if (cached && !cached._partial) return cached;
    const payload = await api("/api/claims/" + encodeURIComponent(claimId));
    store.byId[claimId] = normalize(payload);
    return store.byId[claimId];
  }

  function normalize(raw) {
    const c = asObj(raw);
    return {
      claim_id: c.claim_id || "UNKNOWN",
      created_at: c.created_at || "",
      status: c.status || "analysed",
      stopped_at: c.stopped_at ?? null,
      timings: asArray(c.timings),
      vehicle_description: c.vehicle_description || "",
      fields: asArray(c.fields),
      damage: asArray(c.damage),
      forensics: asArray(c.forensics),
      flags: asArray(c.flags),
      estimate: asObj(c.estimate),
      _partial: false,
    };
  }

  function reachedStages(claim) {
    const timings = asArray(claim.timings);
    const byId = {};
    timings.forEach((t) => {
      if (t && t.stage) byId[t.stage] = Number(t.seconds);
    });
    const stop = claim.stopped_at;
    return STAGES.map((s, i) => {
      let ran = Object.prototype.hasOwnProperty.call(byId, s.id);
      let seconds = ran && Number.isFinite(byId[s.id]) ? byId[s.id] : null;
      if (!timings.length) {
        if (stop === "duplicate") ran = i === 0;
        else if (stop === "not_a_vehicle" || stop === "ai_unavailable") ran = i <= 1;
        else if (claim.status === "analysed") ran = true;
        else ran = i === 0;
        seconds = ran ? seconds : null;
      }
      return { ...s, ran, seconds };
    });
  }

  function renderFeed() {
    const root = $("feed");
    if (!store.order.length) {
      root.innerHTML = '<div class="feed-empty">No claims yet. Upload a file or wait for a WhatsApp photo.</div>';
      return;
    }
    root.innerHTML = store.order
      .map((id) => {
        const c = store.byId[id] || { claim_id: id, status: "analysed" };
        const selected = store.selected === id ? " is-selected" : "";
        const fresh = store.newIds.has(id) ? " is-new" : "";
        const status = c.status || "analysed";
        return (
          '<button type="button" class="claim-card status-' +
          esc(status) +
          selected +
          fresh +
          '" data-id="' +
          esc(id) +
          '">' +
          '<div class="claim-card-top">' +
          '<span class="claim-id">' +
          esc(c.claim_id) +
          "</span>" +
          '<span class="claim-time">' +
          esc(relTime(c.created_at)) +
          "</span></div>" +
          '<span class="pill pill-' +
          esc(status) +
          '">' +
          esc(labelOf(status)) +
          "</span>" +
          '<p class="claim-summary">' +
          esc(cardSummary(c)) +
          "</p></button>"
        );
      })
      .join("");
  }

  function verdictBlock(claim) {
    const status = claim.status || "analysed";
    let title = "Analysed — estimate ready";
    let cls = "verdict--analysed";
    let sub = "";
    if (status === "rejected") {
      cls = "verdict--rejected";
      title = rejectionLine(claim) || "Rejected";
      if (claim.stopped_at === "not_a_vehicle" && claim.vehicle_description) {
        sub = claim.vehicle_description;
      }
    } else if (status === "error") {
      cls = "verdict--error";
      title = "Analysis unavailable — escalated to human review";
    }
    const stages = reachedStages(claim);
    const dup = stages.find((s) => s.id === "stage0_duplicate");
    let skip = "";
    if (claim.stopped_at === "duplicate") {
      const sec = dup && Number.isFinite(dup.seconds) ? dup.seconds.toFixed(2) + "s" : "the first stage";
      skip =
        '<p class="skip-callout">Rejected in ' +
        esc(sec) +
        " — no AI analysis required.</p>";
    }
    return (
      '<section class="verdict ' +
      cls +
      '"><h2>' +
      esc(title) +
      "</h2>" +
      (sub ? "<p>" + esc(sub) + "</p>" : "") +
      skip +
      "</section>"
    );
  }

  function pipelineBlock(claim) {
    const stages = reachedStages(claim);
    const max = Math.max(
      0.08,
      ...stages.map((s) => (s.ran && Number.isFinite(s.seconds) ? s.seconds : 0))
    );
    const cells = stages
      .map((s) => {
        const dur = s.ran && Number.isFinite(s.seconds) ? s.seconds : null;
        const flex = s.ran ? Math.max(dur == null ? 1 : dur, 0.06) : 0.35;
        const widthPct = s.ran && dur != null ? Math.max(8, (dur / max) * 100) : s.ran ? 40 : 0;
        const meta = s.ran
          ? '<span class="tick">✓</span> ' + (dur == null ? "ran" : dur.toFixed(2) + "s")
          : "Not reached";
        return (
          '<div class="pipe-stage' +
          (s.ran ? "" : " is-unreached") +
          '" style="flex:' +
          flex +
          ' 1 0">' +
          '<div class="pipe-name">' +
          esc(s.label) +
          "</div>" +
          '<div class="pipe-meta">' +
          meta +
          "</div>" +
          '<div class="pipe-bar"><span style="width:' +
          widthPct +
          '%"></span></div></div>'
        );
      })
      .join("");
    return (
      '<section class="section"><h3>03 · Pipeline trace</h3>' +
      '<p class="quiet" style="margin:0 0 10px">Cheap checks run first. A duplicate dies here — the expensive model is never called.</p>' +
      '<div class="pipeline">' +
      cells +
      "</div></section>"
    );
  }

  function elaEntries(claim) {
    return asArray(claim.forensics).filter((f) => f && f.check === "ela");
  }

  function imagesBlock(claim) {
    const elas = elaEntries(claim);
    const withSrc = elas.filter((f) => f.original_url || f.heatmap_url);
    if (!withSrc.length) {
      return (
        '<div class="card"><h4>Source imagery</h4>' +
        '<p class="muted-note">No forensic stills. This claim stopped before image forensics, or the photos were not JPEG.</p></div>'
      );
    }
    const blocks = withSrc
      .map((f, i) => {
        const key = "img-" + i;
        const mode = store.imgMode[claim.claim_id + key] || "split";
        const orig = f.original_url;
        const heat = f.heatmap_url;
        let body = "";
        if (orig && heat && (mode === "split" || !mode)) {
          body =
            '<div class="img-split"><div class="img-frame"><img src="' +
            esc(orig) +
            '" alt="Original"></div><div class="img-frame"><img src="' +
            esc(heat) +
            '" alt="ELA heatmap"></div></div>';
        } else if (mode === "heat" && heat) {
          body = '<div class="img-frame"><img src="' + esc(heat) + '" alt="ELA heatmap"></div>';
        } else if (orig) {
          body = '<div class="img-frame"><img src="' + esc(orig) + '" alt="Original"></div>';
        } else if (heat) {
          body = '<div class="img-frame"><img src="' + esc(heat) + '" alt="ELA heatmap"></div>';
        }
        const toggles = heat
          ? '<div class="toggle">' +
            btn("orig", "Original", mode === "orig") +
            btn("heat", "ELA heatmap", mode === "heat") +
            btn("split", "Side by side", mode === "split" || !mode) +
            "</div>"
          : "";
        function btn(m, text, on) {
          return (
            '<button type="button" class="' +
            (on ? "is-on" : "") +
            '" data-img-mode="' +
            m +
            '" data-img-key="' +
            key +
            '">' +
            text +
            "</button>"
          );
        }
        return toggles + body;
      })
      .join("");
    return '<div class="card"><h4>Source imagery</h4>' + blocks + "</div>";
  }

  function fieldsBlock(claim) {
    const fields = asArray(claim.fields);
    if (!fields.length) {
      return '<div class="card"><h4>Extracted fields</h4><p class="muted-note">No fields extracted — documents were absent or this claim exited early.</p></div>';
    }
    const rows = fields
      .map((f) => {
        const empty = f == null || f.value == null || String(f.value).trim() === "" || String(f.value).toLowerCase() === "null";
        const src = f && f.source_doc ? f.source_doc : "uncited";
        const conf = f ? confPct(f.confidence) : null;
        return (
          '<div class="field-row"><div><div class="field-name">' +
          esc(labelOf(f && f.name)) +
          '</div><div class="field-value' +
          (empty ? " is-null" : "") +
          '">' +
          esc(empty ? "Not found in documents" : f.value) +
          '</div></div>' +
          (conf ? '<div class="quiet">' + esc(conf) + "</div>" : "") +
          '<div class="field-cite">Source · ' +
          esc(src) +
          "</div></div>"
        );
      })
      .join("");
    return '<div class="card"><h4>Extracted fields</h4>' + rows + "</div>";
  }

  function priceForPart(claim, part) {
    const items = asArray(asObj(claim.estimate).line_items);
    const hit = items.find((x) => x && x.part === part);
    if (!hit) return null;
    return inr(hit.low) + " – " + inr(hit.high);
  }

  function damageBlock(claim) {
    const damage = asArray(claim.damage);
    if (!damage.length) {
      return '<div class="card"><h4>Observed damage</h4><p class="muted-note">No damaged parts recorded.</p></div>';
    }
    const rows = damage
      .map((d) => {
        if (!d || typeof d !== "object") return "";
        const price = priceForPart(claim, d.part);
        return (
          '<div class="damage-row"><div class="damage-top"><span class="damage-part">' +
          esc(labelOf(d.part)) +
          '</span><span class="pill pill-' +
          esc(d.severity || "minor") +
          '">' +
          esc(labelOf(d.severity)) +
          "</span><span class="quiet">" +
          esc(labelOf(d.damage_type)) +
          "</span></div>" +
          (d.reasoning ? '<p class="damage-reason">' + esc(d.reasoning) + "</p>" : "") +
          (d.source_photo ? '<div class="field-cite">Photo · ' + esc(d.source_photo) + "</div>" : "") +
          (price ? '<div class="damage-price">' + esc(price) + "</div>" : '<div class="quiet">No rate-card match</div>') +
          "</div>"
        );
      })
      .join("");
    return '<div class="card"><h4>Observed damage</h4>' + rows + "</div>";
  }

  function estimateBlock(claim) {
    const est = asObj(claim.estimate);
    const low = est.total_low;
    const high = est.total_high;
    const survey = est.requires_survey
      ? '<div class="survey">' + esc(est.survey_reason || "A survey is required under IRDAI rules.") + "</div>"
      : "";
    const unpriced = asArray(est.unpriced);
    const extra = unpriced.length
      ? '<p class="quiet" style="margin-top:10px">Unpriced parts: ' +
        esc(unpriced.map((u) => labelOf(u && u.part)).join(", ")) +
        "</p>"
      : "";
    return (
      '<div class="card"><h4>Reserve estimate</h4>' +
      '<div class="estimate-total">' +
      esc(inr(low) + " – " + inr(high)) +
      "</div>" +
      '<p class="quiet">Indicative range from the published rate card. Every line is joined to an observed part.</p>' +
      survey +
      extra +
      "</div>"
    );
  }

  function flagsBlock(claim) {
    const flags = asArray(claim.flags);
    if (!flags.length) {
      return (
        '<section class="section"><h3>05 · Cross-check flags</h3>' +
        '<div class="card"><p class="muted-note">No inconsistencies detected</p></div></section>'
      );
    }
    const cards = flags
      .map((f) => {
        if (!f || typeof f !== "object") return "";
        const chips = asArray(f.evidence)
          .map((e) => '<span class="chip">' + esc(labelOf(e)) + "</span>")
          .join("");
        return (
          '<div class="card"><span class="pill pill-' +
          esc(f.severity || "MEDIUM") +
          '">' +
          esc(labelOf(f.severity)) +
          "</span> <span class="quiet">" +
          esc(labelOf(f.rule)) +
          "</span>" +
          '<p class="flag-msg">' +
          esc(f.message || "") +
          "</p>" +
          chips +
          "</div>"
        );
      })
      .join("");
    return '<section class="section"><h3>05 · Cross-check flags</h3>' + cards + "</section>";
  }

  function forensicsBlock(claim) {
    const items = asArray(claim.forensics);
    if (!items.length) {
      return (
        '<section class="section"><h3>06 · Image forensics</h3>' +
        '<div class="card"><p class="muted-note">No forensic checks recorded — this claim did not reach that stage.</p></div></section>'
      );
    }
    const cards = items
      .map((f) => {
        if (!f || typeof f !== "object") return "";
        const lat = f.lat;
        const lon = f.lon;
        const gps =
          lat != null && lon != null
            ? '<p class="quiet" style="margin:8px 0 0">GPS ' +
              esc(lat) +
              ", " +
              esc(lon) +
              ' · <a class="maps-link" target="_blank" rel="noopener noreferrer" href="https://www.google.com/maps?q=' +
              encodeURIComponent(lat) +
              "," +
              encodeURIComponent(lon) +
              '">Open in Google Maps</a></p>'
            : "";
        return (
          '<div class="card"><div class="damage-top"><strong>' +
          esc(labelOf(f.check)) +
          '</strong><span class="pill pill-' +
          esc(f.verdict || "clean") +
          '">' +
          esc(labelOf(f.verdict)) +
          "</span></div>" +
          '<p class="damage-reason">' +
          esc(f.detail || "") +
          "</p>" +
          gps +
          "</div>"
        );
      })
      .join("");
    return '<section class="section"><h3>06 · Image forensics</h3>' + cards + "</section>";
  }

  function renderDetail(claim) {
    if (!claim) return;
    $("detail").innerHTML =
      verdictBlock(claim) +
      pipelineBlock(claim) +
      '<section class="section"><h3>04 · Evidence and findings</h3><div class="two-col"><div>' +
      imagesBlock(claim) +
      "</div><div>" +
      fieldsBlock(claim) +
      damageBlock(claim) +
      estimateBlock(claim) +
      "</div></div></section>" +
      flagsBlock(claim) +
      forensicsBlock(claim);
  }

  function showDetail(claim) {
    showEl($("analyzing"), false);
    showEl($("detail-empty"), !claim);
    showEl($("detail"), !!claim);
    if (claim) renderDetail(claim);
  }

  async function selectClaim(id, opts) {
    const silent = opts && opts.silent;
    store.selected = id;
    renderFeed();
    if (!id) {
      showDetail(null);
      return;
    }
    try {
      const full = await ensureFull(id);
      if (store.selected !== id) return;
      showDetail(full);
      setBannerError(null);
    } catch (err) {
      if (!silent) setBannerError("Could not load claim " + id + ". " + err.message);
    }
  }

  async function pollClaims() {
    try {
      const list = await api("/api/claims");
      const items = asArray(list);
      const ids = items.map((x) => x && x.claim_id).filter(Boolean);
      const known = new Set(store.order);
      const newcomers = ids.filter((id) => !known.has(id));

      for (const item of items) {
        if (!item || !item.claim_id) continue;
        if (!store.byId[item.claim_id]) {
          store.byId[item.claim_id] = hydrateStub(item);
        } else if (store.byId[item.claim_id]._partial) {
          store.byId[item.claim_id] = {
            ...store.byId[item.claim_id],
            flags: asArray(item.flags),
            estimate: asObj(item.estimate),
            created_at: item.created_at || store.byId[item.claim_id].created_at,
          };
        }
      }

      for (const id of newcomers) {
        store.newIds.add(id);
        setTimeout(() => {
          store.newIds.delete(id);
          renderFeed();
        }, 2600);
        try {
          await ensureFull(id);
        } catch (_) {
          /* keep stub */
        }
      }

      Object.keys(store.byId).forEach((id) => {
        if (!ids.includes(id)) delete store.byId[id];
      });
      store.order = ids;
      if (store.selected && !ids.includes(store.selected)) {
        store.selected = ids[0] || null;
      }

      $("feed-error").classList.add("hidden");
      renderFeed();

      if (!store.analyzing) {
        if (newcomers.length) {
          await selectClaim(newcomers[0]);
        } else if (!store.selected && ids.length) {
          await selectClaim(ids[0], { silent: true });
        } else if (!ids.length) {
          store.selected = null;
          showDetail(null);
        }
      }
      setBannerError(null);
    } catch (err) {
      const el = $("feed-error");
      el.textContent = "Live feed unreachable. Claims will not appear until the API is back. " + err.message;
      el.classList.remove("hidden");
    }
  }

  async function pingHealth() {
    const dot = document.querySelector("#health .pulse-dot");
    const label = $("health-label");
    try {
      await api("/api/health");
      dot.classList.remove("pulse-dot--off", "pulse-dot--bad");
      label.textContent = "API live";
    } catch (err) {
      dot.classList.add("pulse-dot--bad");
      dot.classList.remove("pulse-dot--off");
      label.textContent = "API unreachable";
      setBannerError("Cannot reach the ClaimPilot API. Check that the server is running. " + err.message);
    }
  }

  function renderAnalyzeSteps(activeIdx) {
    $("analyze-steps").innerHTML = STAGES.map((s, i) => {
      const state = i < activeIdx ? "is-done" : i === activeIdx ? "is-active" : "";
      const mark = i < activeIdx ? "✓" : String(i + 1);
      return (
        '<li class="' +
        state +
        '"><span class="step-mark">' +
        mark +
        "</span><span>" +
        esc(s.label) +
        (i === activeIdx ? " — in progress" : i < activeIdx ? " — complete" : " — waiting") +
        "</span></li>"
      );
    }).join("");
  }

  function startAnalyzeProgress() {
    store.analyzing = true;
    store.analyzeStep = 0;
    showEl($("detail-empty"), false);
    showEl($("detail"), false);
    showEl($("analyzing"), true);
    renderAnalyzeSteps(0);
    const tick = () => {
      const beat = ANALYZE_BEATS[store.analyzeStep];
      store.analyzeTimer = setTimeout(() => {
        if (!store.analyzing) return;
        if (store.analyzeStep < ANALYZE_BEATS.length - 1) {
          store.analyzeStep += 1;
          renderAnalyzeSteps(store.analyzeStep);
          tick();
        }
      }, beat ? beat.ms : 4000);
    };
    tick();
  }

  function stopAnalyzeProgress() {
    store.analyzing = false;
    if (store.analyzeTimer) {
      clearTimeout(store.analyzeTimer);
      store.analyzeTimer = null;
    }
    showEl($("analyzing"), false);
  }

  function fileChip(file) {
    const wrap = document.createElement("div");
    wrap.className = "thumb";
    if (file.type && file.type.startsWith("image/")) {
      const img = document.createElement("img");
      img.alt = file.name;
      img.src = URL.createObjectURL(file);
      wrap.appendChild(img);
    } else {
      wrap.textContent = (file.name.split(".").pop() || "file").slice(0, 4);
      wrap.title = file.name;
    }
    return wrap;
  }

  function renderThumbs() {
    const docs = $("thumbs-docs");
    const photos = $("thumbs-photos");
    docs.innerHTML = "";
    photos.innerHTML = "";
    store.docs.forEach((f) => docs.appendChild(fileChip(f)));
    store.photos.forEach((f) => photos.appendChild(fileChip(f)));
  }

  function setupDropzone(zoneEl, input, bucketKey) {
    const take = (fileList) => {
      const next = Array.from(fileList || []);
      store[bucketKey] = store[bucketKey].concat(next);
      renderThumbs();
    };
    input.addEventListener("change", () => {
      take(input.files);
      input.value = "";
    });
    ["dragenter", "dragover"].forEach((ev) => {
      zoneEl.addEventListener(ev, (e) => {
        e.preventDefault();
        zoneEl.classList.add("is-hot");
      });
    });
    ["dragleave", "drop"].forEach((ev) => {
      zoneEl.addEventListener(ev, (e) => {
        e.preventDefault();
        zoneEl.classList.remove("is-hot");
      });
    });
    zoneEl.addEventListener("drop", (e) => take(e.dataTransfer.files));
  }

  async function analyzeClaim(e) {
    e.preventDefault();
    const err = $("upload-error");
    err.classList.add("hidden");
    if (!store.docs.length && !store.photos.length) {
      err.textContent = "Add at least one document or damage photo.";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("btn-analyze");
    btn.disabled = true;
    startAnalyzeProgress();
    const fd = new FormData();
    store.docs.forEach((f) => fd.append("documents", f));
    store.photos.forEach((f) => fd.append("photos", f));
    try {
      const payload = await api("/api/analyze", { method: "POST", body: fd });
      const claim = normalize(payload);
      store.byId[claim.claim_id] = claim;
      if (!store.order.includes(claim.claim_id)) {
        store.order.unshift(claim.claim_id);
      }
      store.newIds.add(claim.claim_id);
      setTimeout(() => {
        store.newIds.delete(claim.claim_id);
        renderFeed();
      }, 2600);
      stopAnalyzeProgress();
      await selectClaim(claim.claim_id);
      store.docs = [];
      store.photos = [];
      renderThumbs();
    } catch (ex) {
      stopAnalyzeProgress();
      err.textContent = "Analyse failed. The API did not return a claim. " + ex.message;
      err.classList.remove("hidden");
      if (store.selected) showDetail(store.byId[store.selected]);
      else showDetail(null);
    } finally {
      btn.disabled = false;
    }
  }

  async function resetDemo() {
    if (!window.confirm("Clear every stored claim and perceptual hash? Repeat photos will no longer count as duplicates until they are analysed again.")) {
      return;
    }
    try {
      const data = await api("/api/demo/reset", { method: "POST" });
      if (!data || !data.cleared) throw new Error("Reset was not confirmed by the server.");
      store.order = [];
      store.byId = {};
      store.selected = null;
      store.docs = [];
      store.photos = [];
      renderThumbs();
      renderFeed();
      showDetail(null);
      setBannerError(null);
    } catch (err) {
      setBannerError("Reset failed. " + err.message);
    }
  }

  function onFeedClick(e) {
    const card = e.target.closest(".claim-card");
    if (!card) return;
    selectClaim(card.getAttribute("data-id"));
  }

  function onDetailClick(e) {
    const btn = e.target.closest("[data-img-mode]");
    if (!btn || !store.selected) return;
    const key = store.selected + btn.getAttribute("data-img-key");
    store.imgMode[key] = btn.getAttribute("data-img-mode");
    renderDetail(store.byId[store.selected]);
  }

  function boot() {
    setupDropzone(document.querySelector('[data-zone="documents"]'), $("input-docs"), "docs");
    setupDropzone(document.querySelector('[data-zone="photos"]'), $("input-photos"), "photos");
    $("upload-form").addEventListener("submit", analyzeClaim);
    $("btn-reset").addEventListener("click", resetDemo);
    $("feed").addEventListener("click", onFeedClick);
    $("detail").addEventListener("click", onDetailClick);
    pingHealth();
    pollClaims();
    setInterval(pingHealth, 10000);
    setInterval(pollClaims, POLL_MS);
  }

  boot();
})();

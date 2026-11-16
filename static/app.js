const form = document.getElementById("item-form");
const grid = document.getElementById("items-grid");
const message = document.getElementById("form-message");
const modal = document.getElementById("modal");
const modalContent = document.getElementById("modal-content");

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[char]));
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Request failed.");
  return data;
}

function openModal(html) {
  modal.classList.remove("hidden");
  modalContent.innerHTML = html;
}

async function loadStats() {
  try {
    const stats = await api("/api/stats");
    document.getElementById("lost-count").textContent = stats.lost.total;
    document.getElementById("found-count").textContent = stats.found.total;
    document.getElementById("lost-active").textContent = `Active: ${stats.lost.active}`;
    document.getElementById("found-active").textContent = `Active: ${stats.found.active}`;
    document.getElementById("resolved-count").textContent = stats.lost.resolved + stats.found.resolved;
  } catch (error) { console.error(error); }
}

function itemCard(item) {
  const image = item.image_url
    ? `<img class="item-image" src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.title)}">`
    : `<div class="item-image">No image uploaded</div>`;
  const date = item.event_date ? escapeHtml(item.event_date) : "Date not specified";
  const resolved = item.status === "resolved";
  return `<article class="item-card">
    ${image}
    <div class="item-body">
      <div class="item-meta"><span class="pill ${item.item_type}">${escapeHtml(item.item_type)}</span><span class="pill">${escapeHtml(item.category)}</span></div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.description)}</p>
      <p>⌖ ${escapeHtml(item.location)} · ${date}</p>
      <p><strong>Status:</strong> ${resolved ? "Resolved" : "Active"}</p>
      <div class="item-actions">
        <button class="small-button" onclick="showMatches('${item.id}')">Find matches</button>
        ${resolved ? '<span class="pill">Resolved</span>' : `<button class="small-button" onclick="claimItem('${item.id}', '${item.item_type}')">${item.item_type === "lost" ? "I Found This Item" : "Claim Ownership"}</button>`}
        <button class="small-button" onclick="manageItem('${item.id}')">Manage report</button>
      </div>
    </div>
  </article>`;
}

async function loadItems() {
  grid.innerHTML = '<p class="empty-state">Loading reports...</p>';
  const params = new URLSearchParams();
  const type = document.getElementById("type-filter").value;
  const category = document.getElementById("category-filter").value;
  const query = document.getElementById("search-input").value.trim();
  if (type) params.set("item_type", type);
  if (category) params.set("category", category);
  if (query) params.set("q", query);
  try {
    const items = await api(`/api/items?${params.toString()}`);
    grid.innerHTML = items.length ? items.map(itemCard).join("") :
      '<p class="empty-state">No reports found. Try changing your filters or submit a new report.</p>';
  } catch (error) {
    grid.innerHTML = `<p class="empty-state">${escapeHtml(error.message)} Check your MongoDB connection.</p>`;
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.getElementById("submit-button");
  button.disabled = true;
  button.textContent = "Submitting...";
  message.textContent = "";
  try {
    const response = await api("/api/items", { method: "POST", body: new FormData(form) });
    form.reset();
    message.textContent = response.warning ? `${response.message} Warning: ${response.warning}` : response.message;
    openModal(`<p class="eyebrow">REPORT CREATED</p>
      <h2>Save your resolution key</h2>
      <p>This key is the only way to review claims and resolve this report. FindIt cannot recover it. Do not share it with claimants.</p>
      <div class="key-box"><code id="new-resolution-key">${escapeHtml(response.resolution_key)}</code></div>
      <div class="item-actions">
        <button type="button" class="primary-button" onclick="copyResolutionKey()">Copy key</button>
        <button type="button" class="small-button" onclick="downloadResolutionKey()">Download .txt</button>
      </div>
      <p id="resolution-key-feedback" class="modal-note" role="status" aria-live="polite"></p>
      <p class="modal-note">Keep this key somewhere safe. The public report ID is not sufficient to resolve the item.</p>`);
    window.currentNewResolutionKey = response.resolution_key;
    window.currentResolutionReportId = response.item?.id || "report";
    await Promise.all([loadItems(), loadStats()]);
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Submit report";
  }
});

window.copyResolutionKey = async function () {
  if (!window.currentNewResolutionKey) return;
  const feedback = document.getElementById("resolution-key-feedback");
  try {
    await navigator.clipboard.writeText(window.currentNewResolutionKey);
    if (feedback) feedback.textContent = "Resolution key copied to clipboard.";
  } catch {
    if (feedback) feedback.textContent = "Copy was blocked. Select the key above and copy it manually.";
  }
};

window.downloadResolutionKey = function () {
  if (!window.currentNewResolutionKey) return;
  const reportId = window.currentResolutionReportId || "report";
  const contents = [
    "FindIt - Private Resolution Key",
    `Report ID: ${reportId}`,
    `Resolution key: ${window.currentNewResolutionKey}`,
    "Keep this key private. Anyone with the key can manage and resolve this report."
  ].join("\n");
  const blob = new Blob([contents], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `findit-resolution-key-${reportId}.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoke after the browser has had time to start the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);

  const feedback = document.getElementById("resolution-key-feedback");
  if (feedback) feedback.textContent = "Download started: your resolution key is being saved as a .txt file.";
};

window.claimItem = function (id, itemType) {
  const isLostReport = itemType === "lost";
  const heading = isLostReport ? "I Found This Item" : "Claim Ownership";
  const explanation = isLostReport
    ? "Where and when did you find this item?"
    : "Why do you believe this item belongs to you?";
  const submitLabel = isLostReport ? "Submit finder details" : "Submit ownership claim";

  openModal(`<p class="eyebrow">${isLostReport ? "ITEM FOUND" : "OWNERSHIP CLAIM"}</p><h2>${heading}</h2>
    <p class="modal-note">Your contact details and explanation will be shared with the reporter through the claim record. After submission, the reporter's contact details will be shown to you so you can discuss outside FindIt.</p>
    <form id="claim-form" class="form-grid">
      <label>Your name *<input name="claimant_name" required></label>
      <label>Your email *<input name="claimant_email" type="email" required></label>
      <label class="wide">Phone (optional)<input name="claimant_phone" type="tel"></label>
      <label class="wide">${explanation} *<textarea name="message" rows="4" required></textarea></label>
      <div class="wide"><button class="primary-button" type="submit">${submitLabel}</button></div>
    </form><p id="claim-message" role="status"></p>`);
  document.getElementById("claim-form").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const result = await api(`/api/items/${id}/claims`, { method: "POST", body: new FormData(event.currentTarget) });
      const contact = result.reporter_contact || {};
      openModal(`<p class="eyebrow">${isLostReport ? "FINDER DETAILS SUBMITTED" : "CLAIM SUBMITTED"}</p><h2>Contact the reporter</h2>
        <p>${escapeHtml(result.message)}</p>
        <div class="contact-card"><strong>${escapeHtml(contact.name || "Reporter")}</strong>
          <p>Email: ${escapeHtml(contact.email || "Not provided")}</p>
          <p>Phone: ${escapeHtml(contact.phone || "Not provided")}</p></div>
        <p class="modal-note">Arrange verification and handover directly. FindIt does not verify ownership or mediate the exchange.</p>`);
    } catch (error) {
      document.getElementById("claim-message").textContent = error.message;
      submit.disabled = false;
    }
  });
};

window.manageItem = function (id) {
  openModal(`<p class="eyebrow">REPORT OWNER</p>
    <h2>Manage report</h2>
    <p class="modal-note">Enter the private resolution key you received when submitting this report.</p>
    <form id="manage-form" class="form-grid">
      <label class="wide">Resolution key
        <input name="resolution_key" type="text" autocomplete="off" autocapitalize="characters" spellcheck="false" required placeholder="FIND-XXXX-XXXX-XXXX">
      </label>
      <div class="wide">
        <button class="primary-button" type="submit">View report</button>
      </div>
    </form>
    <p id="manage-message" role="status"></p>`);

  document.getElementById("manage-form").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button[type=submit]");
    const key = new FormData(event.currentTarget).get("resolution_key").trim();
    submit.disabled = true;
    submit.textContent = "Checking...";
    try {
      const manageBody = new FormData();
      manageBody.append("resolution_key", key);
      const data = await api(`/api/items/${id}/manage`, { method: "POST", body: manageBody });
      const claims = data.claims || [];
      openModal(`<p class="eyebrow">REPORT OWNER</p><h2>Manage report</h2>
        <p><strong>Status:</strong> ${escapeHtml(data.status)}</p>
        <h3>Ownership claims (${claims.length})</h3>
        ${claims.length ? claims.map(claim => `<div class="claim-card">
          <strong>${escapeHtml(claim.claimant_name)}</strong>
          <p>Email: ${escapeHtml(claim.claimant_email)}</p><p>Phone: ${escapeHtml(claim.claimant_phone || "Not provided")}</p>
          <p>${escapeHtml(claim.message)}</p><small>${escapeHtml(new Date(claim.created_at).toLocaleString())}</small>
        </div>`).join("") : '<p class="modal-note">No ownership claims have been submitted.</p>'}
        ${data.status === "active" ? '<button class="primary-button" id="resolve-confirm-button">Mark this report as resolved</button>' : '<p class="pill">This report is already resolved</p>'}`);
      const resolveButton = document.getElementById("resolve-confirm-button");
      if (resolveButton) resolveButton.addEventListener("click", async () => {
        if (!confirm("Resolve this report? New claims will no longer be accepted.")) return;
        const body = new FormData();
        body.append("resolution_key", key);
        try {
          const result = await api(`/api/items/${id}/resolve`, { method: "POST", body });
          openModal(`<h2>Report resolved</h2><p>${escapeHtml(result.message)}</p>`);
          await Promise.all([loadItems(), loadStats()]);
        } catch (error) { alert(error.message); }
      });
    } catch (error) {
      const errorMessage = document.getElementById("manage-message");
      if (errorMessage) errorMessage.textContent = error.message;
      submit.disabled = false;
      submit.textContent = "View report";
    }
  });
};

async function showMatches(id) {
  openModal("<h2>Finding possible matches...</h2>");
  try {
    const data = await api(`/api/items/${id}/matches`);
    const matches = data.matching_items;
    modalContent.innerHTML = `<p class="eyebrow">MULTIMODAL SIMILARITY RESULTS</p>
      <h2>Potential matches</h2>
      <p class="modal-note">The ranking combines MobileNetV2 image features, word/character TF-IDF and item metadata. Scores are similarity rankings, not probabilities.</p>
      ${matches.length ? matches.map(item => {
        const details = item.match_details || {};
        const parts = Object.entries(details).map(([key, value]) => `${key.replace("_similarity", "")}: ${value}%`).join(" · ");
        return `<div class="match-row">
          ${item.image_url ? `<img src="${escapeHtml(item.image_url)}" alt="">` : '<div class="item-image" style="width:72px;height:72px">No image</div>'}
          <div><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(item.category)} · ${escapeHtml(item.location)}</p>
          <p>${escapeHtml(item.description)}</p><span class="score">${item.match_score}% overall similarity</span>
          <p class="match-breakdown">${escapeHtml(parts)}</p></div>
        </div>`;
      }).join("") : "<p>No likely matches found yet.</p>"}`;
  } catch (error) { modalContent.innerHTML = `<p>${escapeHtml(error.message)}</p>`; }
}

// Kept for compatibility with the existing page controls; the server no longer exposes public status changes.
document.getElementById("modal-close").addEventListener("click", () => modal.classList.add("hidden"));
modal.addEventListener("click", event => { if (event.target === modal) modal.classList.add("hidden"); });
document.getElementById("refresh-button").addEventListener("click", () => { loadItems(); loadStats(); });
document.getElementById("rebuild-button").addEventListener("click", async () => {
  const button = document.getElementById("rebuild-button");
  button.disabled = true; button.textContent = "Extracting features...";
  try {
    const result = await api("/api/rebuild-image-embeddings", { method: "POST" });
    alert(`${result.message} Updated: ${result.updated}. Skipped: ${result.skipped}.`);
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; button.textContent = "Rebuild image features"; }
});
["type-filter", "category-filter"].forEach(id => document.getElementById(id).addEventListener("change", loadItems));
let searchTimer;
document.getElementById("search-input").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadItems, 350); });
loadItems();
loadStats();

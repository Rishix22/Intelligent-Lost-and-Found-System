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

async function loadStats() {
  try {
    const stats = await api("/api/stats");
    document.getElementById("lost-count").textContent = stats.lost.total;
    document.getElementById("found-count").textContent = stats.found.total;
    document.getElementById("lost-active").textContent = `Active: ${stats.lost.active}`;
    document.getElementById("found-active").textContent = `Active: ${stats.found.active}`;
    document.getElementById("resolved-count").textContent = stats.lost.resolved + stats.found.resolved;
  } catch (error) {
    console.error(error);
  }
}

function itemCard(item) {
  const image = item.image_url
    ? `<img class="item-image" src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.title)}">`
    : `<div class="item-image">No image uploaded</div>`;
  const date = item.event_date ? escapeHtml(item.event_date) : "Date not specified";
  return `<article class="item-card">
    ${image}
    <div class="item-body">
      <div class="item-meta"><span class="pill ${item.item_type}">${escapeHtml(item.item_type)}</span><span class="pill">${escapeHtml(item.category)}</span></div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.description)}</p>
      <p>⌖ ${escapeHtml(item.location)} · ${date}</p>
      <p>Contact: ${escapeHtml(item.contact_name)}</p>
      <div class="item-actions">
        <button class="small-button" onclick="showMatches('${item.id}')">Find matches</button>
        <button class="small-button" onclick="resolveItem('${item.id}')">Mark resolved</button>
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
    message.textContent = response.message;
    form.reset();
    await Promise.all([loadItems(), loadStats()]);
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Submit report";
  }
});

async function showMatches(id) {
  modal.classList.remove("hidden");
  modalContent.innerHTML = "<h2>Finding possible matches...</h2>";
  try {
    const data = await api(`/api/items/${id}/matches`);
    const matches = data.matching_items;
    modalContent.innerHTML = `<p class="eyebrow">SIMILARITY RESULTS</p>
      <h2>Potential matches</h2>
      <p class="modal-note">Compared against the opposite report type using TF-IDF text similarity, category and color. Scores rank similarity; they are not probabilities.</p>
      ${matches.length ? matches.map(item => `<div class="match-row">
        ${item.image_url ? `<img src="${escapeHtml(item.image_url)}" alt="">` : '<div class="item-image" style="width:72px;height:72px">No image</div>'}
        <div><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(item.category)} · ${escapeHtml(item.location)}</p>
        <p>${escapeHtml(item.description)}</p><span class="score">${item.match_score}% similarity score</span></div>
      </div>`).join("") : "<p>No likely matches found yet.</p>"}`;
  } catch (error) {
    modalContent.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

async function resolveItem(id) {
  if (!confirm("Mark this report as resolved?")) return;
  try {
    await api(`/api/items/${id}/status?status=resolved`, { method: "PATCH" });
    await Promise.all([loadItems(), loadStats()]);
  } catch (error) {
    alert(error.message);
  }
}

document.getElementById("modal-close").addEventListener("click", () => modal.classList.add("hidden"));
modal.addEventListener("click", event => {
  if (event.target === modal) modal.classList.add("hidden");
});
document.getElementById("refresh-button").addEventListener("click", () => {
  loadItems(); loadStats();
});
["type-filter", "category-filter"].forEach(id =>
  document.getElementById(id).addEventListener("change", loadItems)
);
let searchTimer;
document.getElementById("search-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadItems, 350);
});

loadItems();
loadStats();

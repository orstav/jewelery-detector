const data = window.REVIEW_DATA || { stats: {}, items: [] };
const decisionsKey = "catalog-attribution-review-second-pass-v1";
const decisionOptions = [
  ["identity", "Single-product identity"],
  ["supporting", "Single-product supporting"],
  ["shared_supporting", "Shared / set supporting"],
  ["wrong_product", "Wrong attribution"],
  ["exclude", "Exclude"],
  ["needs_followup", "Unsure / follow-up"],
];

const issueLabels = {
  category_conflict: "multi-jewelry model/set image",
  multiple_product_ids: "multiple product IDs",
  shared_folder_asset: "shared folder asset",
  folder_filename_mismatch: "folder/filename mismatch",
  missing_product_id: "missing product ID",
  unknown_role_context: "unknown role context",
  other: "other",
};

let decisions = loadDecisions();
let filteredItems = [];
let selectedId = null;

const els = {
  statTotal: document.getElementById("statTotal"),
  statDone: document.getElementById("statDone"),
  statOpen: document.getElementById("statOpen"),
  issueFilter: document.getElementById("issueFilter"),
  statusFilter: document.getElementById("statusFilter"),
  searchInput: document.getElementById("searchInput"),
  applyRecommended: document.getElementById("applyRecommended"),
  assetList: document.getElementById("assetList"),
  detail: document.getElementById("detail"),
  emptyState: document.getElementById("emptyState"),
  assetImage: document.getElementById("assetImage"),
  issueLabel: document.getElementById("issueLabel"),
  assetTitle: document.getElementById("assetTitle"),
  decisionPill: document.getElementById("decisionPill"),
  assetSubtitle: document.getElementById("assetSubtitle"),
  reviewQuestion: document.getElementById("reviewQuestion"),
  recommendedLabel: document.getElementById("recommendedLabel"),
  productIds: document.getElementById("productIds"),
  folderIds: document.getElementById("folderIds"),
  filenameIds: document.getElementById("filenameIds"),
  internalId: document.getElementById("internalId"),
  filename: document.getElementById("filename"),
  roles: document.getElementById("roles"),
  folders: document.getElementById("folders"),
  flags: document.getElementById("flags"),
  decisionButtons: document.getElementById("decisionButtons"),
  correctProductIds: document.getElementById("correctProductIds"),
  notes: document.getElementById("notes"),
  occurrences: document.getElementById("occurrences"),
  relatedProducts: document.getElementById("relatedProducts"),
  markCategoryShared: document.getElementById("markCategoryShared"),
  exportJson: document.getElementById("exportJson"),
  exportCsv: document.getElementById("exportCsv"),
};

function loadDecisions() {
  try {
    return JSON.parse(localStorage.getItem(decisionsKey) || "{}");
  } catch {
    return {};
  }
}

function saveDecisions() {
  localStorage.setItem(decisionsKey, JSON.stringify(decisions));
  render();
}

function textList(values) {
  return values && values.length ? values.join(", ") : "none";
}

function done(item) {
  const d = effectiveDecision(item);
  return Boolean(d && d.decision);
}

function effectiveDecision(item) {
  if (decisions[item.asset_id]?.decision) return decisions[item.asset_id];
  if (item.auto_label?.media_role) {
    return {
      asset_id: item.asset_id,
      decision: item.auto_label.media_role,
      correct_product_ids: item.auto_label.correct_product_ids,
      notes: item.auto_label.notes,
      decision_source: item.auto_label.decision_source,
    };
  }
  return decisions[item.asset_id] || null;
}

function setupFilters() {
  const issues = [...new Set(data.items.map((item) => item.issue))].sort();
  els.issueFilter.innerHTML = [
    '<option value="all">All issues</option>',
    ...issues.map((issue) => `<option value="${issue}">${issueLabel(issue)}</option>`),
  ].join("");

  els.statusFilter.value = "open";

  els.issueFilter.addEventListener("change", render);
  els.statusFilter.addEventListener("change", render);
  els.searchInput.addEventListener("input", render);
  els.applyRecommended.addEventListener("click", applyRecommended);
  els.markCategoryShared.addEventListener("click", markCategoryConflictsShared);
  els.exportJson.addEventListener("click", exportJson);
  els.exportCsv.addEventListener("click", exportCsv);
  els.correctProductIds.addEventListener("input", updateTextDecision);
  els.notes.addEventListener("input", updateTextDecision);
}

function matchesSearch(item, query) {
  if (!query) return true;
  const haystack = [
    item.asset_id,
    item.category,
    ...item.product_ids,
    ...item.folder_product_ids,
    ...item.filename_product_ids,
    ...item.product_folders,
    ...item.flags,
    ...item.image_roles,
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function render() {
  const issue = els.issueFilter.value || "all";
  const status = els.statusFilter.value || "all";
  const query = els.searchInput.value.trim();

  filteredItems = data.items.filter((item) => {
    if (issue !== "all" && item.issue !== issue) return false;
    if (status === "done" && !done(item)) return false;
    if (status === "open" && done(item)) return false;
    return matchesSearch(item, query);
  });

  if (!selectedId || !filteredItems.some((item) => item.asset_id === selectedId)) {
    selectedId = filteredItems[0]?.asset_id || null;
  }

  const total = data.items.length;
  const complete = data.items.filter(done).length;
  els.statTotal.textContent = total;
  els.statDone.textContent = complete;
  els.statOpen.textContent = total - complete;

  renderList();
  renderDetail();
}

function renderList() {
  els.assetList.innerHTML = filteredItems
    .map((item) => {
      const active = item.asset_id === selectedId ? "active" : "";
      const status = done(item) ? "done" : "";
      return `
        <button class="asset-row ${active} ${status}" data-id="${item.asset_id}">
          <img src="${item.image_url}" alt="" loading="lazy">
          <span>
            <span class="row-title">${item.display_title || textList(item.product_ids)}</span>
            <span class="row-subtitle">${item.filename || item.asset_id}</span>
          </span>
        </button>
      `;
    })
    .join("");

  els.assetList.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      selectedId = button.dataset.id;
      render();
    });
  });
}

function renderDetail() {
  const item = data.items.find((candidate) => candidate.asset_id === selectedId);
  els.detail.hidden = !item;
  els.emptyState.hidden = Boolean(item);
  if (!item) return;

  const d = effectiveDecision(item) || {};
  els.assetImage.src = item.image_url;
  const source = d.decision_source === "auto" ? "auto-labeled" : "needs review";
  els.issueLabel.textContent = `${issueLabel(item.issue)} · ${source}`;
  els.assetTitle.textContent = `${item.display_title || textList(item.product_ids)} · ${item.category}`;
  els.assetSubtitle.textContent = item.filename || item.preferred_path;
  els.reviewQuestion.textContent = questionFor(item);
  els.recommendedLabel.innerHTML = recommendedLabelFor(item);
  els.decisionPill.textContent = d.decision ? d.decision.replaceAll("_", " ") : "Open";
  els.decisionPill.classList.toggle("done", Boolean(d.decision));
  els.productIds.textContent = textList(item.product_ids);
  els.folderIds.textContent = textList(item.folder_product_ids);
  els.filenameIds.textContent = textList(item.filename_product_ids);
  els.internalId.textContent = item.asset_id;
  els.filename.textContent = item.filename || "";
  els.roles.textContent = textList(item.image_roles);
  els.folders.textContent = textList(item.product_folders);
  els.flags.textContent = textList(item.flags);
  els.correctProductIds.value = d.correct_product_ids || item.product_ids.join(",");
  els.notes.value = d.notes || "";

  els.decisionButtons.innerHTML = decisionOptions
    .map(([value, label]) => `<button data-decision="${value}" class="${d.decision === value ? "selected" : ""}">${label}</button>`)
    .join("");
  els.decisionButtons.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      decisions[item.asset_id] = {
        ...decisions[item.asset_id],
        asset_id: item.asset_id,
        decision: button.dataset.decision,
        correct_product_ids: els.correctProductIds.value.trim(),
        notes: els.notes.value.trim(),
        updated_at: new Date().toISOString(),
      };
      saveDecisions();
    });
  });

  els.occurrences.innerHTML = (item.occurrences || [])
    .map(
      (occurrence) => `
        <div class="occurrence-card">
          <strong>${occurrence.filename}</strong>
          <p>Product: ${textList(occurrence.product_ids)} · Folder: ${occurrence.folder || "none"}</p>
          <p>${occurrence.kind || "kind?"} · ${occurrence.role || "role?"}</p>
          <p>${occurrence.rel_path || ""}</p>
        </div>
      `,
    )
    .join("");

  els.relatedProducts.innerHTML = item.related_products
    .map(
      (product) => `
        <div class="related-card">
          <strong>${product.product_id}</strong>
          <p>${product.asset_count} assets · ${textList(product.roles)}</p>
          <p>${textList(product.folders)}</p>
        </div>
      `,
    )
    .join("");
}

function questionFor(item) {
  const products = textList(item.product_ids);
  if (item.issue === "category_conflict") {
    return `This model/lifestyle image shows jewelry from multiple categories (${products}). Usually mark it Set/shared and keep all visible product IDs.`;
  }
  if (item.issue === "multiple_product_ids") {
    return `This file is attributed to ${products}. Are these the same listing/variant, a shared image, or is one product wrong?`;
  }
  if (item.issue === "shared_folder_asset") {
    return `The same visual asset appears in multiple folders. Should it remain shared, or should one folder/product own it?`;
  }
  return `Check whether this image should be used as identity, supporting context, shared/set, wrong product, or excluded.`;
}

function issueLabel(issue) {
  return issueLabels[issue] || issue.replaceAll("_", " ");
}

function recommendationFor(item) {
  if (item.issue === "category_conflict") {
    return {
      decision: "shared_supporting",
      note: "Image shows multiple jewelry products/categories. Attach to all visible products, but keep out of identity clustering.",
    };
  }
  if (item.issue === "multiple_product_ids" && item.image_roles.includes("model_or_lifestyle")) {
    return {
      decision: "shared_supporting",
      note: "Lifestyle/model image with multiple product IDs. Usually shared media, not identity evidence.",
    };
  }
  if (item.issue === "shared_folder_asset") {
    return {
      decision: "needs_followup",
      note: "Shared file across folders. Needs a human call: true duplicate listing, variant, or wrong folder.",
    };
  }
  return {
    decision: "needs_followup",
    note: "Needs a human attribution call.",
  };
}

function recommendedLabelFor(item) {
  const rec = recommendationFor(item);
  const fields = derivedFieldsForDecision(rec.decision);
  return `
    <strong>Recommended dataset label:</strong>
    ${rec.decision.replaceAll("_", " ")}
    · identity_eligible=${fields.identity_eligible}
    · supports_multiple_products=${fields.supports_multiple_products}
    <br>${rec.note}
  `;
}

function applyRecommended() {
  const item = data.items.find((candidate) => candidate.asset_id === selectedId);
  if (!item) return;
  const rec = recommendationFor(item);
  decisions[item.asset_id] = {
    ...decisions[item.asset_id],
    asset_id: item.asset_id,
    decision: rec.decision,
    correct_product_ids: item.product_ids.join(","),
    notes: decisions[item.asset_id]?.notes || rec.note,
    updated_at: new Date().toISOString(),
  };
  saveDecisions();
}

function markCategoryConflictsShared() {
  const now = new Date().toISOString();
  data.items
    .filter((item) => item.issue === "category_conflict")
    .forEach((item) => {
      decisions[item.asset_id] = {
        ...decisions[item.asset_id],
        asset_id: item.asset_id,
        decision: "shared_supporting",
        correct_product_ids: item.product_ids.join(","),
        notes: decisions[item.asset_id]?.notes || "Model/lifestyle image with multiple jewelry pieces.",
        updated_at: now,
      };
    });
  saveDecisions();
}

function updateTextDecision() {
  const item = data.items.find((candidate) => candidate.asset_id === selectedId);
  if (!item) return;
  const existing = decisions[item.asset_id] || { asset_id: item.asset_id };
  decisions[item.asset_id] = {
    ...existing,
    correct_product_ids: els.correctProductIds.value.trim(),
    notes: els.notes.value.trim(),
    updated_at: new Date().toISOString(),
  };
  localStorage.setItem(decisionsKey, JSON.stringify(decisions));
}

function exportPayload() {
  return data.items.map((item) => ({
    ...derivedExportFields(item),
    asset_id: item.asset_id,
    issue: item.issue,
    issue_label: issueLabel(item.issue),
    category: item.category,
    current_product_ids: item.product_ids.join("|"),
    current_folders: item.product_folders.join("|"),
    flags: item.flags.join("|"),
    image_roles: item.image_roles.join("|"),
    decision_source: effectiveDecision(item)?.decision_source || "manual",
    decision: effectiveDecision(item)?.decision || "",
    correct_product_ids: effectiveDecision(item)?.correct_product_ids || "",
    notes: effectiveDecision(item)?.notes || "",
    preferred_path: item.preferred_path,
  }));
}

function derivedExportFields(item) {
  const decision = effectiveDecision(item)?.decision || "";
  return {
    ...derivedFieldsForDecision(decision),
    media_role: decision,
  };
}

function derivedFieldsForDecision(decision) {
  const shared = decision === "shared_supporting";
  const supporting = decision === "supporting" || shared;
  return {
    identity_eligible: decision === "identity" ? "true" : "false",
    supports_multiple_products: shared ? "true" : "false",
    catalog_media_eligible: decision && decision !== "exclude" && decision !== "wrong_product" ? "true" : "false",
    clustering_policy: decision === "identity" ? "can_link_product_identity" : supporting ? "attach_after_identity_clustering" : "",
  };
}

function download(filename, type, content) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function exportJson() {
  download("catalog-attribution-decisions.json", "application/json", JSON.stringify(exportPayload(), null, 2));
}

function csvEscape(value) {
  const text = String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

function exportCsv() {
  const rows = exportPayload();
  const headers = Object.keys(rows[0] || {});
  const csv = [headers.join(","), ...rows.map((row) => headers.map((header) => csvEscape(row[header])).join(","))].join("\n");
  download("catalog-attribution-decisions.csv", "text/csv", csv);
}

setupFilters();
render();

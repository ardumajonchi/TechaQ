// TechaQ frontend -- vanilla fetch() + DOM, no framework, per this workspace's convention (see
// progq's/civitas-q's app.js). Talks to the REST API + WebSocket protocol documented in
// python/main.py's module docstring -- keep this file and that docstring in sync; the whole
// point of writing the protocol down there is "client/server never drift silently".
//
// View switching: a single index.html with five <section class="view"> blocks, toggled by the
// tab bar -- simplest option per the brief ("your call, keep it simple").

let socket;
let currentLocations = { room: [], floor: [], column: [], shelf: [] };
let shelfCandidates = []; // enriched candidates currently shown in the Shelf Photo view
let libraryViewMode = "grid"; // "grid" | "table" -- toggled in the Library view's search card
let currentLibraryBooks = []; // last-loaded Library results, for CSV export
let currentSettings = { fetch_synopsis_default: false, ui_language: "en", ui_theme: "dark" };
// The ISBN string (exactly as sent to POST /api/lookup/{isbn}, unencoded) the "Look up" box is
// currently waiting on -- "lookup_status" Socket.IO events are broadcast to every connected tab
// (this Brick's send_message() has no per-sid targeting, same constraint scan_event already
// lives with), so each tab filters to the one lookup it itself kicked off by comparing against
// this, exactly like renderScanLogEntry filters scan_event by `code` rather than reacting to
// every scan any tab triggered.
let currentLookupIsbn = null;

// -- small helpers --------------------------------------------------------------------------

function qs(sel, root = document) {
  return root.querySelector(sel);
}

function qsa(sel, root = document) {
  return Array.from(root.querySelectorAll(sel));
}

async function apiGet(path) {
  const res = await fetch(path);
  return res.json();
}

async function apiSend(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function formatAuthors(authors) {
  return Array.isArray(authors) && authors.length ? authors.join(", ") : t("js.book.unknownAuthor");
}

function formatLocation(book) {
  const parts = [book.room, book.floor, book.column, book.shelf].filter((p) => p);
  return parts.length ? parts.join(" / ") : t("js.book.noLocation");
}

function coverSrc(book) {
  if (book.cover_data_uri) return book.cover_data_uri;
  if (book.cover_url) return book.cover_url;
  return null;
}

function toast(message, isError = false) {
  const container = qs("#toast-container");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " toast-error" : "");
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// -- tab / view switching ---------------------------------------------------------------------

function setupTabs() {
  qsa(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
}

function switchView(view) {
  qsa(".tab-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === view));
  qsa(".view").forEach((sec) => sec.classList.toggle("active", sec.id === `view-${view}`));
  if (view === "library") {
    loadLibrary();
    loadPickOfTheDay();
  }
  if (view === "desert-island") loadDesertIsland();
}

// -- pull-to-refresh (phone/touch only) ----------------------------------------------------------
// Vanilla touch handling, no library -- consistent with this app's "no framework" convention.
// Only attaches on narrow+touch viewports so desktop/mouse users see no behavior change.

function refreshActiveView() {
  const active = qs(".view.active");
  if (!active) return Promise.resolve();
  if (active.id === "view-library") return Promise.all([loadLibrary(), loadPickOfTheDay()]);
  if (active.id === "view-desert-island") return loadDesertIsland();
  return loadLocations();
}

function setupPullToRefresh(scrollEl, onRefresh) {
  const isTouchPhone = "ontouchstart" in window && window.matchMedia("(max-width: 600px)").matches;
  if (!isTouchPhone) return;

  const PULL_THRESHOLD = 60;
  const MAX_PULL = 100;
  const indicator = document.createElement("div");
  indicator.className = "pull-refresh-indicator";
  indicator.innerHTML = `<span class="pull-refresh-icon">⟳</span>`;
  scrollEl.prepend(indicator);

  let startY = null;
  let pulling = false;
  let refreshing = false;

  scrollEl.addEventListener("touchstart", (evt) => {
    if (refreshing || scrollEl.scrollTop > 0) {
      startY = null;
      return;
    }
    startY = evt.touches[0].clientY;
    pulling = true;
  });

  scrollEl.addEventListener("touchmove", (evt) => {
    if (!pulling || startY == null) return;
    const delta = evt.touches[0].clientY - startY;
    if (delta <= 0) return;
    const pull = Math.min(delta, MAX_PULL);
    indicator.style.transform = `translateY(${pull}px)`;
    indicator.classList.toggle("pull-refresh-ready", pull >= PULL_THRESHOLD);
  });

  scrollEl.addEventListener("touchend", (evt) => {
    if (!pulling || startY == null) return;
    const delta = (evt.changedTouches[0]?.clientY ?? startY) - startY;
    pulling = false;
    startY = null;
    if (delta >= PULL_THRESHOLD) {
      refreshing = true;
      indicator.classList.add("pull-refresh-spinning");
      indicator.style.transform = `translateY(${PULL_THRESHOLD}px)`;
      Promise.resolve(onRefresh()).finally(() => {
        refreshing = false;
        indicator.classList.remove("pull-refresh-spinning", "pull-refresh-ready");
        indicator.style.transform = "";
      });
    } else {
      indicator.classList.remove("pull-refresh-ready");
      indicator.style.transform = "";
    }
  });
}

// -- locations (autocomplete datalists + library filter dropdowns) ----------------------------

async function loadLocations() {
  try {
    currentLocations = await apiGet("/api/locations");
  } catch (exc) {
    console.error("failed to load /api/locations", exc);
    return;
  }
  for (const field of ["room", "floor", "column", "shelf"]) {
    const values = currentLocations[field] || [];

    const datalist = qs(`#loc-${field}-options`);
    if (datalist) {
      datalist.innerHTML = values.map((v) => `<option value="${escapeHtml(v)}">`).join("");
    }

    const select = qs(`#filter-${field}`);
    if (select) {
      const previous = select.value;
      select.innerHTML =
        `<option value="">${t("filter.any")}</option>` +
        values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
      if (values.includes(previous)) select.value = previous;
    }
  }
}

// -- Scan / Add view ----------------------------------------------------------------------------

function renderScanLogEntry(payload) {
  const log = qs("#scan-log");
  const empty = qs("#scan-log .empty");
  if (empty) empty.remove();

  const li = document.createElement("li");
  li.className = payload.ok ? "scan-ok" : "scan-fail";
  const time = new Date().toLocaleTimeString();
  if (payload.ok) {
    const book = payload.book || {};
    li.innerHTML = `<span>${time}</span><span>${t("js.scanLog.saved", { title: `<strong>${escapeHtml(book.title || payload.code)}</strong>`, device: escapeHtml(payload.device || t("js.scanner")) })}</span>`;
  } else {
    li.innerHTML = `<span>${time}</span><span>${t("js.scanLog.notFound", { code: `<strong>${escapeHtml(payload.code)}</strong>` })}</span>`;
  }
  log.insertBefore(li, log.firstChild);
  toast(payload.ok ? t("js.toast.saved", { title: payload.book?.title || payload.code }) : t("js.toast.notFound", { code: payload.code }), !payload.ok);
}

function setupScanSocket() {
  socket = io();
  socket.on("scan_event", (payload) => {
    renderScanLogEntry(payload);
    loadLocations(); // a newly-saved book's location values may be new autocomplete options
  });
  socket.on("lookup_status", (payload) => {
    if (payload.isbn !== currentLookupIsbn) return; // some other tab's in-flight lookup
    applyLookupStatus(payload);
  });
}

// -- live per-source lookup checklist ----------------------------------------------------------
// metadata.fetch_by_isbn queries Open Library/Google Books/DNB/BNF CONCURRENTLY (see
// python/engine/metadata.py's docstring), not one after another -- so rather than faking a
// sequential "checking A... checking B..." message, this renders all four as in-flight at once
// (spinner) the moment the lookup starts, then flips each to a checkmark/x as its own
// "lookup_status" "source_done" event arrives, in whatever order they actually finish. If the
// web-search fallback step is reached (a real last-resort catalog miss), the whole checklist
// collapses into a single "Searching the web..." line, since that step genuinely is sequential.
// Entirely a progressive enhancement over the static "Looking up..." text setupIsbnLookup already
// puts in #isbn-lookup-result before this ever has a chance to render -- if Socket.IO doesn't
// deliver these events at all (not connected, degraded per this app's "no feature ever breaks the
// rest" philosophy), that static message just sits there unmodified until the REST response
// lands.

const _LOOKUP_SOURCE_LABEL_KEYS = {
  openlibrary: "js.lookup.checking.openlibrary",
  googlebooks: "js.lookup.checking.googlebooks",
  dnb: "js.lookup.checking.dnb",
  bnf: "js.lookup.checking.bnf",
  opacsbn: "js.lookup.checking.opacsbn",
  isbnsearch: "js.lookup.checking.isbnsearch",
};

function renderLookupChecklist(sources) {
  const items = sources
    .map(
      (source) =>
        `<li data-source="${source}" class="lookup-check-pending">
          <span class="lookup-check-icon">⏳</span> ${t(_LOOKUP_SOURCE_LABEL_KEYS[source] || source)}
        </li>`
    )
    .join("");
  qs("#isbn-lookup-result").innerHTML = `<ul class="lookup-checklist">${items}</ul>`;
}

function applyLookupStatus(payload) {
  const el = qs("#isbn-lookup-result");
  if (payload.phase === "checking") {
    renderLookupChecklist(payload.sources || []);
    return;
  }
  if (payload.phase === "source_done") {
    const item = qs(`.lookup-checklist li[data-source="${payload.source}"]`, el);
    if (!item) return; // checklist already replaced (e.g. web_fallback, or the final result)
    item.className = payload.found ? "lookup-check-done" : "lookup-check-miss";
    qs(".lookup-check-icon", item).textContent = payload.found ? "✓" : "✗";
    return;
  }
  if (payload.phase === "web_fallback") {
    el.innerHTML = `<p class="status">${t("js.lookup.searchingWeb")}</p>`;
  }
}

function renderIsbnLookupResult(data) {
  const el = qs("#isbn-lookup-result");
  if (!data.found) {
    el.innerHTML = `<p class="status error">${t("js.lookup.noMetadata")}</p>`;
    return;
  }
  const book = data.book;
  const cover = coverSrc(book);
  const isbn = book.isbn13 || book.isbn10 || "";
  el.innerHTML = `
    <div class="book-card" style="cursor:default; flex-direction:row; align-items:center;">
      <div class="book-cover" style="width:60px; flex-shrink:0;">
        ${cover ? `<img src="${cover}" alt="">` : "📕"}
      </div>
      <div style="flex:1;">
        <div class="book-title">${escapeHtml(book.title || t("js.book.untitled"))}</div>
        <div class="book-author">${escapeHtml(formatAuthors(book.authors))}</div>
      </div>
    </div>
    <label style="display:block; margin-top:0.6rem;">${t("js.synopsis.label")}
      <textarea id="isbn-lookup-description" rows="3">${escapeHtml(book.description)}</textarea>
    </label>
    ${book.description ? "" : `<button type="button" class="secondary" id="isbn-lookup-fetch-synopsis-btn">${t("js.synopsis.button")}</button>`}
    <button id="isbn-lookup-save-btn">${t("js.bookEdit.saveThisBook")}</button>
  `;
  const descriptionEl = qs("#isbn-lookup-description", el);
  const fetchBtn = qs("#isbn-lookup-fetch-synopsis-btn", el);
  if (fetchBtn) {
    fetchBtn.addEventListener("click", () => fetchSynopsisInto(isbn, descriptionEl, fetchBtn, book.title, (book.authors || [])[0] || ""));
  }
  qs("#isbn-lookup-save-btn").addEventListener("click", async () => {
    try {
      const saved = await apiSend("POST", "/api/books", { ...book, description: descriptionEl.value });
      toast(t("js.toast.saved", { title: saved.title || t("js.book.untitled") }));
      el.innerHTML = "";
      qs("#isbn-lookup-input").value = "";
      loadLocations();
    } catch (exc) {
      toast(t("js.toast.failedSaveBook"), true);
    }
  });
}

function setupIsbnLookup() {
  qs("#isbn-lookup-btn").addEventListener("click", async () => {
    const isbn = qs("#isbn-lookup-input").value.trim();
    if (!isbn) return;
    currentLookupIsbn = isbn;
    qs("#isbn-lookup-result").innerHTML = `<p class="status">${t("js.lookup.looking")}</p>`;
    try {
      const data = await apiSend("POST", `/api/lookup/${encodeURIComponent(isbn)}`, {});
      if (isbn === currentLookupIsbn) currentLookupIsbn = null;
      renderIsbnLookupResult(data);
    } catch (exc) {
      if (isbn === currentLookupIsbn) currentLookupIsbn = null;
      qs("#isbn-lookup-result").innerHTML = `<p class="status error">${t("js.lookup.failed")}</p>`;
    }
  });
}

function renderIsbnPhotoCandidates(candidates) {
  const container = qs("#isbn-photo-candidates");
  if (candidates.length <= 1) {
    container.innerHTML = "";
    return;
  }
  // multiple plausible ISBNs found -- let the user pick which one to fill in, rather than
  // guessing (the single-candidate case auto-fills the input directly, see handleIsbnPhoto).
  container.innerHTML = candidates
    .map((code) => `<button type="button" class="isbn-candidate-btn secondary">${escapeHtml(code)}</button>`)
    .join("");
  qsa(".isbn-candidate-btn", container).forEach((btn) => {
    btn.addEventListener("click", () => {
      qs("#isbn-lookup-input").value = btn.textContent;
      container.innerHTML = "";
    });
  });
}

async function handleIsbnPhoto(file) {
  const status = qs("#isbn-photo-status");
  status.className = "status";
  status.textContent = t("js.isbnPhoto.scanning");
  qs("#isbn-photo-candidates").innerHTML = "";
  try {
    const image_b64 = await fileToBase64(file);
    const data = await apiSend("POST", "/api/scan_photo", { image_b64 });
    const candidates = data.candidates || [];
    if (!candidates.length) {
      status.className = "status error";
      status.textContent = t("js.isbnPhoto.noneFound");
    } else if (candidates.length === 1) {
      qs("#isbn-lookup-input").value = candidates[0];
      status.className = "status success";
      status.textContent = t("js.isbnPhoto.filled");
    } else {
      status.textContent = t("js.isbnPhoto.foundMultiple", { count: candidates.length });
      renderIsbnPhotoCandidates(candidates);
    }
  } catch (exc) {
    status.className = "status error";
    status.textContent = t("js.isbnPhoto.failed");
  }
}

function setupIsbnPhoto() {
  qs("#isbn-photo-input").addEventListener("change", (evt) => {
    const file = evt.target.files[0];
    if (file) handleIsbnPhoto(file);
    evt.target.value = "";
  });
}

function bookFromFormData(form) {
  const fd = new FormData(form);
  const toList = (v) => (v || "").split(",").map((s) => s.trim()).filter(Boolean);
  const pageCountRaw = fd.get("page_count");
  return {
    title: fd.get("title") || "",
    subtitle: fd.get("subtitle") || "",
    authors: toList(fd.get("authors")),
    isbn13: fd.get("isbn13") || "",
    isbn10: fd.get("isbn10") || "",
    publisher: fd.get("publisher") || "",
    published_date: fd.get("published_date") || "",
    page_count: pageCountRaw ? parseInt(pageCountRaw, 10) : null,
    language: fd.get("language") || "",
    categories: toList(fd.get("categories")),
    description: fd.get("description") || "",
    room: fd.get("room") || "",
    floor: fd.get("floor") || "",
    column: fd.get("column") || "",
    shelf: fd.get("shelf") || "",
    notes: fd.get("notes") || "",
    // unchecked checkboxes don't reliably show up in FormData.get() -- read .checked directly
    is_read: !!form.elements.is_read?.checked,
    in_reading_list: !!form.elements.in_reading_list?.checked,
    is_favorite: !!form.elements.is_favorite?.checked,
    source: "manual",
  };
}

function setupManualAddForm() {
  const form = qs("#manual-add-form");
  let coverDataUri = null;
  wireCoverPreview(qs("#manual-cover-input"), qs("#manual-cover-preview"), (dataUri) => {
    coverDataUri = dataUri;
  });
  form.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const status = qs("#manual-add-status");
    status.className = "status";
    status.textContent = t("js.manualAdd.saving");
    try {
      const book = bookFromFormData(form);
      if (coverDataUri) book.cover_data_uri = coverDataUri;
      const saved = await apiSend("POST", "/api/books", book);
      status.className = "status success";
      status.textContent = t("js.manualAdd.saved", { title: saved.title });
      form.reset();
      coverDataUri = null;
      qs("#manual-cover-preview").innerHTML = "";
      qs("#manual-cover-preview").classList.add("hidden");
      loadLocations();
    } catch (exc) {
      status.className = "status error";
      status.textContent = t("js.manualAdd.failed");
    }
  });
}

// -- Search by title/author (add flow) -----------------------------------------------------------

function renderSearchAddResults(results) {
  const container = qs("#search-add-results");
  if (!results.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = "";
  for (const book of results) {
    const cover = coverSrc(book);
    const card = document.createElement("div");
    card.className = "book-card";
    card.innerHTML = `
      <div class="book-cover">${cover ? `<img src="${cover}" alt="">` : "📕"}</div>
      <div class="book-title">${escapeHtml(book.title || t("js.book.untitled"))}</div>
      <div class="book-author">${escapeHtml(formatAuthors(book.authors))}</div>
      <button type="button" class="secondary search-add-save-btn">${t("js.bookEdit.saveThisBook")}</button>
    `;
    qs(".search-add-save-btn", card).addEventListener("click", async () => {
      try {
        const saved = await apiSend("POST", "/api/books", book);
        toast(t("js.toast.saved", { title: saved.title || t("js.book.untitled") }));
        card.remove();
        loadLocations();
      } catch (exc) {
        toast(t("js.toast.failedSaveBook"), true);
      }
    });
    container.appendChild(card);
  }
}

function setupSearchAdd() {
  qs("#search-add-btn").addEventListener("click", async () => {
    const title = qs("#search-add-title-input").value.trim();
    const author = qs("#search-add-author-input").value.trim();
    const status = qs("#search-add-status");
    const results = qs("#search-add-results");
    if (!title && !author) return;
    status.className = "status";
    status.textContent = t("js.searchAdd.searching");
    results.innerHTML = "";
    try {
      const data = await apiSend("POST", "/api/search_add", { title, author });
      const found = data.results || [];
      status.textContent = found.length ? t("js.searchAdd.results", { count: found.length }) : t("js.searchAdd.noMatches");
      renderSearchAddResults(found);
    } catch (exc) {
      status.className = "status error";
      status.textContent = t("js.searchAdd.failed");
    }
  });
}

// -- Library view ------------------------------------------------------------------------------

function renderBookGrid(container, books, { onClick } = {}) {
  if (!books.length) {
    container.innerHTML = `<p class="empty">${t("js.library.noBooksFound")}</p>`;
    return;
  }
  container.innerHTML = "";
  for (const book of books) {
    const cover = coverSrc(book);
    const card = document.createElement("div");
    card.className = "book-card";
    card.innerHTML = `
      <div class="book-cover">${cover ? `<img src="${cover}" alt="">` : "📕"}${book.is_favorite ? `<span class="book-favorite-badge">★</span>` : ""}</div>
      <div class="book-title">${escapeHtml(book.title || t("js.book.untitled"))}</div>
      <div class="book-author">${escapeHtml(formatAuthors(book.authors))}</div>
      <div class="book-location">${escapeHtml(formatLocation(book))}</div>
    `;
    if (onClick) card.addEventListener("click", () => onClick(book));
    container.appendChild(card);
  }
}

async function loadLibrary() {
  const q = qs("#library-search-input").value.trim();
  const room = qs("#filter-room").value;
  const floor = qs("#filter-floor").value;
  const column = qs("#filter-column").value;
  const shelf = qs("#filter-shelf").value;

  const params = new URLSearchParams();
  if (q) {
    params.set("q", q);
  } else {
    if (room) params.set("room", room);
    if (floor) params.set("floor", floor);
    if (column) params.set("column", column);
    if (shelf) params.set("shelf", shelf);
  }

  try {
    const data = await apiGet(`/api/books?${params.toString()}`);
    currentLibraryBooks = data.books || [];
    renderLibraryResults();
  } catch (exc) {
    currentLibraryBooks = [];
    qs("#library-grid").innerHTML = `<p class="empty">${t("js.library.loadFailed")}</p>`;
    qs("#library-table").innerHTML = "";
  }
}

function renderLibraryResults() {
  const gridEl = qs("#library-grid");
  const tableWrapEl = qs("#library-table-wrap");
  if (libraryViewMode === "table") {
    gridEl.classList.add("hidden");
    tableWrapEl.classList.remove("hidden");
    renderBookTable(qs("#library-table"), currentLibraryBooks, { onClick: openBookModal });
  } else {
    tableWrapEl.classList.add("hidden");
    gridEl.classList.remove("hidden");
    renderBookGrid(gridEl, currentLibraryBooks, { onClick: openBookModal });
  }
}

function renderBookTable(tableEl, books, { onClick } = {}) {
  if (!books.length) {
    tableEl.innerHTML = `<tbody><tr><td class="empty">${t("js.library.noBooksFound")}</td></tr></tbody>`;
    return;
  }
  const rows = books
    .map((book) => {
      const cover = coverSrc(book);
      return `
        <tr data-id="${book.id}">
          <td class="table-cover">${cover ? `<img src="${cover}" alt="">` : "📕"}${book.is_favorite ? `<span class="book-favorite-badge">★</span>` : ""}</td>
          <td>${escapeHtml(book.title || t("js.book.untitled"))}</td>
          <td>${escapeHtml(formatAuthors(book.authors))}</td>
          <td class="table-isbn">${escapeHtml(book.isbn13 || book.isbn10 || "")}</td>
          <td>${escapeHtml(formatLocation(book))}</td>
          <td class="table-source">${escapeHtml(book.source || "")}</td>
        </tr>
      `;
    })
    .join("");
  tableEl.innerHTML = `
    <thead>
      <tr>
        <th></th><th>${t("js.table.header.title")}</th><th>${t("js.table.header.authors")}</th><th class="table-isbn">${t("js.table.header.isbn")}</th><th>${t("js.table.header.location")}</th><th class="table-source">${t("js.table.header.source")}</th>
      </tr>
    </thead>
    <tbody>${rows}</tbody>
  `;
  if (onClick) {
    qsa("tbody tr", tableEl).forEach((tr) => {
      const book = books.find((b) => String(b.id) === tr.dataset.id);
      if (book) tr.addEventListener("click", () => onClick(book));
    });
  }
}

const CSV_EXPORT_FIELDS = [
  "title", "subtitle", "authors", "isbn13", "isbn10", "publisher", "published_date",
  "description", "page_count", "categories", "language", "source",
  "room", "floor", "column", "shelf", "notes",
  "is_read", "in_reading_list", "is_favorite",
];

function csvEscape(value) {
  const str = value == null ? "" : String(value);
  if (/[",\n]/.test(str)) return `"${str.replace(/"/g, '""')}"`;
  return str;
}

function booksToCsv(books) {
  const lines = [CSV_EXPORT_FIELDS.join(",")];
  for (const book of books) {
    const row = CSV_EXPORT_FIELDS.map((field) => {
      const value = book[field];
      if (Array.isArray(value)) return csvEscape(value.join(";"));
      return csvEscape(value);
    });
    lines.push(row.join(","));
  }
  return lines.join("\n");
}

function downloadCsv(filename, csvText) {
  const blob = new Blob([csvText], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function setupLibraryView() {
  qs("#library-search-btn").addEventListener("click", loadLibrary);
  qs("#library-search-input").addEventListener("keydown", (evt) => {
    if (evt.key === "Enter") loadLibrary();
  });
  for (const field of ["room", "floor", "column", "shelf"]) {
    qs(`#filter-${field}`).addEventListener("change", loadLibrary);
  }
  qs("#library-clear-btn").addEventListener("click", () => {
    qs("#library-search-input").value = "";
    for (const field of ["room", "floor", "column", "shelf"]) {
      qs(`#filter-${field}`).value = "";
    }
    loadLibrary();
  });
  qs("#library-view-grid-btn").addEventListener("click", () => {
    libraryViewMode = "grid";
    qs("#library-view-grid-btn").classList.add("active");
    qs("#library-view-table-btn").classList.remove("active");
    renderLibraryResults();
  });
  qs("#library-view-table-btn").addEventListener("click", () => {
    libraryViewMode = "table";
    qs("#library-view-table-btn").classList.add("active");
    qs("#library-view-grid-btn").classList.remove("active");
    renderLibraryResults();
  });
  qs("#library-export-btn").addEventListener("click", () => {
    if (!currentLibraryBooks.length) {
      toast(t("js.library.noResultsExport"), true);
      return;
    }
    downloadCsv("techaq-search-results.csv", booksToCsv(currentLibraryBooks));
  });
}

// -- book detail/edit modal ----------------------------------------------------------------------

function renderBookModalBody(book) {
  const cover = coverSrc(book);
  const body = qs("#book-modal-body");
  body.innerHTML = `
    <div id="book-modal-cover" class="book-cover" style="max-width:160px; margin:0 auto 0.75rem;">
      ${cover ? `<img src="${cover}" alt="">` : "📕"}
    </div>
    <label class="scan-photo-label" for="book-modal-cover-input">
      <span class="tab-icon">🖼️</span> <span>${t("field.cover.uploadHint")}</span>
      <input id="book-modal-cover-input" type="file" accept="image/*" class="hidden-file-input">
    </label>
    <form id="book-edit-form" class="book-form">
      <div class="form-grid">
        <label>${t("field.title.label")} <input name="title" value="${escapeHtml(book.title)}" required></label>
        <label>${t("field.subtitle.label")} <input name="subtitle" value="${escapeHtml(book.subtitle)}"></label>
        <label>${t("field.authors.label")} <input name="authors" value="${escapeHtml((book.authors || []).join(", "))}"></label>
        <label>${t("field.isbn13.label")} <input name="isbn13" value="${escapeHtml(book.isbn13)}"></label>
        <label>${t("field.isbn10.label")} <input name="isbn10" value="${escapeHtml(book.isbn10)}"></label>
        <label>${t("field.publisher.label")} <input name="publisher" value="${escapeHtml(book.publisher)}"></label>
        <label>${t("field.publishedDate.label")} <input name="published_date" value="${escapeHtml(book.published_date)}"></label>
        <label>${t("field.pageCount.label")} <input name="page_count" type="number" min="0" value="${book.page_count ?? ""}"></label>
        <label>${t("field.language.label")} <input name="language" value="${escapeHtml(book.language)}"></label>
        <label>${t("field.categories.label")} <input name="categories" value="${escapeHtml((book.categories || []).join(", "))}"></label>
        <label class="span-2">${t("field.description.label")} <textarea name="description" rows="2">${escapeHtml(book.description)}</textarea></label>
        ${book.description ? "" : `<button type="button" class="secondary span-2" id="book-modal-fetch-synopsis-btn">${t("js.synopsis.button")}</button>`}
        <label>${t("field.room.label")} <input name="room" value="${escapeHtml(book.room)}" list="loc-room-options"></label>
        <label>${t("field.floor.label")} <input name="floor" value="${escapeHtml(book.floor)}" list="loc-floor-options"></label>
        <label>${t("field.column.label")} <input name="column" value="${escapeHtml(book.column)}" list="loc-column-options"></label>
        <label>${t("field.shelf.label")} <input name="shelf" value="${escapeHtml(book.shelf)}" list="loc-shelf-options"></label>
        <label class="span-2">${t("field.notes.label")} <textarea name="notes" rows="2">${escapeHtml(book.notes)}</textarea></label>
      </div>
      <div class="candidate-checkbox-row">
        <input type="checkbox" name="is_read" ${book.is_read ? "checked" : ""}> <span>${t("field.isRead.label")}</span>
      </div>
      <div class="candidate-checkbox-row">
        <input type="checkbox" name="in_reading_list" ${book.in_reading_list ? "checked" : ""}> <span>${t("field.inReadingList.label")}</span>
      </div>
      <div class="candidate-checkbox-row">
        <input type="checkbox" name="is_favorite" ${book.is_favorite ? "checked" : ""}> <span>${t("field.isFavorite.label")}</span>
      </div>
      <div style="display:flex; gap:0.5rem;">
        <button type="submit">${t("js.bookEdit.saveChanges")}</button>
        <button type="button" class="danger" id="book-delete-btn">${t("js.bookEdit.delete")}</button>
      </div>
      <p class="status" id="book-edit-status"></p>
    </form>
  `;

  const fetchSynopsisBtn = qs("#book-modal-fetch-synopsis-btn", body);
  if (fetchSynopsisBtn) {
    const isbn = book.isbn13 || book.isbn10 || "";
    const descriptionEl = qs("#book-edit-form textarea[name=description]", body);
    fetchSynopsisBtn.addEventListener("click", () => fetchSynopsisInto(isbn, descriptionEl, fetchSynopsisBtn, book.title, (book.authors || [])[0] || ""));
  }

  let coverDataUri = null;
  wireCoverPreview(qs("#book-modal-cover-input", body), qs("#book-modal-cover", body), (dataUri) => {
    coverDataUri = dataUri;
  });

  qs("#book-edit-form", body).addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const status = qs("#book-edit-status", body);
    status.className = "status";
    status.textContent = t("js.bookEdit.saving");
    try {
      const updated = bookFromFormData(evt.target);
      updated.source = book.source || "manual";
      if (coverDataUri) updated.cover_data_uri = coverDataUri;
      await apiSend("PUT", `/api/books/${book.id}`, updated);
      status.className = "status success";
      status.textContent = t("js.bookEdit.saved");
      loadLibrary();
      loadLocations();
    } catch (exc) {
      status.className = "status error";
      status.textContent = t("js.bookEdit.failed");
    }
  });

  qs("#book-delete-btn", body).addEventListener("click", async () => {
    if (!confirm(t("js.bookEdit.confirmDelete", { title: book.title || t("js.book.untitled") }))) return;
    try {
      await apiSend("DELETE", `/api/books/${book.id}`, {});
      closeBookModal();
      loadLibrary();
      toast(t("js.bookEdit.deleted"));
    } catch (exc) {
      toast(t("js.bookEdit.deleteFailed"), true);
    }
  });
}

function openBookModal(book) {
  renderBookModalBody(book);
  qs("#book-modal").classList.remove("hidden");
}

function closeBookModal() {
  qs("#book-modal").classList.add("hidden");
}

function setupModal() {
  qs("#book-modal-close").addEventListener("click", closeBookModal);
  qs(".modal-backdrop", qs("#book-modal")).addEventListener("click", closeBookModal);
}

// -- Desert Island view (favorites) --------------------------------------------------------------

async function loadDesertIsland() {
  const container = qs("#desert-island-grid");
  try {
    const data = await apiGet("/api/books/favorites");
    renderBookGrid(container, data.books || [], { onClick: openBookModal });
  } catch (exc) {
    container.innerHTML = `<p class="empty">${t("js.library.loadFailed")}</p>`;
  }
}

// -- Library view: pick of the day ---------------------------------------------------------------

async function loadPickOfTheDay() {
  const container = qs("#pick-of-the-day-body");
  try {
    const data = await apiGet("/api/books/pick_of_the_day");
    const book = data.book;
    if (!book) {
      container.innerHTML = `<p class="empty">${t("js.library.noBooksFound")}</p>`;
      return;
    }
    const cover = coverSrc(book);
    container.innerHTML = `
      <div class="book-card" style="cursor:pointer; flex-direction:row; align-items:center;">
        <div class="book-cover" style="width:60px; flex-shrink:0;">
          ${cover ? `<img src="${cover}" alt="">` : "📕"}
        </div>
        <div style="flex:1;">
          <div class="book-title">${escapeHtml(book.title || t("js.book.untitled"))}</div>
          <div class="book-author">${escapeHtml(formatAuthors(book.authors))}</div>
        </div>
      </div>
    `;
    qs(".book-card", container).addEventListener("click", () => openBookModal(book));
  } catch (exc) {
    container.innerHTML = `<p class="empty">${t("js.library.loadFailed")}</p>`;
  }
}

// -- Ask AI view --------------------------------------------------------------------------------

function setupAiSearch() {
  qs("#ai-search-btn").addEventListener("click", async () => {
    const description = qs("#ai-description-input").value.trim();
    const status = qs("#ai-search-status");
    const results = qs("#ai-results");
    if (!description) return;
    status.className = "status";
    status.textContent = t("js.ai.asking");
    results.innerHTML = "";
    try {
      const data = await apiSend("POST", "/api/ai_search", { description });
      if (!data.available) {
        status.className = "status error";
        status.textContent = t("js.ai.unavailable");
        return;
      }
      status.textContent = data.results.length ? t("js.ai.results", { count: data.results.length }) : t("js.ai.noMatches");
      renderBookGrid(results, data.results, { onClick: openBookModal });
    } catch (exc) {
      status.className = "status error";
      status.textContent = t("js.ai.failed");
    }
  });
}

// -- Shelf Photo view ---------------------------------------------------------------------------

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      // reader.result is a data: URI ("data:image/jpeg;base64,AAAA...") -- strip the prefix,
      // main.py's /api/shelf_photo expects raw base64 with no data: URI wrapper.
      const commaIdx = reader.result.indexOf(",");
      resolve(reader.result.slice(commaIdx + 1));
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// reads a picked file as a data: URI (kept intact, unlike fileToBase64's stripped form -- this
// one feeds straight into BookIn.cover_data_uri on the backend and an <img src> for the live
// preview) and reports it via onChange; also swaps it into imgContainerEl immediately.
function wireCoverPreview(inputEl, imgContainerEl, onChange) {
  inputEl.addEventListener("change", () => {
    const file = inputEl.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      imgContainerEl.innerHTML = `<img src="${reader.result}" alt="">`;
      imgContainerEl.classList.remove("hidden");
      onChange(reader.result);
    };
    reader.readAsDataURL(file);
  });
}

function renderShelfCandidates() {
  const container = qs("#shelf-candidates");
  const saveBtn = qs("#shelf-save-btn");
  if (!shelfCandidates.length) {
    container.innerHTML = "";
    saveBtn.classList.add("hidden");
    return;
  }
  saveBtn.classList.remove("hidden");
  container.innerHTML = shelfCandidates
    .map((cand, idx) => {
      const resolved = cand.resolved;
      const cover = resolved ? coverSrc(resolved) : null;
      return `
        <div class="candidate-card" data-idx="${idx}">
          <div class="candidate-cover">${cover ? `<img src="${cover}" alt="">` : "📕"}</div>
          <div class="candidate-fields">
            <div class="candidate-checkbox-row">
              <input type="checkbox" class="cand-selected" ${resolved ? "checked" : ""}>
              <span>${resolved ? t("js.shelf.metadataFound") : t("js.shelf.noMatch")}</span>
            </div>
            <div class="row">
              <input class="cand-title" placeholder="${t("field.title.label")}" value="${escapeHtml(resolved ? resolved.title : cand.title || "")}">
            </div>
            <div class="row">
              <input class="cand-author" placeholder="${t("js.shelf.authorPlaceholder")}" value="${escapeHtml(resolved ? formatAuthors(resolved.authors) : cand.author || "")}">
            </div>
            <div class="row">
              <input class="cand-room" placeholder="${t("field.room.label")}" list="loc-room-options">
              <input class="cand-shelf" placeholder="${t("field.shelf.label")}" list="loc-shelf-options">
            </div>
          </div>
        </div>
      `;
    })
    .join("");
}

async function handleShelfPhoto(file) {
  const status = qs("#shelf-photo-status");
  status.className = "status";
  status.textContent = t("js.shelf.processing");
  shelfCandidates = [];
  renderShelfCandidates();
  try {
    const image_b64 = await fileToBase64(file);
    const data = await apiSend("POST", "/api/shelf_photo", { image_b64 });
    shelfCandidates = data.candidates || [];
    status.textContent = shelfCandidates.length
      ? t("js.shelf.foundCandidates", { count: shelfCandidates.length })
      : t("js.shelf.noCandidates");
    renderShelfCandidates();
  } catch (exc) {
    status.className = "status error";
    status.textContent = t("js.shelf.failed");
  }
}

function setupShelfPhoto() {
  qs("#shelf-photo-input").addEventListener("change", (evt) => {
    const file = evt.target.files[0];
    if (file) handleShelfPhoto(file);
  });

  qs("#shelf-save-btn").addEventListener("click", async () => {
    const cards = qsa(".candidate-card");
    const books = [];
    for (const card of cards) {
      const checkbox = qs(".cand-selected", card);
      if (!checkbox.checked) continue;
      const idx = parseInt(card.dataset.idx, 10);
      const resolved = shelfCandidates[idx].resolved || {};
      books.push({
        ...resolved,
        title: qs(".cand-title", card).value,
        authors: qs(".cand-author", card).value.split(",").map((s) => s.trim()).filter(Boolean),
        room: qs(".cand-room", card).value,
        shelf: qs(".cand-shelf", card).value,
        source: resolved.source || "ocr",
      });
    }
    if (!books.length) {
      toast(t("js.shelf.noneSelected"), true);
      return;
    }
    try {
      const data = await apiSend("POST", "/api/shelf_photo/confirm", { books });
      toast(t("js.shelf.savedBooks", { count: data.ids.length }));
      shelfCandidates = [];
      renderShelfCandidates();
      qs("#shelf-photo-input").value = "";
      qs("#shelf-photo-status").textContent = "";
      loadLocations();
    } catch (exc) {
      toast(t("js.shelf.saveFailed"), true);
    }
  });
}

// -- Settings: preferences (language, theme, synopsis default) ---------------------------------

function applyTheme(theme) {
  if (theme === "dark" || !theme) {
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = theme;
  }
  qs("#pref-theme-dark-btn")?.classList.toggle("active", theme === "dark" || !theme);
  qs("#pref-theme-light-btn")?.classList.toggle("active", theme === "light");
  qs("#pref-theme-day1-btn")?.classList.toggle("active", theme === "day1");
  const themeColorMeta = qs("#theme-color-meta");
  if (themeColorMeta) {
    const colors = { light: "#f7f1e6", day1: "#146b8c" };
    themeColorMeta.content = colors[theme] || "#14120f";
  }
}

async function setupPreferences() {
  try {
    currentSettings = await apiGet("/api/settings");
  } catch (exc) {
    console.error("failed to load /api/settings", exc);
  }

  qs("#pref-language").value = currentSettings.ui_language;
  qs("#pref-fetch-synopsis").checked = currentSettings.fetch_synopsis_default;
  applyTheme(currentSettings.ui_theme);
  if (typeof applyTranslations === "function") applyTranslations();

  qs("#pref-language").addEventListener("change", async (evt) => {
    const ui_language = evt.target.value;
    try {
      currentSettings = await apiSend("POST", "/api/settings", { ui_language });
      if (typeof setCurrentLanguage === "function") setCurrentLanguage(currentSettings.ui_language);
    } catch (exc) {
      toast(t("js.prefs.langFailed"), true);
    }
  });

  qs("#pref-theme-dark-btn").addEventListener("click", async () => {
    try {
      currentSettings = await apiSend("POST", "/api/settings", { ui_theme: "dark" });
      applyTheme(currentSettings.ui_theme);
    } catch (exc) {
      toast(t("js.prefs.themeFailed"), true);
    }
  });

  qs("#pref-theme-light-btn").addEventListener("click", async () => {
    try {
      currentSettings = await apiSend("POST", "/api/settings", { ui_theme: "light" });
      applyTheme(currentSettings.ui_theme);
    } catch (exc) {
      toast(t("js.prefs.themeFailed"), true);
    }
  });

  qs("#pref-theme-day1-btn").addEventListener("click", async () => {
    try {
      currentSettings = await apiSend("POST", "/api/settings", { ui_theme: "day1" });
      applyTheme(currentSettings.ui_theme);
    } catch (exc) {
      toast(t("js.prefs.themeFailed"), true);
    }
  });

  qs("#pref-fetch-synopsis").addEventListener("change", async (evt) => {
    const fetch_synopsis_default = evt.target.checked;
    try {
      currentSettings = await apiSend("POST", "/api/settings", { fetch_synopsis_default });
    } catch (exc) {
      toast(t("js.prefs.synopsisFailed"), true);
    }
  });
}

// -- manual "fetch synopsis" button (shared by the ISBN-lookup preview and the book modal) ------

async function fetchSynopsisInto(isbn, textareaEl, btnEl, title = "", author = "") {
  if (!isbn) return;
  btnEl.disabled = true;
  const originalText = btnEl.textContent;
  btnEl.textContent = t("js.synopsis.fetching");
  try {
    const data = await apiSend("POST", `/api/synopsis/${encodeURIComponent(isbn)}`, { title, author });
    if (data.description) {
      textareaEl.value = data.description;
      btnEl.remove();
    } else {
      toast(t("js.synopsis.noneFound"), true);
      btnEl.disabled = false;
      btnEl.textContent = originalText;
    }
  } catch (exc) {
    toast(t("js.synopsis.fetchFailed"), true);
    btnEl.disabled = false;
    btnEl.textContent = originalText;
  }
}

// -- Settings: full-library CSV import/export ---------------------------------------------------

function setupSettingsCsv() {
  qs("#settings-export-btn").addEventListener("click", async () => {
    try {
      const data = await apiGet("/api/books");
      downloadCsv("techaq-library.csv", booksToCsv(data.books || []));
    } catch (exc) {
      toast(t("js.settingsCsv.exportFailed"), true);
    }
  });

  qs("#settings-import-btn").addEventListener("click", async () => {
    const input = qs("#settings-import-input");
    const status = qs("#settings-import-status");
    const file = input.files[0];
    if (!file) {
      status.className = "status error";
      status.textContent = t("js.settingsCsv.chooseFile");
      return;
    }
    status.className = "status";
    status.textContent = t("js.settingsCsv.importing");
    try {
      const csvText = await file.text();
      const data = await apiSend("POST", "/api/import_csv", { csv: csvText });
      const errCount = (data.errors || []).length;
      status.className = errCount ? "status error" : "status success";
      status.textContent = t("js.settingsCsv.importResult", {
        added: data.added,
        skipped: data.skipped,
        errPart: errCount ? t("js.settingsCsv.errPart", { count: errCount }) : "",
      });
      input.value = "";
      loadLocations();
    } catch (exc) {
      status.className = "status error";
      status.textContent = t("js.settingsCsv.importFailed");
    }
  });
}

// -- boot --------------------------------------------------------------------------------------

function main() {
  setupTabs();
  setupScanSocket();
  setupIsbnLookup();
  setupIsbnPhoto();
  setupManualAddForm();
  setupSearchAdd();
  setupLibraryView();
  setupModal();
  setupAiSearch();
  setupShelfPhoto();
  setupSettingsCsv();
  setupPreferences();
  setupPullToRefresh(qs("#content"), refreshActiveView);
  loadLocations();
}

main();

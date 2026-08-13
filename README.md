# TechaQ

A home book-collection inventory manager for the Arduino UNO Q. Scan a book's EAN/ISBN
barcode with a USB/Bluetooth HID scanner, add it manually, or photograph a shelf of spines
for OCR — TechaQ fetches cover art and synopsis from public book APIs, tracks each book's
physical location (room/floor/column/shelf), and lets you search by keyword or describe a
book in natural language via a local LLM. Every action gets an "IBM PC vibes" beep on an
optional Modulino Buzzer.

Built on the official `arduino:web_ui`, `arduino:dbstorage_sqlstore`, and `arduino:llm`
Bricks, plus a custom `ocr_runtime` Brick (Tesseract OCR).

![TechaQ library view](docs/screenshot.png)

*The Library view: cover art, authors, and shelf location fetched automatically from a barcode
scan, alongside a manually-added book still awaiting a location.*

## Running it

Deploy with the Arduino App CLI like any other app Brick bundle (`app.yaml` declares the
Bricks above and exposes port 7000). Once deployed, open the app's URL
(`http://<device-ip>:7000/`) in a browser, or use `python/cli.py` (via `docker exec` into the
running container) for a terminal-only workflow — both drive the exact same `engine/` code.

Every feature degrades gracefully if its dependency isn't available: no Modulino Buzzer means
silent operation, no local LLM means AI search reports itself unavailable, and no OCR brick
means shelf-photo review is disabled — the rest of the app is unaffected either way.

### Barcode scanner setup

Reading a USB/Bluetooth HID scanner requires a small host-side (outside the app container)
setup step — see [`host/README.md`](host/README.md) for the full walkthrough, including the
one-time `sudo apt-get install python3-evdev` a human has to run interactively on the board
and the systemd user service that runs `host/scanner_reader.py`.

## User guide

### Adding books

- **Scan**: plug in (or Bluetooth-pair) a HID barcode scanner — tested with an Eyoyo mini, but
  any HID-compliant scanner works, since they all enumerate as a keyboard and "type" the
  decoded digits. A scan POSTs to `/api/scan`, which looks up the code and auto-saves on a hit.
- **Manual**: fill in the Add Book form yourself — every field a scan would populate is
  editable, plus the four location fields and the read/reading-list/favorite checkboxes. You can
  also upload a cover image yourself instead of relying on an auto-fetched one; uploading a new
  cover from a book's detail view replaces whatever cover it had before.
- **Look up an ISBN**: type (or paste) an ISBN/EAN to preview its metadata before saving, or tap
  "Scan ISBN from photo" to photograph the barcode's printed digits instead of typing — the same
  `ocr_runtime` OCR pipeline reads the digits, offers up any plausible ISBN-looking candidates for
  you to pick, and fills the lookup field for you to confirm.
- **Add by title/author**: for books with no barcode to scan (or that you don't have in hand),
  search a public book catalog by title (and optionally author); each result is shown as a
  candidate card you can save directly, with no ISBN required.
- **Shelf photo**: capture or upload a photo of several book spines. TechaQ preprocesses the
  image (grayscale/contrast, tries multiple rotations since spines are usually vertical), OCRs
  it via the `ocr_runtime` Brick, and asks the local LLM to pull out title/author guesses. Each
  guess is resolved against real book-search results and shown to you for review — nothing is
  saved until you confirm which candidates are real.

Every book also carries three checkboxes: **Read**, **Reading list**, and **Favorite** — set them
when adding a book, or later from its detail view.

Every card on the Scan/Add page, and the Library's Search & filter card, can be collapsed by
tapping its title bar — handy for getting a long card out of the way once you're done with it.
On a phone, pulling down past the top of the Library or Desert Island view triggers a
pull-to-refresh of that view's data.

### Finding books

- **Keyword search** matches title, subtitle, authors, description, and notes.
- **Location filters** (room/floor/column/shelf) narrow the list to one physical spot.
- **"Describe it to find it"** lets you type a natural-language description (e.g. "that book
  about a kid wizard and his owl") — a local LLM turns it into a search-tool call and only ever
  returns real matches; it can never invent a book that doesn't exist in the search results.
- **Grid or list view** — toggle the Library between a cover-art grid and a compact table, and
  export whatever's currently displayed (after a search or filter) as a CSV file.
- **Pick of the day** — a randomly-selected book from your whole library, shown at the top of the
  Library view with its cover, title, and author; tap it to open the usual detail/edit view. A
  new pick is drawn each time you visit the Library tab.
- **Desert Island** — a dedicated tab listing every book marked as a favorite, the ones you'd
  take with you. It refreshes each time you open the tab, so favoriting/unfavoriting a book from
  its detail view is reflected the next time you visit.


### Settings

- **Preferences** — pick the UI language (English, Italiano, Deutsch, Français, Español), switch
  between the dark (default), light, and "Day1" themes (the last one a pastiche of amazon.com
  circa 2001 — white page with a solid teal nav bar, square boxy borders, Verdana type, and flat
  orange buttons), and choose whether
  scanning/looking up a book fetches its synopsis automatically or leaves that for a manual
  "Fetch synopsis" button (shown on a book's lookup preview and detail view whenever its
  description is still empty). All these preferences are stored server-side, shared across every
  device viewing the app.
- **Import / export the whole library as CSV** — export every book to a CSV file, or import a
  CSV of books in that same format; rows whose ISBN already matches a book already in the
  library are skipped rather than duplicated.
- **Barcode scanner device exclusion** — exclude a misbehaving HID device (e.g. a real attached
  keyboard) from the host-side scanner-reading service; see [`host/README.md`](host/README.md).

### Editing and deleting

Every book's detail view is editable in place, including its location, and can be deleted from
the same screen.

## How it works

- **Engine.** `python/engine/library.py` is the one real code path for every book operation —
  `python/main.py` (WebUI) and `python/cli.py` (terminal) both call into it, so behavior can
  never diverge between the two front ends.
- **Read/reading-list/favorite flags.** `is_read`, `in_reading_list`, and `is_favorite` are plain
  booleans on `BookRecord`, stored as `INTEGER` columns (SQLite has no native boolean type, same
  convention as `settings.py`'s `fetch_synopsis_default`). `BookDB` migrates any pre-existing
  `books` table in place via `PRAGMA table_info` + `ALTER TABLE ... ADD COLUMN`, so upgrading
  doesn't require dropping the on-device database.
- **Add by title/author.** `Library.search_add` reuses the exact same `metadata.
  search_by_title_author` catalog search that already backs AI-describe and OCR-candidate
  resolution — no separate metadata code path. Results are unsaved candidates; saving one goes
  through the normal `add_book`/`POST /api/books` path, identical to saving an ISBN-lookup
  preview.
- **Desert Island.** `Library.list_favorites` / `GET /api/books/favorites` filters the library
  down to `is_favorite = 1`, rendered in its own tab with the same book-grid component the
  Library view uses, so opening a book still goes through the normal edit modal.
- **Pick of the day.** `BookDB.random_book` (`SELECT * FROM books ORDER BY RANDOM() LIMIT 1`) backs
  `Library.pick_of_the_day` / `GET /api/books/pick_of_the_day`, rendered as a single inline book
  block in the Library view the same way an ISBN-lookup preview is — clicking it opens the normal
  edit modal. A new pick is drawn once per Library-tab visit, not on every search/filter.
- **Manual cover upload.** The Scan/Add manual-entry form and the book edit modal both expose a
  file picker that reads the chosen image into a `cover_data_uri` data URI client-side (the same
  `BookIn.cover_data_uri` field `to_book()` already decoded for OCR/lookup-preview saves) and
  includes it in the `POST`/`PUT` body — no new backend path was needed, since `update_book`'s
  existing "preserve the old cover when the edit carries none" logic only fires when
  `cover_image` is empty, and a decoded upload already has it set.
- **Collapsible sections.** Every card on the Scan/Add page, and the Library's Search & filter
  card, is a native `<details>`/`<summary>` element rather than a JS-driven toggle — no state to
  manage, keyboard/screen-reader accessible for free, and degrades to always-expanded if CSS
  fails to load.
- **Pull-to-refresh.** `app.js`'s `setupPullToRefresh` is a small vanilla touch-event handler
  (touchstart/touchmove/touchend) attached to `#content`, gated to touch+narrow (`≤600px`)
  viewports so desktop/mouse users see no behavior change. Pulling past a threshold re-runs
  whichever view is currently active (`loadLibrary`+`loadPickOfTheDay`, `loadDesertIsland`, or a
  harmless `loadLocations` refresh elsewhere).
- **Metadata.** `python/engine/metadata.py` queries six sources concurrently by ISBN — Open
  Library, Google Books, two national-library SRU catalogs (Deutsche Nationalbibliothek and
  Bibliothèque nationale de France, sharing one Dublin-Core XML parsing helper), Italy's OPAC SBN
  union catalog (ICCU's own search-frontend JSON endpoint, undocumented but confirmed reliable and
  correctly ISBN-scoped), and isbnsearch.org (a third-party ISBNdb-backed page, scraped via
  regex) — merging field-by-field (first non-empty value wins, longest description wins, cover
  falls back Open Library → Google thumbnail); `source` reports which of the six actually hit.
  Fetching the synopsis specifically is optional per-call (`include_description`) since it costs
  an extra Open Library work-lookup and/or a Google Books call — the Settings "fetch synopsis
  automatically" toggle controls the default. Google Books is tried first (usually the richer,
  more editorially-written description when available) with Open Library's work-level
  description as a fallback if Google Books misses or is rate-limited — its free API enforces one
  shared daily quota across every TechaQ install, so treating it as the only synopsis source (as
  this app originally did) meant synopsis fetching would silently stop working for everyone once
  that shared quota was exhausted for the day, with no way to tell the two "empty" cases apart.
  `fetch_description`/the manual "Fetch synopsis" button share this same two-source logic.
  `search_by_title_author` backs both the AI-describe and OCR-candidate-resolution flows. A
  Google Books API key is optional.
- **Web-search metadata fallback.** When all six catalog sources miss, `python/engine/
  web_lookup.py`'s `WebMetadataFallback` scrapes a handful of DuckDuckGo Lite search-result
  snippets for the ISBN and has the local LLM guess a `{title, author}` from them — never
  inventing one, and only ever proposed, never trusted outright: `library.py` resolves the guess
  against `search_by_title_author`'s real catalog search before it counts as a match, then
  restores the originally-requested ISBN (not the matched edition's own) and tags `source` with
  a `websearch+` prefix so it's visibly distinguishable from a direct catalog hit. No keyless web
  search API tested proved reliable, so this is a best-effort last resort: it can still come up
  empty on a given request (DuckDuckGo Lite intermittently bot-blocks scraping) and degrades to
  today's plain "not found" behavior when it does, exactly like an outage on any other source.
- **Settings.** `python/engine/settings.py`'s `SettingsStore` persists one shared row (fetch-
  synopsis default, UI language, UI theme) in the same `techaq.db` SQLite file as the book table,
  via the same `arduino:dbstorage_sqlstore` wrapper — settings are server-side and common to
  every device viewing the app, not per-browser.
- **AI describe-to-find.** `python/engine/ai_search.py` wraps the `arduino:llm` Brick with a
  single `search_books` tool that calls the real metadata search — the model can only propose
  search terms, never fabricate a result. Defensive construction throughout: a missing/broken
  Brick degrades to "AI search unavailable" rather than crashing the app.
- **Shelf-photo OCR.** `bricks/ocr_runtime/` is a custom Brick (own Dockerfile, root-built,
  `apt-get install tesseract-ocr`) exposing a small HTTP OCR service, following the same shape
  as `scummvm-q`'s custom runtime Brick — see [`bricks/ocr_runtime/README.md`](bricks/ocr_runtime/README.md)
  for its full architecture and HTTP contract. `python/engine/ocr.py` preprocesses the image with
  Pillow, calls the service, and has the local LLM extract `{title, author}` candidates from
  the raw OCR text — explicitly allowed to say "unknown" rather than guess. The same
  preprocess/OCR pipeline backs photo-to-ISBN scanning, but pattern-matches digit runs instead
  of calling the LLM, since there's no free text to interpret.
- **CSV import/export.** `python/engine/library.py`'s `import_csv` parses rows with the stdlib
  `csv` module and adds each through the same `add_book` path as every other entry point,
  skipping (and counting) rows whose ISBN already exists in the library — export is a client-side
  reformatting of whatever's already loaded, so no separate backend route is needed for it.
- **Barcode scanner.** `host/scanner_reader.py` runs on the board's host OS (not inside the
  app container — HID input devices aren't reachable from in-container, see
  [`host/README.md`](host/README.md)) and POSTs decoded scans to the app's own `/api/scan`
  endpoint.
- **Buzzer.** `sketch/` runs on the paired MCU and drives a physical Modulino Buzzer via a
  single `play_tone(freq, ms)` Bridge RPC; `python/hw.py` calls it for scan/save/search/error/
  delete/startup tones tuned for "IBM PC speaker" vibes, degrading to silent operation with no
  buzzer/MCU attached.
- **Web UI.** `assets/` is a mobile-first responsive frontend served by the `arduino:web_ui`
  Brick, with real-time scan/save toasts over its Socket.IO channel. `assets/i18n.js` holds all
  five languages' strings and the `applyTranslations()`/`t()` machinery; every static label uses
  a `data-i18n*` attribute and every dynamic string (toasts, statuses) routes through `t()`. The
  light theme is a `[data-theme="light"]` CSS-variable override block in `style.css`, and the
  "Day1" theme (an amazon.com-circa-2001 pastiche) is a second such block — every other rule in
  the file already consumes those variables, so no other CSS changes were needed beyond a set
  of Day1-only cosmetic exceptions (a Verdana font override, a flat-square `border-radius: 0`
  reset, the white-logo-row-over-teal-navbar chrome, and flat rather than glossy buttons) scoped
  strictly under `[data-theme="day1"]` and called out as such in a comment.
- **Live ISBN lookup status.** An "Look up an ISBN" preview no longer just shows a static
  "Looking up..." message: `engine/metadata.py`'s `fetch_by_isbn` reports each of its six
  concurrent catalog fetches (Open Library, Google Books, DNB, BNF, OPAC SBN, isbnsearch.org) the
  moment that source's own
  thread finishes, in whatever order they actually complete; `library.py`'s `lookup_isbn` and
  `main.py` relay each step over the same Socket.IO channel as scan/save toasts as a `lookup_status`
  event, and `app.js` renders a per-source checklist that flips from spinner to check/miss live,
  followed by "Searching the web..." if every catalog misses and the web-search fallback kicks in.
  Purely cosmetic transparency, not a new code path: the REST response that actually resolves the
  lookup is unchanged, so a tab with no Socket.IO connection just keeps the plain static message
  and still works.
- **Installable app icon.** `assets/icons/` (favicon, Apple touch icon, and 192/512px "any" +
  "maskable" PNGs) plus `assets/manifest.json` mean "Add to Home Screen" on iOS, Android, and
  desktop Chrome saves TechaQ with its own book icon instead of a browser-tab screenshot;
  `index.html`'s `<link rel="manifest">`/`apple-touch-icon`/`theme-color` tags wire it up, and
  `applyTheme()` in `app.js` flips the `theme-color` meta alongside the CSS variables so the
  browser/status-bar chrome matches whichever theme is active.

## Tests

`pytest` covers `engine/` (metadata merge logic across all four sources, settings persistence,
AI search grounding, OCR text-cleanup, library CRUD) with no hardware/network/LLM dependency —
mocked HTTP and a stub LLM stand in for the real Bricks. There's no frontend test harness (i18n,
theming, and the Settings-UI wiring are verified live in-browser instead), consistent with how
every other frontend feature in this app has been verified.

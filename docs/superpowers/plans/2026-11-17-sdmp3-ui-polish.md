# SDMP3 UI Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the redundant search-scope file list, make keyboard/search/results scrolling automatic, and label `/sdcard` unmistakably as internal storage.

**Architecture:** All product changes stay inside the existing `HTML_TEMPLATE` in `project.py`: delete the scope-list DOM/CSS/JS while preserving the `/api/items` fetch for the sub-folder dropdown; add a sticky search-control block plus `visualViewport`-driven `--kb-inset`; and render a themed source-kind tag in both the folded route summary and the source path line. No sibling module, route, or copy-engine behavior changes.

**Tech Stack:** Python/Flask string template, server-derived theme tokens, vanilla JS, jsdom/fetch harness, Node syntax gate.

**Spec:** `docs/superpowers/specs/2026-11-17-sdmp3-ui-polish-design.md`

## Global Constraints

- `project.py` keeps **one** inline `<script>`; any JS syntax error disables the whole UI. Render `/`, extract that script, and run `node --check` after every code task.
- `HTML_TEMPLATE` is a plain Python triple-quoted string: escape backslashes twice (e.g. write `\\/` in source to emit `\/` in JS).
- No remote assets. No `fonts.googleapis.com` link. No new colour literal anywhere in `project.py`; use existing theme tokens/kind classes.
- Every `font-size` is `calc(Npx * var(--font-scale))`; no raw `Npx` font sizes.
- Markup built with `innerHTML` must escape names/paths with `esc()`; the new source-kind tag uses `textContent`.
- Keep the copy engine byte-identical: no parallel copy, no queue/sort/verify changes.
- `/api/items` stays: it is the sole data source for `#searchSubfolder`.
- Search still renders into `#treeContainer` through `appendItemRows()`/`itemRowHtml()`; do not reintroduce a separate results panel.
- Bump `VERSION` to `1.10.1` for these user-visible changes.
- Verification gates: `python -m py_compile project.py sdmp3_cli.py sdmp3_themes.py sdmp3_state.py sdmp3_groups.py sdmp3_fonts.py`, `bash -n install.sh`, the four sibling-module self-tests, render→`node --check`, `node test_ui.js`, `python test_backend.py`.

## Review Focus

- **A folder has 4 000 files and the list is removed.** The sub-folder dropdown must still populate from `/api/items` on every scope change; assertion written in Task 1 Step 3, verified in Task 3 Step 6.
- **The soft keyboard is open.** `--kb-inset` must reflect `visualViewport.height`, and the tree height must shrink instead of hiding rows under the keyboard; assertion written in Task 1 Step 4, verified in Task 4 Step 6.
- **The user is already looking at the results.** Repeated searches must not yank the page back and forth; assertion written in Task 1 Step 4, verified in Task 4 Step 6.
- **A zero-result search still renders `#searchViewHead`.** Reveal must be a no-op rather than throwing and killing later handlers; assertion written in Task 1 Step 4, verified in Task 4 Step 6.
- **The source is `/sdcard/My Books` or an unknown mount.** Kind lookup must use longest matching root and must never inject a path into markup; assertion written in Task 1 Step 4, verified in Task 4 Step 6.

---

### Task 1: Write the failing harness contract

**Files:**
- Modify: `/data/data/com.termux/files/usr/tmp/uitest/test_backend.py`
- Modify: `/data/data/com.termux/files/usr/tmp/uitest/test_ui.js`
- Test: same two files

**Interfaces:**
- Consumes: current rendered `/` and the existing inline JS.
- Produces: assertions naming the DOM/JS contract implemented by Tasks 2–5: `#searchControls`, `#sourceKindTag`, `#sourcePathKindTag`, `--kb-inset`, `syncViewportInset()`, `revealSearchResults()`, and the exact string `المساحة الداخلية`.

- [ ] **Step 1: Add backend contract assertions**

Append to `test_backend.py` before the final print:

```python
def t6_ui_polish_rendered_contract():
    html = client.get('/').get_data(as_text=True)
    for gone in ('id="searchScopeItems"', 'id="searchScopeMore"',
                 'بحث عن مجلد أو ملف'):
        assert gone not in html, gone
    for needed in ('id="searchControls"', 'id="sourceKindTag"',
                   'id="sourcePathKindTag"', '--kb-inset',
                   'function syncViewportInset', 'function revealSearchResults',
                   'المساحة الداخلية', 'v1.10.1'):
        assert needed in html, needed

check('t6 rendered contract for search polish and source kind', t6_ui_polish_rendered_contract)
```

- [ ] **Step 2: Add jsdom stubs and scroll recording**

Inside `beforeParse(window)`, before the `fetch` stub:

```js
window.__scrolls = [];
Object.defineProperty(window, 'visualViewport', {
  configurable: true,
  value: { height: window.innerHeight - 220, offsetTop: 0, addEventListener() {} },
});
window.Element.prototype.scrollIntoView = function (arg) {
  window.__scrolls.push({ id: this.id || this.className, arg: arg || {} });
};
window.scrollTo = function () {};
```

Also make the existing `/api/search` stub return `{ results: [], from_cache: false }` when the URL contains `q=__none__`, and keep the normal `SEARCH` response otherwise.

- [ ] **Step 3: Add jsdom removal/sub-folder assertions**

Add after the existing Task 2 block:

```js
await check('t3 scope file list is gone but subfolder menu remains', async () => {
  const doc = window.document;
  if (doc.getElementById('searchScopeItems') || doc.getElementById('searchScopeMore')) {
    throw new Error('scope list still rendered');
  }
  if (doc.documentElement.innerHTML.includes('بحث عن مجلد أو ملف')) {
    throw new Error('old heading still rendered');
  }
  const sel = doc.getElementById('searchSubfolder');
  if (!sel) throw new Error('missing #searchSubfolder');
  window.setSearchScope(ROOT, false);
  await waitFor(() => sel.options.length > 1);
  if (!Array.from(sel.options).some(o => o.value === 'Books')) {
    throw new Error('subfolder menu was not populated from /api/items');
  }
});
```

- [ ] **Step 4: Add jsdom keyboard/reveal/source-kind assertions**

```js
await check('t4 keyboard inset tracks visualViewport', () => {
  window.syncViewportInset();
  const inset = window.document.documentElement.style.getPropertyValue('--kb-inset');
  if (inset !== '220px') throw new Error('--kb-inset=' + inset);
});

await check('t4 revealSearchResults uses nearest scrolling', async () => {
  const doc = window.document;
  doc.getElementById('searchInput').value = 'a';
  window.searchDeep(false);
  await waitFor(() => !!doc.getElementById('searchViewHead'));
  window.__scrolls.length = 0;
  window.revealSearchResults();
  await waitFor(() => window.__scrolls.length >= 2);
  const ids = window.__scrolls.map(s => s.id);
  if (ids[0] !== 'searchControls' || !ids.includes('searchViewHead')) {
    throw new Error('scroll calls: ' + JSON.stringify(window.__scrolls));
  }
  if (window.__scrolls.some(s => s.arg.block !== 'nearest')) {
    throw new Error('reveal must not force a distant scroll');
  }
});

await check('t4 zero-result search reveals without throwing', async () => {
  const doc = window.document;
  doc.getElementById('searchInput').value = '__none__';
  window.searchDeep(false);
  await waitFor(() => !!Array.from(doc.querySelectorAll('#treeContainer div'))
    .find(d => d.textContent === 'لم يتم العثور على نتائج.'));
  if (!doc.getElementById('searchViewHead')) throw new Error('empty search lost its head');
});

await check('t5 source kind tags cover root, subfolder, and unknown paths', () => {
  const doc = window.document;
  const src = doc.getElementById('custom_source_path');
  const tags = ['sourceKindTag', 'sourcePathKindTag'].map(id => doc.getElementById(id));
  if (tags.some(t => !t)) throw new Error('missing source kind tag');
  src.value = '/sdcard';
  window.updateRouteSummary();
  tags.forEach(t => {
    if (t.textContent !== 'المساحة الداخلية' || t.style.display === 'none') {
      throw new Error('internal label: ' + t.textContent);
    }
    if (!t.classList.contains('pill-kind-internal')) throw new Error('internal colour missing');
  });
  window.eval('STORAGE_ROOTS.push({path:"/storage/ABCD-1234",kind:"sdcard",name:"كارت SD",icon:"ico-sd"})');
  src.value = '/storage/ABCD-1234/Books';
  window.updateRouteSummary();
  tags.forEach(t => {
    if (t.textContent !== 'كارت SD' || !t.classList.contains('pill-kind-sdcard')) {
      throw new Error('sdcard subfolder label: ' + t.textContent);
    }
  });
  src.value = '/mnt/unknown/x';
  window.updateRouteSummary();
  tags.forEach(t => {
    if (t.style.display !== 'none') throw new Error('unknown path still tagged');
  });
  src.value = '/sdcard';
  window.updateRouteSummary();
});
```

- [ ] **Step 5: Run the harnesses to prove they fail**

Run from `/data/data/com.termux/files/usr/tmp/uitest`:

```sh
python render.py && node --check app.js && node test_ui.js
python test_backend.py
```

Expected: `node --check` passes; `test_ui.js` and `test_backend.py` report failures naming `#searchControls`, `syncViewportInset`, `revealSearchResults`, the removed ids/phrase, or `v1.10.1`.

- [ ] **Step 6: Commit the failing contract**

```sh
git -C /storage/emulated/0/Download/SD-Copy-MP3-Manager add docs/superpowers/specs/2026-11-17-sdmp3-ui-polish-design.md docs/superpowers/plans/2026-11-17-sdmp3-ui-polish.md
git -C /storage/emulated/0/Download/SD-Copy-MP3-Manager commit -m "test: pin search polish and source kind contract"
```

The two harness files live outside the checkout; do not git-add them.

---

### Task 2: Remove the redundant scope file list

**Files:**
- Modify: `project.py:2207-2227` (search CSS), `project.py:2770-2805` (search markup), `project.py:4959-5083` (scope JS)
- Test: both throwaway harnesses

**Interfaces:**
- Consumes: `esc()`, `formatBytes()` (becomes unused here), `includePath()`, `excludePath()`, `refreshCheckboxStates()` (other call sites remain).
- Produces: `loadSearchScope()` → `renderSearchSubfolders(items)` with an explicit array argument; `#searchSubfolder` remains the only scope-items consumer.

- [ ] **Step 1: Delete list-only CSS**

Remove `.scope-items`, `.scope-item`, `.scope-item .icon`, `.scope-item-name`, `.scope-item-name:hover`, `.scope-item-size`, and `.scope-empty`. Replace the stale comment above `.search-scope` with one that says it is a sticky control bar naming the folder and offering the sub-folder menu. Keep `.search-scope`, `.scope-line`, `.scope-path`, and `.scope-select`.

- [ ] **Step 2: Replace the heading and delete the list markup**

Change only the `<h2 class="section-title">` text from `بحث عن مجلد أو ملف` to `البحث`, and leave the rest of the section head (including `#searchSpeedBadge`) in place. Remove `<div class="scope-items" id="searchScopeItems"></div>` and `#searchScopeMore`. Keep `#searchScopePath`, `#searchSubfolder`, both scope buttons, `#searchInput`, `#historyToggleBtn`, `#foldersOnlyCheck`, and the hint.

- [ ] **Step 3: Delete list-only JS**

Remove `SCOPE_FILES_STEP`, `scopeItems`, `scopeShown`, `renderSearchScopeItems()`, `loadMoreScopeItems()`, `toggleScopeItem()`, and the delegated listener on `#searchScopeItems`. Keep `setSearchScope`, `searchScopeUp`, `enterSearchSubfolder`, and `loadSearchScope`.

- [ ] **Step 4: Rewire `/api/items` to the dropdown only**

Change the JS signature and body to:

```js
function renderSearchSubfolders(items) { /* builds the <select> from items.filter(i => i.is_dir) */ }
```

In `loadSearchScope()`, remove all `#searchScopeItems` writes and pass `(data.items || [])` to `renderSearchSubfolders()` on success and `[]` on failure. Never rebuild the `<select>` on a keystroke.

- [ ] **Step 5: Run the focused gates**

```sh
python -m py_compile project.py
python render.py && node --check app.js
node test_ui.js
```

Expected: `py_compile` and `node --check` pass; `t3 scope file list is gone but subfolder menu remains` passes; keyboard/source-kind assertions may still fail.

- [ ] **Step 6: Commit**

```sh
git add project.py
git commit -m "ui: drop redundant search scope file list"
```

---

### Task 3: Make search sticky and keyboard-aware

**Files:**
- Modify: `project.py:2111-2126` (root CSS), `project.py:2770-2805` (sticky wrapper), `project.py:4952-5120` (search state), `project.py:5134-5165` (searchDeep + reveal calls)
- Test: both harnesses

**Interfaces:**
- Consumes: `#searchControls`, `#treeContainer`, `searchRequestToken`, `treeInSearchMode`.
- Produces: CSS custom property `--kb-inset` on `documentElement`; functions `syncViewportInset()` and `revealSearchResults()`; `#searchControls` wraps all search inputs.

- [ ] **Step 1: Add the viewport custom property and tree clamp**

In the rendered `:root`, after the theme loop, add `--kb-inset: 0px;`. Change `.tree` max-height from `62vh` to:

```css
max-height: max(180px, calc(62vh - var(--kb-inset)));
```

Keep all other `.tree` properties unchanged.

- [ ] **Step 2: Wrap the search controls**

Wrap `#searchScope`, the `#searchInput` path line, `#foldersOnlyCheck`, and the hint in:

```html
<div class="search-sticky" id="searchControls"> ... </div>
```

Add CSS: `position: sticky; top: 0; z-index: 8; background: var(--bg); padding: 6px 0 8px; border-bottom: 1px solid var(--rule);` plus a max-width-480 rule that reduces the vertical padding. No colour literals.

- [ ] **Step 3: Implement `syncViewportInset()`**

Signature: `function syncViewportInset() -> undefined`. Read `window.visualViewport`; compute `Math.max(0, Math.round((window.innerHeight || 0) - (vv.height || window.innerHeight || 0) - (vv.offsetTop || 0)))`; write the result as `Npx` to `document.documentElement.style.setProperty('--kb-inset', ...)` only when it changes. Register `resize`/`scroll` on both `window.visualViewport` (when present) and `window`. Never throw when `visualViewport` is absent.

- [ ] **Step 4: Implement `revealSearchResults()`**

Signature: `function revealSearchResults() -> undefined`. Call `syncViewportInset()`, then call `scrollIntoView({behavior: 'smooth', block: 'nearest'})` on `#searchControls` and, inside a `requestAnimationFrame`, on `#searchViewHead` when present. `nearest` is mandatory: it prevents repeated typing searches from fighting the user’s scroll. Return silently if either element is missing.

- [ ] **Step 5: Call reveal after every rendered search result**

At the end of both success and catch branches in `searchDeep()` (after results or the empty/error message is in the DOM), call `revealSearchResults()`. Do not call it before the DOM is written. Empty results still render `#searchViewHead`, so reveal must not throw.

- [ ] **Step 6: Run the focused gates**

```sh
python render.py && node --check app.js && node test_ui.js
```

Expected: the keyboard inset and reveal assertions pass. Earlier search-result and source-tree assertions stay green.

- [ ] **Step 7: Commit**

```sh
git add project.py
git commit -m "ui: keep search controls visible above the keyboard"
```

---

### Task 4: Label and polish the source route

**Files:**
- Modify: `project.py:2183-2205` (route CSS), `project.py:2673-2708` (route markup), `project.py:4353-4366` (`updateRouteSummary`), `project.py:4378-4423` (storage lookup), `VERSION`
- Test: both harnesses

**Interfaces:**
- Consumes: `storageForPath(path)` returning `{path, kind, name, icon}` or `null`; `STORAGE_KIND_LIST`.
- Produces: `sourceKindLabel(kind) -> string`; `applySourceKindTag(el, path) -> undefined`; tags `#sourceKindTag` and `#sourcePathKindTag`; `v1.10.1`.

- [ ] **Step 1: Add the kind-tag markup**

In the source `.route-sum`, add `<span class="pill kind-tag" id="sourceKindTag" style="display:none;"></span>` after `#routeSourceSummary`. In the source `.path-line`, add `<span class="pill kind-tag" id="sourcePathKindTag" style="display:none;"></span>` before `#custom_source_path` and give the input a `source-path-input` class.

- [ ] **Step 2: Add route CSS**

Use token-only CSS: `.kind-tag { flex-shrink:0; font-size:calc(10.5px * var(--font-scale)); padding:3px 7px; }`, `.route-sum { gap:8px; }`, `.source-path-input { color:var(--text-muted); font-size:calc(12px * var(--font-scale)); }`, and a small media query so the tag does not wrap awkwardly on phones. Do not add a colour literal or raw `px` font size.

- [ ] **Step 3: Implement the label helpers**

In the storage section, define:

```js
const SOURCE_KIND_LABELS = {
  internal: 'المساحة الداخلية',
  sdcard: 'كارت SD',
  usb: 'USB OTG',
  card: 'كارت ذاكرة'
};
function sourceKindLabel(kind) { /* exact map value or 'وحدة تخزين' */ }
function applySourceKindTag(el, path) {
  // use textContent, reset all pill-kind-* classes, then add the matching one;
  // hide the tag when storageForPath(path) is null.
}
```

`applySourceKindTag()` must set `title` to the resolved storage name/path and must never use `innerHTML`.

- [ ] **Step 4: Wire tags into route updates**

Inside `updateRouteSummary()`, after setting `#routeSourceSummary`, call `applySourceKindTag()` for `#sourceKindTag` and `#sourcePathKindTag` with the same `src` value. Existing calls from `window.onload`, `updateSourceRoot()`, and `chooseStorage()` then cover all source changes without new listeners.

- [ ] **Step 5: Bump the version**

Replace the contents of `VERSION` with `1.10.1`. Update the jsdom `/api/system` stub to return `version: '1.10.1'`.

- [ ] **Step 6: Run the full gates**

From the checkout:

```sh
python -m py_compile project.py sdmp3_cli.py sdmp3_themes.py sdmp3_state.py sdmp3_groups.py sdmp3_fonts.py
bash -n install.sh
python sdmp3_themes.py
python sdmp3_state.py
python sdmp3_groups.py
python sdmp3_fonts.py
```

From `/data/data/com.termux/files/usr/tmp/uitest`:

```sh
python render.py && node --check app.js && node test_ui.js && python test_backend.py
```

Expected: every gate green, including the four module self-tests and all backend/jsdom assertions.

- [ ] **Step 7: Commit**

```sh
git add project.py VERSION
git commit -m "ui: name internal storage and polish source route"
```

---

### Task 5: Live smoke and release

**Files:**
- Modify: none (verification only)
- Test: running app on port 5050

**Interfaces:**
- Consumes: committed `VERSION` `1.10.1` and rendered `/`.
- Produces: confirmed live page and running `sdmp3` service.

- [ ] **Step 1: Restart the service**

```sh
sdmp3 update && sdmp3 restart
sdmp3 version
```

Expected: `1.10.1` and a healthy restart with no stderr traceback.

- [ ] **Step 2: Smoke-test the rendered page**

```sh
curl -fsS http://localhost:5050/ -o /tmp/sdmp3-live.html
python - <<'PY'
html = open('/tmp/sdmp3-live.html', encoding='utf-8').read()
assert html.count('<script>') == 1
for gone in ('id="searchScopeItems"', 'id="searchScopeMore"', 'بحث عن مجلد أو ملف'):
    assert gone not in html, gone
for needed in ('id="searchControls"', 'id="sourceKindTag"', '--kb-inset', 'v1.10.1'):
    assert needed in html, needed
print('live page OK')
PY
```

Expected: `live page OK`.

- [ ] **Step 3: Verify git state**

```sh
git status --short
git log -3 --oneline
```

Expected: clean tree, three UI-polish commits on top of `f50c200`.

- [ ] **Step 4: Tag and push the patch release**

```sh
git tag -a v1.10.1 -m "v1.10.1: search and source-route polish"
git push origin main
git push origin v1.10.1
```

Expected: remote `main` and tag `v1.10.1` both updated.

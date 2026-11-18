# SDMP3 search + settings polish — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Amendment (execution):** the shipped version is **2.0.0**, per the user's
> instruction during execution. Every `2.0.0` below supersedes the plan's
> original `1.10.2`; the ledger records the ruling.

**Goal:** Put the search box directly under the file-browser sort row with a collapsed folder picker, make results reveal without a manual scroll, and move font/size settings into the «المظهر» tab.

**Architecture:** All product changes stay inside `HTML_TEMPLATE` in `project.py`: delete the standalone «البحث» section, merge its controls into «متصفح الملفات» as an attached header (sort → search → collapsed scope panel → tree), replace the reveal with a deterministic `scrollTop = 0` + conditional page scroll, and move the three typography controls into `#settingsPaneAppearance`. Docs (`AGENTS.md`) and `VERSION` follow.

**Tech Stack:** Python/Flask string template, server-derived theme tokens, vanilla JS, jsdom/fetch harness, Node syntax gate.

**Spec:** `docs/superpowers/specs/2026-11-17-sdmp3-search-settings-polish-design.md`

## Global Constraints

- `project.py` keeps **one** inline `<script>`; any JS syntax error disables the whole UI. Render `/`, extract that script, run `node --check` after every code task.
- `HTML_TEMPLATE` is a plain Python triple-quoted string: double backslashes in JS (`\\/`, `\\n`).
- No remote assets, no new colour literals; use `--field`, `--field-line`, `--border`, `--accent`, `--text-muted`, `--bg`, `--accent-text`.
- Every `font-size` is `calc(Npx * var(--font-scale))`; no raw `Npx`.
- `innerHTML` built from names/paths escapes with `esc()`; the scope toggle reads no path.
- Keep `/api/items` (it feeds `#searchSubfolder`).
- Search still renders into `#treeContainer` via `appendItemRows()`/`itemRowHtml()`; no separate results panel.
- Copy engine untouched (no backend/queue/sort/verify changes).
- Gates: `python -m py_compile project.py sdmp3_cli.py sdmp3_themes.py sdmp3_state.py sdmp3_groups.py sdmp3_fonts.py`, `bash -n install.sh`, the four module self-tests, render→`node --check`, `node test_ui.js`, `python test_backend.py`.
- Harnesses live outside the checkout at `/data/data/com.termux/files/usr/tmp/uitest/`; never `git add` them.

## Review Focus

- **A folder with 4 000 files and the panel collapsed.** The sub-folder `<select>` must still populate from `/api/items` while `#searchScopePanel` is `display:none`; pinned by the existing `t3 scope file list is gone but subfolder menu remains` (it calls `setSearchScope` with the panel collapsed) in Task 1.
- **The keyboard is open and the tree is below the fold.** A completed search must scroll the result head into view, not leave the user hunting; pinned by `t4 revealSearchResults resets the tree and scrolls only when hidden` in Task 2.
- **The reader is already looking at results.** Typing must not jump the page while the head is visible; pinned by the same Task 2 test (visible branch = zero `scrollBy` calls).
- **Zero results.** `#searchViewHead` still exists and reveal must not throw; pinned by the existing `t4 zero-result search reveals without throwing` after the reveal rewrite.
- **Saving a font from «المظهر».** The POST must apply `--app-font`/`--font-scale` and report in `#settingsSaveState`, not `#generalSaveState`; pinned by `t6 font saves report in the appearance pane` in Task 3.

---

### Task 1: Make search part of the file browser with a collapsed folder picker

**Files:**
- Modify: `project.py:2215-2231` (search CSS), `project.py:2648` (sprite), `project.py:2776-2839` (search + browser markup), `project.py:5014-5038` (scope JS)
- Modify: `AGENTS.md:220-264` (Page layout), `AGENTS.md:475-498` (Search scope)
- Test: `/data/data/com.termux/files/usr/tmp/uitest/test_ui.js`, `/data/data/com.termux/files/usr/tmp/uitest/test_backend.py`

**Interfaces:**
- Consumes: `setSearchScope`, `loadSearchScope`, `renderSearchSubfolders(items)`, `enterSearchSubfolder`, `searchScopeUp`, `openPathBrowser('search')`.
- Produces: `toggleSearchScope() -> undefined`; ids `#searchControls`, `#searchScopePanel` (collapsed by default), `#scopeToggleBtn`, `#ico-folder`; `#searchInput` is a descendant of the `#treeContainer` section and follows `#sourceSort`.

- [ ] **Step 1: Write the failing jsdom tests**

Add after `t3 scope file list is gone but subfolder menu remains` in `test_ui.js`:

```js
await check('t3 search sits inside the browser, above the tree', () => {
  const doc = window.document;
  const tree = doc.getElementById('treeContainer');
  const input = doc.getElementById('searchInput');
  if (!tree || !input) throw new Error('missing tree or search input');
  const section = tree.closest('section');
  if (!section || !section.contains(input)) throw new Error('search input is not in the browser section');
  const pos = doc.getElementById('sourceSort').compareDocumentPosition(input);
  if (!(pos & window.Node.DOCUMENT_POSITION_FOLLOWING)) {
    throw new Error('search input must come after the sort selector');
  }
});

await check('t3 folder scope panel is collapsed and toggles', () => {
  const doc = window.document;
  const panel = doc.getElementById('searchScopePanel');
  const btn = doc.getElementById('scopeToggleBtn');
  if (!panel || !btn) throw new Error('missing scope panel or toggle');
  if (panel.style.display !== 'none') throw new Error('scope panel should start collapsed');
  window.toggleSearchScope();
  if (panel.style.display === 'none') throw new Error('toggle did not open the panel');
  if (btn.getAttribute('aria-expanded') !== 'true') throw new Error('aria-expanded not set');
  window.toggleSearchScope();
  if (panel.style.display !== 'none') throw new Error('toggle did not close the panel');
});
```

- [ ] **Step 2: Run jsdom to verify it fails**

Run from `/data/data/com.termux/files/usr/tmp/uitest`: `python render.py && node --check app.js && node test_ui.js`
Expected: FAIL on `t3 search sits inside the browser...` (input not in the section) and `t3 folder scope panel...` (`window.toggleSearchScope is not a function`).

- [ ] **Step 3: Write the failing backend contract**

Append to `test_backend.py` before the final print:

```python
def t7_search_lives_in_browser():
    html = client.get('/').get_data(as_text=True)
    for gone in ('id="searchScopeItems"', 'id="searchScopeMore"',
                 'بحث عن مجلد أو ملف', '>البحث</h2>'):
        assert gone not in html, gone
    for needed in ('id="searchControls"', 'id="searchScopePanel"',
                   'id="scopeToggleBtn"', 'id="ico-folder"'):
        assert needed in html, needed
    sort_idx = html.index('ترتيب حسب:')
    input_idx = html.index('id="searchInput"')
    tree_idx = html.index('id="treeContainer"')
    assert sort_idx < input_idx < tree_idx, (sort_idx, input_idx, tree_idx)


check('t7 search lives in the browser above the tree', t7_search_lives_in_browser)
```

- [ ] **Step 4: Run the backend contract to verify it fails**

Run: `python test_backend.py`
Expected: FAIL `t7 ...: AssertionError: id="searchScopePanel"`.

- [ ] **Step 5: Replace the search/browser markup**

Delete the whole «البحث» `<section>` (the one containing `<h2>البحث</h2>` and `#historyBlock`) and replace the «متصفح الملفات» `<section>` body with this single section (keep the old `#historyBlock` inner rows exactly as they were):

```html
    <section class="section">
        <div class="section-head">
            <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
            <h2 class="section-title">متصفح الملفات</h2>
            <span class="section-kicker">المصدر</span>
            <span id="searchSpeedBadge" class="section-kicker" style="display:none; margin-inline-start:0; color:var(--accent-text);">
                <svg class="icon" style="width:12px;height:12px;" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg> فوري
            </span>
        </div>
        <div class="browser-head">
            <div class="browser-sort">
                <span style="font-weight:600;">ترتيب حسب:</span>
                <select id="sourceSort" style="background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:3px 8px; font-size:calc(12px * var(--font-scale));">
                    <option value="name">الاسم</option>
                    <option value="date_desc">التاريخ (الأحدث أولاً)</option>
                    <option value="date_asc">التاريخ (الأقدم أولاً)</option>
                </select>
            </div>
            <div id="searchControls">
                <div class="browser-search">
                    <input type="text" id="searchInput" placeholder="ابحث في هذا المجلد..." style="margin-bottom:0; flex:1 1 160px;" aria-label="نص البحث">
                    <button class="btn btn-primary" onclick="searchDeep(true)">بحث</button>
                    <button type="button" class="btn btn-outline" id="historyToggleBtn" onclick="toggleHistoryBlock()" title="يعرض المجلدات التي حدّدتها سابقاً من البحث — اضغط عنصراً لإعادته إلى التحديد">السجل</button>
                    <button type="button" class="btn btn-outline btn-sm" id="scopeToggleBtn" onclick="toggleSearchScope()" aria-expanded="false" aria-controls="searchScopePanel" title="مجلد البحث وخياراته">
                        <svg class="icon" aria-hidden="true"><use href="#ico-folder"></use></svg>
                        مجلد البحث
                    </button>
                </div>
                <div class="browser-scope" id="searchScopePanel" style="display:none;">
                    <div class="scope-line">
                        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#ico-folder"></use></svg>
                        <div class="scope-path" id="searchScopePath" title="مجلد البحث الحالي">/sdcard</div>
                        <select class="scope-select" id="searchSubfolder" onchange="enterSearchSubfolder(this.value)" aria-label="مجلد فرعي للبحث">
                            <option value="">مجلد فرعي…</option>
                        </select>
                        <button type="button" class="btn btn-outline btn-sm" onclick="searchScopeUp()" title="مجلد أعلى">
                            <svg class="icon" aria-hidden="true"><use href="#ico-up"></use></svg> أعلى
                        </button>
                        <button type="button" class="btn btn-outline btn-sm" onclick="openPathBrowser('search')">تحديد مجلد</button>
                    </div>
                    <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color: var(--text-muted); font-size:calc(13px * var(--font-scale)); margin:0;">
                        <input type="checkbox" id="foldersOnlyCheck" class="checkbox" checked style="width:16px; height:16px; margin:0;">
                        بحث في أسماء المجلدات فقط
                    </label>
                    <div class="search-scope-hint" style="color:var(--text-muted); font-size:calc(12px * var(--font-scale));">تظهر النتائج داخل شجرة الملفات أسفل مربع البحث مباشرة.</div>
                </div>
            </div>
        </div>
        <div id="historyBlock" class="search-history" style="display:none;">
            <div class="history-head">
                <svg class="icon" style="width:14px;height:14px;color:var(--text-muted);" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 16 14"></polyline></svg>
                <span class="history-title">آخر نتائج البحث</span>
                <button type="button" class="btn btn-outline" onclick="clearSearchHistory()">مسح الكل</button>
            </div>
            <div id="historyChips" class="history-chips"></div>
            <div id="historyRows" class="history-rows"></div>
        </div>
        <div class="tree" id="treeContainer" style="border-radius:0 0 10px 10px;"></div>
    </section>
```

- [ ] **Step 6: Replace the search CSS**

Replace the `.search-sticky` / `.search-scope` block (`project.py:2215-2231`) with:

```css
        /* The file browser is one attached block: the sort row, the search row
           and the optional folder-scope panel on top, the scrollable tree
           beneath. Search sits directly above the results. */
        .browser-head { border:1px solid var(--field-line); border-bottom:0; border-radius:10px 10px 0 0;
            background:var(--field); }
        .browser-sort { display:flex; align-items:center; gap:8px; padding:6px 11px;
            font-size:calc(12px * var(--font-scale)); color:var(--text-muted);
            border-bottom:1px solid var(--field-line); }
        .browser-search { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:8px 10px; }
        .browser-search > .btn { white-space:nowrap; }
        .browser-scope { display:flex; flex-direction:column; gap:8px; padding:0 10px 10px; }
        .scope-line { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
        .scope-line > .icon { width:15px; height:15px; color:var(--accent); flex-shrink:0; }
        .scope-path { flex:1 1 130px; min-width:0; font-family:var(--num-font); direction:ltr; text-align:left;
            font-size:calc(11px * var(--font-scale)); color:var(--text);
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .scope-select { max-width:46%; padding:5px 8px; border-radius:7px; border:1px solid var(--border);
            background:var(--bg); color:var(--text); font-family:inherit; font-size:calc(11.5px * var(--font-scale)); }
        @media (max-width: 480px) { .browser-search { padding:6px 8px; } }
```

- [ ] **Step 7: Add the folder sprite symbol and the toggle function**

Add after `#ico-chevron-down` in the sprite:

```html
    <symbol id="ico-folder" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></symbol>
```

Add beside `searchScopeUp()`:

```js
    // The folder picker is occasional: folded until asked for, so the search
    // row and the results tree stay adjacent.
    function toggleSearchScope() {
        let panel = document.getElementById('searchScopePanel');
        let btn = document.getElementById('scopeToggleBtn');
        if (!panel) return;
        let open = panel.style.display === 'none' || !panel.style.display;
        panel.style.display = open ? 'flex' : 'none';
        if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
```

- [ ] **Step 8: Update `AGENTS.md`**

In «Page layout», change "four `<section class="section">` blocks" to "three", and add after the Route bullet:

```markdown
- **File browser section**: the sort row, the search row (`#searchControls`),
  the folded folder-scope panel (`#searchScopePanel`, opened by `#scopeToggleBtn`)
  and `#treeContainer` are one attached block; search sits directly above the
  results, so typing never leaves the input far from them.
```

Rewrite the «Search scope» bullets: keep the `searchScopePath` / `setSearchScope`
paragraph but change "reloads the entries" to "reloads the sub-folder menu", then
replace the `#searchScopeItems`/`SCOPE_FILES_STEP`/`toggleScopeItem` bullet with:

```markdown
- The folder picker is a **collapsed panel** (`#searchScopePanel`, opened by the
  small `#scopeToggleBtn`): path, `#searchSubfolder`, «أعلى» (`searchScopeUp`),
  «تحديد مجلد» (`openPathBrowser('search')`, the third `pickerMode`) and
  «المجلدات فقط». It is folded by default; the old `#searchScopeItems` list and
  `SCOPE_FILES_STEP` are gone. `/api/items` now feeds only `#searchSubfolder`.
```

- [ ] **Step 9: Run the focused gates**

Run from the checkout: `python -m py_compile project.py`
Run from `/data/data/com.termux/files/usr/tmp/uitest`: `python render.py && node --check app.js && node test_ui.js && python test_backend.py`
Expected: the two new `t3` checks and `t7` pass; the reveal/settings checks may still fail (Task 2/3).

- [ ] **Step 10: Commit**

```sh
git add project.py AGENTS.md
git commit -m "ui: fold search into the file browser with a collapsed folder picker"
```

---

### Task 2: Reveal results without a manual scroll

**Files:**
- Modify: `project.py:5071-5085` (`revealSearchResults`)
- Test: `/data/data/com.termux/files/usr/tmp/uitest/test_ui.js`

**Interfaces:**
- Consumes: `#treeContainer`, `#searchViewHead`, `#actionBar`, `syncViewportInset()`.
- Produces: `revealSearchResults() -> undefined` that sets `#treeContainer.scrollTop = 0` and calls `window.scrollBy({top, behavior:'smooth'})` only when the result head is off-screen or within 72px of the visible bottom.

- [ ] **Step 1: Write the failing test**

Replace `t4 revealSearchResults uses nearest scrolling` in `test_ui.js` with:

```js
await check('t4 revealSearchResults resets the tree and scrolls only when hidden', async () => {
  const doc = window.document;
  doc.getElementById('searchInput').value = 'a';
  window.searchDeep(false);
  await waitFor(() => !!doc.getElementById('searchViewHead'));
  const tree = doc.getElementById('treeContainer');
  const head = doc.getElementById('searchViewHead');
  window.__scrollBys = [];
  window.scrollBy = arg => window.__scrollBys.push(arg);

  head.getBoundingClientRect = () => ({ top: 120, bottom: 180, left: 0, right: 0, width: 300, height: 60 });
  window.revealSearchResults();
  if (window.__scrollBys.length) throw new Error('moved while visible: ' + JSON.stringify(window.__scrollBys));
  if (tree.scrollTop !== 0) throw new Error('tree scrollTop=' + tree.scrollTop);

  head.getBoundingClientRect = () => ({ top: 900, bottom: 960, left: 0, right: 0, width: 300, height: 60 });
  window.__scrollBys = [];
  window.revealSearchResults();
  if (!window.__scrollBys.length) throw new Error('did not reveal an off-screen result');
  if (!(window.__scrollBys[0].top > 700)) throw new Error('wrong delta: ' + JSON.stringify(window.__scrollBys));
});
```

Also add `window.scrollBy = function () {};` beside `window.scrollTo = function () {};` in `beforeParse`.

- [ ] **Step 2: Run to verify it fails**

Run: `python render.py && node --check app.js && node test_ui.js`
Expected: FAIL `t4 revealSearchResults resets the tree...` (no `scrollBy` call; `scrollTop` untouched).

- [ ] **Step 3: Implement the reveal**

Replace `revealSearchResults()` with:

```js
    // After a search the results must be visible without a manual scroll. The
    // tree is the results panel and sits right under the search row now, so the
    // common case needs no movement; only an off-screen head is scrolled into
    // view. `nearest`-style scrolling used to do nothing when the head was only
    // partly visible, which is why this computes the delta explicitly.
    function revealSearchResults() {
        syncViewportInset();
        let tree = document.getElementById('treeContainer');
        if (!tree) return;
        tree.scrollTop = 0;                     // a new result set starts at its first row
        let head = document.getElementById('searchViewHead') || tree;
        let rect = head.getBoundingClientRect();
        let visibleBottom = window.innerHeight || 0;
        let vv = window.visualViewport;
        if (vv && vv.height) visibleBottom = Math.min(visibleBottom, vv.height + (vv.offsetTop || 0));
        let bar = document.getElementById('actionBar');
        if (bar) {
            let barTop = bar.getBoundingClientRect().top;
            if (barTop > 0) visibleBottom = Math.min(visibleBottom, barTop);
        }
        if (rect.top < 0 || rect.top > visibleBottom - 72) {
            let delta = rect.top - 12;
            if (window.scrollBy) window.scrollBy({ top: delta, behavior: 'smooth' });
            else document.documentElement.scrollTop += delta;
        }
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `python render.py && node --check app.js && node test_ui.js`
Expected: `t4 revealSearchResults resets the tree...` and `t4 zero-result search reveals without throwing` pass.

- [ ] **Step 5: Commit**

```sh
git add project.py
git commit -m "ui: reveal search results without a manual scroll"
```

---

### Task 3: Move typography into «المظهر» and bump the version

**Files:**
- Modify: `project.py:3015-3057` (settings panes), `project.py:5390-5404` (`pickFont`/`pickFontSize`)
- Modify: `AGENTS.md:379-403` (Settings modal), `AGENTS.md:403` (heading)
- Modify: `VERSION`
- Test: both harnesses

**Interfaces:**
- Consumes: `saveTypography(payload, stateId)`, `#fontSelect`, `#fontPreview`, `#sizeSelect`, `#settingsSaveState`.
- Produces: the three typography ids live inside `#settingsPaneAppearance`; `pickFont`/`pickFontSize` write status to `#settingsSaveState`; `VERSION = 2.0.0`.

- [ ] **Step 1: Write the failing tests**

Add to `test_ui.js` before the closing of the async block:

```js
await check('t6 fonts live in the appearance tab', () => {
  const doc = window.document;
  const appearance = doc.getElementById('settingsPaneAppearance');
  const general = doc.getElementById('settingsPaneGeneral');
  for (const id of ['fontSelect', 'sizeSelect', 'fontPreview']) {
    const el = doc.getElementById(id);
    if (!el) throw new Error('missing #' + id);
    if (!appearance.contains(el)) throw new Error('#' + id + ' is not in the appearance pane');
    if (general.contains(el)) throw new Error('#' + id + ' still in general');
  }
});

await check('t6 font saves report in the appearance pane', async () => {
  calls.length = 0;
  const app = window.document.getElementById('settingsSaveState');
  const gen = window.document.getElementById('generalSaveState');
  if (app) app.textContent = '';
  if (gen) gen.textContent = '';
  const sel = window.document.getElementById('fontSelect');
  const other = Array.from(sel.options).find(o => o.value !== sel.value);
  window.pickFont(other.value);
  await waitFor(() => calls.some(c => c.url === '/api/settings' && JSON.parse(c.opts.body).font));
  if (gen && gen.textContent) throw new Error('font status leaked to general: ' + gen.textContent);
  if (!app || !app.textContent) throw new Error('font status did not reach #settingsSaveState');
});
```

Append to `test_backend.py` before the final print:

```python
def t8_typography_in_appearance():
    html = client.get('/').get_data(as_text=True)
    app = html[html.index('id="settingsPaneAppearance"'):html.index('id="settingsPaneGeneral"')]
    gen = html[html.index('id="settingsPaneGeneral"'):html.index('id="settingsPaneIndex"')]
    for sid in ('fontSelect', 'sizeSelect', 'fontPreview'):
        assert 'id="{}"'.format(sid) in app, sid
        assert 'id="{}"'.format(sid) not in gen, sid
    assert 'v2.0.0' in html


check('t8 typography lives in the appearance tab', t8_typography_in_appearance)
```

Also change the `t6_ui_polish_rendered_contract` assertion `'v1.10.1'` to `'v2.0.0'`.

- [ ] **Step 2: Run to verify it fails**

Run: `python test_backend.py` and `node test_ui.js`
Expected: FAIL `t8 ...` (fontSelect not in appearance) and `t6 fonts live in the appearance tab`.

- [ ] **Step 3: Move the markup**

Cut the three typography blocks (`#fontSelect` + `#fontPreview`, and the `#sizeSelect` field) out of `#settingsPaneGeneral` and insert them in `#settingsPaneAppearance` after the theme `settings-field` and before `#settingsSaveState`. Keep the ids and Jinja loops exactly. `#settingsPaneGeneral` keeps only the «بعد النسخ» note and the auto-spoof switch.

- [ ] **Step 4: Route the status line**

In `pickFont` and `pickFontSize`, change the `saveTypography(..., 'generalSaveState')` argument to `'settingsSaveState'`. Leave `pickAutoSpoof` on `'generalSaveState'`.

- [ ] **Step 5: Bump the version and sync the harness**

Write `2.0.0` to `VERSION`; change the jsdom `/api/system` stub to `version: '2.0.0'`.

- [ ] **Step 6: Update `AGENTS.md`**

In «Settings modal», replace the «المظهر» and «عام» bullets: «المظهر» now owns the theme selector **and** `#fontSelect` / `#sizeSelect` / `#fontPreview` (saving through `#settingsSaveState`); «عام» owns only the `auto_spoof` switch. Rename the `## General settings: fonts` heading to `## Typography: fonts`.

- [ ] **Step 7: Run the full gates**

```sh
python -m py_compile project.py sdmp3_cli.py sdmp3_themes.py sdmp3_state.py sdmp3_groups.py sdmp3_fonts.py
bash -n install.sh
python sdmp3_themes.py && python sdmp3_state.py && python sdmp3_groups.py && python sdmp3_fonts.py
```
Then from `/data/data/com.termux/files/usr/tmp/uitest`: `python render.py && node --check app.js && node test_ui.js && python test_backend.py`
Expected: all green.

- [ ] **Step 8: Commit**

```sh
git add project.py VERSION AGENTS.md
git commit -m "ui: move typography into the appearance tab"
```

---

### Task 4: Live smoke and release

**Files:** none (verification only)

**Interfaces:**
- Consumes: committed `VERSION` `2.0.0` and rendered `/`.

- [ ] **Step 1: Smoke-test the working tree on a spare port**

```sh
python project.py --port 5051 --no-wakelock > /data/data/com.termux/files/usr/tmp/opencode/sdmp3-smoke-5051.log 2>&1 &
# poll http://localhost:5051/ until it answers, then:
python - <<'PY'
html = open('/data/data/com.termux/files/usr/tmp/opencode/sdmp3-live.html', encoding='utf-8').read()
assert html.count('<script>') == 1
for gone in ('id="searchScopeItems"', 'id="searchScopeMore"', 'بحث عن مجلد أو ملف', '>البحث</h2>'):
    assert gone not in html, gone
for needed in ('id="searchScopePanel"', 'id="scopeToggleBtn"', 'v2.0.0'):
    assert needed in html, needed
print('live page OK')
PY
```

- [ ] **Step 2: Full gate and git state**

Run the full gate commands from Task 3 Step 7; `git status --short` must be clean and the three commits present.

- [ ] **Step 3: Release only after explicit approval**

`sdmp3` installs from a separate clone, so the live service stays on the old version until the new commits are pushed. Ask the user before running:

```sh
git tag -a v2.0.0 -m "v2.0.0: search and settings polish"
git push origin main
git push origin v2.0.0
sdmp3 update && sdmp3 restart && sdmp3 version
```

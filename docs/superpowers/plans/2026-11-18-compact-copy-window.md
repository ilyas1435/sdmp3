# SDMP3 compact copy window + folded route + search-scope dialog — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (chosen) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the copy dialog minimizable with a queue hook, fold the route line down to volume kinds, move the folder-scope controls into a dismissible dialog, and turn the storage picker into one horizontal icon row.

**Architecture:** Everything is inside `HTML_TEMPLATE` in `project.py` except one server-side addition (`short` in `STORAGE_KINDS` / `get_available_storages()`). No backend copy-engine change: the queue is a JS array plus a hidden renderer, so the next batch can push jobs without touching the worker.

**Spec:** `docs/superpowers/specs/2026-11-18-compact-copy-window-design.md`

## Global Constraints

- One inline `<script>`; `HTML_TEMPLATE` is a plain Python string — double backslashes.
- No colour literals; every `font-size` is `calc(Npx * var(--font-scale))`.
- `innerHTML` from names/paths uses `esc()`; tiles carry `data-path` only.
- `#progressModal` is never closed by Esc; only its own buttons close it.
- Copy stays single-threaded, one file at a time; no route/worker change.
- Gates: `python -m py_compile …`, `bash -n install.sh`, four module self-tests, render→`node --check`, `node test_ui.js`, `python test_backend.py`.
- Harnesses live in `/data/data/com.termux/files/usr/tmp/uitest/` — never `git add` them.

## Review Focus

- **Minimized dialog must stay minimized.** A status poll returning `copying` must not re-open it; only the bar button or a terminal state does.
- **A finished copy is never silent.** `done`/`error`/`cancelled` restores a minimized dialog once, with verification and errors visible.
- **Destination unset.** The folded route must say «لم تُحدَّد» instead of an invisible pill.
- **Source outside every known volume.** The summary must still say something («مسار مخصص»), not render an empty line.
- **Scope dialog with no card mounted.** Opening it must not throw; `/api/items` failing leaves the sub-folder menu empty.
- **Esc and backdrop.** Esc closes the scope dialog but never `#progressModal`.

---

### Task 1: Slim, minimizable progress dialog with a queue hook

**Files:** `project.py` (progress modal markup ~3251-3277, modal CSS ~2442, action bar markup ~2868-2885, JS `showProgressModal`/`checkTransferStatus`/`resetTransfer` ~6334-6692)

**Interfaces:**
- Produces: `minimizeProgressModal()`, `restoreProgressModal()`, `barPrimaryAction()`, `transferQueue`, `renderQueue()`; ids `#minimizeProgressBtn`, `#barActionBtn`, `#barActionLabel`, `#barQueueBadge`, `#queueBlock`, `#queueRows`, `#queueCount`.

- [ ] **Step 1: Write the failing jsdom tests**

Replace the existing `t5 progress modal explains background continuation` check with:

```js
await check('t5 progress dialog has no continuation note and can be minimized', () => {
  const doc = window.document;
  if (doc.getElementById('backgroundNote')) throw new Error('background note still rendered');
  if (doc.documentElement.innerHTML.includes('يستمر النسخ في الخلفية')) throw new Error('note text still present');
  if (!doc.getElementById('minimizeProgressBtn')) throw new Error('missing minimize button');
  if (!doc.getElementById('queueBlock') || !doc.getElementById('queueRows')) throw new Error('missing queue hook');
  if (!doc.getElementById('barActionLabel')) throw new Error('missing action-bar label');
});

await check('t5 minimizing hides the dialog and a running poll keeps it hidden', async () => {
  const doc = window.document;
  window.showProgressModal();
  const modal = doc.getElementById('progressModal');
  if (modal.style.display !== 'flex') throw new Error('dialog did not open');
  window.minimizeProgressModal();
  if (modal.style.display !== 'none') throw new Error('minimize did not hide the dialog');
  statusResponse = { status: 'copying', action: 'copy', phase: 'copying', progress: 30, total: 10, copied: 3, file: 'x.mp3' };
  window.checkTransferStatus();
  await waitFor(() => doc.getElementById('barActionLabel').textContent.includes('استعادة'));
  if (modal.style.display !== 'none') throw new Error('a running poll reopened the dialog');
  statusResponse = { status: 'idle', phase: '', progress: 0 };
  window.checkTransferStatus();
  await flush();
});

await check('t5 bar button restores the dialog and a finished run restores it too', async () => {
  const doc = window.document;
  const modal = doc.getElementById('progressModal');
  window.minimizeProgressModal();
  window.barPrimaryAction();
  if (modal.style.display !== 'flex') throw new Error('bar button did not restore');
  window.minimizeProgressModal();
  statusResponse = { status: 'done', action: 'copy', phase: '', progress: 100, total: 10, copied: 10, time_taken: '2.0s' };
  window.checkTransferStatus();
  await waitFor(() => modal.style.display === 'flex');
  statusResponse = { status: 'idle', phase: '', progress: 0 };
  window.checkTransferStatus();
  await flush();
});

await check('t5 queue block stays hidden while the queue is empty', () => {
  const doc = window.document;
  window.eval('transferQueue.length = 0');
  window.renderQueue();
  if (doc.getElementById('queueBlock').style.display !== 'none') throw new Error('empty queue is visible');
  window.eval('transferQueue.push({label: "دفعة ثانية", sizeText: "1 GB"})');
  window.renderQueue();
  if (doc.getElementById('queueBlock').style.display === 'none') throw new Error('queued job not shown');
  if (!doc.getElementById('queueRows').textContent.includes('دفعة ثانية')) throw new Error('queue row missing');
  window.eval('transferQueue.length = 0');
  window.renderQueue();
});
```

- [ ] **Step 2: Run to verify it fails**

`python render.py && node --check app.js && node test_ui.js`
Expected: FAIL on the missing ids and `window.minimizeProgressModal is not a function`.

- [ ] **Step 3: Write the failing backend contract**

```python
def t9_compact_progress_dialog():
    html = client.get('/').get_data(as_text=True)
    for gone in ('id="backgroundNote"', 'يستمر النسخ في الخلفية', 'جاري التحضير...'):
        assert gone not in html, gone
    for needed in ('id="minimizeProgressBtn"', 'id="queueBlock"', 'id="barActionBtn"',
                   'id="barActionLabel"', 'id="queueRows"', 'function minimizeProgressModal',
                   'function restoreProgressModal'):
        assert needed in html, needed


check('t9 compact progress dialog', t9_compact_progress_dialog)
```

Run `python test_backend.py` → FAIL `id="backgroundNote"` first.

- [ ] **Step 4: Markup — header row, drop the note, add the queue block**

Replace the `<h2 id="modalTitle">…</h2>` line and the `#backgroundNote` block with:

```html
        <div class="modal-head-row">
            <h2 id="modalTitle">العملية قيد التنفيذ...</h2>
            <button type="button" class="icon-btn" id="minimizeProgressBtn" onclick="minimizeProgressModal()" aria-label="تصغير النافذة" title="تصغير ومتابعة تصفح الموقع">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </button>
        </div>
```

`#currentFile` becomes empty (`…` only as the inline placeholder is removed; keep the element).

After `#timeTakenDiv` add:

```html
        <div id="queueBlock" class="queue-block" style="display:none;">
            <div class="queue-head">
                <span class="queue-title">قائمة الانتظار</span>
                <span id="queueCount" class="num"></span>
            </div>
            <div id="queueRows" class="queue-rows"></div>
        </div>
```

Action bar: give the primary button an id, a label span and the queue badge:

```html
                    <span id="barQueueBadge" class="actionbar-eta" style="display:none;"></span>
                </span>
            </button>
            <span id="cartGroupPill" ...></span>
            <button class="btn btn-primary actionbar-go" id="barActionBtn" onclick="barPrimaryAction()">
                <svg class="icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                <span id="barActionLabel">بدء النسخ</span>
            </button>
```

(The badge goes after `#barEta` inside `.actionbar-nums`.)

- [ ] **Step 5: CSS — replace `.background-note`, add head row + queue**

Replace the `.background-note` rules with:

```css
        .modal-head-row { display:flex; align-items:center; gap:10px; border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:6px; }
        .modal-head-row h2 { margin:0; flex:1 1 auto; font-size:calc(16px * var(--font-scale)); border:0; padding:0; }
        .queue-block { margin-top:14px; border:1px solid var(--field-line); border-radius:10px; background:var(--field); padding:8px 10px; }
        .queue-head { display:flex; align-items:center; justify-content:space-between; gap:8px;
            font-size:calc(11px * var(--font-scale)); color:var(--text-muted); letter-spacing:.04em; }
        .queue-rows { display:flex; flex-direction:column; gap:4px; margin-top:6px; }
        .queue-row { display:flex; align-items:center; gap:8px; font-size:calc(12px * var(--font-scale)); color:var(--text); }
        .queue-row-label { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .queue-row-size { color:var(--text-muted); }
```

- [ ] **Step 6: JS — minimize/restore, bar label, queue**

Replace `showProgressModal()` and add beside it:

```js
    let progressModalVisible = false;
    let progressMinimized = false;
    let transferRunning = false;
    // Queue hook for the upcoming scheduled copy: the next batch only has to
    // push {label, sizeText, ...} and call renderQueue(). Nothing enqueues yet.
    let transferQueue = [];

    function showProgressModal() {
        progressMinimized = false;
        document.getElementById('progressModal').style.display = 'flex';
        progressModalVisible = true;
        document.getElementById('cancelProgressBtn').style.display = 'block';
        document.getElementById('closeProgressBtn').style.display = 'none';
        document.getElementById('timeTakenDiv').style.display = 'none';
        document.getElementById('cardWarningBox').style.display = 'none';
        document.getElementById('inlineNotify').style.display = 'none';
        document.getElementById('currentFile').style.color = 'var(--text-muted)';
    }

    // The dialog is an interruption, so it folds into the action bar instead of
    // covering the page: browsing and selecting the next batch stay possible.
    function minimizeProgressModal() {
        let modal = document.getElementById('progressModal');
        if (modal) modal.style.display = 'none';
        progressModalVisible = false;
        progressMinimized = true;
    }

    function restoreProgressModal() {
        let modal = document.getElementById('progressModal');
        if (!modal) return;
        progressMinimized = false;
        modal.style.display = 'flex';
        progressModalVisible = true;
    }

    function barPrimaryAction() {
        if (transferRunning) restoreProgressModal();
        else startTransfer();
    }

    function renderQueue() {
        let block = document.getElementById('queueBlock');
        let rows = document.getElementById('queueRows');
        let count = document.getElementById('queueCount');
        let badge = document.getElementById('barQueueBadge');
        if (!block || !rows) return;
        let n = transferQueue.length;
        block.style.display = n ? 'block' : 'none';
        if (count) count.textContent = n ? String(n) : '';
        if (badge) {
            badge.style.display = n ? '' : 'none';
            badge.textContent = n ? ('+' + n + ' في الانتظار') : '';
        }
        rows.innerHTML = n ? transferQueue.map(function(job, i) {
            return '<div class="queue-row"><span class="num">' + (i + 1) + '</span>'
                + '<span class="queue-row-label">' + esc(job.label || '') + '</span>'
                + '<span class="queue-row-size num">' + esc(job.sizeText || '') + '</span></div>';
        }).join('') : '';
    }
```

In `checkTransferStatus()`:
- auto-open line becomes `if(!progressModalVisible && !progressMinimized) showProgressModal();`
- after `mirrorProgressInBar(data)` add:

```js
                transferRunning = (data.status === 'copying');
                let label = document.getElementById('barActionLabel');
                if (label) {
                    if (transferRunning) {
                        let pct = (data.phase === 'verifying') ? (data.verify_progress || 0)
                            : (data.action === 'delete' ? null : (data.progress || 0));
                        label.textContent = 'استعادة النسخ' + (pct === null ? '' : ' ' + pct + '٪');
                    } else {
                        label.textContent = 'بدء النسخ';
                    }
                }
```

- inside the terminal branch (`done`/`cancelled`/`error`), call `if (progressMinimized) restoreProgressModal();` before the `cancel/close` button switch.
- set `transferRunning = false;` in that branch too.

In `resetTransfer()`, reset `progressMinimized = false; transferRunning = false;` and restore the bar label to «بدء النسخ».

At the end of the script's top-level initialisation add `renderQueue();`.

- [ ] **Step 7: Run the focused gates**

`python -m py_compile project.py`, then `python render.py && node --check app.js && node test_ui.js && python test_backend.py`.
Expected: new t5 checks + t9 pass; route/scope/storage checks still pass.

- [ ] **Step 8: Commit**

```sh
git add project.py
git commit -m "ui: minimizable copy dialog with a queue hook"
```

---

### Task 2: Folded route states volume kinds, not paths

**Files:** `project.py` (summary markup ~2680-2694, route CSS ~2192-2214, `updateRouteSummary` ~4360-4375, `applySourceKindTag` ~4447-4461)

**Interfaces:**
- Consumes `sourceKindLabel(kind)`, `storageForPath(path)`.
- Produces `#destKindTag`; `applySourceKindTag(el, path, emptyText)` gains an optional fallback text.

- [ ] **Step 1: Write the failing jsdom test**

Add after the source-kind test:

```js
await check('t5 folded route shows volume kinds instead of paths', () => {
  const doc = window.document;
  const summary = doc.querySelector('#routeDetails > summary');
  if (!summary) throw new Error('missing route summary');
  if (summary.querySelector('.section-step')) throw new Error('step numbers still in the summary');
  if (summary.querySelector('.route-sum-path')) throw new Error('paths still in the summary');
  const labels = Array.from(summary.querySelectorAll('.route-sum-label')).map(e => e.textContent.trim());
  if (labels[0] !== 'المصدر') throw new Error('source label: ' + JSON.stringify(labels));
  doc.getElementById('custom_source_path').value = '/sdcard/Music';
  doc.getElementById('custom_dest_path').value = '';
  window.updateRouteSummary();
  const src = doc.getElementById('sourceKindTag');
  const dst = doc.getElementById('destKindTag');
  if (!src || src.textContent !== 'المساحة الداخلية') throw new Error('source kind: ' + (src && src.textContent));
  if (!dst || dst.textContent !== 'لم تُحدَّد') throw new Error('dest placeholder: ' + (dst && dst.textContent));
  doc.getElementById('custom_source_path').value = '/mnt/custom/elsewhere';
  window.updateRouteSummary();
  if (!src.textContent.trim()) throw new Error('unknown source rendered nothing');
});
```

- [ ] **Step 2: Run to verify it fails**

`python render.py && node --check app.js && node test_ui.js` → FAIL (`step numbers still in the summary`).

- [ ] **Step 3: Replace the summary markup**

```html
            <summary>
                <span class="route-sum">
                    <span class="route-sum-label">المصدر</span>
                    <span class="pill kind-tag" id="sourceKindTag">—</span>
                </span>
                <span class="route-sum">
                    <span class="route-sum-label">الوجهة</span>
                    <span class="pill kind-tag is-empty" id="destKindTag">لم تُحدَّد</span>
                </span>
                <svg class="icon route-chev" viewBox="0 0 24 24" aria-hidden="true"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </summary>
```

- [ ] **Step 4: CSS — taller summary, drop the path rules**

`.route-details > summary` padding becomes `12px 12px`; delete `.route-sum-path` and `.route-sum-path.is-empty`; add:

```css
        .kind-tag.is-empty { color:var(--text-muted); border-color:var(--border); background:transparent; }
        .route-sum-label { font-size:calc(12px * var(--font-scale)); }
```

Remove the phone rule that hides `.route-sum-label` (the label is now the only name for each pill).

- [ ] **Step 5: Rewrite `updateRouteSummary` + extend the tag helper**

```js
    function updateRouteSummary() {
        let srcBox = document.getElementById('custom_source_path');
        let dstBox = document.getElementById('custom_dest_path');
        let src = (srcBox && srcBox.value) || currentSourceRoot || '';
        let dst = (dstBox && dstBox.value) || '';
        // The folded line answers "which volume", never "which folder": the
        // full path stays in the expanded block and in the pill's title.
        applySourceKindTag(document.getElementById('sourceKindTag'), src, 'مسار مخصص');
        applySourceKindTag(document.getElementById('destKindTag'), dst, 'لم تُحدَّد');
        applySourceKindTag(document.getElementById('sourcePathKindTag'), src);
    }
```

In `applySourceKindTag`, add the third parameter and use it before hiding:

```js
    function applySourceKindTag(el, path, emptyText) {
        if (!el) return;
        let st = storageForPath(path);
        STORAGE_KIND_LIST.forEach(function(k) { el.classList.remove('pill-kind-' + k); });
        if (!st) {
            if (emptyText) {
                el.textContent = emptyText;
                el.title = '';
                el.classList.add('is-empty');
                el.style.display = '';
            } else {
                el.style.display = 'none';
                el.textContent = '';
                el.title = '';
            }
            return;
        }
        el.classList.remove('is-empty');
        el.textContent = sourceKindLabel(st.kind);
        el.title = (st.name || sourceKindLabel(st.kind)) + ' — ' + (st.path || path);
        el.classList.add('pill-kind-' + st.kind);
        el.style.display = '';
    }
```

- [ ] **Step 6: Backend contract**

```python
def t10_route_summary_shows_kinds():
    html = client.get('/').get_data(as_text=True)
    for gone in ('id="routeSourceSummary"', 'id="routeDestSummary"', 'route-sum-path'):
        assert gone not in html, gone
    assert 'id="destKindTag"' in html
    summary = html[html.index('id="routeDetails"'):html.index('</summary>')]
    assert '.section-step' not in summary and 'section-step' not in summary
    assert 'المصدر' in summary


check('t10 folded route shows volume kinds', t10_route_summary_shows_kinds)
```

Update the harness `t6`/`t4` assertions that mention `id="routeSourceSummary"` if any.

- [ ] **Step 7: Run gates, commit**

```sh
python -m py_compile project.py && cd /data/data/com.termux/files/usr/tmp/uitest && python render.py && node --check app.js && node test_ui.js && python test_backend.py
git add project.py && git commit -m "ui: fold the route line down to volume kinds"
```

---

### Task 3: Search-scope dialog beside the index button

**Files:** `project.py` (browser markup ~2800-2830, `.route-joc` button ~2773, modal list ~3201+, JS `toggleSearchScope` ~5022-5033, search CSS ~2222-2230)

**Interfaces:**
- Produces `openSearchScope()`, `closeSearchScope(event)`; ids `#searchScopeModal`, `#scopeToggleBtn` (now in `.route-joc-row`).
- Removes `#searchScopePanel`, `toggleSearchScope()`.

- [ ] **Step 1: Failing jsdom tests**

Rewrite `t3 folder scope panel is collapsed and toggles` as:

```js
await check('t3 folder scope is a dismissible dialog next to the JOC button', () => {
  const doc = window.document;
  const modal = doc.getElementById('searchScopeModal');
  const btn = doc.getElementById('scopeToggleBtn');
  if (!modal || !btn) throw new Error('missing scope dialog or button');
  if (modal.style.display === 'flex') throw new Error('scope dialog starts open');
  if (!btn.closest('.route-joc-row')) throw new Error('scope button is not in the JOC row');
  if (doc.getElementById('searchScopePanel')) throw new Error('inline scope panel still exists');
  window.openSearchScope();
  if (modal.style.display !== 'flex') throw new Error('openSearchScope did not open');
  if (!modal.querySelector('#searchSubfolder')) throw new Error('dialog lost the subfolder menu');
  window.closeSearchScope({ target: modal });
  if (modal.style.display !== 'none') throw new Error('backdrop click did not close');
  window.openSearchScope();
  window.closeSearchScope();
  if (modal.style.display !== 'none') throw new Error('close button path did not close');
});

await check('t3 Esc closes the scope dialog but never the progress dialog', () => {
  const doc = window.document;
  window.openSearchScope();
  window.showProgressModal();
  doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
  if (doc.getElementById('searchScopeModal').style.display !== 'none') throw new Error('Esc did not close the scope dialog');
  if (doc.getElementById('progressModal').style.display !== 'flex') throw new Error('Esc closed the progress dialog');
  window.resetTransfer();
});
```

- [ ] **Step 2: Run to verify it fails** — `python render.py && node --check app.js && node test_ui.js`.

- [ ] **Step 3: Move the button, delete the inline panel, add the modal**

In `.browser-search` remove the `#scopeToggleBtn` button. Delete the whole `<div class="browser-scope" id="searchScopePanel">…</div>`.

Wrap the JOC button:

```html
        <div class="route-joc-row">
            <button class="btn btn-outline route-joc" onclick="openJocModal()">
                <svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path></svg>
                إنشاء فهرس JOC
            </button>
            <button type="button" class="btn btn-outline route-joc" id="scopeToggleBtn" onclick="openSearchScope()" aria-haspopup="dialog" aria-controls="searchScopeModal">
                <svg class="icon" aria-hidden="true"><use href="#ico-folder"></use></svg>
                مجلد البحث
            </button>
        </div>
```

Add the modal **after** `#progressModal` (so Esc closes this dialog first and the
copy dialog keeps its own close button; a modal placed earlier would be skipped by
the Esc handler, which always takes the last open dialog):

```html
<div class="modal" id="searchScopeModal" onclick="closeSearchScope(event)">
    <div class="modal-content" style="max-width:460px;">
        <h3 style="margin-top:0; font-size:calc(16px * var(--font-scale)); border-bottom:1px solid var(--border); padding-bottom:15px;">مجلد البحث</h3>
        <div class="settings-note">البحث يجري داخل هذا المجلد وحده، ولا يغيّر مجلد التصفح.</div>
        <div class="scope-line">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#ico-folder"></use></svg>
            <div class="scope-path" id="searchScopePath" title="مجلد البحث الحالي">/sdcard</div>
            <select class="scope-select" id="searchSubfolder" onchange="enterSearchSubfolder(this.value)" aria-label="مجلد فرعي للبحث">
                <option value="">مجلد فرعي…</option>
            </select>
        </div>
        <div class="scope-line" style="margin-top:8px;">
            <button type="button" class="btn btn-outline btn-sm" onclick="searchScopeUp()" title="مجلد أعلى">
                <svg class="icon" aria-hidden="true"><use href="#ico-up"></use></svg> أعلى
            </button>
            <button type="button" class="btn btn-outline btn-sm" onclick="openPathBrowser('search')">تحديد مجلد</button>
        </div>
        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color:var(--text-muted); font-size:calc(13px * var(--font-scale)); margin:12px 0 0;">
            <input type="checkbox" id="foldersOnlyCheck" class="checkbox" checked style="width:16px; height:16px; margin:0;">
            بحث في أسماء المجلدات فقط
        </label>
        <div style="display:flex; gap:10px; margin-top:16px;">
            <button class="btn btn-outline" style="flex:1;" onclick="closeSearchScope()">إغلاق</button>
        </div>
    </div>
</div>
```

CSS: `.route-joc-row { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }` (and `.route-joc-row .route-joc { margin-top:0; }`), keep `.browser-scope` rules only if still used — delete them.

- [ ] **Step 4: JS**

Replace `toggleSearchScope()` with:

```js
    function openSearchScope() {
        let modal = document.getElementById('searchScopeModal');
        if (!modal) return;
        modal.style.display = 'flex';
        loadSearchScope();
    }

    function closeSearchScope(e) {
        if (e && e.target && e.target.id !== 'searchScopeModal') return;
        let modal = document.getElementById('searchScopeModal');
        if (modal) modal.style.display = 'none';
    }
```

- [ ] **Step 5: Backend contract**

```python
def t11_scope_dialog():
    html = client.get('/').get_data(as_text=True)
    for gone in ('id="searchScopePanel"', 'function toggleSearchScope'):
        assert gone not in html, gone
    for needed in ('id="searchScopeModal"', 'function openSearchScope', 'function closeSearchScope',
                   'class="route-joc-row"'):
        assert needed in html, needed
    row = html[html.index('route-joc-row'):html.index('id="searchScopeModal"')]
    assert 'id="scopeToggleBtn"' in row and 'إنشاء فهرس JOC' in row


check('t11 scope dialog beside the JOC button', t11_scope_dialog)
```

Update the old t7 assertion that requires `id="searchScopePanel"`.

- [ ] **Step 6: Gates + commit**

```sh
git add project.py && git commit -m "ui: open the search folder in a dismissible dialog"
```

---

### Task 4: Storage picker becomes one row of icon tiles

**Files:** `project.py` (`STORAGE_KINDS` ~588, `get_available_storages()` ~596-624, storage CSS ~2289-2313, both card loops ~2698-2740)

**Interfaces:**
- Produces `short` on every storage dict (`الهاتف` / `SD` / `OTG` / `كارت`).

- [ ] **Step 1: Failing backend test**

```python
def t12_storage_tiles():
    import project as pj
    shorts = {k: v['short'] for k, v in pj.STORAGE_KINDS.items()}
    assert shorts == {'internal': 'الهاتف', 'sdcard': 'SD', 'usb': 'OTG', 'card': 'كارت'}, shorts
    html = client.get('/').get_data(as_text=True)
    assert 'storage-short' in html
    for gone in ('class="storage-path"', 'class="storage-name"', 'class="storage-badge"'):
        assert gone not in html, gone
    assert html.count('class="storage-card kind-') >= 2


check('t12 storage picker is a row of icon tiles', t12_storage_tiles)
```

- [ ] **Step 2: Run → FAIL** (`KeyError: 'short'`).

- [ ] **Step 3: Server**

`STORAGE_KINDS` gains `'short'` per kind; `get_available_storages()` adds `'short': STORAGE_KINDS[kind]['short']` to both dicts.

- [ ] **Step 4: Markup + CSS**

Both card loops become:

```html
                    <button type="button" class="storage-card kind-{{ st.kind }}" data-path="{{ st.path }}" onclick="chooseStorage('source', this)" title="{{ st.name }} — {{ st.path }}">
                        <svg class="icon storage-ico"><use href="#{{ st.icon }}"></use></svg>
                        <span class="storage-short">{{ st.short }}</span>
                    </button>
```

CSS replaces the card block:

```css
        .storage-grid { display:flex; flex-direction:row; gap:8px; margin-bottom:10px; flex-wrap:wrap; }
        .storage-card { display:flex; flex:1 1 0; min-width:78px; flex-direction:column; align-items:center; gap:6px;
            padding:9px 6px; border:1px solid var(--field-line); border-radius:12px;
            background:var(--field); color:var(--text); cursor:pointer; text-align:center;
            transition:border-color .15s, background .15s; font-family:inherit; appearance:none; -webkit-appearance:none; }
        .storage-card:hover { border-color:var(--kind, var(--accent)); }
        .storage-card.active { border-color:var(--kind, var(--accent)); background:var(--kind-soft, var(--accent-soft)); }
        .storage-card .storage-ico { width:30px; height:30px; padding:5px; flex-shrink:0; box-sizing:border-box;
            border-radius:9px; background:var(--kind-tile, var(--accent-soft)); color:var(--kind, var(--text-muted)); }
        .storage-short { font-size:calc(11.5px * var(--font-scale)); font-weight:600; color:var(--text-muted); }
        .storage-card.active .storage-short { color:var(--kind-ink, var(--accent)); }
```

Keep the four `.kind-*` variable blocks. Delete `.storage-meta/.storage-name/.storage-path/.storage-badge` rules.

- [ ] **Step 5: jsdom check** — each visible card has a non-empty `.storage-short` and no `.storage-path`:

```js
await check('t5 storage cards are icon tiles with a one-word caption', () => {
  const cards = Array.from(window.document.querySelectorAll('#source_storage_cards .storage-card'));
  if (!cards.length) throw new Error('no storage cards');
  cards.forEach(c => {
    if (!c.querySelector('.storage-short')) throw new Error('missing caption');
    if (c.querySelector('.storage-path')) throw new Error('path still rendered');
  });
  if (!window.getComputedStyle) return;
});
```

- [ ] **Step 6: Gates + commit**

```sh
git add project.py && git commit -m "ui: shrink the storage picker to icon tiles"
```

---

### Task 5: Version, docs, full gates, smoke

**Files:** `VERSION`, `AGENTS.md`, harnesses

- [ ] **Step 1:** `VERSION` → `2.1.0`; jsdom `/api/system` stub → `'2.1.0'`; backend `v2.0.0` assertions → `v2.1.0`.

- [ ] **Step 2: AGENTS.md** —
  - «Page layout» → File browser bullet: the scope controls now live in the JOC row and open `#searchScopeModal`; no inline panel.
  - «Search scope»: replace the collapsed-panel bullet with the dialog (`openSearchScope`/`closeSearchScope`, Esc/backdrop), keep `searchScopePath` text.
  - «A copy lives in the Termux worker»: the modal is minimizable (`minimizeProgressModal`/`restoreProgressModal`, bar label «استعادة النسخ NN٪»); the `.background-note` claim is gone; note that `transferQueue`/`renderQueue()` is the hook for the scheduled-copy batch and that nothing enqueues yet.
  - «Route section» bullet: the folded line shows volume kinds (`#sourceKindTag`/`#destKindTag`), never paths; ١/٢ stay inside the expanded block.
  - Storage table/section: cards are icon tiles with `short` captions; `get_available_storages()` owns `short`.

- [ ] **Step 3: Full gates**

```sh
python -m py_compile project.py sdmp3_cli.py sdmp3_themes.py sdmp3_state.py sdmp3_groups.py sdmp3_fonts.py
bash -n install.sh
python sdmp3_themes.py && python sdmp3_state.py && python sdmp3_groups.py && python sdmp3_fonts.py
cd /data/data/com.termux/files/usr/tmp/uitest && python render.py && node --check app.js && node test_ui.js && python test_backend.py
```

- [ ] **Step 4: Smoke on 5051** — boot the working tree, assert one `<script>`, no `id="backgroundNote"`/`id="searchScopePanel"`, and presence of `id="searchScopeModal"`, `id="minimizeProgressBtn"`, `id="destKindTag"`, `storage-short`, `v2.1.0`.

- [ ] **Step 5: Commit + release after approval** — tag `v2.1.0`, push, `sdmp3 update && sdmp3 restart` only with explicit user approval.

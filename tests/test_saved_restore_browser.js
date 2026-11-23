// Full-page regression test using Chromium's built-in DevTools protocol.
// No browser library or application dependency is required.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdtempSync, rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const {join} = require('node:path');
const net = require('node:net');

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
    const server = net.createServer();
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const debugPort = server.address().port;
    await new Promise(resolve => server.close(resolve));
    const profile = mkdtempSync(join(tmpdir(), 'sdmp3-restore-browser-'));
    const chrome = spawn(process.env.CHROMIUM || '/repl/tools/bin/chromium', [
        '--headless', '--no-sandbox', '--disable-dev-shm-usage',
        '--no-first-run', '--disable-background-networking',
        `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
        'about:blank',
    ], {stdio: 'ignore'});
    let socket;
    try {
        let pages;
        for (let i = 0; i < 100; i++) {
            try {
                pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
                if (pages.some(page => page.type === 'page')) break;
            } catch (_) {}
            await delay(100);
        }
        assert(pages, 'Chromium failed to start');
        socket = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
        await new Promise((resolve, reject) => {
            socket.addEventListener('open', resolve, {once: true});
            socket.addEventListener('error', reject, {once: true});
        });
        let sequence = 0;
        const pending = new Map();
        const exceptions = [];
        socket.addEventListener('message', event => {
            const message = JSON.parse(event.data);
            if (message.method === 'Runtime.exceptionThrown') {
                exceptions.push(message.params.exceptionDetails);
            }
            const request = pending.get(message.id);
            if (!request) return;
            pending.delete(message.id);
            if (message.error) request.reject(new Error(JSON.stringify(message.error)));
            else request.resolve(message.result);
        });
        const send = (method, params = {}) => new Promise((resolve, reject) => {
            const id = ++sequence;
            pending.set(id, {resolve, reject});
            socket.send(JSON.stringify({id, method, params}));
        });
        const evaluate = async expression => {
            const result = await send('Runtime.evaluate', {
                expression, returnByValue: true, awaitPromise: true,
            });
            if (result.exceptionDetails) {
                throw new Error(JSON.stringify(result.exceptionDetails));
            }
            return result.result.value;
        };
        const waitFor = async expression => {
            for (let i = 0; i < 100; i++) {
                if (await evaluate(expression)) return;
                await delay(100);
            }
            throw new Error('Timed out: ' + expression);
        };
        await send('Runtime.enable');
        await send('Page.navigate', {url: process.env.SDMP3_TEST_URL});
        await waitFor("document.readyState === 'complete' && typeof loadSavedOperation === 'function'");
        const cases = JSON.parse(process.env.SDMP3_RESTORE_CASES);
        const results = [];
        for (const test of cases) {
            await evaluate(`selectedPaths.clear(); copyGroups = []; excludedPaths.clear();
                selectedSizes = {}; savedOperationLoadingId = '';
                openSavedOperationsModal();`);
            const selector = `[data-saved-operation-load="${test.id}"]`;
            await waitFor(`!!document.querySelector(${JSON.stringify(selector)})`);
            await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
            await waitFor("!savedOperationLoadingId");
            const result = await evaluate(`({
                paths: Array.from(selectedPaths.keys()),
                groups: copyGroups.map(group => ({name: group.name, paths: group.paths})),
                excludes: Array.from(excludedPaths),
                status: document.getElementById('savedOperationsStatus').textContent,
                warning: document.getElementById('groupStatusHint').textContent,
                closed: document.getElementById('savedOperationsModal').style.display === 'none',
                shown: document.getElementById('selectedModal').style.display === 'flex'
            })`);
            if (test.expectedError) {
                assert.deepEqual(result.paths, [], JSON.stringify(result));
                assert(result.status.includes(test.expectedError), JSON.stringify(result));
                assert.match(result.status, /الإصدار \d+\.\d+\]/, JSON.stringify(result));
                assert.equal(result.closed, false, JSON.stringify(result));
                results.push(result);
                continue;
            }
            assert.deepEqual(result.paths, test.paths, JSON.stringify(result));
            assert.equal(result.closed, true, JSON.stringify(result));
            assert.equal(result.shown, true, JSON.stringify(result));
            assert(!result.status.includes('خطأ داخلي'), JSON.stringify(result));
            if (test.warning) assert(result.warning.includes(test.warning), JSON.stringify(result));
            results.push(result);
        }
        assert.deepEqual(exceptions, [], JSON.stringify(exceptions));
        console.log(JSON.stringify(results));
    } finally {
        if (socket) socket.close();
        chrome.kill('SIGTERM');
        await Promise.race([
            new Promise(resolve => chrome.once('exit', resolve)), delay(3000),
        ]);
        if (chrome.exitCode === null) chrome.kill('SIGKILL');
        rmSync(profile, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
    }
}

main().catch(error => { console.error(error); process.exitCode = 1; });

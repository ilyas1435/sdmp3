const assert = require('node:assert/strict');
const {assignPaths, moveGroup, clampSegments, leafName} = require('../static/sdmp3_group_ops.js');

function group(id, name, paths = []) {
    return {id, name, paths};
}

assert.deepEqual(clampSegments(['قديم', 'الصف', 'المادة', 'الدرس']), [
    'الصف', 'المادة', 'الدرس'
], 'smart organization keeps nested source folders up to the supported limit');
assert.deepEqual(clampSegments(['../outside', 'مسار:قديم']), ['-outside', 'مسار-قديم']);
assert.equal(leafName('مجموعة/مادة/درس'), 'درس',
    'inline child-folder renaming receives only the last path segment');

{
    const groups = [group(1, 'أ', ['/src/1']), group(2, 'ب', ['/src/2'])];
    const result = assignPaths(groups, ['/src/2', '/src/1'], 2, ['/src/1', '/src/2']);
    assert.equal(result.error, '');
    assert.equal(result.moved, 2);
    assert.deepEqual(result.groups[0].paths, []);
    assert.deepEqual(result.groups[1].paths, ['/src/1', '/src/2']);
    assert.deepEqual(groups[0].paths, ['/src/1'], 'assignment must not mutate persisted input');
}

{
    const groups = [group(1, 'أ'), group(2, 'أ/ب'), group(3, 'ج')];
    const result = moveGroup(groups, 1, 3);
    assert.equal(result.error, '');
    assert.equal(result.moved, true);
    assert.deepEqual(result.groups.map(item => item.name), ['ج/أ', 'ج/أ/ب', 'ج']);
    assert.deepEqual(result.groups[1].paths, []);
    assert.deepEqual(groups.map(item => item.name), ['أ', 'أ/ب', 'ج']);
}

{
    const groups = [group(1, 'أ'), group(2, 'أ/ب')];
    const result = moveGroup(groups, 1, 2);
    assert.match(result.error, /نفسه/);
    assert.equal(result.moved, false);
}

{
    const groups = [
        group(1, 'المجموعة'),
        group(2, 'المجموعة/المادة'),
        group(3, 'المجموعة/المادة/الدرس'),
        group(4, 'أخرى')
    ];
    const result = moveGroup(groups, 2, 0);
    assert.equal(result.error, '');
    assert.equal(result.moved, true);
    assert.deepEqual(result.groups.map(item => item.name), [
        'المجموعة', 'المادة', 'المادة/الدرس', 'أخرى'
    ], 'moving a nested folder to the root also keeps its descendants');
}

{
    const groups = [group(1, 'أ'), group(2, 'أ/ب'), group(3, 'ج/د')];
    const result = moveGroup(groups, 1, 3);
    assert.match(result.error, /3 مستويات/);
    assert.deepEqual(groups.map(item => item.name), ['أ', 'أ/ب', 'ج/د']);
}

{
    const groups = [group(1, 'أ'), group(2, 'ج'), group(3, 'ج/أ')];
    const result = moveGroup(groups, 1, 2);
    assert.match(result.error, /نفس الاسم/);
    assert.deepEqual(groups.map(item => item.name), ['أ', 'ج', 'ج/أ']);
}

console.log('group operations: all checks passed');

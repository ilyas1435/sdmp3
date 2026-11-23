const assert = require('node:assert/strict');
const {assignPaths, moveGroup, clampSegments, leafName, renameDepthError} = require('../static/sdmp3_group_ops.js');

function group(id, name, paths = []) {
    return {id, name, paths};
}

assert.deepEqual(clampSegments(['قديم', 'الصف', 'المادة', 'الدرس']), [
    'الصف', 'المادة', 'الدرس'
], 'smart organization keeps nested source folders up to the supported limit');
assert.deepEqual(clampSegments(['../outside', 'مسار:قديم']), ['-outside', 'مسار-قديم']);
assert.deepEqual(clampSegments(['a:b?c*d']), ['a-b-c-d'],
    'every illegal character is sanitized, not just the first one');
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
    const smartGroups = [
        group(1, 'جذر'),
        group(2, 'جذر/فنان', ['/src/artist.mp3']),
        group(3, 'جذر/فنان/ألبوم', ['/src/album.mp3']),
        group(4, 'الأرشيف')
    ];
    const extracted = moveGroup(smartGroups, 2, 0);
    assert.equal(extracted.error, '');
    assert.deepEqual(extracted.groups.map(item => item.name), [
        'جذر', 'فنان', 'فنان/ألبوم', 'الأرشيف'
    ], 'smart-created nested folders can be extracted to the root');
    assert.deepEqual(extracted.groups[1].paths, ['/src/artist.mp3']);
    assert.deepEqual(extracted.groups[2].paths, ['/src/album.mp3']);

    const reparented = moveGroup(extracted.groups, 2, 4);
    assert.equal(reparented.error, '');
    assert.deepEqual(reparented.groups.map(item => item.name), [
        'جذر', 'الأرشيف/فنان', 'الأرشيف/فنان/ألبوم', 'الأرشيف'
    ], 'smart-created folders can be moved to another depth and parent');
    assert.deepEqual(reparented.groups[1].paths, ['/src/artist.mp3']);
    assert.deepEqual(reparented.groups[2].paths, ['/src/album.mp3']);
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

{
    const groups = [group(1, 'أ'), group(2, 'أ/ب'), group(3, 'أ/ب/ج')];
    assert.match(renameDepthError(groups, groups[0], 'ب/ج'),
        /3 مستويات/, 'renaming a parent with a slash must not push a child to level 4');
    assert.equal(renameDepthError(groups, groups[0], 'د'), '',
        'a plain rename keeps the chain within the depth limit');
    assert.equal(renameDepthError(groups, groups[1], 'ج'), '',
        'renaming the middle folder keeps its existing child at level 3');
}

console.log('group operations: all checks passed');

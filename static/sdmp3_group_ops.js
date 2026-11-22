(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.SDMP3GroupOps = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const MAX_DEPTH = 3;
    const MAX_PATH = 180;
    const MAX_SEGMENT = 80;
    const BAD_CHARS = /[\/\\:*?"<>|\u0000-\u001f]/;

    function parts(path) {
        return String(path || '').split('/').map(function (part) {
            return part.trim();
        }).filter(Boolean);
    }

    function key(path) {
        return parts(path).join('/').toLowerCase();
    }

    function clampSegments(input, maxDepth) {
        const depthLimit = Number.isInteger(maxDepth) && maxDepth > 0
            ? Math.min(maxDepth, MAX_DEPTH) : MAX_DEPTH;
        let segments = (input || []).map(function (segment) {
            return String(segment || '').trim()
                .replace(BAD_CHARS, '-')
                .replace(/[ .]+/g, ' ')
                .trim()
                .slice(0, MAX_SEGMENT)
                .trim();
        }).filter(function (segment) {
            return segment && segment !== '.' && segment !== '..';
        });
        if (segments.length > depthLimit) segments = segments.slice(-depthLimit);
        while (segments.length > 1 && segments.join('/').length > MAX_PATH) {
            segments = segments.slice(1);
        }
        return segments;
    }

    function assignPaths(groups, requestedPaths, targetId, selectedOrder) {
        const requested = new Set(requestedPaths || []);
        const ordered = [];
        const seen = new Set();
        (selectedOrder || []).forEach(function (path) {
            if (requested.has(path) && !seen.has(path)) {
                seen.add(path);
                ordered.push(path);
            }
        });
        if (!ordered.length) return {groups: groups, moved: 0, error: ''};

        const next = groups.map(function (group) {
            return Object.assign({}, group, {
                paths: (group.paths || []).filter(function (path) {
                    return !requested.has(path);
                })
            });
        });
        if (targetId) {
            const target = next.find(function (group) {
                return Number(group.id) === Number(targetId);
            });
            if (!target) return {groups: groups, moved: 0, error: 'المجلد غير موجود'};
            const targetPaths = new Set(target.paths);
            ordered.forEach(function (path) { targetPaths.add(path); });
            target.paths = (selectedOrder || []).filter(function (path) {
                return targetPaths.has(path);
            });
        }
        return {groups: next, moved: ordered.length, error: ''};
    }

    function moveGroup(groups, sourceId, parentId) {
        const source = groups.find(function (group) {
            return Number(group.id) === Number(sourceId);
        });
        if (!source) return {groups: groups, error: 'المجلد غير موجود', moved: false};

        const parent = parentId ? groups.find(function (group) {
            return Number(group.id) === Number(parentId);
        }) : null;
        if (parentId && !parent) {
            return {groups: groups, error: 'المجلد الأب غير موجود', moved: false};
        }

        const oldParts = parts(source.name);
        const oldKey = key(source.name);
        const parentKey = parent ? key(parent.name) : '';
        if (parent && (parentKey === oldKey || parentKey.indexOf(oldKey + '/') === 0)) {
            return {groups: groups, error: 'لا يمكن وضع المجلد داخل نفسه أو أحد مجلداته الفرعية', moved: false};
        }

        const moving = groups.filter(function (group) {
            const groupKey = key(group.name);
            return groupKey === oldKey || groupKey.indexOf(oldKey + '/') === 0;
        });
        const movingIds = new Set(moving.map(function (group) { return group.id; }));
        const newPrefix = (parent ? parts(parent.name) : []).concat(oldParts[oldParts.length - 1]);
        const proposals = [];
        const proposedKeys = new Set();

        for (const group of moving) {
            const suffix = parts(group.name).slice(oldParts.length);
            const newParts = newPrefix.concat(suffix);
            const newName = newParts.join('/');
            if (newParts.length > MAX_DEPTH) {
                return {groups: groups, error: 'أقصى تداخل 3 مستويات', moved: false};
            }
            if (newName.length > MAX_PATH) {
                return {groups: groups, error: 'مسار المجلد طويل جداً', moved: false};
            }
            for (const segment of newParts) {
                if (!segment || segment.length > MAX_SEGMENT || segment === '.' ||
                        segment === '..' || BAD_CHARS.test(segment) || /[ .]$/.test(segment)) {
                    return {groups: groups, error: 'اسم أو مسار المجلد غير صالح', moved: false};
                }
            }
            const newKey = key(newName);
            if (proposedKeys.has(newKey)) {
                return {groups: groups, error: 'يوجد مجلد بنفس الاسم', moved: false};
            }
            proposedKeys.add(newKey);
            proposals.push({id: group.id, name: newName});
        }

        for (const group of groups) {
            if (movingIds.has(group.id)) continue;
            if (proposedKeys.has(key(group.name))) {
                return {groups: groups, error: 'يوجد مجلد بنفس الاسم', moved: false};
            }
        }

        if (proposals.every(function (proposal) {
            const current = groups.find(function (group) { return group.id === proposal.id; });
            return current && key(current.name) === key(proposal.name);
        })) {
            return {groups: groups, error: '', moved: false};
        }

        const next = groups.map(function (group) {
            const proposal = proposals.find(function (item) { return item.id === group.id; });
            return proposal ? Object.assign({}, group, {name: proposal.name}) : group;
        });
        return {groups: next, error: '', moved: true};
    }

    return {assignPaths: assignPaths, moveGroup: moveGroup, clampSegments: clampSegments};
});

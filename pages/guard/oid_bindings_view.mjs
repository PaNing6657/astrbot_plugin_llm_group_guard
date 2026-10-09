export function groupOidBindings(bindings) {
  const groups = new Map();
  for (const binding of Array.isArray(bindings) ? bindings : []) {
    const oid = String(binding?.oid ?? "");
    let group = groups.get(oid);
    if (!group) {
      group = { oid, bindings: [] };
      groups.set(oid, group);
    }
    group.bindings.push(binding);
  }
  return Array.from(groups.values());
}

export function countOidBindings(bindings) {
  return Array.isArray(bindings) ? bindings.length : 0;
}

export function canExpandOidBindingGroup(group) {
  return Array.isArray(group?.bindings) && group.bindings.length > 1;
}

export function getVisibleOidBindings(group, expandedOids) {
  const bindings = Array.isArray(group?.bindings) ? group.bindings : [];
  return expandedOids?.has(group?.oid) ? bindings : bindings.slice(0, 1);
}

export function toggleOidBindingGroup(expandedOids, oid) {
  if (expandedOids.has(oid)) {
    expandedOids.delete(oid);
    return false;
  }
  expandedOids.add(oid);
  return true;
}

export function reconcileExpandedOidBindingGroups(expandedOids, groups) {
  const expandableOids = new Set(
    groups.filter(canExpandOidBindingGroup).map((group) => group.oid),
  );
  for (const oid of expandedOids) {
    if (!expandableOids.has(oid)) expandedOids.delete(oid);
  }
}

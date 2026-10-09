import assert from "node:assert/strict";
import test from "node:test";

import {
  canExpandOidBindingGroup,
  countOidBindings,
  getVisibleOidBindings,
  groupOidBindings,
  reconcileExpandedOidBindingGroups,
  toggleOidBindingGroup,
} from "../pages/guard/oid_bindings_view.mjs";

const binding = (user_id, oid) => ({ user_id, oid });

test("groups accounts with the same OID and leaves single-account OIDs collapsed", () => {
  const groups = groupOidBindings([
    binding("100", "42"),
    binding("101", "42"),
    binding("200", "77"),
  ]);

  assert.deepEqual(groups.map(({ oid, bindings }) => [oid, bindings.map((row) => row.user_id)]), [
    ["42", ["100", "101"]],
    ["77", ["200"]],
  ]);
  assert.equal(canExpandOidBindingGroup(groups[0]), true);
  assert.equal(canExpandOidBindingGroup(groups[1]), false);
});

test("expanding and collapsing changes which QQ bindings are visible", () => {
  const [group] = groupOidBindings([binding("100", "42"), binding("101", "42")]);
  const expanded = new Set();

  assert.deepEqual(getVisibleOidBindings(group, expanded).map((row) => row.user_id), ["100"]);
  assert.equal(toggleOidBindingGroup(expanded, "42"), true);
  assert.deepEqual(getVisibleOidBindings(group, expanded).map((row) => row.user_id), ["100", "101"]);
  assert.equal(toggleOidBindingGroup(expanded, "42"), false);
  assert.deepEqual(getVisibleOidBindings(group, expanded).map((row) => row.user_id), ["100"]);
});

test("expanded groups stay open after bindings are added or deleted while still expandable", () => {
  const expanded = new Set();
  let groups = groupOidBindings([
    binding("100", "42"),
    binding("101", "42"),
    binding("102", "42"),
    binding("200", "77"),
  ]);
  toggleOidBindingGroup(expanded, "42");

  groups = groupOidBindings([binding("100", "42"), binding("102", "42"), binding("200", "77")]);
  reconcileExpandedOidBindingGroups(expanded, groups);
  assert.equal(expanded.has("42"), true);
  assert.equal(getVisibleOidBindings(groups[0], expanded).length, 2);

  groups = groupOidBindings([
    binding("100", "42"),
    binding("102", "42"),
    binding("103", "42"),
    binding("200", "77"),
  ]);
  reconcileExpandedOidBindingGroups(expanded, groups);
  assert.equal(expanded.has("42"), true);
  assert.equal(getVisibleOidBindings(groups[0], expanded).length, 3);
});

test("groups that become singletons lose expanded styling, and the count remains per QQ binding", () => {
  const expanded = new Set(["42"]);
  let groups = groupOidBindings([binding("100", "42"), binding("200", "77")]);
  reconcileExpandedOidBindingGroups(expanded, groups);

  assert.equal(expanded.has("42"), false);
  assert.equal(canExpandOidBindingGroup(groups[0]), false);
  assert.equal(countOidBindings([binding("100", "42"), binding("200", "77")]), 2);
});

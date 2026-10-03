import { describe, expect, it } from 'vitest';
import { filterMembers } from './OrganizationMembers';
import type { Unit } from './Directory';

const members = [{ id: 'a', username: 'Alice', role: 'admin' }, { id: 'b', username: 'Bob', role: 'member' }, { id: 'c', username: 'Carol', role: 'member' }];
const units: Unit[] = [
  { id: 'root', name: '研发', kind: 'department', parent_id: null, member_ids: ['a'], revision: 1 },
  { id: 'child', name: '算法', kind: 'department', parent_id: 'root', member_ids: ['a', 'b'], revision: 1 },
  { id: 'group', name: '协作', kind: 'group', parent_id: null, member_ids: ['c'], revision: 1 },
];
describe('member directory filters', () => {
  it('includes descendants only when requested and deduplicates members', () => {
    expect(filterMembers(members, units, 'root', false, '', '').map(m => m.id)).toEqual(['a']);
    expect(filterMembers(members, units, 'root', true, '', '').map(m => m.id)).toEqual(['a', 'b']);
  });
  it('keeps group-only members in unassigned departments', () => {
    expect(filterMembers(members, units, 'unassigned', true, '', '').map(m => m.id)).toEqual(['c']);
    expect(filterMembers(members, units, 'group', false, '', '').map(m => m.id)).toEqual(['c']);
  });
  it('combines role and case-insensitive name filters with scope', () => {
    expect(filterMembers(members, units, 'root', true, ' BO ', 'member').map(m => m.id)).toEqual(['b']);
    expect(filterMembers(members, units, 'root', true, 'BO', 'admin')).toEqual([]);
    expect(filterMembers(members, units, 'missing', false, '', '')).toEqual([]);
  });
});

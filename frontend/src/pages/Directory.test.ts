import { describe, expect, it } from 'vitest';
import { departmentRows, parentChoices, unitPath, type Unit } from './Directory';

const units: Unit[] = [
  { id: 'root', name: '研发', kind: 'department', parent_id: null, member_ids: [], revision: 1 },
  { id: 'child', name: '算法', kind: 'department', parent_id: 'root', member_ids: ['alice'], revision: 1 },
  { id: 'leaf', name: '模型', kind: 'department', parent_id: 'child', member_ids: [], revision: 1 },
  { id: 'other', name: '产品', kind: 'department', parent_id: null, member_ids: [], revision: 1 },
  { id: 'group', name: '项目组', kind: 'group', parent_id: null, member_ids: ['alice'], revision: 1 },
];
describe('organization directory navigation', () => {
  it('renders departments in a tree without mixing project groups', () => {
    const rows = departmentRows(units);
    expect(rows.map(u => u.id)).toEqual(['root', 'other']);
    expect(rows[0].children?.[0].children?.[0].id).toBe('leaf');
    expect(rows[1].children).toBeUndefined();
  });
  it('excludes self, descendants and groups from a department move', () => {
    expect(parentChoices(units, units[1]).map(u => u.id)).toEqual(['root', 'other']);
    expect(parentChoices(units, units[0]).map(u => u.id)).toEqual(['other']);
  });
  it('disambiguates department names using their path', () => {
    expect(unitPath(units[2], units)).toBe('研发 / 算法 / 模型');
  });
});

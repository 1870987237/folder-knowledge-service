import { describe, it, expect } from 'vitest';
import { sameMarkdown, visualMarkdownReason } from './markdownSafety';

describe('Markdown visual editing guard', () => {
  it('permits ordinary Markdown formatting changes without treating them as edits', () => {
    expect(sameMarkdown('# 标题\n\n- 一\n- 二\n', '# 标题\n\n* 一\n* 二\n')).toBe(true);
    expect(sameMarkdown('| a | b |\n|---|---|\n| 1 | 2 |', '| a   | b   |\n| --- | --- |\n| 1   | 2   |\n')).toBe(true);
    expect(sameMarkdown('原文', '改变内容')).toBe(false);
    expect(sameMarkdown('```py\nprint(1)\n```', '```py\nprint(2)\n```')).toBe(false);
  });
  it('preserves unsafe or unsupported syntax in source mode', () => {
    for (const text of ['---\ntitle: x\n---\n# X', '<script>alert(1)</script>', '![x](https://example.com/x)', '[x](javascript:alert%281%29)', '[x](../local.md)', '[x][ref]\n\n[ref]: https://example.com', 'x[^1]\n\n[^1]: note']) expect(visualMarkdownReason(text)).not.toBe('');
    expect(visualMarkdownReason('# 标题\n\n**加粗**\n\n- [ ] 任务\n\n[链接](https://example.com)')).toBe('');
  });
});

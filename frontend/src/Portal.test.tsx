import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { DocumentPreview } from './Portal';

describe('knowledge rendering', () => {
  it('renders Markdown structure and tables without executing embedded HTML', () => {
    const html = renderToStaticMarkup(<DocumentPreview content={'# Guide\n\n| Key | Value |\n|---|---|\n| A | B |\n\n<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">'} />);
    expect(html).toContain('<h1>Guide</h1>');
    expect(html).toContain('<table>');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('onerror');
  });
  it('does not load remote images or turn unsafe/local links into navigation', () => {
    const html = renderToStaticMarkup(<DocumentPreview content={'![tracking](https://tracker.invalid/pixel)\n\n[bad](javascript:alert%281%29) [local](/admin) [docs](https://example.com)'} />);
    expect(html).not.toContain('<img');
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('href="/admin');
    expect(html).toContain('rel="noopener noreferrer"');
  });
});

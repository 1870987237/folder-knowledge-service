import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';

const parser = unified().use(remarkParse).use(remarkGfm);
export function visualMarkdownReason(text: string): string {
  if (/^---\s*\n/.test(text) || /^\s*\[.+\]:/m.test(text)) return '文档包含元数据或引用定义，请使用源码编辑以保留原文。';
  let reason = '';
  function visit(node: any) {
    if (['html', 'image', 'imageReference', 'footnoteDefinition', 'footnoteReference'].includes(node.type)) reason = '文档包含 HTML、图片或脚注，请使用源码编辑以保留原文。';
    if (node.type === 'link' && !/^https?:\/\//i.test(node.url)) reason = '文档包含本地或特殊协议链接，请使用源码编辑以保留原文。';
    node.children?.forEach(visit);
  }
  visit(parser.parse(text));
  return reason;
}
export function sameMarkdown(a: string, b: string): boolean {
  function normalize(node: any): any {
    if (Array.isArray(node)) return node.map(normalize);
    if (node && typeof node === 'object') return Object.fromEntries(Object.entries(node)
      .filter(([key]) => !['position', 'spread'].includes(key)).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, value]) => [key, normalize(value)]));
    return node;
  }
  return JSON.stringify(normalize(parser.parse(a))) === JSON.stringify(normalize(parser.parse(b)));
}

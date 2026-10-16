"""Create disposable knowledge and a packaged BM25 plugin for browser validation.

Building this program index requires KB_SEARCH_TRUSTED_PYTHON=1 and approval
of the generated package digest in the isolated UI service."""
import json
from pathlib import Path
import uuid

from folder_kb.management import Registry
from folder_kb.plugin_package import package_plugin


def create_fixture():
    base = Path('data').resolve() / ('rag-ui-' + uuid.uuid4().hex[:8])
    root = base / 'knowledge'
    (root / 'guide').mkdir(parents=True)
    (root / 'guide/连接说明.md').write_text(
        '# 连接知识服务\nAgent 使用 MCP 连接已授权的知识库。\n'
        '## 访问密钥\n在管理平台创建专用 Agent Key，按知识库授权。\n'
        '密钥用于连接鉴权，不应写入知识文档。\n'
        '## 索引更新\n知识文件发生修改后需要重建索引，再检索最新内容。\n', encoding='utf-8')
    (root / 'guide/deployment.txt').write_text('Rollback restores the previous release. Keep a backup before deployment.\n', encoding='utf-8')
    registry = Registry(str(root), str(base / 'state'))
    registry.save_library('rag-demo', 'RAG 检索测试', 'guide', True, create=True)
    package_plugin(Path('examples/plugins/bm25-markdown/package'), Path('examples/plugins/bm25-markdown/definition.json'), base / 'upload')
    (base / 'fixture.json').write_text(json.dumps({'root':str(root),'state':str(base/'state')}), encoding='utf-8')
    print(base)


if __name__ == '__main__':
    create_fixture()

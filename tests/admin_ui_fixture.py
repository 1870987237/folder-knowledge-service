"""Create isolated data for manual admin browser checks; never open live state.

Run from the repository root: python -m tests.admin_ui_fixture
Then start folder_kb with the printed knowledge/state directories on a test port.
"""
from pathlib import Path
import uuid

from folder_kb.management import Registry
from folder_kb.inbox import Inbox, Material
from folder_kb.template_package import package_template


def create_fixture():
    base = Path('data').resolve() / ('admin-ui-check-' + uuid.uuid4().hex[:8])
    root = base / 'knowledge'
    (root / 'demo/knowledge').mkdir(parents=True)
    (root / 'demo/metadata').mkdir()
    (root / 'demo/knowledge/INDEX.md').write_text('# Example knowledge', encoding='utf-8')
    registry = Registry(str(root), str(base / 'state'))
    registry.save_library('demo', '界面测试知识库', 'demo/knowledge', True, create=True)
    key = registry.create_key('界面测试 Agent', ['demo'], None)
    inbox = Inbox(registry)
    inbox.grants(key['key']['id'], ['demo'])
    for index in range(2):
        inbox.submit(key['token'], Material(
            submission_id=f'ui-example-{index + 1}', library_id='demo',
            title='使用说明补充' if index == 0 else '待确认的观察',
            kind='new_content' if index == 0 else 'observation',
            content={'text': '建议补充配置示例。\n正文应按纯文本展示。'},
            context={'scenario': '隔离测试环境'}))
    package_template(Path('examples/templates/reference-docs'),
                     Path('examples/template-platform-config.json'), base / 'upload')
    print(base)


if __name__ == '__main__':
    create_fixture()

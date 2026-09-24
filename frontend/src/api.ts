const messages: Record<string, string> = {
  not_found: '路径或资源不存在，请检查输入。',
  workspace_view_mismatch: '工作根与检索子目录组合后，必须与知识库挂载目录一致。',
  workspace_binding_conflict: '同一工作范围的知识库需使用相同模板、配置及排除规则。',
  workspace_repository_mismatch: '请选择此知识库关联的 Git 仓库。',
  workspace_unavailable: '工作范围不可用，请检查目录及仓库发布状态。',
  workspace_protocol_required: '完整工作目录要求协议 1.1 的模板。',
  invalid_template_config: '实例配置不符合模板规范，请核对配置说明。',
  template_version_conflict: '该版本的定义已固定，请注册新版本。',
  template_digest_mismatch: 'ZIP 与注册摘要不匹配，请使用同一次打包的两个文件。',
  invalid_template_package: '程序包结构不合法，请使用标准打包工具生成 ZIP。',
  template_not_installed: '请先安装模板程序包。',
  template_package_changed: '已安装的程序包发生了变更，无法使用此版本。',
  template_package_unavailable: '程序包不存在或不可用。',
  inbox_claim_conflict: '材料已被领取、已完成或租约已过期，请刷新状态后重试。',
  library_disabled: '知识库已停用。', invalid_grants: '提交范围必须在当前访问授权内。',
  library_unavailable: '知识目录暂时不可用，请检查目录映射。',
};
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : '操作失败，请重试。'; }
export function parseObject(text: string): Record<string, unknown> {
  let value: unknown;
  try { value = JSON.parse(text); } catch { throw new Error('请输入有效的 JSON。'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('JSON 顶层必须是对象。');
  return value as Record<string, unknown>;
}
export function query(values: Record<string, string | number | undefined | null>): string {
  return new URLSearchParams(Object.entries(values).filter(([, v]) => v !== '' && v != null).map(([k, v]) => [k, String(v)])).toString();
}
export class Api {
  private controllers = new Set<AbortController>();
  private disposed = false;
  constructor(private token: string, private unauthorized: () => void) {}
  dispose() { this.disposed = true; this.token = ''; this.controllers.forEach(c => c.abort()); this.controllers.clear(); }
  async request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
    if (this.disposed) throw new DOMException('Session ended', 'AbortError');
    const controller = new AbortController(); this.controllers.add(controller);
    try {
      const binary = body instanceof Blob;
      const response = await fetch('/admin/api' + path, {
        method, signal: controller.signal, cache: 'no-store',
        headers: { Authorization: 'Bearer ' + this.token, ...(body === undefined ? {} : { 'Content-Type': binary ? 'application/zip' : 'application/json' }) },
        body: body === undefined ? undefined : binary ? body : JSON.stringify(body),
      });
      const data = await response.json().catch(() => null);
      if (this.disposed) throw new DOMException('Session ended', 'AbortError');
      if (!response.ok) {
        if (response.status === 401) { this.unauthorized(); throw new ApiError('会话已失效，请重新登录。', 401); }
        const details = Array.isArray(data?.detail) ? data.detail.map((d: { loc?: string[]; msg: string }) => `${d.loc?.slice(1).join('.') || '输入'}：${d.msg}`).join('；') : '';
        throw new ApiError(messages[data?.error] || data?.message || details || `请求失败（${response.status}）`, response.status);
      }
      return data as T;
    } finally { this.controllers.delete(controller); }
  }
}

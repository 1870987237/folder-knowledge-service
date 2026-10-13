export interface Library {
  id: string; name: string; path: string; enabled: boolean;
  availability: { status: string; message: string };
  repository?: { repository_id: string; subpath: string };
}
export interface AgentKey {
  id: string; agent_name: string; library_ids: string[]; expires_at: string | null; revoked_at: string | null;
  activity: { calls?: number; errors?: number; last_seen?: string };
}
export interface Activity {
  id?: number; timestamp: string; agent_name?: string; key_id?: string; kind?: string;
  transport?: string; tool?: string; action?: string; library_id?: string; path?: string;
  status: string; error?: string; reason?: string; duration_ms?: number;
}
export interface Overview {
  libraries: Library[]; keys: AgentKey[]; metrics: { calls: number; errors: number };
  active_requests: { request_id?: string; agent_name: string; tool: string; library_id: string }[];
}
export interface RepositoryJob {
  id: string; status: string; trigger: string; created_at: string;
  finished_at?: string; before_commit?: string; after_commit?: string; error?: string;
}
export interface Repository {
  id: string; name: string; url: string; branch: string; published_branch?: string | null; import_path?: string;
  credential: string; interval_seconds: number; enabled: boolean;
  current_commit?: string; release_path?: string; last_sync?: string; next_sync?: number; last_error?: string;
  libraries: { library_id: string; name: string; subpath: string }[]; job?: RepositoryJob;
}
export interface RepoList { repositories: Repository[]; worker_online: boolean }
export interface PluginFeatures {
  manifest_version: string; instructions: string;
  indexing?: { mode: string; splitter: string; include: string[]; exclude: string[]; metadata_fields: string[]; entrypoint?: string[] | null } | null;
}
export interface TemplateDefinition {
  id: string; version: string; name: string; description: string; maintainer: string;
  protocol_version: string; capabilities: string[]; required_secrets: string[];
  config_schema: Record<string, unknown>; runtime: Record<string, unknown>; plugin?: PluginFeatures | null;
}
export interface Template { definition: TemplateDefinition; status: string; package_sha256?: string; capabilities?: Record<string, { integration: string }>; libraries?: { id: string; name: string }[]; legacy_template?: boolean }
export interface Workspace {
  type: 'local' | 'git'; root_path?: string; repository_id?: string;
  workspace_subpath?: string; knowledge_subpath: string; exclude_paths: string[];
}
export interface Binding {
  template_id: string; template_version: string; config: Record<string, unknown>;
  workspace?: Workspace | null; workspace_key?: string;
}
export interface Material {
  kind: string; title: string; content: { text: string; format: string };
  source?: Record<string, string>; context?: Record<string, string>;
}
export interface InboxItem {
  update_job?: { id: string; status: string } | null;
  id: string; library_id: string; submission_id: string; title?: string; kind?: string;
  status: string; created_at: string; updated_at: string; lease_until?: number;
  submitted_by: { id: string; type: string }; material?: Material; claim_token?: string;
  result?: { outcome: string; summary: string; references: string[]; version?: string };
}
export interface Entry { path: string; type: 'file' | 'directory'; size?: number; bytes?: number; modified_at?: string }
export interface DirectoryPage { entries: Entry[]; next_offset: number | null; partial?: boolean }
export interface FilePage {
  lines: { line: number; column?: number; text: string; line_complete?: boolean }[];
  next_cursor: string | null; version: string;
}

export interface PluginBindingValue { plugin_id: string; plugin_version: string; config: Record<string, unknown>; workspace?: Workspace | null }

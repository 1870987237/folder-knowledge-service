"""Platform-owned template registration, binding and job metadata."""

TEMPLATE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS template_versions (
        id TEXT NOT NULL, version TEXT NOT NULL, metadata_json TEXT NOT NULL,
        package_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(id,version))""",
    """CREATE TABLE IF NOT EXISTS library_templates (
        library_id TEXT PRIMARY KEY REFERENCES libraries(id),
        template_id TEXT NOT NULL, template_version TEXT NOT NULL, config_json TEXT NOT NULL,
        FOREIGN KEY(template_id,template_version) REFERENCES template_versions(id,version))""",
    """CREATE TABLE IF NOT EXISTS template_jobs (
        id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
        template_id TEXT NOT NULL, template_version TEXT NOT NULL,
        base_version TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT, error_code TEXT,
        FOREIGN KEY(template_id,template_version) REFERENCES template_versions(id,version))""",
)

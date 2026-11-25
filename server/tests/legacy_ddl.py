"""The schema strings CSA shipped BEFORE Alembic, frozen here as migration fixtures.

Every production database was created by one of these plus a try/except ALTER list, so the catch-up
baselines have to land on any of them. Copied verbatim (comments stripped) from:

  JUL_2026_*  commit aa5bd7c, 2026-07-17: no facts, tenant_config, entity_stats, session_metrics,
              registry or custom_entity_types; served and queries still inside the corpus; control
              has no expires_at, role or domain.
  SEP_2026_*  commit 47c9f97, 2026-09-04: no tel.ui_events, no cli_pairings.
  SEP05_2026_* commit 467ab85, 2026-09-05: the LAST pre-Alembic shape, SEP_2026 plus the two tables
              that merged the day the baselines were written (tel.ui_events from #106, cli_pairings
              from #107). This is what a box that pulled a master image before this change has, and
              it is the shape that exercises the catch-up's SKIP path on those two tables.

Neither carries the columns the ALTER list added at open (sessions.harness, hidden_context.as_of,
mentions.dominant, reviews.flagger, costs.trace_id, user_tokens.expires_at/role, orgs.domain).
"""

SEP_2026_CORPUS = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, project TEXT, git_branch TEXT,
  started_at TEXT, ended_at TEXT, n_user INT, n_asst INT,
  summary TEXT, deployed_to_prod INT, source TEXT DEFAULT 'session', user TEXT DEFAULT 'owner',
  redaction_mode TEXT DEFAULT 'secrets',
  shipped_by TEXT);
CREATE TABLE IF NOT EXISTS worked_on(session_id TEXT, item TEXT);
CREATE TABLE IF NOT EXISTS hidden_context(session_id TEXT, type TEXT, scope TEXT, claim TEXT, evidence TEXT, ref TEXT, confidence TEXT,
  repo TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS facts(
  ref TEXT PRIMARY KEY,
  claim TEXT, type TEXT, scope TEXT,
  author TEXT,
  domain TEXT,
  created_at TEXT,
  valid_at TEXT,
  invalid_at TEXT,
  expired_at TEXT,
  state TEXT DEFAULT 'extracted',
  confidence REAL);
CREATE TABLE IF NOT EXISTS fact_edges(
  src_ref TEXT, rel TEXT, dst_ref TEXT,
  at TEXT, actor TEXT, note TEXT,
  PRIMARY KEY(src_ref, rel, dst_ref));
CREATE INDEX IF NOT EXISTS ix_fact_edges_dst ON fact_edges(dst_ref);
CREATE TABLE IF NOT EXISTS registry(
  kind TEXT, key TEXT, value TEXT,
  as_of TEXT, source TEXT,
  PRIMARY KEY(kind, key, source));
CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, type TEXT, name TEXT, attributes TEXT);
CREATE TABLE IF NOT EXISTS mentions(session_id TEXT, entity_id TEXT);
CREATE TABLE IF NOT EXISTS actions(session_id TEXT, verb TEXT, target_id TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS relations(session_id TEXT, source_id TEXT, rel TEXT, target_id TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS links(session_id TEXT, url TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS data_sources(session_id TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS aliases(alias_id TEXT PRIMARY KEY, canonical_id TEXT);
CREATE TABLE IF NOT EXISTS reviews(kind TEXT, ref TEXT, status TEXT, note TEXT, PRIMARY KEY(kind,ref));
CREATE TABLE IF NOT EXISTS entity_types(id TEXT PRIMARY KEY, type TEXT);
CREATE TABLE IF NOT EXISTS custom_entity_types(key TEXT PRIMARY KEY, description TEXT NOT NULL, created_at TEXT,
  updated_at TEXT, updated_by TEXT);
CREATE TABLE IF NOT EXISTS hc_types(ref TEXT PRIMARY KEY, type TEXT);
CREATE TABLE IF NOT EXISTS claim_clusters(ref TEXT PRIMARY KEY, cluster_id TEXT, sessions INT);
CREATE TABLE IF NOT EXISTS tenant_config(
  id INT PRIMARY KEY CHECK (id = 1),
  entity_floor TEXT, entity_gc_days INT, daily_usd REAL, relay_accounts TEXT,
  min_redaction TEXT, redaction_deny TEXT, redaction_allow TEXT,
  settle_min INT, token_ttl_days INT, updated_at TEXT, updated_by TEXT,
  registry_sources TEXT, registry_refute TEXT);
CREATE TABLE IF NOT EXISTS entity_stats(entity_id TEXT PRIMARY KEY, sessions INT, distinct_authors INT,
  dominant_n INT, acted_n INT, last_seen TEXT, name_sessions INT, headline_n INT);
CREATE TABLE IF NOT EXISTS signals(ref TEXT, kind TEXT, session_id TEXT, at TEXT, PRIMARY KEY(ref,kind,session_id));
CREATE TABLE IF NOT EXISTS costs(at TEXT, step TEXT, model TEXT, session_id TEXT, input_tokens INT, output_tokens INT, cache_read_tokens INT, cost_usd REAL);
CREATE INDEX IF NOT EXISTS ix_hc_session ON hidden_context(session_id);
CREATE INDEX IF NOT EXISTS ix_hc_ref ON hidden_context(ref);
CREATE TABLE IF NOT EXISTS pr_links(session_id TEXT, repo TEXT, pr_number INT, url TEXT, first_seen TEXT, last_seen TEXT, n_events INT, PRIMARY KEY(session_id,repo,pr_number));
CREATE INDEX IF NOT EXISTS ix_pr_links_n ON pr_links(pr_number);
CREATE TABLE IF NOT EXISTS session_workspace(session_id TEXT, kind TEXT, value TEXT, PRIMARY KEY(session_id,kind,value));
CREATE VIEW IF NOT EXISTS session_dirs AS SELECT session_id, value AS path FROM session_workspace WHERE kind='dir';
CREATE VIEW IF NOT EXISTS session_branches AS SELECT session_id, value AS branch FROM session_workspace WHERE kind='branch';
CREATE TABLE IF NOT EXISTS session_metrics(session_id TEXT PRIMARY KEY, model TEXT, tool_calls INT, tool_errors INT,
  dup_calls INT, reread_calls INT, top_read TEXT, top_read_n INT, top_tool TEXT, in_tokens INT, out_tokens INT,
  cache_read_tokens INT, cache_write_tokens INT, cost_usd REAL, wall_s INT, idle_s INT, max_gap_s INT);
CREATE TABLE IF NOT EXISTS harvest_actions(session_id TEXT, verb TEXT, target_id TEXT, context TEXT, PRIMARY KEY(session_id,verb,target_id,context));
CREATE TABLE IF NOT EXISTS harvest_mentions(session_id TEXT, entity_id TEXT, PRIMARY KEY(session_id,entity_id));
CREATE TABLE IF NOT EXISTS harvest_links(session_id TEXT, url TEXT, title TEXT, PRIMARY KEY(session_id,url,title));
CREATE INDEX IF NOT EXISTS ix_mentions_eid ON mentions(entity_id);
CREATE INDEX IF NOT EXISTS ix_mentions_sid ON mentions(session_id);
CREATE INDEX IF NOT EXISTS ix_actions_tid ON actions(target_id);
CREATE INDEX IF NOT EXISTS ix_actions_verb ON actions(verb COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_relations_src ON relations(source_id);
CREATE INDEX IF NOT EXISTS ix_relations_tgt ON relations(target_id);
CREATE INDEX IF NOT EXISTS ix_entities_type ON entities(type);
"""

SEP_2026_TELEMETRY = """
CREATE TABLE IF NOT EXISTS tel.served(session TEXT, asked_at TEXT, question TEXT, ref TEXT, kind TEXT, project TEXT, person TEXT, asked_by TEXT, trace_id TEXT);
CREATE INDEX IF NOT EXISTS tel.ix_served_session ON served(session);
CREATE INDEX IF NOT EXISTS tel.ix_served_ref ON served(ref, session);
CREATE TABLE IF NOT EXISTS tel.queries(at TEXT, asked_by TEXT, session TEXT, engine TEXT, query_text TEXT, planned_query TEXT, row_count INT, latency_ms INT, acted_on INT DEFAULT 0, result_ids TEXT, trace_id TEXT);
CREATE INDEX IF NOT EXISTS tel.ix_queries_engine ON queries(engine);
CREATE INDEX IF NOT EXISTS tel.ix_queries_session ON queries(session);
CREATE TABLE IF NOT EXISTS tel.spans(trace_id TEXT, span_id TEXT, parent_id TEXT, name TEXT, kind TEXT,
  at TEXT, dur_ms INT, status TEXT, attrs TEXT);
CREATE INDEX IF NOT EXISTS tel.ix_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS tel.ix_spans_at ON spans(at);
"""

SEP_2026_CONTROL = """
CREATE TABLE IF NOT EXISTS orgs(
  id TEXT PRIMARY KEY, name TEXT, invite_hash TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS user_tokens(
  token_hash TEXT PRIMARY KEY, email TEXT, org_id TEXT, hostname TEXT,
  created TEXT, revoked_at TEXT, last_used_at TEXT);
CREATE INDEX IF NOT EXISTS ix_user_tokens_org ON user_tokens(org_id);
CREATE TABLE IF NOT EXISTS admin_log(at TEXT, action TEXT, org_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS workspace_requests(
  id TEXT PRIMARY KEY, company TEXT, email TEXT, company_norm TEXT, email_norm TEXT,
  created TEXT, status TEXT, handled_at TEXT, org_id TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_requests_norm ON workspace_requests(company_norm, email_norm);
CREATE TABLE IF NOT EXISTS client_directives(
  id TEXT PRIMARY KEY, org_id TEXT, target_email TEXT, target_hostname TEXT,
  since TEXT, created TEXT, created_by TEXT, revoked_at TEXT);
CREATE INDEX IF NOT EXISTS ix_client_directives_org ON client_directives(org_id);
CREATE TABLE IF NOT EXISTS client_directive_acks(
  directive_id TEXT, token_hash TEXT, email TEXT, hostname TEXT,
  at TEXT, status TEXT, shipped INT, detail TEXT,
  PRIMARY KEY(directive_id, token_hash));
"""

JUL_2026_CORPUS = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, project TEXT, git_branch TEXT,
  started_at TEXT, ended_at TEXT, n_user INT, n_asst INT,
  summary TEXT, deployed_to_prod INT, source TEXT DEFAULT 'session', user TEXT DEFAULT 'owner',
  redaction_mode TEXT DEFAULT 'secrets');
CREATE TABLE IF NOT EXISTS worked_on(session_id TEXT, item TEXT);
CREATE TABLE IF NOT EXISTS hidden_context(session_id TEXT, type TEXT, scope TEXT, claim TEXT, evidence TEXT, ref TEXT, confidence TEXT,
  repo TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, type TEXT, name TEXT, attributes TEXT);
CREATE TABLE IF NOT EXISTS mentions(session_id TEXT, entity_id TEXT);
CREATE TABLE IF NOT EXISTS actions(session_id TEXT, verb TEXT, target_id TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS relations(session_id TEXT, source_id TEXT, rel TEXT, target_id TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS links(session_id TEXT, url TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS data_sources(session_id TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS aliases(alias_id TEXT PRIMARY KEY, canonical_id TEXT);
CREATE TABLE IF NOT EXISTS reviews(kind TEXT, ref TEXT, status TEXT, note TEXT, PRIMARY KEY(kind,ref));
CREATE TABLE IF NOT EXISTS entity_types(id TEXT PRIMARY KEY, type TEXT);
CREATE TABLE IF NOT EXISTS hc_types(ref TEXT PRIMARY KEY, type TEXT);
CREATE TABLE IF NOT EXISTS claim_clusters(ref TEXT PRIMARY KEY, cluster_id TEXT, sessions INT);
CREATE TABLE IF NOT EXISTS signals(ref TEXT, kind TEXT, session_id TEXT, at TEXT, PRIMARY KEY(ref,kind,session_id));
CREATE TABLE IF NOT EXISTS costs(at TEXT, step TEXT, model TEXT, session_id TEXT, input_tokens INT, output_tokens INT, cache_read_tokens INT, cost_usd REAL);
CREATE TABLE IF NOT EXISTS served(session TEXT, asked_at TEXT, question TEXT, ref TEXT, kind TEXT, project TEXT, person TEXT, asked_by TEXT);
CREATE INDEX IF NOT EXISTS ix_served_session ON served(session);
CREATE TABLE IF NOT EXISTS queries(at TEXT, asked_by TEXT, session TEXT, engine TEXT, query_text TEXT, planned_query TEXT, row_count INT, latency_ms INT, acted_on INT DEFAULT 0, result_ids TEXT);
CREATE INDEX IF NOT EXISTS ix_queries_engine ON queries(engine);
CREATE TABLE IF NOT EXISTS pr_links(session_id TEXT, repo TEXT, pr_number INT, url TEXT, first_seen TEXT, last_seen TEXT, n_events INT, PRIMARY KEY(session_id,repo,pr_number));
CREATE INDEX IF NOT EXISTS ix_pr_links_n ON pr_links(pr_number);
CREATE TABLE IF NOT EXISTS session_workspace(session_id TEXT, kind TEXT, value TEXT, PRIMARY KEY(session_id,kind,value));
CREATE VIEW IF NOT EXISTS session_dirs AS SELECT session_id, value AS path FROM session_workspace WHERE kind='dir';
CREATE VIEW IF NOT EXISTS session_branches AS SELECT session_id, value AS branch FROM session_workspace WHERE kind='branch';
CREATE TABLE IF NOT EXISTS harvest_actions(session_id TEXT, verb TEXT, target_id TEXT, context TEXT, PRIMARY KEY(session_id,verb,target_id,context));
CREATE TABLE IF NOT EXISTS harvest_mentions(session_id TEXT, entity_id TEXT, PRIMARY KEY(session_id,entity_id));
CREATE TABLE IF NOT EXISTS harvest_links(session_id TEXT, url TEXT, title TEXT, PRIMARY KEY(session_id,url,title));
CREATE INDEX IF NOT EXISTS ix_mentions_eid ON mentions(entity_id);
CREATE INDEX IF NOT EXISTS ix_mentions_sid ON mentions(session_id);
CREATE INDEX IF NOT EXISTS ix_actions_tid ON actions(target_id);
CREATE INDEX IF NOT EXISTS ix_actions_verb ON actions(verb COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_relations_src ON relations(source_id);
CREATE INDEX IF NOT EXISTS ix_relations_tgt ON relations(target_id);
CREATE INDEX IF NOT EXISTS ix_entities_type ON entities(type);
"""

JUL_2026_CONTROL = """
CREATE TABLE IF NOT EXISTS orgs(
  id TEXT PRIMARY KEY, name TEXT, invite_hash TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS user_tokens(
  token_hash TEXT PRIMARY KEY, email TEXT, org_id TEXT, hostname TEXT,
  created TEXT, revoked_at TEXT, last_used_at TEXT);
CREATE INDEX IF NOT EXISTS ix_user_tokens_org ON user_tokens(org_id);
CREATE TABLE IF NOT EXISTS admin_log(at TEXT, action TEXT, org_id TEXT, detail TEXT);
"""

#: 2026-09-05: the two tables that merged after SEP_2026 and before Alembic, still as DDL strings.
#: A box that pulled a master image on that day has them; the baselines must skip, not recreate.
SEP05_2026_TELEMETRY = SEP_2026_TELEMETRY + """
CREATE TABLE IF NOT EXISTS tel.ui_events(id INTEGER PRIMARY KEY, at TEXT NOT NULL, user TEXT NOT NULL, event TEXT NOT NULL, page TEXT NOT NULL, meta TEXT);
CREATE INDEX IF NOT EXISTS tel.ix_tel_ui_events_at ON ui_events(at);
"""

SEP05_2026_CONTROL = SEP_2026_CONTROL + """
CREATE TABLE IF NOT EXISTS cli_pairings(
  nonce_hash TEXT PRIMARY KEY, hostname TEXT, created TEXT,
  approved_at TEXT, email TEXT, org_id TEXT, verified INT, claimed_at TEXT);
"""

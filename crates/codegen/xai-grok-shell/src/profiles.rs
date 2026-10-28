//! Authoritative, secret-free local profile control plane used by Hub and mobile.
//! Writes use a filesystem lock, revision preconditions and atomic replacement.
use crate::agent::config::{Config, ModelEntry};
use anyhow::{Context, Result, bail};
use clap::{Args, Subcommand};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use xai_grok_agent::AgentDefinition;

#[derive(Debug, Clone, Args)]
pub struct ProfilesArgs {
    #[arg(long, global = true)]
    pub cwd: Option<PathBuf>,
    #[arg(long, global = true, default_value = "effective", value_parser = ["effective", "user", "project"])]
    pub scope: String,
    #[command(subcommand)]
    pub command: ProfilesCommand,
}
#[derive(Debug, Clone, Subcommand)]
pub enum ProfilesCommand {
    List {
        #[arg(long)]
        json: bool,
    },
    Get {
        id: String,
        #[arg(long)]
        json: bool,
    },
    Validate {
        #[arg(long)]
        json: bool,
    },
    Apply {
        #[arg(long)]
        json: bool,
    },
    Policy {
        #[command(subcommand)]
        command: PolicyCommand,
    },
}
#[derive(Debug, Clone, Subcommand)]
pub enum PolicyCommand {
    Get {
        #[arg(long)]
        json: bool,
    },
    Validate {
        #[arg(long)]
        json: bool,
    },
    Apply {
        #[arg(long)]
        json: bool,
    },
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DelegationDefaults {
    pub model: Option<String>,
    pub effort: Option<String>,
    pub speed: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DelegationPolicy {
    pub mode: String,
    #[serde(default)]
    pub defaults: DelegationDefaults,
}
impl Default for DelegationPolicy {
    fn default() -> Self {
        Self {
            mode: "automatic".into(),
            defaults: DelegationDefaults::default(),
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppliedProfile {
    pub id: String,
    pub revision: String,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ProfileEdit {
    id: String,
    expected_revision: Option<String>,
    #[serde(default = "project_scope")]
    write_scope: String,
    definition: Value,
}
fn project_scope() -> String {
    "project".into()
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct PolicyEdit {
    expected_revision: Option<String>,
    policy: DelegationPolicy,
}
const SAFE_FIELDS: &[&str] = &[
    "name",
    "description",
    "model",
    "effort",
    "speed",
    "identity",
    "skills",
    "promptBody",
    "promptFiles",
    "ruleFiles",
    "tools",
    "disallowedTools",
];
fn hash(value: &Value) -> String {
    blake3::hash(&serde_json::to_vec(value).expect("JSON serializable"))
        .to_hex()
        .to_string()
}
fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 96
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
        && id != "."
        && id != ".."
}
fn config() -> Result<Config> {
    crate::config::load_agent_config_disk_only().map_err(anyhow::Error::msg)
}
fn catalog(cfg: &Config) -> indexmap::IndexMap<String, ModelEntry> {
    crate::agent::remote_config::resolve_model_catalog(cfg, None)
}
fn provider(entry: &ModelEntry) -> String {
    safe_provider(&entry.info.base_url)
}
pub(crate) fn safe_provider(base_url: &str) -> String {
    url::Url::parse(base_url)
        .ok()
        .and_then(|u| u.host_str().map(str::to_owned))
        .unwrap_or_else(|| "unknown".into())
}
fn valid_credential_ref(reference: &str) -> bool {
    reference.strip_prefix("env:").is_some_and(|v| {
        !v.is_empty()
            && v.chars()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
    }) || (reference.starts_with("op://")
        && !reference.contains(['\n', '\r'])
        && reference.len() < 512)
}
fn valid_avatar(avatar: &str) -> bool {
    if let Ok(url) = url::Url::parse(avatar) {
        return url.scheme() == "https"
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none();
    }
    !avatar.is_empty()
        && avatar.chars().count() <= 16
        && avatar
            .chars()
            .all(|c| !c.is_ascii() && !c.is_alphanumeric() && !c.is_whitespace() && !c.is_control())
        && avatar
            .chars()
            .any(|c| matches!(c as u32, 0x00a9 | 0x00ae | 0x203c..=0x3299 | 0x1f000..=0x1faff))
}
fn safe_definition(def: &AgentDefinition) -> Value {
    let full = def.to_json_value();
    let mut safe = serde_json::Map::new();
    for key in SAFE_FIELDS {
        if let Some(v) = full.get(*key) {
            safe.insert((*key).into(), v.clone());
        }
    }
    // Keep the required editable client field explicit even if the native
    // serializer changes its representation of inherited settings.
    safe.entry("model").or_insert_with(|| json!("inherit"));
    if let Some(identity) = safe.get_mut("identity").and_then(Value::as_object_mut) {
        if let Some(slack) = identity.get_mut("slack").and_then(Value::as_object_mut) {
            if slack
                .get("credentialRef")
                .and_then(Value::as_str)
                .is_some_and(|reference| !valid_credential_ref(reference))
            {
                slack.insert("credentialRef".into(), json!("[invalid reference removed]"));
            }
        }
        if let Some(avatar) = identity.get("avatar").and_then(Value::as_str) {
            if !valid_avatar(avatar) {
                identity.insert("avatar".into(), json!("[invalid avatar removed]"));
            }
        }
    }
    Value::Object(safe)
}
pub(crate) fn definition_revision(def: &AgentDefinition) -> Result<String> {
    let mut frozen = def.clone();
    let references = frozen.resolve_prompt_references_with_fingerprint()?;
    Ok(hash(
        &json!({"source": def.to_json_value(), "resolved": frozen.to_json_value(), "references": references}),
    ))
}
fn definitions(cwd: &Path) -> Vec<AgentDefinition> {
    let mut defs = xai_grok_agent::discovery::discover(cwd);
    for name in ["general-purpose", "explore", "plan"] {
        if !defs.iter().any(|d| d.name == name)
            && let Some(def) = xai_grok_agent::discovery::by_name_in_cwd(name, cwd)
        {
            defs.push(def);
        }
    }
    defs.sort_by(|a, b| a.name.cmp(&b.name));
    defs
}
fn get(cwd: &Path, id: &str) -> Result<AgentDefinition> {
    get_scoped(cwd, id, "effective")
}
fn get_scoped(cwd: &Path, id: &str, scope: &str) -> Result<AgentDefinition> {
    if !valid_id(id) {
        bail!("Invalid profile ID; use an existing profile name.")
    }
    for (path, source_scope) in xai_grok_agent::discovery::source_files(cwd) {
        if !in_scope(scope, source_scope) {
            continue;
        }
        if scope == "effective"
            && ["general-purpose", "explore", "plan"].contains(&id)
            && source_scope != xai_grok_agent::config::AgentScope::Project
        {
            continue;
        }
        match AgentDefinition::from_file(&path) {
            Ok(mut def) if def.name == id => {
                def.scope = source_scope;
                return Ok(def);
            }
            Err(_) if path.file_stem().and_then(|s| s.to_str()) == Some(id) => {
                bail!("Profile source is malformed; repair it before starting an agent.")
            }
            _ => {}
        }
    }
    if scope == "effective" {
        if let Some(def) = xai_grok_agent::discovery::by_name_in_cwd(id, cwd) {
            return Ok(def);
        }
    }
    definitions(cwd).into_iter().filter(|d| scope == "effective" || in_scope(scope, d.scope)).find(|d| d.name == id).context(
        "Profile is unavailable or invalid; list valid profiles and retry. No agent was started.",
    )
}
fn in_scope(scope: &str, source: xai_grok_agent::config::AgentScope) -> bool {
    scope == "effective"
        || match source {
            xai_grok_agent::config::AgentScope::Project => scope == "project",
            xai_grok_agent::config::AgentScope::User => scope == "user",
            _ => false,
        }
}
fn listed_profiles(cwd: &Path, scope: &str, cfg: &Config) -> Vec<Value> {
    let mut profiles = std::collections::BTreeMap::new();
    for (path, source_scope) in xai_grok_agent::discovery::source_files(cwd) {
        if !in_scope(scope, source_scope) {
            continue;
        }
        let entry = match AgentDefinition::from_file(&path) {
            Ok(mut def) => {
                if scope == "effective"
                    && ["general-purpose", "explore", "plan"].contains(&def.name.as_str())
                    && source_scope != xai_grok_agent::config::AgentScope::Project
                {
                    continue;
                }
                def.scope = source_scope;
                profile_value(&def, cfg)
            }
            Err(_) => {
                let id = path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .unwrap_or("invalid-profile");
                json!({"version":1,"id":id,"revision":null,"sourcePath":path,"sourceScope":if source_scope == xai_grok_agent::config::AgentScope::Project {"project"} else {"user"},"definition":{"name":id,"description":"Profile source requires repair","model":"inherit"},"validation":{"valid":false,"errors":["Profile source is malformed or unreadable. Repair the source file before use."]}})
            }
        };
        if let Some(id) = entry.get("id").and_then(Value::as_str) {
            profiles.entry(id.to_owned()).or_insert(entry);
        }
    }
    if scope == "effective" {
        for def in definitions(cwd) {
            profiles
                .entry(def.name.clone())
                .or_insert_with(|| profile_value(&def, cfg));
        }
    }
    profiles.into_values().collect()
}
fn effort_choices(entry: &ModelEntry) -> Vec<String> {
    if !entry.info.supports_reasoning_effort {
        return vec![];
    }
    if entry.info.reasoning_efforts.is_empty() {
        return ["low", "medium", "high", "xhigh"]
            .map(str::to_owned)
            .to_vec();
    }
    entry
        .info
        .reasoning_efforts
        .iter()
        .map(|o| o.value.to_string())
        .collect()
}
pub(crate) fn validate_settings(
    model: Option<&str>,
    effort: Option<&str>,
    speed: Option<&str>,
    models: &indexmap::IndexMap<String, ModelEntry>,
) -> Vec<String> {
    let mut errors = Vec::new();
    let selected = model.filter(|m| *m != "inherit");
    let entry = selected.and_then(|m| crate::agent::config::find_model_by_id(models, m));
    if selected.is_some() && entry.is_none_or(|e| !e.info.user_selectable || e.info.hidden) {
        errors.push(format!(
            "Selected model is unavailable or not allowed; select a catalog model."
        ));
    }
    if let Some(effort) = effort {
        if !["low", "medium", "high", "xhigh", "max"].contains(&effort) {
            errors.push(format!(
                "Unsupported reasoning effort; select a catalog reasoning level."
            ));
        } else if let Some(entry) = entry
            && !effort_choices(entry).iter().any(|e| e == effort)
        {
            errors.push(format!(
                "Reasoning effort '{effort}' is unsupported by the selected model. Supported: {}.",
                effort_choices(entry).join(", ")
            ));
        }
    }
    if let Some(speed) = speed {
        if !["standard", "fast"].contains(&speed) {
            errors.push(format!(
                "Unsupported speed; choose standard or a supported fast model."
            ));
        } else if let Some(m) = selected
            && !speed_choices(m, models).contains(&speed)
        {
            errors.push("Requested speed is unsupported by the selected model; select a speed advertised for that catalog model.".into());
        }
    }
    if let Some(entry) = entry
        && !entry.has_own_credentials()
        && !crate::util::is_xai_api_url(&entry.info.base_url)
    {
        errors.push("Provider requires its own configured credential; parent credentials will not be forwarded.".into());
    }
    errors
}
pub(crate) fn supports_fast(model: &str, models: &indexmap::IndexMap<String, ModelEntry>) -> bool {
    let Some(selected) = crate::agent::config::find_model_by_id(models, model) else {
        return false;
    };
    let Some(fast) = crate::agent::config::find_model_by_id(models, "grok-4.7-build-fast") else {
        return false;
    };
    ["grok-4.7-build", "grok-4.7-build-fast"].contains(&selected.info.model.as_str())
        && fast.info.model == "grok-4.7-build-fast"
        && fast.info.user_selectable
        && !fast.info.hidden
        && selected.info.base_url == fast.info.base_url
}
pub(crate) fn speed_choices(
    model: &str,
    models: &indexmap::IndexMap<String, ModelEntry>,
) -> Vec<&'static str> {
    let actual = crate::agent::config::find_model_by_id(models, model)
        .map(|entry| entry.info.model.as_str())
        .unwrap_or(model);
    if actual == "grok-4.7-build-fast" {
        vec!["fast"]
    } else if supports_fast(model, models) {
        vec!["standard", "fast"]
    } else {
        vec!["standard"]
    }
}
fn validate_definition(def: &AgentDefinition, cfg: &Config) -> Vec<String> {
    let mut errors = Vec::new();
    if !valid_id(&def.name) {
        errors.push("Profile name must be a simple ID of 1–96 ASCII letters, digits, dots, hyphens or underscores.".into());
    }
    if def.description.trim().is_empty() {
        errors.push("Description is required.".into());
    }
    if let Some(identity) = &def.identity
        && let Some(slack) = &identity.slack
        && let Some(reference) = &slack.credential_ref
    {
        if !valid_credential_ref(reference) {
            errors.push("Slack credentialRef must be an env:VARIABLE or op:// reference, never a credential value.".into());
        }
    }
    if def
        .identity
        .as_ref()
        .and_then(|i| i.avatar.as_ref())
        .is_some_and(|avatar| !valid_avatar(avatar))
    {
        errors.push("Avatar must be an emoji or an HTTPS URL without credentials, query parameters or fragments.".into());
    }
    let effort = def.effort.map(|e| {
        let s: &'static str = e.into();
        s
    });
    errors.extend(validate_settings(
        match &def.model {
            xai_grok_agent::config::ModelOverride::Override(s) => Some(s.as_str()),
            _ => None,
        },
        effort,
        def.speed.as_deref(),
        &catalog(cfg),
    ));
    if let Err(_) = definition_revision(def) {
        errors.push(
            "A prompt or rule reference is unreadable, non-absolute, malformed, or too large."
                .into(),
        );
    }
    errors
}
fn validation(errors: Vec<String>) -> Value {
    json!({"valid": errors.is_empty(), "errors": errors})
}
fn profile_value(def: &AgentDefinition, cfg: &Config) -> Value {
    let source_scope = match def.scope {
        xai_grok_agent::config::AgentScope::Project => "project",
        xai_grok_agent::config::AgentScope::User => "user",
        xai_grok_agent::config::AgentScope::Bundled => "bundled",
        xai_grok_agent::config::AgentScope::BuiltIn => "builtin",
    };
    json!({"version":1,"id":def.name,"revision":definition_revision(def).ok(),"sourceScope":source_scope,"sourcePath":def.source_path,"definition":safe_definition(def),"validation":validation(validate_definition(def,cfg))})
}
fn policy_path() -> Result<PathBuf> {
    xai_grok_config::user_grok_home()
        .map(|p| p.join("delegation-policy.json"))
        .context("Grok home unavailable")
}
pub(crate) fn read_policy() -> Result<DelegationPolicy> {
    let path = policy_path()?;
    match std::fs::read(&path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .context("Delegation policy is malformed; repair it before spawning a child."),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(DelegationPolicy::default()),
        Err(e) => Err(e.into()),
    }
}
fn validate_policy(policy: &DelegationPolicy, cfg: &Config) -> Vec<String> {
    let mut errors = validate_settings(
        policy.defaults.model.as_deref(),
        policy.defaults.effort.as_deref(),
        policy.defaults.speed.as_deref(),
        &catalog(cfg),
    );
    if !["automatic", "profiles-only", "general-only"].contains(&policy.mode.as_str()) {
        errors.push("Routing mode must be automatic, profiles-only or general-only.".into());
    }
    errors
}
fn policy_value(policy: &DelegationPolicy, cfg: &Config) -> Value {
    let value = serde_json::to_value(policy).expect("policy serializable");
    json!({"version":1,"revision":hash(&value),"policy":value,"validation":validation(validate_policy(policy,cfg))})
}
fn read_input() -> Result<Value> {
    let mut text = String::new();
    std::io::stdin().take(2_097_153).read_to_string(&mut text)?;
    if text.len() > 2_097_152 {
        bail!("Input is too large")
    }
    serde_json::from_str(&text).context("Expected JSON on stdin")
}
fn lock() -> Result<std::fs::File> {
    let home = xai_grok_config::user_grok_home().context("Grok home unavailable")?;
    std::fs::create_dir_all(&home)?;
    let path = home.join("profile-control.lock");
    if std::fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("Refusing symlinked profile lock")
    }
    let file = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)?;
    file.lock_exclusive()?;
    Ok(file)
}
fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().context("Missing parent")?;
    std::fs::create_dir_all(parent)?;
    if std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("Refusing symlinked profile target")
    }
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        temp.as_file()
            .set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    temp.write_all(bytes)?;
    temp.as_file().sync_all()?;
    temp.persist(path)?;
    Ok(())
}
fn edited_definition(
    edit: &ProfileEdit,
    existing: Option<&AgentDefinition>,
) -> Result<AgentDefinition> {
    let object = edit
        .definition
        .as_object()
        .context("definition must be an object")?;
    if object.keys().any(|k| !SAFE_FIELDS.contains(&k.as_str())) {
        bail!("definition contains unsupported or credential-bearing fields")
    }
    if !valid_id(&edit.id) {
        bail!("Invalid profile ID")
    }
    let mut full = existing
        .map(AgentDefinition::to_json_value)
        .unwrap_or_else(|| json!({"name":edit.id,"description":""}));
    let full_object = full.as_object_mut().context("Invalid definition")?;
    for (k, v) in object {
        full_object.insert(k.clone(), v.clone());
    }
    full_object.insert("name".into(), json!(edit.id));
    AgentDefinition::from_json(&full).map_err(Into::into)
}
fn check_revision(expected: Option<&str>, actual: Option<&str>) -> Result<()> {
    if expected != actual {
        bail!(
            "Revision conflict: profile or referenced source changed. Reload, review and retry; nothing was written."
        )
    }
    Ok(())
}
/// Resolve existing ancestors too, so an absent agents directory beneath a
/// symlink cannot make a project write silently target the user profile store.
fn resolved_destination(path: &Path) -> Result<PathBuf> {
    let mut existing = path;
    let mut missing = Vec::new();
    loop {
        match std::fs::canonicalize(existing) {
            Ok(mut resolved) => {
                for component in missing.into_iter().rev() {
                    resolved.push(component);
                }
                return Ok(resolved);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                missing.push(
                    existing
                        .file_name()
                        .context("Invalid profile destination")?,
                );
                existing = existing.parent().context("Invalid profile destination")?;
            }
            Err(error) => return Err(error).context("Profile destination could not be resolved"),
        }
    }
}
fn ensure_distinct_project_target(project: &Path, user: &Path) -> Result<()> {
    if resolved_destination(project)? == resolved_destination(user)? {
        bail!("Project profile destination aliases user scope; select user scope explicitly")
    }
    Ok(())
}
fn profile_edit_target(cwd: &Path, edit: &ProfileEdit) -> Result<PathBuf> {
    if !valid_id(&edit.id) {
        bail!("Invalid profile ID")
    }
    let target_dir = match edit.write_scope.as_str() {
        "user" => xai_grok_config::user_grok_home()
            .context("Grok home unavailable")?
            .join("agents"),
        "project" => {
            let project = cwd.join(".grok/agents");
            let user = xai_grok_config::user_grok_home()
                .context("Grok home unavailable")?
                .join("agents");
            ensure_distinct_project_target(&project, &user)?;
            project
        }
        _ => bail!("writeScope must be project or user"),
    };
    Ok(target_dir.join(format!("{}.md", edit.id)))
}
/// An existing destination must never become a create operation merely because
/// discovery could not parse it, read it, or match its declared profile name.
fn existing_edit_target(target: &Path, edit: &ProfileEdit) -> Result<Option<AgentDefinition>> {
    let metadata = match std::fs::symlink_metadata(target) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("Profile destination could not be inspected"),
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        bail!("Profile destination is not a regular file; nothing was written")
    }
    let mut definition = AgentDefinition::from_file(target)
        .context("Existing profile destination must be repaired before editing")?;
    if definition.name != edit.id {
        bail!("Profile destination declares another profile; nothing was written")
    }
    definition.scope = if edit.write_scope == "user" {
        xai_grok_agent::config::AgentScope::User
    } else {
        xai_grok_agent::config::AgentScope::Project
    };
    Ok(Some(definition))
}
fn edit_profile(cwd: &Path, input: &ProfileEdit, cfg: &Config, apply: bool) -> Result<Value> {
    let target = profile_edit_target(cwd, input)?;
    let existing = existing_edit_target(&target, input)?;
    if input.write_scope == "user"
        && ["general-purpose", "explore", "plan"].contains(&input.id.as_str())
    {
        bail!("Built-in subagent overrides require project scope")
    }
    let revision = existing.as_ref().map(definition_revision).transpose()?;
    if apply {
        check_revision(input.expected_revision.as_deref(), revision.as_deref())?;
    }
    let inherited = get(cwd, &input.id).ok();
    let mut def = edited_definition(input, existing.as_ref().or(inherited.as_ref()))?;
    let errors = validate_definition(&def, cfg);
    if apply && !errors.is_empty() {
        bail!("Profile validation failed: {}", errors.join(" "))
    }
    if apply {
        let mut full = def.to_json_value();
        let body = full
            .as_object_mut()
            .and_then(|o| o.remove("promptBody"))
            .and_then(|v| v.as_str().map(str::to_owned))
            .unwrap_or_default();
        let text = format!("---\n{}\n---\n\n{}\n", serde_json::to_string(&full)?, body);
        atomic_write(&target, text.as_bytes())?;
        def.source_path = Some(target);
        def.scope = if input.write_scope == "user" {
            xai_grok_agent::config::AgentScope::User
        } else {
            xai_grok_agent::config::AgentScope::Project
        };
    }
    Ok(profile_value(&def, cfg))
}
pub async fn run(args: ProfilesArgs) -> Result<()> {
    match run_inner(args).await {
        Ok(()) => Ok(()),
        Err(error) => {
            let conflict = error.to_string().starts_with("Revision conflict:");
            let output = json!({"error":{"code":if conflict {"revision_conflict"} else {"validation_failed"},"message":if conflict {"Profile or referenced instructions changed. Reload and review before retrying."} else {"The profile operation was refused. Reload the catalog, validate the requested settings and retry."}}});
            println!("{}", serde_json::to_string(&output)?);
            // Avoid forwarding arbitrary config/parser errors to tools or logs.
            Err(anyhow::anyhow!("profile operation refused"))
        }
    }
}
async fn run_inner(args: ProfilesArgs) -> Result<()> {
    let cwd = args.cwd.unwrap_or(std::env::current_dir()?);
    if !cwd.is_absolute() || !cwd.is_dir() {
        bail!("--cwd must be an absolute existing workspace directory")
    }
    std::env::set_current_dir(&cwd)?;
    let cfg = config()?;
    let output = match args.command {
        ProfilesCommand::List { .. } => {
            let profiles = listed_profiles(&cwd, &args.scope, &cfg);
            let available_models = catalog(&cfg);
            let models = available_models.iter()
                .filter(|(_, entry)| entry.info.user_selectable && !entry.info.hidden)
                .map(|(id, entry)| {
                    let available = entry.has_own_credentials()
                        || crate::util::is_xai_api_url(&entry.info.base_url);
                    json!({"id":id,"model":entry.info.model,"provider":provider(entry),"reasoningEfforts":effort_choices(entry),"speeds":speed_choices(id,&available_models),"available":available,"reason":if available {None::<&str>}else{Some("Provider credential unavailable")}})
                }).collect::<Vec<_>>();
            let revision = hash(&json!(profiles));
            json!({"version":1,"revision":revision,"profiles":profiles,"models":models,"policy":policy_value(&read_policy()?,&cfg)})
        }
        ProfilesCommand::Get { id, .. } => {
            profile_value(&get_scoped(&cwd, &id, &args.scope)?, &cfg)
        }
        ProfilesCommand::Validate { .. } | ProfilesCommand::Apply { .. } => {
            let apply = matches!(args.command, ProfilesCommand::Apply { .. });
            let input: ProfileEdit = serde_json::from_value(read_input()?)?;
            if !["project", "user"].contains(&input.write_scope.as_str()) {
                bail!("writeScope must be project or user")
            }
            let _lock = if apply { Some(lock()?) } else { None };
            edit_profile(&cwd, &input, &cfg, apply)?
        }
        ProfilesCommand::Policy { command } => match command {
            PolicyCommand::Get { .. } => policy_value(&read_policy()?, &cfg),
            PolicyCommand::Validate { .. } | PolicyCommand::Apply { .. } => {
                let apply = matches!(command, PolicyCommand::Apply { .. });
                let edit: PolicyEdit = serde_json::from_value(read_input()?)?;
                let _lock = if apply { Some(lock()?) } else { None };
                if apply {
                    let current = read_policy()?;
                    let actual = hash(&serde_json::to_value(&current)?);
                    check_revision(edit.expected_revision.as_deref(), Some(&actual))?;
                }
                let errors = validate_policy(&edit.policy, &cfg);
                if apply && !errors.is_empty() {
                    bail!("Policy validation failed: {}", errors.join(" "))
                }
                if apply {
                    atomic_write(&policy_path()?, &serde_json::to_vec_pretty(&edit.policy)?)?;
                }
                policy_value(&edit.policy, &cfg)
            }
        },
    };
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}
/// Resolve and freeze a named profile at actual native admission, under the same
/// lock as profile edits. The revision covers resolved referenced source content.
pub(crate) fn admit_profile(
    cwd: &Path,
    id: &str,
    expected: Option<&str>,
    cfg: &Config,
) -> Result<(AgentDefinition, AppliedProfile)> {
    let _lock = lock()?;
    let mut def = get(cwd, id)?;
    let source = def.to_json_value();
    let references = def.resolve_prompt_references_with_fingerprint()?;
    let revision =
        hash(&json!({"source": source, "resolved": def.to_json_value(), "references": references}));
    if let Some(expected) = expected {
        check_revision(Some(expected), Some(&revision))?;
    }
    let errors = validate_definition(&def, cfg);
    if !errors.is_empty() {
        bail!(
            "Profile cannot start: {} No agent was started.",
            errors.join(" ")
        )
    }
    // Stock top-level harness names are also emitted by ordinary Terminal
    // flags. Keep their session model controls; only specialist/General
    // admission owns the immutable profile settings contract.
    let stock_harness = def.scope == xai_grok_agent::config::AgentScope::BuiltIn
        && !["general-purpose", "explore", "plan"].contains(&def.name.as_str());
    def.applied_profile_revision = (!stock_harness).then(|| revision.clone());
    Ok((
        def,
        AppliedProfile {
            id: id.to_owned(),
            revision,
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile(value: Value) -> AgentDefinition {
        AgentDefinition::from_json(&value).expect("valid test profile")
    }

    #[test]
    fn profile_project_write_refuses_home_alias_before_directory_creation() {
        let dir = tempfile::tempdir().unwrap();
        let user = dir.path().join("home/.grok/agents");
        std::fs::create_dir_all(dir.path().join("home")).unwrap();
        assert!(ensure_distinct_project_target(&user, &user).is_err());
        let project = dir.path().join("workspace/.grok/agents");
        assert!(ensure_distinct_project_target(&project, &user).is_ok());
        assert!(!user.exists());
        assert!(!project.exists());
    }

    #[cfg(unix)]
    #[test]
    fn profile_project_write_refuses_symlink_alias_with_missing_agents_directory() {
        let dir = tempfile::tempdir().unwrap();
        let user_home = dir.path().join("user-grok");
        let workspace = dir.path().join("workspace");
        std::fs::create_dir_all(&user_home).unwrap();
        std::fs::create_dir_all(&workspace).unwrap();
        std::os::unix::fs::symlink(&user_home, workspace.join(".grok")).unwrap();
        assert!(
            ensure_distinct_project_target(
                &workspace.join(".grok/agents"),
                &user_home.join("agents")
            )
            .is_err()
        );
        std::fs::create_dir(user_home.join("agents")).unwrap();
        assert!(
            ensure_distinct_project_target(
                &workspace.join(".grok/agents"),
                &user_home.join("agents")
            )
            .is_err()
        );
    }

    #[test]
    fn profile_stock_terminal_harness_resolves_without_requiring_a_saved_profile() {
        let dir = tempfile::tempdir().unwrap();
        let def = get_scoped(dir.path(), "grok-build-plan", "effective").unwrap();
        assert_eq!(def.name, "grok-build-plan");
        assert!(def.applied_profile_revision.is_none());
        assert!(get_scoped(dir.path(), "missing-profile-routing-fixture", "effective").is_err());
    }

    #[test]
    fn profile_source_cannot_mint_native_revision_proof() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("forged.md");
        std::fs::write(&path, "---\nname: forged\ndescription: Test\nappliedProfileRevision: forged-revision\n---\nRole body").unwrap();
        assert!(
            AgentDefinition::from_file(&path)
                .unwrap()
                .applied_profile_revision
                .is_none()
        );
        let mut trusted = profile(json!({"name":"frozen","description":"Frozen snapshot"}));
        trusted.applied_profile_revision = Some("trusted-admission-revision".into());
        assert_eq!(
            AgentDefinition::from_json(&trusted.to_json_value())
                .unwrap()
                .applied_profile_revision,
            trusted.applied_profile_revision
        );
    }

    #[test]
    fn profile_safe_export_removes_invalid_credentials_and_avatar_auth() {
        let def = profile(
            json!({"name":"cfo","description":"Financial role","identity":{"avatar":"https://user:private@example.com/avatar?token=private","slack":{"botKey":"cfo","credentialRef":"xoxb-private-test"}}}),
        );
        let exported = safe_definition(&def).to_string();
        assert!(!exported.contains("private"));
        assert!(!exported.contains("xoxb"));
        let errors = validate_definition(&def, &Config::default());
        assert!(errors.iter().any(|e| e.contains("credentialRef")));
        assert!(errors.iter().any(|e| e.contains("Avatar")));
        assert!(!errors.join(" ").contains("private"));
    }

    #[test]
    fn profile_safe_export_keeps_inherited_model_explicit_for_clients() {
        let def = profile(json!({"name":"researcher","description":"Research"}));
        let exported = safe_definition(&def);
        assert_eq!(exported["model"], "inherit");
        assert!(AgentDefinition::from_json(&exported).is_ok());
    }

    #[test]
    fn profile_avatar_contract_matches_renderable_client_values() {
        for avatar in ["🦉", "👩‍💻", "https://example.com/avatar.png"] {
            assert!(valid_avatar(avatar), "{avatar}");
        }
        for avatar in [
            "",
            "CFO",
            "http://example.com/avatar.png",
            "file:///tmp/avatar.png",
            "javascript:alert(1)",
            "https://example.com/avatar?token=private",
            "https://example.com/avatar#private",
            "https://user:private@example.com/avatar",
        ] {
            assert!(!valid_avatar(avatar));
            let def = profile(
                json!({"name":"cfo","description":"Financial role","identity":{"avatar":avatar}}),
            );
            assert_eq!(
                safe_definition(&def)["identity"]["avatar"],
                "[invalid avatar removed]"
            );
            assert!(
                validate_definition(&def, &Config::default())
                    .iter()
                    .any(|e| e.contains("Avatar"))
            );
        }
    }

    #[test]
    fn profile_rejected_model_never_echoes_untrusted_value() {
        let errors = validate_settings(
            Some("private-invalid-selection"),
            Some("private-effort"),
            Some("fast"),
            &Default::default(),
        );
        assert!(!errors.is_empty());
        assert!(!errors.join(" ").contains("private"));
    }

    #[test]
    fn profile_revision_tracks_exact_frozen_reference_and_clears_source_paths() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("role.md");
        std::fs::write(&path, "---\nmodel: claude-only\n---\n\nOriginal role body").unwrap();
        let def =
            profile(json!({"name":"cfo","description":"Financial role","promptFiles":[path]}));
        let revision = definition_revision(&def).unwrap();
        let mut frozen = def.clone();
        frozen.resolve_prompt_references().unwrap();
        assert!(frozen.prompt_files.is_empty());
        assert!(
            frozen
                .prompt_body
                .as_ref()
                .unwrap()
                .contains("Original role body")
        );
        assert!(!frozen.prompt_body.as_ref().unwrap().contains("claude-only"));
        let body = frozen.prompt_body.clone();
        frozen.resolve_prompt_references().unwrap();
        assert_eq!(body, frozen.prompt_body);
        std::fs::write(
            &path,
            "---\nmodel: changed-frontmatter-only\n---\n\nOriginal role body",
        )
        .unwrap();
        assert_ne!(revision, definition_revision(&def).unwrap());
        assert_eq!(body, frozen.prompt_body);
        std::fs::write(&path, "Changed role body").unwrap();
        assert_ne!(revision, definition_revision(&def).unwrap());
        assert_eq!(body, frozen.prompt_body);
    }

    #[test]
    fn profile_references_have_aggregate_bound_and_no_partial_mutation() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.md");
        let b = dir.path().join("b.md");
        std::fs::write(&a, "a".repeat(600_000)).unwrap();
        std::fs::write(&b, "b".repeat(600_000)).unwrap();
        let mut def = profile(
            json!({"name":"cfo","description":"Financial role","promptBody":"Original","ruleFiles":[a,b]}),
        );
        assert!(def.resolve_prompt_references().is_err());
        assert_eq!(def.prompt_body.as_deref(), Some("Original"));
        assert_eq!(def.rule_files.len(), 2);
        let mut body_only = profile(
            json!({"name":"cfo","description":"Financial role","promptBody":"x".repeat(1_048_577)}),
        );
        assert!(body_only.resolve_prompt_references().is_err());
        assert_eq!(body_only.prompt_body.as_ref().unwrap().len(), 1_048_577);
        let tiny = dir.path().join("tiny.md");
        std::fs::write(&tiny, "a").unwrap();
        let mut formatted = profile(
            json!({"name":"cfo","description":"Financial role","promptBody":"x".repeat(1_048_570),"ruleFiles":[tiny]}),
        );
        assert!(formatted.resolve_prompt_references().is_err());
        assert_eq!(formatted.rule_files.len(), 1);
    }

    #[test]
    fn profile_revision_precondition_refuses_stale_and_missing_revision() {
        assert!(check_revision(Some("first"), Some("second")).is_err());
        assert!(check_revision(None, Some("existing")).is_err());
        assert!(check_revision(Some("existing"), Some("existing")).is_ok());
        assert!(check_revision(None, None).is_ok());
    }

    #[test]
    fn profile_create_refuses_malformed_or_mismatched_existing_destination_without_writing() {
        let dir = tempfile::tempdir().unwrap();
        let agents = dir.path().join(".grok/agents");
        std::fs::create_dir_all(&agents).unwrap();
        let target = agents.join("researcher.md");
        let input: ProfileEdit = serde_json::from_value(json!({
            "id":"researcher", "expectedRevision":null, "writeScope":"project",
            "definition":{"description":"Replacement", "promptBody":"New instructions"}
        }))
        .unwrap();
        for original in [
            "---\nname: [broken\n---\nOriginal malformed user instructions\n",
            "---\nname: another-profile\ndescription: Existing\n---\nOriginal user instructions\n",
        ] {
            std::fs::write(&target, original).unwrap();
            assert!(edit_profile(dir.path(), &input, &Config::default(), true).is_err());
            assert_eq!(std::fs::read(&target).unwrap(), original.as_bytes());
        }
    }

    #[cfg(unix)]
    #[test]
    fn profile_create_refuses_symlink_destination_without_writing() {
        let dir = tempfile::tempdir().unwrap();
        let agents = dir.path().join(".grok/agents");
        std::fs::create_dir_all(&agents).unwrap();
        let protected = dir.path().join("protected.md");
        let original = "---\nname: researcher\ndescription: Original\n---\nUser instructions\n";
        std::fs::write(&protected, original).unwrap();
        let target = agents.join("researcher.md");
        std::os::unix::fs::symlink(&protected, &target).unwrap();
        let input: ProfileEdit = serde_json::from_value(json!({
            "id":"researcher", "expectedRevision":null, "writeScope":"project",
            "definition":{"description":"Replacement"}
        }))
        .unwrap();
        assert!(edit_profile(dir.path(), &input, &Config::default(), true).is_err());
        assert!(
            std::fs::symlink_metadata(target)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(std::fs::read(protected).unwrap(), original.as_bytes());
    }

    #[test]
    fn profile_edits_preserve_unedited_permission_restrictions() {
        let existing = profile(
            json!({"name":"researcher","description":"Research","disallowedTools":["bash"],"promptBody":"Original"}),
        );
        let edit: ProfileEdit = serde_json::from_value(json!({"id":"researcher","expectedRevision":"old","writeScope":"user","definition":{"description":"Updated"}})).unwrap();
        let edited = edited_definition(&edit, Some(&existing)).unwrap();
        assert_eq!(edited.disallowed_tools, existing.disallowed_tools);
        assert_eq!(edited.prompt_body, existing.prompt_body);
    }
}

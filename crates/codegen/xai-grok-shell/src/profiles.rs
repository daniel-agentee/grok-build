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
    // Preserve filesystem error types for the control-plane response. The
    // general one-shot loader stringifies them for its legacy callers.
    let effective = crate::config::load_effective_config_disk_only()?;
    let mut config = Config::new_from_toml_cfg(&effective).map_err(anyhow::Error::msg)?;
    crate::config::apply_policy(&mut config);
    Ok(config)
}
fn catalog(cfg: &Config) -> indexmap::IndexMap<String, ModelEntry> {
    let auth = cfg.create_auth_manager().current_or_expired();
    let cached = crate::agent::remote_config::load_cached_models(&cfg.endpoints, auth.as_ref());
    crate::agent::remote_config::resolve_model_catalog(cfg, cached.map(|c| c.models))
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
    }) || reference.strip_prefix("seat-bot:").is_some_and(|key| {
        !key.is_empty()
            && key.len() <= 96
            && key
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
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
pub(crate) fn definition_revision(def: &AgentDefinition, cwd: &Path) -> Result<String> {
    let mut frozen = def.clone();
    let references = frozen.resolve_prompt_references_with_fingerprint(cwd)?;
    Ok(hash(
        &json!({"source": def.to_json_value(), "resolved": frozen.to_json_value(), "references": references}),
    ))
}
fn unreadable_reference(path: &Path, canonical: Option<&Path>, error: &std::io::Error) -> Value {
    let metadata = std::fs::metadata(canonical.unwrap_or(path)).ok().map(|metadata| {
        let mut state = json!({"size":metadata.len(),"isFile":metadata.is_file(),
            "readonly":metadata.permissions().readonly(),
            "modified":metadata.modified().ok().and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok()).map(|time| (time.as_secs(),time.subsec_nanos()))});
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            state["unix"] = json!({"device":metadata.dev(),"inode":metadata.ino(),
                "mode":metadata.mode(),"ctime":metadata.ctime(),"ctimeNanos":metadata.ctime_nsec()});
        }
        state
    });
    json!({"path":path,"canonical":canonical,"state":"unreadable",
        "errorKind":format!("{:?}",error.kind()),"osError":error.raw_os_error(),"metadata":metadata})
}
/// Hash references even when their UTF-8/frontmatter is invalid. No referenced
/// bytes enter the response; missing and refused targets have explicit states.
fn repair_reference_fingerprint(def: &AgentDefinition, cwd: &Path) -> Result<String> {
    let chain = xai_grok_agent::repo::RepoDirChain::resolve(cwd);
    let root = std::fs::canonicalize(chain.git_root.as_deref().unwrap_or(cwd))?;
    let external_import =
        def.scope == xai_grok_agent::config::AgentScope::User && def.source_path.is_some();
    let mut references = Vec::new();
    for path in def.prompt_files.iter().chain(&def.rule_files) {
        if !path.is_absolute() {
            references.push(json!({"path":path,"state":"relative"}));
            continue;
        }
        let canonical = match std::fs::canonicalize(path) {
            Ok(path) => path,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                references.push(json!({"path":path,"state":"missing"}));
                continue;
            }
            Err(error) => {
                references.push(unreadable_reference(path, None, &error));
                continue;
            }
        };
        if !external_import && !canonical.starts_with(&root) {
            references.push(json!({"path":path,"canonical":canonical,"state":"outside-workspace"}));
            continue;
        }
        let file = match std::fs::File::open(&canonical) {
            Ok(file) => file,
            Err(error) => {
                references.push(unreadable_reference(path, Some(&canonical), &error));
                continue;
            }
        };
        let metadata = match file.metadata() {
            Ok(metadata) => metadata,
            Err(error) => {
                references.push(unreadable_reference(path, Some(&canonical), &error));
                continue;
            }
        };
        if !metadata.is_file() {
            references.push(json!({"path":path,"canonical":canonical,"state":"not-file"}));
            continue;
        }
        // Streaming keeps repair tokens exact for corrupt and oversized files
        // without allocating the file. Bound a concurrent growing file to the
        // size observed at open plus one; its next revision will differ.
        let mut reader = file.take(metadata.len().saturating_add(1));
        let mut digest = blake3::Hasher::new();
        let mut buffer = [0u8; 8192];
        let mut read_error = None;
        loop {
            let count = match reader.read(&mut buffer) {
                Ok(count) => count,
                Err(error) => {
                    read_error = Some(error);
                    break;
                }
            };
            if count == 0 {
                break;
            }
            digest.update(&buffer[..count]);
        }
        if let Some(error) = read_error {
            references.push(unreadable_reference(path, Some(&canonical), &error));
            continue;
        }
        references.push(json!({"path":path,"canonical":canonical,"size":metadata.len(),"digest":digest.finalize().to_hex().as_str()}));
    }
    Ok(hash(&json!(references)))
}
/// Invalid referenced content still has a distinct edit token. It must never
/// look like an absent destination, including while one invalid file replaces another.
fn definition_control_revision(def: &AgentDefinition, cwd: &Path) -> Result<String> {
    match definition_revision(def, cwd) {
        Ok(revision) => Ok(revision),
        Err(error)
            if error
                .downcast_ref::<xai_grok_agent::AgentBuildError>()
                .is_some_and(|cause| {
                    matches!(
                        cause,
                        xai_grok_agent::AgentBuildError::ParseError(_)
                            | xai_grok_agent::AgentBuildError::IoError(_)
                    )
                }) =>
        {
            Ok(hash(
                &json!({"repairDefinition": def.to_json_value(), "references": repair_reference_fingerprint(def, cwd)?}),
            ))
        }
        Err(error) => Err(error),
    }
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
fn listed_profiles(cwd: &Path, scope: &str, cfg: &Config) -> Result<Vec<Value>> {
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
                profile_value(&def, cfg, cwd)?
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
            if !profiles.contains_key(&def.name) {
                profiles.insert(def.name.clone(), profile_value(&def, cfg, cwd)?);
            }
        }
    }
    Ok(profiles.into_values().collect())
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
fn validate_definition(def: &AgentDefinition, cfg: &Config, cwd: &Path) -> Vec<String> {
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
            errors.push("Slack credentialRef must be a seat-bot:BOT_KEY, env:VARIABLE or op:// reference, never a credential value. Runtime currently supports only the matching seat-bot binding.".into());
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
    if let Err(_) = definition_revision(def, cwd) {
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
fn profile_value(def: &AgentDefinition, cfg: &Config, cwd: &Path) -> Result<Value> {
    let source_scope = match def.scope {
        xai_grok_agent::config::AgentScope::Project => "project",
        xai_grok_agent::config::AgentScope::User => "user",
        xai_grok_agent::config::AgentScope::Bundled => "bundled",
        xai_grok_agent::config::AgentScope::BuiltIn => "builtin",
    };
    Ok(
        json!({"version":1,"id":def.name,"revision":definition_control_revision(def,cwd)?,"sourceScope":source_scope,"sourcePath":def.source_path,"definition":safe_definition(def),"validation":validation(validate_definition(def,cfg,cwd))}),
    )
}
fn policy_path() -> Result<PathBuf> {
    xai_grok_config::user_grok_home()
        .map(|p| p.join("delegation-policy.json"))
        .context("Grok home unavailable")
}
/// A malformed policy is unusable at admission but remains repairable through
/// the control plane with a revision bound to its exact corrupt bytes.
fn policy_state(path: &Path) -> Result<(Option<DelegationPolicy>, String)> {
    match std::fs::read(path) {
        Ok(bytes) => match serde_json::from_slice::<DelegationPolicy>(&bytes) {
            Ok(policy) => {
                let revision = hash(&serde_json::to_value(&policy)?);
                Ok((Some(policy), revision))
            }
            Err(_) => Ok((
                None,
                hash(&json!({"repairPolicy": blake3::hash(&bytes).to_hex().as_str()})),
            )),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let policy = DelegationPolicy::default();
            let revision = hash(&serde_json::to_value(&policy)?);
            Ok((Some(policy), revision))
        }
        Err(error) => Err(error.into()),
    }
}
pub(crate) fn read_policy() -> Result<DelegationPolicy> {
    policy_state(&policy_path()?)?
        .0
        .context("Delegation policy is malformed; repair it before spawning a child.")
}
fn policy_control_value(cfg: &Config) -> Result<Value> {
    let (policy, revision) = policy_state(&policy_path()?)?;
    Ok(match policy {
        Some(policy) => policy_value(&policy, cfg),
        None => {
            json!({"version":1,"revision":revision,"policy":DelegationPolicy::default(),"validation":{"valid":false,"errors":["Delegation policy is malformed. Review and apply an explicit replacement to repair it; delegation remains blocked."]}})
        }
    })
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
    file.try_lock_exclusive()?;
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
#[derive(Debug, thiserror::Error)]
#[error(
    "Revision conflict: profile or referenced source changed. Reload, review and retry; nothing was written."
)]
struct RevisionConflict;

fn check_revision(expected: Option<&str>, actual: Option<&str>) -> Result<()> {
    if expected != actual {
        return Err(RevisionConflict.into());
    }
    Ok(())
}

fn control_error_response(error: &anyhow::Error) -> Value {
    let (code, message) = if error.downcast_ref::<RevisionConflict>().is_some() {
        (
            "revision_conflict",
            "Profile or referenced instructions changed. Reload and review before retrying.",
        )
    } else if error.chain().any(|cause| {
        cause.downcast_ref::<std::io::Error>().is_some_and(|io| {
            !matches!(
                io.kind(),
                std::io::ErrorKind::InvalidData | std::io::ErrorKind::InvalidInput
            )
        })
    }) {
        (
            "service_unavailable",
            "Profile storage is temporarily unavailable. Retry when local storage is ready.",
        )
    } else {
        (
            "validation_failed",
            "The profile operation was refused. Reload the catalog, validate the requested settings and retry.",
        )
    };
    json!({"error":{"code":code,"message":message}})
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
    let revision = existing
        .as_ref()
        .map(|def| definition_control_revision(def, cwd))
        .transpose()?;
    if apply {
        check_revision(input.expected_revision.as_deref(), revision.as_deref())?;
    }
    let inherited = get(cwd, &input.id).ok();
    let mut def = edited_definition(input, existing.as_ref().or(inherited.as_ref()))?;
    def.source_path = Some(target.clone());
    def.scope = if input.write_scope == "user" {
        xai_grok_agent::config::AgentScope::User
    } else {
        xai_grok_agent::config::AgentScope::Project
    };
    let errors = validate_definition(&def, cfg, cwd);
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
    profile_value(&def, cfg, cwd)
}
#[cfg(test)]
pub(crate) fn apply_profile_control_for_test(cwd: &Path, input: Value) -> Result<Value> {
    edit_profile(
        cwd,
        &serde_json::from_value(input)?,
        &Config::default(),
        true,
    )
}
pub async fn run(args: ProfilesArgs) -> Result<()> {
    match run_inner(args).await {
        Ok(()) => Ok(()),
        Err(error) => {
            let output = control_error_response(&error);
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
            let profiles = listed_profiles(&cwd, &args.scope, &cfg)?;
            let available_models = catalog(&cfg);
            let models = available_models.iter()
                .filter(|(_, entry)| entry.info.user_selectable && !entry.info.hidden)
                .map(|(id, entry)| {
                    let available = entry.has_own_credentials()
                        || crate::util::is_xai_api_url(&entry.info.base_url);
                    json!({"id":id,"model":entry.info.model,"provider":provider(entry),"reasoningEfforts":effort_choices(entry),"speeds":speed_choices(id,&available_models),"available":available,"reason":if available {None::<&str>}else{Some("Provider credential unavailable")}})
                }).collect::<Vec<_>>();
            let revision = hash(&json!(profiles));
            json!({"version":1,"revision":revision,"profiles":profiles,"models":models,"policy":policy_control_value(&cfg)?})
        }
        ProfilesCommand::Get { id, .. } => {
            profile_value(&get_scoped(&cwd, &id, &args.scope)?, &cfg, &cwd)?
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
            PolicyCommand::Get { .. } => policy_control_value(&cfg)?,
            PolicyCommand::Validate { .. } | PolicyCommand::Apply { .. } => {
                let apply = matches!(command, PolicyCommand::Apply { .. });
                let edit: PolicyEdit = serde_json::from_value(read_input()?)?;
                let _lock = if apply { Some(lock()?) } else { None };
                if apply {
                    let (_, actual) = policy_state(&policy_path()?)?;
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
    let references = def.resolve_prompt_references_with_fingerprint(cwd)?;
    let revision =
        hash(&json!({"source": source, "resolved": def.to_json_value(), "references": references}));
    if let Some(expected) = expected {
        check_revision(Some(expected), Some(&revision))?;
    }
    let errors = validate_definition(&def, cfg, cwd);
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

    fn with_cached_catalog(test_name: &str, test: impl FnOnce(Config, &Path)) {
        use crate::agent::remote_config::{ModelFetchAuth, resolve_models_cache_scope};
        use xai_grok_cloud_config::remote_settings::ModelsCacheManager;
        use xai_grok_test_support::EnvGuard;

        const CHILD_ENV: &str = "GROK_TEST_PROFILE_CATALOG_CHILD";
        let pass_marker = format!("profile-catalog-isolated-passed:{test_name}");
        // grok_home() is process-cached. Re-exec with the private home already
        // selected so earlier catalog tests cannot redirect this cache fixture.
        if std::env::var(CHILD_ENV).ok().as_deref() != Some(test_name) {
            let home = tempfile::tempdir().unwrap();
            let filter = module_path!().split_once("::").unwrap().1;
            let mut command = std::process::Command::new(std::env::current_exe().unwrap());
            command
                .env("GROK_HOME", home.path())
                .env(CHILD_ENV, test_name)
                .arg("--exact")
                .arg(format!("{filter}::{test_name}"))
                .arg("--nocapture")
                .arg("--test-threads=1")
                .stdin(std::process::Stdio::null());
            xai_tty_utils::detach_std_command(&mut command);
            let output = command.output().unwrap();
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(
                output.status.success() && stdout.contains(&pass_marker),
                "isolated catalog test failed:\n{stdout}\n{stderr}"
            );
            return;
        }
        let home = PathBuf::from(std::env::var_os("GROK_HOME").unwrap());
        let _environment = [
            EnvGuard::set(
                "GROK_AUTH_PATH",
                home.join("absent-auth.json").to_str().unwrap(),
            ),
            EnvGuard::unset("GROK_AUTH"),
            EnvGuard::unset("GROK_CODE_XAI_API_KEY"),
            EnvGuard::set("XAI_API_KEY", "profile-catalog-test-key"),
        ];
        let cfg = Config::default();
        let auth = cfg.create_auth_manager().current_or_expired();
        assert!(auth.is_none());
        let fetch_auth = ModelFetchAuth::resolve(&cfg.endpoints, auth.is_some());
        assert_eq!(fetch_auth, ModelFetchAuth::ApiKey);
        let scope = resolve_models_cache_scope(&cfg.endpoints, fetch_auth, auth.as_ref());
        let models = ["cached-only-model", "grok-4.7-build-fast"]
            .into_iter()
            .map(|id| {
                let mut info = crate::agent::config::ModelInfo::fallback(id);
                info.base_url = "https://api.x.ai/v1".into();
                info.hidden = false;
                info.supports_reasoning_effort = true;
                info.reasoning_efforts = vec![xai_grok_sampling_types::ReasoningEffortOption {
                    id: "xhigh".into(),
                    value: xai_grok_sampling_types::ReasoningEffort::Xhigh,
                    label: "Extra high".into(),
                    description: None,
                    default: true,
                }];
                (
                    id.into(),
                    ModelEntry {
                        info,
                        mtls_cert_dir: None,
                        api_key: None,
                        env_key: None,
                        auth_provider: None,
                        api_base_url: None,
                    },
                )
            })
            .collect();
        let cache = ModelsCacheManager::new();
        assert_eq!(cache.path, home.join("models_cache.json"));
        cache.persist(&models, None, &scope, chrono::Utc::now());
        assert!(cache.load_fresh(&scope).is_some());
        test(cfg, &cache.path);
        println!("{pass_marker}");
    }

    #[test]
    #[serial_test::serial]
    fn profile_catalog_uses_fresh_scoped_cache_for_list_and_validation_siblings() {
        with_cached_catalog(
            "profile_catalog_uses_fresh_scoped_cache_for_list_and_validation_siblings",
            |cfg, cache| {
                let original_cache = std::fs::read(cache).unwrap();
                let models = catalog(&cfg);
                assert!(
                    models.contains_key("cached-only-model"),
                    "profile catalog must include the authenticated fresh cached catalog"
                );
                assert_eq!(effort_choices(&models["cached-only-model"]), ["xhigh"]);
                assert_eq!(speed_choices("grok-4.7-build-fast", &models), ["fast"]);
                let def = profile(json!({"name":"cached-profile","description":"Cached route",
                "model":"grok-4.7-build-fast","effort":"xhigh","speed":"fast"}));
                let cwd = cache.parent().unwrap();
                assert!(validate_definition(&def, &cfg, cwd).is_empty());
                assert_eq!(
                    profile_value(&def, &cfg, cwd).unwrap()["validation"]["valid"],
                    true
                );
                let policy = DelegationPolicy {
                    mode: "automatic".into(),
                    defaults: DelegationDefaults {
                        model: Some("grok-4.7-build-fast".into()),
                        effort: Some("xhigh".into()),
                        speed: Some("fast".into()),
                    },
                };
                assert!(validate_policy(&policy, &cfg).is_empty());
                assert_eq!(policy_value(&policy, &cfg)["validation"]["valid"], true);
                assert_eq!(
                    edit_profile(
                        cwd,
                        &ProfileEdit {
                            id: def.name.clone(),
                            expected_revision: None,
                            write_scope: "project".into(),
                            definition: safe_definition(&def)
                        },
                        &cfg,
                        false
                    )
                    .unwrap()["validation"]["valid"],
                    true
                );
                assert_eq!(
                    original_cache,
                    std::fs::read(cache).unwrap(),
                    "catalog reads must not renew or rewrite cache provenance"
                );
            },
        );
    }

    #[test]
    #[serial_test::serial]
    fn profile_catalog_rejects_absent_stale_malformed_and_mismatched_cache() {
        with_cached_catalog(
            "profile_catalog_rejects_absent_stale_malformed_and_mismatched_cache",
            |cfg, cache| {
                let original: Value =
                    serde_json::from_slice(&std::fs::read(cache).unwrap()).unwrap();
                for (field, invalid) in [
                    ("grok_version", json!("different-version")),
                    ("auth_method", json!("session")),
                    ("origin", json!("https://another-endpoint.invalid")),
                    ("identity", json!("another-identity")),
                    ("fetched_at", json!("2000-01-01T00:00:00Z")),
                    ("renewed_at", json!("2000-01-01T00:00:00Z")),
                ] {
                    let mut changed = original.clone();
                    changed[field] = invalid;
                    std::fs::write(cache, serde_json::to_vec(&changed).unwrap()).unwrap();
                    assert!(!catalog(&cfg).contains_key("cached-only-model"), "{field}");
                }
                std::fs::write(cache, b"malformed").unwrap();
                assert!(!catalog(&cfg).contains_key("cached-only-model"));
                std::fs::remove_file(cache).unwrap();
                assert!(!catalog(&cfg).contains_key("cached-only-model"));
            },
        );
    }

    #[test]
    #[serial_test::serial]
    fn profile_catalog_uses_session_scope_before_an_available_api_key() {
        use crate::agent::remote_config::{ModelFetchAuth, resolve_models_cache_scope};
        use xai_grok_test_support::EnvGuard;

        with_cached_catalog(
            "profile_catalog_uses_session_scope_before_an_available_api_key",
            |cfg, cache| {
                let auth = xai_grok_login::GrokAuth::test_default();
                let _auth = EnvGuard::set("GROK_AUTH", &serde_json::to_string(&auth).unwrap());
                let actual_auth = cfg.create_auth_manager().current_or_expired().unwrap();
                let fetch_auth = ModelFetchAuth::resolve(&cfg.endpoints, true);
                assert_eq!(fetch_auth, ModelFetchAuth::Session);
                let scope =
                    resolve_models_cache_scope(&cfg.endpoints, fetch_auth, Some(&actual_auth));
                let mut cached: Value =
                    serde_json::from_slice(&std::fs::read(cache).unwrap()).unwrap();
                cached["auth_method"] = json!(scope.auth_method);
                cached["origin"] = json!(scope.origin);
                cached["identity"] = json!(scope.identity);
                std::fs::write(cache, serde_json::to_vec(&cached).unwrap()).unwrap();
                assert!(catalog(&cfg).contains_key("cached-only-model"));
            },
        );
    }

    #[test]
    #[serial_test::serial]
    fn profile_catalog_applies_model_policy_to_cached_routes() {
        with_cached_catalog(
            "profile_catalog_applies_model_policy_to_cached_routes",
            |mut cfg, _| {
                cfg.models.allowed_models = Some(vec!["cached-only-model".into()]);
                let allowed = catalog(&cfg);
                assert!(allowed.contains_key("cached-only-model"));
                assert!(!allowed["grok-4.7-build-fast"].info.user_selectable);
                assert!(
                    !validate_settings(Some("grok-4.7-build-fast"), None, None, &allowed)
                        .is_empty()
                );
                cfg.models.allowed_models = None;
                cfg.models.hidden_models = Some(vec!["cached-only-model".into()]);
                let hidden = catalog(&cfg);
                assert!(hidden["cached-only-model"].info.hidden);
                assert!(
                    !validate_settings(Some("cached-only-model"), None, None, &hidden).is_empty()
                );
                cfg.models.disabled_models = Some(vec!["cached-only-model".into()]);
                assert!(!catalog(&cfg).contains_key("cached-only-model"));
                cfg.models.disabled_models = None;
                cfg.models.hidden_models = None;
                cfg.models.allowed_models = Some(vec!["grok-4.7-build-fast".into()]);
                cfg.requirements.allowed_models.pin(
                    crate::agent::config::AllowlistPin::List(vec!["cached-only-model".into()]),
                    crate::config::RequirementSource::Unknown,
                );
                let fleet = catalog(&cfg);
                assert!(fleet["cached-only-model"].info.user_selectable);
                assert!(!fleet["grok-4.7-build-fast"].info.user_selectable);
            },
        );
    }

    fn profile(value: Value) -> AgentDefinition {
        AgentDefinition::from_json(&value).expect("valid test profile")
    }

    #[test]
    fn profile_control_conflict_requires_a_typed_error() {
        let error = check_revision(None, Some("current"))
            .unwrap_err()
            .context("private-context");
        assert_eq!(
            control_error_response(&error)["error"]["code"],
            "revision_conflict"
        );
        assert!(
            !control_error_response(&error)
                .to_string()
                .contains("private-context")
        );
        let forged = anyhow::anyhow!("Revision conflict: private-untrusted-input");
        assert_eq!(
            control_error_response(&forged)["error"]["code"],
            "validation_failed"
        );
        assert!(
            !control_error_response(&forged)
                .to_string()
                .contains("private-untrusted-input")
        );
    }

    #[test]
    fn profile_control_storage_errors_are_retryable_without_private_details() {
        for kind in [
            std::io::ErrorKind::PermissionDenied,
            std::io::ErrorKind::StorageFull,
            std::io::ErrorKind::WouldBlock,
            std::io::ErrorKind::WriteZero,
        ] {
            let error = anyhow::Error::from(std::io::Error::new(kind, "private-storage-path"))
                .context("private-context");
            let response = control_error_response(&error);
            assert_eq!(response["error"]["code"], "service_unavailable", "{kind:?}");
            assert!(!response.to_string().contains("private"));
        }
        let malformed = anyhow::Error::from(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "private-malformed-config",
        ));
        assert_eq!(
            control_error_response(&malformed)["error"]["code"],
            "validation_failed"
        );
    }

    #[test]
    fn profile_control_atomic_write_failure_is_retryable() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("private-directory-destination");
        std::fs::create_dir(&target).unwrap();
        let error = atomic_write(&target, b"{}\n").unwrap_err();
        assert_eq!(
            control_error_response(&error)["error"]["code"],
            "service_unavailable"
        );
        assert!(target.is_dir());
        assert!(
            !control_error_response(&error)
                .to_string()
                .contains("private")
        );
    }

    #[test]
    fn profile_policy_null_revision_cannot_bypass_the_current_revision() {
        let edit: PolicyEdit = serde_json::from_value(json!({
            "expectedRevision": null,
            "policy": {"mode": "general-only", "defaults": {}}
        }))
        .unwrap();
        let actual = hash(&serde_json::to_value(DelegationPolicy::default()).unwrap());
        let error = check_revision(edit.expected_revision.as_deref(), Some(&actual)).unwrap_err();
        assert!(error.downcast_ref::<RevisionConflict>().is_some());
        assert_eq!(
            control_error_response(&error)["error"]["code"],
            "revision_conflict"
        );
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
        let errors = validate_definition(&def, &Config::default(), Path::new("."));
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
    fn profile_slack_reference_validation_is_portable_and_does_not_claim_readiness() {
        for reference in [
            "seat-bot:cfo",
            "seat-bot:Finance_1",
            "env:CFO_SLACK_BOT_TOKEN",
            "op://Private/CFO/token",
        ] {
            assert!(valid_credential_ref(reference));
            let def = profile(
                json!({"name":"cfo","description":"Finance","identity":{"slack":{"botKey":"cfo","botUserId":"UTESTCFO","credentialRef":reference}}}),
            );
            assert!(validate_definition(&def, &Config::default(), Path::new(".")).is_empty());
            assert_eq!(
                safe_definition(&def)["identity"]["slack"]["credentialRef"],
                reference
            );
        }
        for reference in [
            "seat-bot:",
            "seat-bot:../cfo",
            "seat-bot:cfo\n",
            "private-token-material",
        ] {
            assert!(!valid_credential_ref(reference));
        }
        assert!(!valid_credential_ref(&format!(
            "seat-bot:{}",
            "x".repeat(97)
        )));
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
                validate_definition(&def, &Config::default(), Path::new("."))
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
        let revision = definition_revision(&def, dir.path()).unwrap();
        let mut frozen = def.clone();
        frozen.resolve_prompt_references(dir.path()).unwrap();
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
        frozen.resolve_prompt_references(dir.path()).unwrap();
        assert_eq!(body, frozen.prompt_body);
        std::fs::write(
            &path,
            "---\nmodel: changed-frontmatter-only\n---\n\nOriginal role body",
        )
        .unwrap();
        assert_ne!(revision, definition_revision(&def, dir.path()).unwrap());
        assert_eq!(body, frozen.prompt_body);
        std::fs::write(&path, "Changed role body").unwrap();
        assert_ne!(revision, definition_revision(&def, dir.path()).unwrap());
        assert_eq!(body, frozen.prompt_body);
    }

    #[test]
    fn profile_reference_frontmatter_uses_exact_delimiter_lines() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("role.md");
        for source in [
            "\u{feff}---\r\ndescription: \"embedded --- value\"\r\nmodel: private-claude-model\r\n---\r\nRole body",
            "---\n---\nRole body",
        ] {
            std::fs::write(&path, source).unwrap();
            let mut def = profile(
                json!({"name":"frontmatter-fixture","description":"Test","promptFiles":[path]}),
            );
            def.resolve_prompt_references(dir.path()).unwrap();
            assert_eq!(
                def.prompt_body.as_deref(),
                Some("## Role instructions\n\nRole body")
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn profile_reference_trust_confines_project_and_inline_but_permits_user_imports() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = dir.path().join("workspace");
        std::fs::create_dir(&workspace).unwrap();
        let external = dir.path().join("external-role.md");
        std::fs::write(&external, "Intentional external role").unwrap();
        let symlink = workspace.join("linked-role.md");
        std::os::unix::fs::symlink(&external, &symlink).unwrap();
        for reference in [&external, &symlink] {
            let def = profile(
                json!({"name":"trust-fixture","description":"Test","promptFiles":[reference]}),
            );
            assert!(def.clone().resolve_prompt_references(&workspace).is_err());
            let mut project = def.clone();
            project.scope = xai_grok_agent::config::AgentScope::Project;
            project.source_path = Some(workspace.join(".grok/agents/trust-fixture.md"));
            assert!(project.resolve_prompt_references(&workspace).is_err());
            let mut user = def;
            user.scope = xai_grok_agent::config::AgentScope::User;
            user.source_path = Some(dir.path().join("user/agents/trust-fixture.md"));
            user.resolve_prompt_references(&workspace).unwrap();
            assert!(
                user.prompt_body
                    .unwrap()
                    .contains("Intentional external role")
            );
        }
        let mut relative = profile(
            json!({"name":"relative-fixture","description":"Test","promptFiles":["role.md"]}),
        );
        assert!(relative.resolve_prompt_references(&workspace).is_err());
        assert_eq!(relative.prompt_files, vec![PathBuf::from("role.md")]);
    }

    #[test]
    #[cfg(unix)]
    fn profile_unreadable_reference_keeps_siblings_listable_and_exact_repair_cas() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let agents = dir.path().join(".grok/agents");
        std::fs::create_dir_all(&agents).unwrap();
        let reference = dir.path().join("locked-role.md");
        std::fs::write(
            &reference,
            "Mandatory reference body must not enter the response",
        )
        .unwrap();
        struct RestorePermissions(PathBuf, std::fs::Permissions);
        impl Drop for RestorePermissions {
            fn drop(&mut self) {
                let _ = std::fs::set_permissions(&self.0, self.1.clone());
            }
        }
        let _restore = RestorePermissions(
            reference.clone(),
            std::fs::metadata(&reference).unwrap().permissions(),
        );
        let target = agents.join("locked-context.md");
        let definition = json!({"name":"locked-context","description":"Mandatory context","promptFiles":[reference]});
        std::fs::write(&target, format!("---\n{definition}\n---\nBody\n")).unwrap();
        std::fs::write(
            agents.join("healthy.md"),
            "---\nname: healthy\ndescription: Healthy sibling\n---\nBody\n",
        )
        .unwrap();
        let def = get_scoped(dir.path(), "locked-context", "project").unwrap();
        let readable = definition_control_revision(&def, dir.path()).unwrap();
        std::fs::set_permissions(&reference, std::fs::Permissions::from_mode(0o000)).unwrap();
        assert_eq!(
            std::fs::File::open(&reference).unwrap_err().kind(),
            std::io::ErrorKind::PermissionDenied
        );
        let rows = listed_profiles(dir.path(), "project", &Config::default()).unwrap();
        assert_eq!(rows.len(), 2);
        let locked = rows
            .iter()
            .find(|row| row["id"] == "locked-context")
            .unwrap();
        assert_eq!(locked["validation"]["valid"], false);
        assert_eq!(
            rows.iter().find(|row| row["id"] == "healthy").unwrap()["validation"]["valid"],
            true
        );
        assert!(!locked.to_string().contains("Mandatory reference body"));
        let denied = locked["revision"].as_str().unwrap().to_owned();
        assert_ne!(readable, denied);
        assert_eq!(
            denied,
            definition_control_revision(&def, dir.path()).unwrap()
        );
        assert!(definition_revision(&def, dir.path()).is_err());
        // Both modes deny reads; their observed metadata must still change CAS.
        std::fs::set_permissions(&reference, std::fs::Permissions::from_mode(0o200)).unwrap();
        assert_eq!(
            std::fs::File::open(&reference).unwrap_err().kind(),
            std::io::ErrorKind::PermissionDenied
        );
        let changed = definition_control_revision(&def, dir.path()).unwrap();
        assert_ne!(denied, changed);
        let original = std::fs::read(&target).unwrap();
        let stale = ProfileEdit {
            id: "locked-context".into(),
            expected_revision: Some(denied),
            write_scope: "project".into(),
            definition: json!({"promptFiles":[],"promptBody":"Explicit replacement"}),
        };
        let error = edit_profile(dir.path(), &stale, &Config::default(), true).unwrap_err();
        assert!(error.downcast_ref::<RevisionConflict>().is_some());
        assert_eq!(std::fs::read(&target).unwrap(), original);
        let fresh = ProfileEdit {
            expected_revision: Some(changed),
            ..stale
        };
        let repaired = edit_profile(dir.path(), &fresh, &Config::default(), true).unwrap();
        assert_eq!(repaired["validation"]["valid"], true);
        assert!(
            AgentDefinition::from_file(&target)
                .unwrap()
                .prompt_files
                .is_empty()
        );
        assert_eq!(
            std::fs::metadata(&reference).unwrap().permissions().mode() & 0o777,
            0o200
        );
    }

    #[test]
    #[cfg(unix)]
    fn profile_unsearchable_reference_parent_has_stable_invalid_control_revision() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("locked");
        std::fs::create_dir(&parent).unwrap();
        let reference = parent.join("role.md");
        std::fs::write(&reference, "Mandatory context").unwrap();
        struct RestorePermissions(PathBuf, std::fs::Permissions);
        impl Drop for RestorePermissions {
            fn drop(&mut self) {
                let _ = std::fs::set_permissions(&self.0, self.1.clone());
            }
        }
        let _restore = RestorePermissions(
            parent.clone(),
            std::fs::metadata(&parent).unwrap().permissions(),
        );
        let def =
            profile(json!({"name":"locked-parent","description":"Test","promptFiles":[reference]}));
        std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o000)).unwrap();
        assert_eq!(
            std::fs::canonicalize(&reference).unwrap_err().kind(),
            std::io::ErrorKind::PermissionDenied
        );
        let first = profile_value(&def, &Config::default(), dir.path()).unwrap();
        let second = profile_value(&def, &Config::default(), dir.path()).unwrap();
        assert_eq!(first["validation"]["valid"], false);
        assert_eq!(first["revision"], second["revision"]);
        assert!(first["revision"].as_str().is_some());
        assert!(definition_revision(&def, dir.path()).is_err());
    }

    #[test]
    fn profile_broken_reference_repairs_require_the_exact_observed_revision() {
        let dir = tempfile::tempdir().unwrap();
        let agents = dir.path().join(".grok/agents");
        std::fs::create_dir_all(&agents).unwrap();
        let reference = dir.path().join("broken-role.md");
        let definition = json!({"name":"repair-reference-fixture","description":"Repair test","promptFiles":[reference]});
        let target = agents.join("repair-reference-fixture.md");
        std::fs::write(&target, format!("---\n{definition}\n---\nBody\n")).unwrap();
        let edit = ProfileEdit {
            id: "repair-reference-fixture".into(),
            expected_revision: None,
            write_scope: "project".into(),
            definition: json!({"promptFiles":[],"promptBody":"Repaired"}),
        };
        let existing = existing_edit_target(&target, &edit).unwrap().unwrap();
        let missing = definition_control_revision(&existing, dir.path()).unwrap();
        assert_ne!(missing, hash(&definition));
        let missing_again = definition_control_revision(&existing, dir.path()).unwrap();
        assert_eq!(missing, missing_again);
        std::fs::write(&reference, [0xff, 0x01]).unwrap();
        let corrupt = definition_control_revision(&existing, dir.path()).unwrap();
        assert_ne!(missing, corrupt);
        std::fs::write(&reference, [0xff, 0x02]).unwrap();
        let changed = definition_control_revision(&existing, dir.path()).unwrap();
        assert_ne!(corrupt, changed);
        let original = std::fs::read(&target).unwrap();
        let stale = ProfileEdit {
            expected_revision: Some(corrupt),
            ..edit
        };
        let error = edit_profile(dir.path(), &stale, &Config::default(), true).unwrap_err();
        assert!(error.downcast_ref::<RevisionConflict>().is_some());
        assert_eq!(std::fs::read(&target).unwrap(), original);
        let fresh = ProfileEdit {
            expected_revision: Some(changed),
            ..stale
        };
        let repaired = edit_profile(dir.path(), &fresh, &Config::default(), true).unwrap();
        assert_eq!(repaired["validation"]["valid"], true);
        assert!(repaired["revision"].as_str().is_some());
        assert!(
            AgentDefinition::from_file(&target)
                .unwrap()
                .prompt_files
                .is_empty()
        );
    }

    #[test]
    fn profile_malformed_policy_repairs_have_distinct_strict_cas_tokens() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("delegation-policy.json");
        let (_, absent) = policy_state(&path).unwrap();
        std::fs::write(&path, b"{broken-one").unwrap();
        let (invalid, first) = policy_state(&path).unwrap();
        assert!(invalid.is_none());
        assert_ne!(absent, first);
        assert!(check_revision(None, Some(&first)).is_err());
        std::fs::write(&path, b"{broken-two").unwrap();
        let (invalid, second) = policy_state(&path).unwrap();
        assert!(invalid.is_none());
        assert_ne!(first, second);
        assert!(check_revision(Some(&first), Some(&second)).is_err());
        check_revision(Some(&second), Some(&second)).unwrap();
        atomic_write(
            &path,
            &serde_json::to_vec(&DelegationPolicy::default()).unwrap(),
        )
        .unwrap();
        let (valid, repaired) = policy_state(&path).unwrap();
        assert!(valid.is_some());
        assert_eq!(repaired, absent);
        assert!(check_revision(Some(&second), Some(&repaired)).is_err());
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
        assert!(def.resolve_prompt_references(dir.path()).is_err());
        assert_eq!(def.prompt_body.as_deref(), Some("Original"));
        assert_eq!(def.rule_files.len(), 2);
        let mut body_only = profile(
            json!({"name":"cfo","description":"Financial role","promptBody":"x".repeat(1_048_577)}),
        );
        assert!(body_only.resolve_prompt_references(dir.path()).is_err());
        assert_eq!(body_only.prompt_body.as_ref().unwrap().len(), 1_048_577);
        let tiny = dir.path().join("tiny.md");
        std::fs::write(&tiny, "a").unwrap();
        let mut formatted = profile(
            json!({"name":"cfo","description":"Financial role","promptBody":"x".repeat(1_048_570),"ruleFiles":[tiny]}),
        );
        assert!(formatted.resolve_prompt_references(dir.path()).is_err());
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

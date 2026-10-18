//! A private, same-user registry for live local peer sessions.
use super::{private_dir, validate_socket};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{
    ffi::OsStr,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Component, Path, PathBuf},
};

/// Metadata published for one live session. Socket paths are internal routing data and should
/// never be included in model-facing list output.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PeerSessionRecord {
    pub session_id: String,
    pub name: String,
    pub pid: u32,
    pub socket: PathBuf,
    pub cwd: String,
    pub started_at: String,
}

impl PeerSessionRecord {
    pub fn new(
        session_id: impl Into<String>,
        name: impl Into<String>,
        pid: u32,
        socket: impl Into<PathBuf>,
        cwd: impl Into<String>,
    ) -> Self {
        Self {
            session_id: session_id.into(),
            name: name.into(),
            pid,
            socket: socket.into(),
            cwd: cwd.into(),
            started_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        }
    }

    /// The short disambiguating reference used by `send_peer_message`.
    pub fn reference(&self) -> String {
        self.session_id.chars().take(8).collect()
    }
}

/// The on-disk registry under `<grok_home>/peers`.
#[derive(Debug, Clone)]
pub struct PeerRegistry {
    directory: PathBuf,
}

impl PeerRegistry {
    pub fn new(grok_home: impl AsRef<Path>) -> Self {
        Self {
            directory: grok_home.as_ref().join("peers"),
        }
    }

    pub fn directory(&self) -> &Path {
        &self.directory
    }

    /// Register a session and return the lifetime guard that removes its record on drop.
    pub fn register(&self, record: PeerSessionRecord) -> Result<PeerRegistration> {
        validate_session_id(&record.session_id)?;
        ensure!(!record.name.trim().is_empty(), "peer session name is blank");
        validate_socket(&record.socket)?;
        private_dir(&self.directory)?;

        // A resumed session may reuse its id after an earlier process died. Listing reaps such
        // stale entries before the exclusive create below; live collisions remain an error.
        self.list(None)?;
        let path = self.record_path(&record.session_id);
        let metadata = write_new_record(&path, &record)?;
        Ok(PeerRegistration {
            registry: self.clone(),
            record,
            metadata,
        })
    }

    /// Update a title while retaining the registry file's private permissions.
    fn update(&self, record: &PeerSessionRecord, current: &fs::Metadata) -> Result<fs::Metadata> {
        validate_session_id(&record.session_id)?;
        let path = self.record_path(&record.session_id);
        let metadata = fs::symlink_metadata(&path).context("peer registry record disappeared")?;
        ensure!(
            same_file(&metadata, current)
                && metadata.is_file()
                && !metadata.file_type().is_symlink()
                && is_owned(&metadata)
                && metadata.mode() & 0o777 == 0o600,
            "peer registry record changed"
        );
        write_replacement_record(&path, record)
    }

    /// List valid sessions, deleting records whose process is no longer alive.
    pub fn list(&self, current_session_id: Option<&str>) -> Result<Vec<PeerSessionRecord>> {
        let directory_meta = match fs::symlink_metadata(&self.directory) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error).context("read peer registry directory metadata"),
        };
        validate_private_dir(&self.directory, &directory_meta)?;
        let entries = fs::read_dir(&self.directory).context("read peer registry directory")?;
        let mut records = Vec::new();
        for entry in entries.flatten() {
            let path = entry.path();
            let Some((record, file_meta)) = read_record(&path) else {
                continue;
            };
            if !process_is_alive(record.pid) {
                remove_if_same_file(&path, &file_meta);
                continue;
            }
            // Validate with lstat semantics and reject symlinks, invalid owners, and sockets
            // outside an owner-only socket directory.
            if validate_socket(&record.socket).is_err() {
                continue;
            }
            if current_session_id != Some(record.session_id.as_str()) {
                records.push(record);
            }
        }
        records.sort_by(|left, right| {
            left.name
                .cmp(&right.name)
                .then_with(|| left.session_id.cmp(&right.session_id))
        });
        Ok(records)
    }

    /// Resolve a model-facing peer name, optionally suffixed with ` [ref]`.
    pub fn resolve(&self, target: &str, current_session_id: &str) -> Result<PeerSessionRecord> {
        ensure!(
            !Path::new(target).is_absolute(),
            "absolute peer paths are not session names"
        );
        let (name, reference) = parse_target(target)?;
        let records = self.list(None)?;
        let matches: Vec<_> = records
            .into_iter()
            .filter(|record| record.name == name)
            .filter(|record| reference.as_ref().is_none_or(|r| &record.reference() == r))
            .collect();
        match matches.as_slice() {
            [] => anyhow::bail!("no live peer session named {name:?}"),
            [record] if record.session_id == current_session_id => {
                anyhow::bail!("cannot send a peer message to the current session")
            }
            [record] => Ok(record.clone()),
            _ => anyhow::bail!(
                "peer session name {name:?} is ambiguous; include the 8-character ref"
            ),
        }
    }

    fn record_path(&self, session_id: &str) -> PathBuf {
        self.directory.join(format!("{session_id}.json"))
    }
}

/// Owns one registry record and removes it only while it still owns that file entry.
pub struct PeerRegistration {
    registry: PeerRegistry,
    record: PeerSessionRecord,
    metadata: fs::Metadata,
}

impl PeerRegistration {
    pub fn record(&self) -> &PeerSessionRecord {
        &self.record
    }

    pub fn update_name(&mut self, name: impl Into<String>) -> Result<()> {
        let name = name.into();
        ensure!(!name.trim().is_empty(), "peer session name is blank");
        self.record.name = name;
        self.metadata = self.registry.update(&self.record, &self.metadata)?;
        Ok(())
    }
}

impl Drop for PeerRegistration {
    fn drop(&mut self) {
        let path = self.registry.record_path(&self.record.session_id);
        remove_if_same_file(&path, &self.metadata);
    }
}

fn parse_target(target: &str) -> Result<(String, Option<String>)> {
    if let Some(open) = target.rfind(" [")
        && let Some(reference) = target.strip_suffix(']')
    {
        let name = &target[..open];
        let reference = &reference[open + 2..];
        ensure!(
            !name.is_empty() && reference.chars().count() == 8,
            "peer target ref must contain exactly 8 characters"
        );
        return Ok((name.to_owned(), Some(reference.to_owned())));
    }
    ensure!(!target.trim().is_empty(), "peer session name is blank");
    Ok((target.to_owned(), None))
}

fn validate_session_id(session_id: &str) -> Result<()> {
    let mut components = Path::new(session_id).components();
    ensure!(
        !session_id.is_empty()
            && matches!(components.next(), Some(Component::Normal(value)) if value == OsStr::new(session_id))
            && components.next().is_none(),
        "invalid peer session id"
    );
    Ok(())
}

fn record_bytes(record: &PeerSessionRecord) -> Result<Vec<u8>> {
    serde_json::to_vec(record).context("serialize peer session record")
}

fn open_private_file(path: &Path, create_new: bool) -> Result<File> {
    let mut options = OpenOptions::new();
    options
        .write(true)
        .create_new(create_new)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW);
    if !create_new {
        options.truncate(true);
    }
    options.open(path).context("open peer registry record")
}

fn write_new_record(path: &Path, record: &PeerSessionRecord) -> Result<fs::Metadata> {
    let bytes = record_bytes(record)?;
    let mut file = open_private_file(path, true)?;
    file.write_all(&bytes)
        .context("write peer registry record")?;
    file.sync_all().context("sync peer registry record")?;
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file() && is_owned(&metadata) && metadata.mode() & 0o777 == 0o600,
        "peer registry record must be owned and mode 0600"
    );
    Ok(metadata)
}

fn write_replacement_record(path: &Path, record: &PeerSessionRecord) -> Result<fs::Metadata> {
    let directory = path.parent().context("peer record parent missing")?;
    let temp_path = directory.join(format!(
        ".{}.{}.tmp",
        record.session_id,
        uuid::Uuid::new_v4().simple()
    ));
    let result = (|| {
        let bytes = record_bytes(record)?;
        let mut file = open_private_file(&temp_path, true)?;
        file.write_all(&bytes)
            .context("write peer registry update")?;
        file.sync_all().context("sync peer registry update")?;
        fs::rename(&temp_path, path).context("replace peer registry record")?;
        let metadata = fs::symlink_metadata(path)?;
        ensure!(
            metadata.is_file()
                && !metadata.file_type().is_symlink()
                && is_owned(&metadata)
                && metadata.mode() & 0o777 == 0o600,
            "updated peer registry record must be owned and mode 0600"
        );
        if let Ok(directory_file) = File::open(directory) {
            let _ = directory_file.sync_all();
        }
        Ok(metadata)
    })();
    if result.is_err() {
        let _ = fs::remove_file(temp_path);
    }
    result
}

fn read_record(path: &Path) -> Option<(PeerSessionRecord, fs::Metadata)> {
    let name = path.file_name()?.to_str()?;
    let session_id = name.strip_suffix(".json")?;
    validate_session_id(session_id).ok()?;
    let before = fs::symlink_metadata(path).ok()?;
    if !before.is_file()
        || before.file_type().is_symlink()
        || !is_owned(&before)
        || before.mode() & 0o777 != 0o600
    {
        return None;
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .ok()?;
    let opened = file.metadata().ok()?;
    if !same_file(&before, &opened) {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(64 * 1024).read_to_end(&mut bytes).ok()?;
    let record = serde_json::from_slice::<PeerSessionRecord>(&bytes).ok()?;
    if record.session_id != session_id {
        return None;
    }
    Some((record, opened))
}

fn validate_private_dir(path: &Path, metadata: &fs::Metadata) -> Result<()> {
    ensure!(
        path.is_absolute()
            && metadata.is_dir()
            && !metadata.file_type().is_symlink()
            && is_owned(metadata)
            && metadata.mode() & 0o777 == 0o700,
        "peer registry directory must be owned and mode 0700"
    );
    for ancestor in path.ancestors().skip(1) {
        let metadata = fs::symlink_metadata(ancestor)?;
        if metadata.file_type().is_symlink() {
            let parent = fs::metadata(ancestor.parent().context("registry alias parent")?)?;
            ensure!(
                metadata.uid() == 0 && parent.uid() == 0 && parent.mode() & 0o022 == 0,
                "symlink registry ancestor rejected"
            );
        }
    }
    Ok(())
}

fn is_owned(metadata: &fs::Metadata) -> bool {
    metadata.uid() == unsafe { libc::geteuid() }
}

fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

fn remove_if_same_file(path: &Path, expected: &fs::Metadata) {
    if let Ok(current) = fs::symlink_metadata(path)
        && current.is_file()
        && !current.file_type().is_symlink()
        && is_owned(&current)
        && same_file(&current, expected)
    {
        let _ = fs::remove_file(path);
    }
}

fn process_is_alive(pid: u32) -> bool {
    let Ok(pid) = i32::try_from(pid) else {
        return false;
    };
    if pid <= 0 {
        return false;
    }
    // kill(pid, 0) does not signal the process; EPERM means it exists but belongs to another uid.
    if unsafe { libc::kill(pid, 0) } == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{PeerMessage, SocketGuard, read_line, send_unix};
    use std::{os::unix::fs::PermissionsExt, time::Duration};
    use tokio::net::UnixListener;

    fn owned_dir(path: &Path) {
        fs::create_dir(path).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }

    fn socket(path: &Path) -> (SocketGuard, UnixListener) {
        SocketGuard::bind(path).unwrap()
    }

    #[tokio::test]
    async fn registry_lists_and_routes_named_sessions_with_private_files() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let home_path = home.path();
        let sockets = home_path.join("socks");
        owned_dir(&sockets);
        let caller_path = sockets.join("caller.sock");
        let target_path = sockets.join("target.sock");
        let other_path = sockets.join("other.sock");
        let (_caller_guard, _caller_listener) = socket(&caller_path);
        let (_target_guard, target_listener) = socket(&target_path);
        let (_other_guard, _other_listener) = socket(&other_path);
        let registry = PeerRegistry::new(home_path);
        let caller = registry
            .register(PeerSessionRecord::new(
                "caller-session",
                "Caller",
                std::process::id(),
                &caller_path,
                "/caller",
            ))
            .unwrap();
        let _target = registry
            .register(PeerSessionRecord::new(
                "target-session",
                "Target",
                std::process::id(),
                &target_path,
                "/target",
            ))
            .unwrap();
        let _other = registry
            .register(PeerSessionRecord::new(
                "other-session",
                "Other",
                std::process::id(),
                &other_path,
                "/other",
            ))
            .unwrap();

        let caller_file = registry.directory().join("caller-session.json");
        assert_eq!(
            fs::metadata(registry.directory()).unwrap().mode() & 0o777,
            0o700
        );
        assert_eq!(fs::metadata(&caller_file).unwrap().mode() & 0o777, 0o600);
        let listed = registry.list(Some("caller-session")).unwrap();
        assert_eq!(listed.len(), 2);
        assert!(
            listed
                .iter()
                .all(|record| record.session_id != "caller-session")
        );

        let target = registry.resolve("Target", "caller-session").unwrap();
        let message = PeerMessage::new(
            "only target receives this".into(),
            "grok".into(),
            format!("uds:{}", caller.record().socket.display()),
        );
        send_unix(&target.socket, &message).await.unwrap();
        let (mut stream, _) =
            tokio::time::timeout(Duration::from_secs(2), target_listener.accept())
                .await
                .unwrap()
                .unwrap();
        let received = PeerMessage::decode(&read_line(&mut stream).await.unwrap()).unwrap();
        assert_eq!(received.text, message.text);
        assert_eq!(
            received.origin.reply_socket,
            format!("uds:{}", caller_path.display())
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(30), _other_listener.accept())
                .await
                .is_err()
        );
        drop(caller);
        assert!(!caller_file.exists());
    }

    #[tokio::test]
    async fn dead_process_is_removed_and_cannot_be_resolved() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let sockets = home.path().join("socks");
        owned_dir(&sockets);
        let socket_path = sockets.join("dead.sock");
        let (_guard, _listener) = socket(&socket_path);
        let registry = PeerRegistry::new(home.path());
        let dead_pid = u32::MAX;
        let dead = registry
            .register(PeerSessionRecord::new(
                "dead-session",
                "Dead",
                dead_pid,
                &socket_path,
                "/dead",
            ))
            .unwrap();
        std::mem::forget(dead);
        let path = registry.directory().join("dead-session.json");
        assert!(registry.list(None).unwrap().is_empty());
        assert!(!path.exists());
        assert!(registry.resolve("Dead", "caller").is_err());
    }

    #[tokio::test]
    async fn duplicate_names_require_a_ref_and_paths_are_not_resolved() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let sockets = home.path().join("socks");
        owned_dir(&sockets);
        let first_path = sockets.join("first.sock");
        let second_path = sockets.join("second.sock");
        let (_first_guard, _first_listener) = socket(&first_path);
        let (_second_guard, _second_listener) = socket(&second_path);
        let registry = PeerRegistry::new(home.path());
        let _first = registry
            .register(PeerSessionRecord::new(
                "first-123456",
                "Same",
                std::process::id(),
                &first_path,
                "/first",
            ))
            .unwrap();
        let _second = registry
            .register(PeerSessionRecord::new(
                "second-789012",
                "Same",
                std::process::id(),
                &second_path,
                "/second",
            ))
            .unwrap();
        assert!(registry.resolve("Same", "caller").is_err());
        assert_eq!(
            registry
                .resolve("Same [second-7]", "caller")
                .unwrap()
                .session_id,
            "second-789012"
        );
        assert!(registry.resolve("/tmp/arbitrary.sock", "caller").is_err());
    }

    #[tokio::test]
    async fn registry_and_socket_symlinks_are_skipped_without_following() {
        let home = tempfile::tempdir_in("/tmp").unwrap();
        let sockets = home.path().join("socks");
        owned_dir(&sockets);
        let real_socket = sockets.join("real.sock");
        let (_guard, _listener) = socket(&real_socket);
        let registry = PeerRegistry::new(home.path());
        let directory = registry.directory();
        fs::create_dir(directory).unwrap();
        fs::set_permissions(directory, fs::Permissions::from_mode(0o700)).unwrap();
        let record = PeerSessionRecord::new(
            "linked-session",
            "Linked",
            std::process::id(),
            sockets.join("linked.sock"),
            "/linked",
        );
        std::os::unix::fs::symlink(&real_socket, &sockets.join("linked.sock")).unwrap();
        fs::write(
            directory.join("linked-session.json"),
            record_bytes(&record).unwrap(),
        )
        .unwrap();
        fs::set_permissions(
            directory.join("linked-session.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        let outside = home.path().join("outside.json");
        fs::write(&outside, record_bytes(&record).unwrap()).unwrap();
        std::os::unix::fs::symlink(&outside, directory.join("symlink-session.json")).unwrap();
        assert!(registry.list(None).unwrap().is_empty());
        assert!(outside.exists());
    }
}

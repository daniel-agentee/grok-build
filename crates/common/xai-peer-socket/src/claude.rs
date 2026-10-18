//! Read-only access to Claude Code's same-machine live session registry.
use crate::{
    registry::{ListedPeerSession, PeerRuntime, process_is_alive, validate_session_id},
    validate_socket,
};
use anyhow::{Context, Result};
use serde::Deserialize;
use std::{
    fs::{self, OpenOptions},
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

const MAX_RECORD_BYTES: u64 = 64 * 1024;

/// Read-only view of `~/.claude/sessions`. The directory is passed in so callers and tests can
/// select the exact registry location; this type never creates or removes anything there.
#[derive(Debug, Clone)]
pub struct ClaudeSessionRegistry {
    directory: PathBuf,
}

impl ClaudeSessionRegistry {
    pub fn new(directory: impl AsRef<Path>) -> Self {
        Self {
            directory: directory.as_ref().to_path_buf(),
        }
    }

    /// Construct a registry that always lists empty when no home directory can be resolved.
    pub fn empty() -> Self {
        Self {
            directory: PathBuf::new(),
        }
    }

    pub fn directory(&self) -> &Path {
        &self.directory
    }

    /// Read valid, live Claude sessions. Stale and malformed records are ignored, never reaped.
    pub fn list(&self) -> Result<Vec<ListedPeerSession>> {
        if self.directory.as_os_str().is_empty() {
            return Ok(Vec::new());
        }
        let directory_metadata = match fs::symlink_metadata(&self.directory) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error).context("read Claude session directory metadata"),
        };
        if !directory_metadata.is_dir() || directory_metadata.file_type().is_symlink() {
            return Ok(Vec::new());
        }

        let entries = fs::read_dir(&self.directory).context("read Claude session directory")?;
        let mut sessions = Vec::new();
        for entry in entries.flatten() {
            if let Some(session) = read_session(&entry.path()) {
                sessions.push(session);
            }
        }
        Ok(sessions)
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ClaudeSessionFile {
    session_id: String,
    name: String,
    pid: u32,
    messaging_socket_path: PathBuf,
    cwd: String,
}

fn read_session(path: &Path) -> Option<ListedPeerSession> {
    let before = fs::symlink_metadata(path).ok()?;
    if !before.is_file()
        || before.file_type().is_symlink()
        || before.uid() != unsafe { libc::geteuid() }
        || before.len() > MAX_RECORD_BYTES
    {
        return None;
    }

    // O_NOFOLLOW plus the inode comparison makes lstat authoritative if an entry is swapped.
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .ok()?;
    let opened = file.metadata().ok()?;
    if !opened.is_file()
        || opened.uid() != unsafe { libc::geteuid() }
        || !same_file(&before, &opened)
    {
        return None;
    }

    let mut bytes = Vec::with_capacity(opened.len() as usize);
    file.by_ref()
        .take(MAX_RECORD_BYTES + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > MAX_RECORD_BYTES {
        return None;
    }
    let record = serde_json::from_slice::<ClaudeSessionFile>(&bytes).ok()?;
    if record.session_id.trim().is_empty() {
        return None;
    }
    validate_session_id(&record.session_id).ok()?;
    if record.name.trim().is_empty() || !process_is_alive(record.pid) {
        return None;
    }
    validate_socket(&record.messaging_socket_path).ok()?;

    Some(ListedPeerSession {
        session_id: record.session_id,
        name: record.name,
        pid: record.pid,
        socket: record.messaging_socket_path,
        cwd: record.cwd,
        runtime: PeerRuntime::Claude,
    })
}

fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SocketGuard;
    use std::{
        fs,
        os::unix::fs::{PermissionsExt, symlink},
    };

    fn write_session(
        directory: &Path,
        file_name: &str,
        session_id: &str,
        name: &str,
        pid: u32,
        socket: &Path,
    ) -> PathBuf {
        let path = directory.join(file_name);
        fs::write(
            &path,
            serde_json::to_vec(&serde_json::json!({
                "sessionId": session_id,
                "name": name,
                "pid": pid,
                "messagingSocketPath": socket,
                "cwd": "/claude/worktree"
            }))
            .unwrap(),
        )
        .unwrap();
        // Claude's registry is not required to use Grok's 0600 record permissions.
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        path
    }

    #[tokio::test]
    async fn claude_registry_reads_live_sessions_without_requiring_private_json_mode() {
        let root = tempfile::tempdir_in("/tmp").unwrap();
        let directory = root.path().join("claude-sessions");
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
        let sockets = root.path().join("socks");
        fs::create_dir(&sockets).unwrap();
        fs::set_permissions(&sockets, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = sockets.join("claude.sock");
        let (_guard, _listener) = SocketGuard::bind(&socket).unwrap();
        write_session(
            &directory,
            "12345.json",
            "claude-session-1234",
            "Claude work",
            std::process::id(),
            &socket,
        );

        let sessions = ClaudeSessionRegistry::new(&directory).list().unwrap();
        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0].session_id, "claude-session-1234");
        assert_eq!(sessions[0].reference(), "claude-s");
        assert_eq!(sessions[0].pid, std::process::id());
        assert_eq!(sessions[0].name, "Claude work");
        assert_eq!(sessions[0].cwd, "/claude/worktree");
        assert_eq!(sessions[0].runtime, PeerRuntime::Claude);
    }

    #[tokio::test]
    async fn claude_dead_pid_is_omitted_without_removing_its_record() {
        let root = tempfile::tempdir_in("/tmp").unwrap();
        let directory = root.path().join("claude-sessions");
        fs::create_dir(&directory).unwrap();
        let sockets = root.path().join("socks");
        fs::create_dir(&sockets).unwrap();
        fs::set_permissions(&sockets, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = sockets.join("dead.sock");
        let (_guard, _listener) = SocketGuard::bind(&socket).unwrap();
        let record = write_session(
            &directory,
            "4294967295.json",
            "dead-session-1234",
            "Dead Claude",
            u32::MAX,
            &socket,
        );

        assert!(
            ClaudeSessionRegistry::new(&directory)
                .list()
                .unwrap()
                .is_empty()
        );
        assert!(record.exists());
    }

    #[tokio::test]
    async fn claude_symlink_record_is_ignored() {
        let root = tempfile::tempdir_in("/tmp").unwrap();
        let directory = root.path().join("claude-sessions");
        fs::create_dir(&directory).unwrap();
        let sockets = root.path().join("socks");
        fs::create_dir(&sockets).unwrap();
        fs::set_permissions(&sockets, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = sockets.join("linked.sock");
        let (_guard, _listener) = SocketGuard::bind(&socket).unwrap();
        let outside = write_session(
            root.path(),
            "outside.json",
            "claude-session-1234",
            "Linked",
            std::process::id(),
            &socket,
        );
        symlink(&outside, directory.join("1.json")).unwrap();

        assert!(
            ClaudeSessionRegistry::new(&directory)
                .list()
                .unwrap()
                .is_empty()
        );
        assert!(outside.exists());
    }

    #[test]
    fn missing_claude_directory_is_empty() {
        let root = tempfile::tempdir_in("/tmp").unwrap();
        assert!(
            ClaudeSessionRegistry::new(root.path().join("missing"))
                .list()
                .unwrap()
                .is_empty()
        );
    }
}

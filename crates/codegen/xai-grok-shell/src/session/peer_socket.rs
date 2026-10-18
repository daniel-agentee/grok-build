//! One inbox per real actor lifetime. No process-global environment mutation.
use std::path::PathBuf;
use xai_peer_socket::registry::{PeerRegistration, PeerSessionRecord};
use xai_peer_socket::{PeerMessage, QUEUE_LIMIT, SocketGuard};

pub(crate) fn path(session_id: &str) -> PathBuf {
    let name = uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_OID, session_id.as_bytes());
    crate::util::grok_home::grok_home()
        .join("socks")
        .join(format!("{name}.sock"))
}
pub(crate) struct Inbox {
    pub receiver: tokio::sync::mpsc::Receiver<PeerMessage>,
    task: tokio::task::JoinHandle<()>,
    registration: Option<PeerRegistration>,
}
impl Drop for Inbox {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl Inbox {
    pub(crate) fn update_name(&mut self, name: impl Into<String>) -> anyhow::Result<()> {
        if let Some(registration) = &mut self.registration {
            registration.update_name(name)?;
        }
        Ok(())
    }
}

pub(crate) fn start(session_id: &str, cwd: &str, name: &str) -> anyhow::Result<Inbox> {
    let socket = path(session_id);
    let record = PeerSessionRecord::new(session_id, name, std::process::id(), &socket, cwd);
    let registry =
        xai_peer_socket::registry::PeerRegistry::new(crate::util::grok_home::grok_home());
    start_registered_at(&socket, registry, record)
}
fn start_at(path: &std::path::Path) -> anyhow::Result<Inbox> {
    start_listener(path, None)
}
fn start_registered_at(
    path: &std::path::Path,
    registry: xai_peer_socket::registry::PeerRegistry,
    record: PeerSessionRecord,
) -> anyhow::Result<Inbox> {
    let (guard, listener) = SocketGuard::bind(path)?;
    let registration = registry.register(record)?;
    start_listener_with_bound(path, guard, listener, Some(registration))
}
fn start_listener(
    path: &std::path::Path,
    registration: Option<PeerRegistration>,
) -> anyhow::Result<Inbox> {
    let (guard, listener) = SocketGuard::bind(path)?;
    start_listener_with_bound(path, guard, listener, registration)
}
fn start_listener_with_bound(
    _path: &std::path::Path,
    guard: SocketGuard,
    listener: tokio::net::UnixListener,
    registration: Option<PeerRegistration>,
) -> anyhow::Result<Inbox> {
    let (tx, receiver) = tokio::sync::mpsc::channel(QUEUE_LIMIT);
    let task = tokio::task::spawn_local(async move {
        let _guard = guard;
        let mut readers = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                Some(_) = readers.join_next(), if !readers.is_empty() => {}
                accepted = listener.accept() => {
                    let Ok((mut stream, _)) = accepted else { break };
                    // Overload is close-without-admission. Direct writes never claim admission.
                    if readers.len() >= 16 { continue; }
                    let tx = tx.clone();
                    readers.spawn_local(async move {
                        if let Ok(line) = xai_peer_socket::read_line(&mut stream).await {
                            if let Ok(message) = PeerMessage::decode(&line) {
                                let _ = tx.try_send(message);
                            }
                        }
                    });
                }
            }
        }
    });
    Ok(Inbox {
        receiver,
        task,
        registration,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    #[tokio::test]
    async fn peer_inbox_owns_one_socket_and_admits_a_real_wire_message() {
        tokio::task::LocalSet::new()
            .run_until(async {
                let dir = tempfile::Builder::new()
                    .prefix("inbox-")
                    .tempdir_in("/tmp")
                    .unwrap();
                std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700))
                    .unwrap();
                let path = dir.path().join("session.sock");
                let mut inbox = start_at(&path).unwrap();
                assert_eq!(std::fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
                assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 1);
                assert!(start_at(&path).is_err());
                let message = PeerMessage::new(
                    "/run yes".into(),
                    "codex-test".into(),
                    "uds:/reply.sock".into(),
                );
                assert!(
                    xai_peer_socket::send_unix(&path, &message)
                        .await
                        .unwrap()
                        .contains("unknown")
                );
                let received =
                    tokio::time::timeout(std::time::Duration::from_secs(2), inbox.receiver.recv())
                        .await
                        .unwrap()
                        .unwrap();
                assert_eq!(received.text, message.text);
                assert_eq!(received.origin, message.origin);
                assert_eq!(received.display(), "Message from @codex-test: /run yes");
                assert_eq!(inbox.receiver.max_capacity(), QUEUE_LIMIT);
                drop(inbox);
                tokio::task::yield_now().await;
                assert!(!path.exists());
            })
            .await;
    }

    #[tokio::test]
    async fn registered_inboxes_publish_names_update_them_and_remove_records_on_drop() {
        tokio::task::LocalSet::new()
            .run_until(async {
                let home = tempfile::Builder::new()
                    .prefix("peer-registry-")
                    .tempdir_in("/tmp")
                    .unwrap();
                let sockets = home.path().join("socks");
                std::fs::create_dir(&sockets).unwrap();
                std::fs::set_permissions(&sockets, std::fs::Permissions::from_mode(0o700)).unwrap();
                let registry = xai_peer_socket::registry::PeerRegistry::new(home.path());
                let caller_path = sockets.join("caller.sock");
                let target_path = sockets.join("target.sock");
                let caller_record = PeerSessionRecord::new(
                    "caller-session",
                    "Caller",
                    std::process::id(),
                    &caller_path,
                    "/caller",
                );
                let target_record = PeerSessionRecord::new(
                    "target-session",
                    "Target",
                    std::process::id(),
                    &target_path,
                    "/target",
                );
                let mut caller =
                    start_registered_at(&caller_path, registry.clone(), caller_record).unwrap();
                let _target =
                    start_registered_at(&target_path, registry.clone(), target_record).unwrap();
                let records = registry.list(Some("caller-session")).unwrap();
                assert_eq!(records.len(), 1);
                assert_eq!(records[0].name, "Target");
                assert_eq!(records[0].cwd, "/target");
                assert_eq!(
                    std::fs::metadata(registry.directory()).unwrap().mode() & 0o777,
                    0o700
                );
                let caller_file = registry.directory().join("caller-session.json");
                assert_eq!(
                    std::fs::metadata(&caller_file).unwrap().mode() & 0o777,
                    0o600
                );

                caller.update_name("Renamed Caller").unwrap();
                assert_eq!(
                    registry
                        .list(None)
                        .unwrap()
                        .into_iter()
                        .find(|record| record.session_id == "caller-session")
                        .unwrap()
                        .name,
                    "Renamed Caller"
                );
                drop(caller);
                assert!(!caller_file.exists());
            })
            .await;
    }
}

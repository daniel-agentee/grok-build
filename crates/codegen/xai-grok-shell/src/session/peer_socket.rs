//! One inbox per real actor lifetime. No process-global environment mutation.
use std::path::PathBuf;
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
}
impl Drop for Inbox {
    fn drop(&mut self) {
        self.task.abort();
    }
}
pub(crate) fn start(session_id: &str) -> anyhow::Result<Inbox> {
    start_at(&path(session_id))
}
fn start_at(path: &std::path::Path) -> anyhow::Result<Inbox> {
    let (guard, listener) = SocketGuard::bind(path)?;
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
    Ok(Inbox { receiver, task })
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
}

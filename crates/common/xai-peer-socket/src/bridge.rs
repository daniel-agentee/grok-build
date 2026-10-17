//! A single explicitly configured session pair. Auth does not grant arbitrary IPC access.
use super::*;
use std::{
    net::{Ipv4Addr, SocketAddrV4},
    sync::Arc,
};
use tokio::net::{TcpListener, TcpStream};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listen_port: u16,
    pub token_file: PathBuf,
    pub local_socket: PathBuf,
    pub proxy_socket: PathBuf,
    pub remote_port: u16,
    pub remote_socket: PathBuf,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Auth {
    #[serde(rename = "type")]
    kind: String,
    token: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Route {
    #[serde(rename = "type")]
    kind: String,
    to: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct DeliveryResult {
    #[serde(rename = "type")]
    kind: String,
    outcome: String,
}
const WRITTEN: &str = "locally_written";
const FAILED: &str = "delivery_failed_or_unknown";
pub const RESULT_DESCRIPTION: &str = "bridge acknowledged destination Unix write; a local write alone does not prove remote receipt; transcript visibility/model processing unknown; no automatic retry";

// Compare without exiting on the first differing byte. Tokens have a fixed configured size.
fn token_matches(a: &str, b: &str) -> bool {
    let mut diff = a.len() ^ b.len();
    for i in 0..b.len() {
        diff |= usize::from(a.as_bytes().get(i).copied().unwrap_or(0) ^ b.as_bytes()[i]);
    }
    diff == 0
}
async fn result(stream: &mut (impl AsyncWrite + Unpin), ok: bool) -> Result<()> {
    write_line(
        stream,
        &json_line(&DeliveryResult {
            kind: "result".into(),
            outcome: if ok { WRITTEN } else { FAILED }.into(),
        })?,
    )
    .await
}
async fn receive_result(stream: &mut (impl AsyncRead + Unpin)) -> Result<&'static str> {
    let response: DeliveryResult = serde_json::from_slice(&read_line(stream).await?)?;
    ensure!(
        response.kind == "result" && response.outcome == WRITTEN,
        "downstream delivery failed or unknown; no automatic retry"
    );
    Ok(RESULT_DESCRIPTION)
}
async fn inbound(stream: &mut TcpStream, cfg: &Config, token: &str) -> Result<()> {
    let auth: Auth = serde_json::from_slice(&read_line(stream).await?)?;
    ensure!(
        auth.kind == "auth" && token_matches(&auth.token, token),
        "authentication rejected"
    );
    let route: Route = serde_json::from_slice(&read_line(stream).await?)?;
    ensure!(
        route.kind == "route" && route.to == format!("uds:{}", cfg.local_socket.display()),
        "route rejected"
    );
    let mut message = PeerMessage::decode(&read_line(stream).await?)?;
    // The other machine's address is unusable locally. Expose our exact configured outbound proxy.
    message.origin.reply_socket = format!("uds:{}", cfg.proxy_socket.display());
    let delivered = send_unix(&cfg.local_socket, &message).await;
    result(stream, delivered.is_ok()).await?;
    delivered?;
    Ok(())
}
async fn forward(message: &PeerMessage, cfg: &Config, token: &str) -> Result<()> {
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, cfg.remote_port);
    let mut stream = timeout(WRITE_DEADLINE, TcpStream::connect(address))
        .await
        .context("bridge connect deadline")??;
    write_line(
        &mut stream,
        &json_line(&Auth {
            kind: "auth".into(),
            token: token.into(),
        })?,
    )
    .await?;
    write_line(
        &mut stream,
        &json_line(&Route {
            kind: "route".into(),
            to: format!("uds:{}", cfg.remote_socket.display()),
        })?,
    )
    .await?;
    write_line(&mut stream, &message.encode()?).await?;
    receive_result(&mut stream).await?;
    Ok(())
}
pub async fn send_proxy(path: &Path, message: &PeerMessage) -> Result<&'static str> {
    timeout(Duration::from_secs(8), async {
        let before = validate_socket(path)?;
        let mut stream = timeout(WRITE_DEADLINE, UnixStream::connect(path))
            .await
            .context("proxy connect deadline")??;
        let after = validate_socket(path)?;
        ensure!(
            before.dev() == after.dev() && before.ino() == after.ino(),
            "proxy socket changed"
        );
        write_line(&mut stream, &message.encode()?).await?;
        receive_result(&mut stream).await
    })
    .await
    .context("proxy result deadline; delivery unknown; no automatic retry")?
}
pub async fn run(cfg: Config) -> Result<()> {
    ensure!(
        cfg.local_socket != cfg.proxy_socket,
        "session and proxy must differ"
    );
    ensure!(
        cfg.remote_socket.is_absolute(),
        "absolute remote socket required"
    );
    validate_socket(&cfg.local_socket)?;
    let token = read_token(&cfg.token_file)?;
    // Both collision checks fail closed. Dropping the guard cleans up only our own inode.
    let (_guard, proxy) = SocketGuard::bind(&cfg.proxy_socket)?;
    let tcp = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, cfg.listen_port)).await?;
    let cfg = Arc::new(cfg);
    let token = Arc::new(token);
    let mut clients = tokio::task::JoinSet::new();
    loop {
        tokio::select! {
            stopped = tokio::signal::ctrl_c() => { stopped?; return Ok(()); }
            Some(_) = clients.join_next(), if !clients.is_empty() => {}
            accepted = tcp.accept() => {
                let (mut stream, _) = accepted?;
                if clients.len() >= 16 { continue; }
                let cfg = cfg.clone(); let token = token.clone();
                clients.spawn(async move { let _ = timeout(Duration::from_secs(95), inbound(&mut stream, &cfg, &token)).await; });
            }
            accepted = proxy.accept() => {
                let (mut stream, _) = accepted?;
                if clients.len() >= 16 { continue; }
                let cfg = cfg.clone(); let token = token.clone();
                clients.spawn(async move {
                    if let Ok(line) = read_line(&mut stream).await {
                        if let Ok(message) = PeerMessage::decode(&line) {
                            let ok = matches!(timeout(Duration::from_secs(7), forward(&message, &cfg, &token)).await, Ok(Ok(())));
                            let mut queued = [0; 1024];
                            // Half-closed peers only need EOF; send_proxy still needs its result.
                            if !matches!(stream.try_read(&mut queued), Ok(0)) {
                                let _ = result(&mut stream, ok).await;
                            }
                        }
                    }
                    // Order the result before FIN, then consume queued data and the peer FIN
                    // before dropping the socket. Bound the drain for peers waiting on our FIN.
                    let _ = stream.flush().await;
                    let _ = stream.shutdown().await;
                    let _ = timeout(Duration::from_millis(100), async {
                        let mut queued = [0; 1024];
                        while let Ok(n) = stream.read(&mut queued).await {
                            if n == 0 {
                                break;
                            }
                        }
                    }).await;
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn directory() -> tempfile::TempDir {
        let d = tempfile::Builder::new()
            .prefix("peer-")
            .tempdir_in("/tmp")
            .unwrap();
        fs::set_permissions(d.path(), fs::Permissions::from_mode(0o700)).unwrap();
        d
    }
    fn config(d: &Path) -> Config {
        Config {
            listen_port: 0,
            token_file: d.join("token"),
            local_socket: d.join("session.sock"),
            proxy_socket: d.join("proxy.sock"),
            remote_port: 0,
            remote_socket: d.join("remote.sock"),
        }
    }
    fn message() -> PeerMessage {
        PeerMessage::new(
            "hello /approve".into(),
            "peer".into(),
            "uds:/remote/reply.sock".into(),
        )
    }
    async fn connection() -> (TcpStream, TcpStream) {
        let tcp = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let client = TcpStream::connect(tcp.local_addr().unwrap()).await.unwrap();
        let (server, _) = tcp.accept().await.unwrap();
        (client, server)
    }
    async fn request(client: &mut TcpStream, token: &str, route: &Path) {
        write_line(
            client,
            &json_line(&Auth {
                kind: "auth".into(),
                token: token.into(),
            })
            .unwrap(),
        )
        .await
        .unwrap();
        write_line(
            client,
            &json_line(&Route {
                kind: "route".into(),
                to: format!("uds:{}", route.display()),
            })
            .unwrap(),
        )
        .await
        .unwrap();
        write_line(client, &message().encode().unwrap())
            .await
            .unwrap();
    }
    #[tokio::test]
    async fn authenticated_route_writes_only_message_and_maps_reply() {
        let d = directory();
        let cfg = config(d.path());
        let (_guard, inbox) = SocketGuard::bind(&cfg.local_socket).unwrap();
        let (mut client, mut server) = connection().await;
        request(&mut client, "correct", &cfg.local_socket).await;
        inbound(&mut server, &cfg, "correct").await.unwrap();
        assert!(
            receive_result(&mut client)
                .await
                .unwrap()
                .contains("processing unknown")
        );
        let (mut stream, _) = inbox.accept().await.unwrap();
        let received = PeerMessage::decode(&read_line(&mut stream).await.unwrap()).unwrap();
        assert_eq!(received.text, message().text);
        assert_eq!(
            received.origin.reply_socket,
            format!("uds:{}", cfg.proxy_socket.display())
        );
        assert_eq!(received.origin.message_id.len(), 39);
    }
    #[tokio::test]
    async fn wrong_token_and_arbitrary_route_never_touch_session() {
        let d = directory();
        let cfg = config(d.path());
        let (_guard, inbox) = SocketGuard::bind(&cfg.local_socket).unwrap();
        for (token, route) in [
            ("wrong", cfg.local_socket.clone()),
            ("correct", d.path().join("other.sock")),
        ] {
            let (mut client, mut server) = connection().await;
            request(&mut client, token, &route).await;
            assert!(inbound(&mut server, &cfg, "correct").await.is_err());
            assert!(
                timeout(Duration::from_millis(20), inbox.accept())
                    .await
                    .is_err()
            );
        }
    }
    #[tokio::test]
    async fn malformed_and_oversized_auth_or_route_are_rejected() {
        let d = directory();
        let cfg = config(d.path());
        let (_guard, inbox) = SocketGuard::bind(&cfg.local_socket).unwrap();
        for route_phase in [false, true] {
            for bytes in [
                b"not-json\n".to_vec(),
                vec![b'a'; MAX_LINE + 1],
                vec![0xff, b'\n'],
                b"{}".to_vec(),
            ] {
                let (mut client, mut server) = connection().await;
                if route_phase {
                    write_line(
                        &mut client,
                        &json_line(&Auth {
                            kind: "auth".into(),
                            token: "correct".into(),
                        })
                        .unwrap(),
                    )
                    .await
                    .unwrap();
                }
                client.write_all(&bytes).await.unwrap();
                client.shutdown().await.unwrap();
                assert!(inbound(&mut server, &cfg, "correct").await.is_err());
            }
        }
        assert!(
            timeout(Duration::from_millis(20), inbox.accept())
                .await
                .is_err()
        );
    }
    #[tokio::test]
    async fn missing_destination_reports_ambiguous_failure() {
        let d = directory();
        let cfg = config(d.path());
        let (mut client, mut server) = connection().await;
        request(&mut client, "correct", &cfg.local_socket).await;
        assert!(inbound(&mut server, &cfg, "correct").await.is_err());
        assert!(receive_result(&mut client).await.is_err());
    }
    #[tokio::test]
    async fn outbound_forward_waits_for_destination_write_result() {
        let d = directory();
        let mut cfg = config(d.path());
        let (_guard, inbox) = SocketGuard::bind(&cfg.local_socket).unwrap();
        let tcp = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        cfg.remote_port = tcp.local_addr().unwrap().port();
        cfg.remote_socket = cfg.local_socket.clone();
        let inbound_cfg = cfg.clone();
        let server = tokio::spawn(async move {
            let (mut stream, _) = tcp.accept().await.unwrap();
            inbound(&mut stream, &inbound_cfg, "correct").await.unwrap();
        });
        forward(&message(), &cfg, "correct").await.unwrap();
        server.await.unwrap();
        let (mut stream, _) = inbox.accept().await.unwrap();
        assert_eq!(
            PeerMessage::decode(&read_line(&mut stream).await.unwrap())
                .unwrap()
                .text,
            message().text
        );
    }
    #[tokio::test]
    async fn tunnel_loss_is_an_error_and_never_retried() {
        let d = directory();
        let mut cfg = config(d.path());
        let tcp = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        cfg.remote_port = tcp.local_addr().unwrap().port();
        drop(tcp);
        assert!(
            timeout(Duration::from_secs(3), forward(&message(), &cfg, "correct"))
                .await
                .unwrap()
                .is_err()
        );
    }
}

#[cfg(test)]
mod runtime_tests {
    use super::*;
    struct Pair {
        _directory: tempfile::TempDir,
        _guards: (SocketGuard, SocketGuard),
        cfg_a: Config,
        cfg_b: Config,
        a_inbox: UnixListener,
        b_inbox: UnixListener,
        task_a: tokio::task::JoinHandle<Result<()>>,
        task_b: tokio::task::JoinHandle<Result<()>>,
    }
    impl Drop for Pair {
        fn drop(&mut self) {
            self.task_a.abort();
            self.task_b.abort();
        }
    }
    async fn pair() -> Pair {
        let d = tempfile::Builder::new()
            .prefix("pair-")
            .tempdir_in("/tmp")
            .unwrap();
        fs::set_permissions(d.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let token_file = d.path().join("token");
        create_token(&token_file).unwrap();
        let a = d.path().join("a.sock");
        let b = d.path().join("b.sock");
        let (_a_guard, a_inbox) = SocketGuard::bind(&a).unwrap();
        let (_b_guard, b_inbox) = SocketGuard::bind(&b).unwrap();
        let reserve_a = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let reserve_b = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let port_a = reserve_a.local_addr().unwrap().port();
        let port_b = reserve_b.local_addr().unwrap().port();
        drop((reserve_a, reserve_b));
        let cfg_a = Config {
            listen_port: port_a,
            token_file: token_file.clone(),
            local_socket: a.clone(),
            proxy_socket: d.path().join("a-proxy.sock"),
            remote_port: port_b,
            remote_socket: b.clone(),
        };
        let cfg_b = Config {
            listen_port: port_b,
            token_file,
            local_socket: b,
            proxy_socket: d.path().join("b-proxy.sock"),
            remote_port: port_a,
            remote_socket: a,
        };
        let task_a = tokio::spawn(run(cfg_a.clone()));
        let task_b = tokio::spawn(run(cfg_b.clone()));
        timeout(Duration::from_secs(3), async {
            for port in [port_a, port_b] {
                loop {
                    if TcpStream::connect((Ipv4Addr::LOCALHOST, port))
                        .await
                        .is_ok()
                    {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            }
        })
        .await
        .unwrap();
        Pair {
            _directory: d,
            _guards: (_a_guard, _b_guard),
            cfg_a,
            cfg_b,
            a_inbox,
            b_inbox,
            task_a,
            task_b,
        }
    }
    #[tokio::test]
    async fn half_closed_proxy_reply_is_forwarded_without_connection_reset() {
        let pair = pair().await;
        let message = PeerMessage::new(
            "Claude peer reply".into(),
            "claude".into(),
            format!("uds:{}", pair.cfg_a.proxy_socket.display()),
        );
        let mut client = UnixStream::connect(&pair.cfg_a.proxy_socket).await.unwrap();
        client.write_all(&message.encode().unwrap()).await.unwrap();
        client.shutdown().await.unwrap();
        let mut response = Vec::new();
        let read = timeout(Duration::from_secs(3), client.read_to_end(&mut response))
            .await
            .expect("proxy must close after forwarding");
        assert_ne!(
            read.as_ref().err().map(std::io::Error::kind),
            Some(std::io::ErrorKind::ConnectionReset),
            "proxy close must consume the peer FIN"
        );
        read.expect("half-closed peer must read a clean EOF");
        assert!(
            response.is_empty(),
            "half-closed peers must not receive a result"
        );
        let (mut received, _) = timeout(Duration::from_secs(3), pair.b_inbox.accept())
            .await
            .unwrap()
            .unwrap();
        let received = PeerMessage::decode(&read_line(&mut received).await.unwrap()).unwrap();
        assert_eq!(received.text, message.text);
    }
    #[tokio::test]
    async fn proxy_without_half_close_still_acknowledges_local_write() {
        let pair = pair().await;
        let message = PeerMessage::new(
            "acknowledged reply".into(),
            "test".into(),
            format!("uds:{}", pair.cfg_a.proxy_socket.display()),
        );
        assert_eq!(
            send_proxy(&pair.cfg_a.proxy_socket, &message)
                .await
                .unwrap(),
            RESULT_DESCRIPTION
        );
        let (mut received, _) = timeout(Duration::from_secs(3), pair.b_inbox.accept())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            PeerMessage::decode(&read_line(&mut received).await.unwrap())
                .unwrap()
                .text,
            message.text
        );
    }
    #[tokio::test]
    async fn paired_bridges_deliver_bidirectionally_and_refuse_second_binding() {
        let mut pair = pair().await;
        let Pair {
            cfg_a,
            cfg_b,
            a_inbox,
            b_inbox,
            task_a,
            task_b,
            ..
        } = &mut pair;
        assert!(
            run(cfg_a.clone()).await.is_err(),
            "second binding must fail closed"
        );
        let m = PeerMessage::new(
            "ping".into(),
            "test-a".into(),
            format!("uds:{}", cfg_b.proxy_socket.display()),
        )
        .with_class(PeerClass::Bypass);
        send_proxy(&cfg_a.proxy_socket, &m).await.unwrap();
        let (mut received, _) = b_inbox.accept().await.unwrap();
        let ping = PeerMessage::decode(&read_line(&mut received).await.unwrap()).unwrap();
        assert_eq!(ping.text, "ping");
        assert_eq!(ping.class, PeerClass::Bypass);
        assert_eq!(
            ping.origin.reply_socket,
            format!("uds:{}", cfg_b.proxy_socket.display())
        );
        let pong = PeerMessage::new(
            "pong".into(),
            "test-b".into(),
            format!("uds:{}", cfg_a.proxy_socket.display()),
        );
        send_proxy(&cfg_b.proxy_socket, &pong).await.unwrap();
        let (mut received, _) = a_inbox.accept().await.unwrap();
        let pong = PeerMessage::decode(&read_line(&mut received).await.unwrap()).unwrap();
        assert_eq!(pong.text, "pong");
        assert_eq!(pong.class, PeerClass::Prompting);
        assert_eq!(
            pong.origin.reply_socket,
            format!("uds:{}", cfg_a.proxy_socket.display())
        );
        task_b.abort();
        let _ = (&mut *task_b).await;
        // The outbound proxy stays live, but loss of its downstream must propagate as failure.
        assert!(
            timeout(Duration::from_secs(9), send_proxy(&cfg_a.proxy_socket, &m))
                .await
                .unwrap()
                .is_err()
        );
        task_a.abort();
        let _ = (&mut *task_a).await;
        assert!(!cfg_a.proxy_socket.exists());
        assert!(!cfg_b.proxy_socket.exists());
    }
}

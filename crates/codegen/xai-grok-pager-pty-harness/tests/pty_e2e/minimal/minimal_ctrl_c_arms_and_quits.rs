// Per-test-case module for the `pty_e2e` integration test crate.
#[allow(unused_imports)]
use crate::common::*;

/// Minimal mode: Ctrl+C must NOT arm quit or exit. Quit stays on Ctrl+Q
/// (double-press confirmation). Accidental Ctrl+C must not kill the session.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore]
async fn minimal_ctrl_c_does_not_quit() {
    let content = ContentController::start().await.expect("start content");
    let mut harness = spawn_minimal(&content);
    wait_minimal_ready(&mut harness);

    // Ctrl+C (CSI legacy ETX) must not arm quit or exit.
    harness.inject_keys(b"\x03").expect("inject Ctrl+C");
    harness.update(Duration::from_millis(500));
    assert!(
        !harness.contains_text("again to quit"),
        "Ctrl+C must not arm quit confirmation\nscreen:\n{}",
        harness.screen_contents()
    );
    assert!(
        harness.wait_exit_code(Duration::from_millis(200)).is_none(),
        "Ctrl+C must not exit the process\nscreen:\n{}",
        harness.screen_contents()
    );

    // Positive: Ctrl+Q still arms double-press quit.
    harness.inject_keys(b"\x11").expect("inject Ctrl+Q");
    harness
        .wait_for_text("again to quit", Duration::from_secs(5))
        .unwrap_or_else(|e| {
            panic!(
                "quit-confirmation hint expected after first Ctrl+Q: {e}\nscreen:\n{}",
                harness.screen_contents()
            )
        });

    harness.inject_keys(b"\x11").expect("inject Ctrl+Q again");
    let code = harness.wait_exit_code(Duration::from_secs(5));
    assert!(
        code.is_some(),
        "second Ctrl+Q should quit minimal\nscreen:\n{}",
        harness.screen_contents()
    );
}

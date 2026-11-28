//! A worker started with `IKI_REGISTRATION_TOKEN` starts itself again without it in its
//! environment, and still registers with it.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::process::Command;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

#[test]
fn a_worker_registers_with_the_token_it_handed_itself() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (seen, requests) = mpsc::channel::<(String, String)>();
    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut request = String::new();
            let _ = reader.read_line(&mut request);
            let mut authorization = String::new();
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                    break;
                }
                if let Some((k, v)) = line.split_once(':')
                    && k.eq_ignore_ascii_case("authorization")
                {
                    v.trim().clone_into(&mut authorization);
                }
            }
            let _ = seen.send((request.trim().to_owned(), authorization));
            let reply = r#"{"detail": "This registration token was revoked."}"#;
            let _ = write!(
                stream,
                "HTTP/1.1 401 X\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{reply}",
                reply.len()
            );
        }
    });
    let dir = std::env::temp_dir().join(format!("iki-registration-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let ran = Command::new(env!("CARGO_BIN_EXE_iki-worker"))
        .arg("start")
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("IKI_URL", &url)
        .env("IKI_ALLOW_HTTP", "1")
        .env("IKI_REGISTRATION_TOKEN", "iki_wreg_handed")
        .env("IKI_WORKER_CACHE", dir.join("cache"))
        .env("IKI_WORK_DIR", dir.join("work"))
        .env("IKI_WORKER_MEMORY", "1Gi")
        .env("IKI_WORKER_CPU", "1")
        .env("IKI_WORKER_SCRATCH_DISK", "1Gi")
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&ran.stderr);
    assert!(!ran.status.success(), "{stderr}");
    assert!(stderr.contains("Couldn't register"), "{stderr}");
    // It started again in its own place, rather than carry on with the token in its
    // environment.
    assert!(!stderr.contains("it stays there"), "{stderr}");
    let (request, authorization) = requests.recv_timeout(Duration::from_secs(10)).unwrap();
    assert!(
        request.starts_with("POST /worker/v1/register "),
        "{request}"
    );
    assert_eq!(authorization, "Bearer iki_wreg_handed");
    std::fs::remove_dir_all(&dir).unwrap();
}

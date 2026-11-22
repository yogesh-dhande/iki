//! The local CLI owns each run's working directory through its final exit status.

use std::path::Path;
use std::process::Command;
use std::{fs, io};

fn check_run(case: &str, action: &str, exit_code: i32, keep: bool) -> io::Result<()> {
    let dir = std::env::temp_dir().join(format!("iki-local-{case}-{keep}-{}", std::process::id()));
    fs::create_dir(&dir)?;
    let jobs = dir.join("iki-worker/jobs");
    let sibling = jobs.join("another-job");
    fs::create_dir_all(&sibling)?;
    let script = format!(
        r#"set -e
printf '%s' "${{IKI_INPUT%/*}}" > job-dir
printf saved > "$IKI_CHECKPOINT_DIR/state"
{action}"#
    );
    let input = serde_json::json!({
        "apiVersion": "iki/v1", "kind": "RunInput",
        "run": {
            "id": "r1", "hrid": "RUN-1", "attempt": 1, "action": "example.test",
            "plugin": {"name": "example", "version": "1"}, "project": "p1"
        },
        "command": ["sh", "-c", script], "environment": "example", "params": {}, "inputs": {}
    });
    fs::write(dir.join("fixture.json"), input.to_string())?;
    let mut worker = Command::new(env!("CARGO_BIN_EXE_iki-worker"));
    worker
        .current_dir(&dir)
        .env_remove("IKI_WORK_DIR")
        .env("TMPDIR", &dir)
        .args([
            "run",
            "--local",
            "fixture.json",
            "--checkpoint-dir",
            "checkpoint",
        ]);
    if keep {
        worker.arg("--keep");
    }
    let result = worker.output()?;
    let stderr = String::from_utf8_lossy(&result.stderr);
    assert_eq!(result.status.code(), Some(exit_code), "{stderr}");
    let recorded = fs::read_to_string(dir.join("job-dir"))?;
    let job = Path::new(&recorded);
    assert_eq!(job.parent(), Some(jobs.as_path()));
    assert!(sibling.is_dir());
    assert_eq!(fs::read(dir.join("checkpoint/state"))?, b"saved");
    if keep {
        assert!(job.join("input.json").is_file());
        assert!(
            stderr
                .lines()
                .any(|line| line.to_lowercase().contains("kept")
                    && line.contains("job_dir=")
                    && line.contains(&recorded)),
            "{stderr}"
        );
    } else {
        assert!(!job.exists(), "job directory remains at {recorded}");
    }
    fs::remove_dir_all(dir)
}

#[test]
fn successful_run_cleans_up_unless_kept() {
    for keep in [false, true] {
        check_run("success", r#"printf '{}' > "$IKI_OUTPUT""#, 0, keep).unwrap();
    }
}

#[test]
fn failed_run_cleans_up_unless_kept() {
    for keep in [false, true] {
        check_run("failure", "exit 3", 1, keep).unwrap();
    }
}

#[test]
fn interrupted_run_cleans_up_unless_kept() {
    for keep in [false, true] {
        // Signal the worker once the job is running, just as Ctrl-C would.
        check_run(
            "interrupt",
            r#"kill -INT "$PPID"; exec sleep 10"#,
            130,
            keep,
        )
        .unwrap();
    }
}

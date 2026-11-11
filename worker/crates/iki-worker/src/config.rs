//! How a worker is set up: environment variables, as for GitHub's and GitLab's runners,
//! so the same image works anywhere a secret can be put in the environment.

use std::path::PathBuf;

use iki_protocol::Capacity;

/// Everything `iki-worker start` needs.
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    /// The server, such as `https://iki.example.com`, without a trailing slash.
    pub url: String,
    /// `iki_wreg_…`, to register with. Not needed once `state_file` holds a token.
    pub registration_token: Option<String>,
    /// Where to keep the worker's own token, so a restart doesn't register again.
    /// Without one, the worker registers each time it starts and deregisters on exit.
    pub state_file: Option<PathBuf>,
    /// How people tell it apart: the host or pod name unless set.
    pub name: String,
    /// A directory per job goes here, removed when the job ends.
    pub work_dir: PathBuf,
    /// One directory per plugin, by name, holding its code.
    pub plugins_dir: PathBuf,
    /// What it can give one job.
    pub capacity: Capacity,
    /// Keep job directories after their jobs end, to debug a command.
    pub keep_job_dirs: bool,
    /// `IKI_ALLOW_HTTP`: plain http:// to any host, for a development cluster
    /// whose server has no TLS. The worker warns when it starts with it.
    pub allow_http: bool,
}

/// What `IKI_URL` must look like, in words for whoever set it.
const URL_RULE: &str = "IKI_URL must be the server's https:// address, such as \
    https://iki.example.com (plain http:// is allowed only for localhost).";

/// The server's base URL, or why it's not acceptable. Plain HTTP would send the worker's
/// token and every result in the clear, so it's allowed only on this machine, unless
/// `allow_http` (a development cluster) says otherwise.
pub fn server_url(raw: &str, allow_http: bool) -> Result<String, String> {
    let url = raw.trim().trim_end_matches('/');
    let (scheme, rest) = url.split_once("://").ok_or(URL_RULE)?;
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.is_empty() || authority.contains('@') {
        return Err(URL_RULE.to_owned());
    }
    let host = if let Some(bracketed) = authority.strip_prefix('[') {
        bracketed.split(']').next().unwrap_or_default()
    } else {
        authority.split(':').next().unwrap_or_default()
    };
    match scheme.to_ascii_lowercase().as_str() {
        "https" => Ok(url.to_owned()),
        "http" if allow_http || matches!(host, "localhost" | "127.0.0.1" | "::1") => {
            Ok(url.to_owned())
        }
        _ => Err(URL_RULE.to_owned()),
    }
}

/// Bytes from `512Mi`, `4Gi`, `2G` or a plain number, as Kubernetes writes them.
pub fn bytes(raw: &str) -> Option<i64> {
    let raw = raw.trim();
    let split = raw.find(|c: char| !c.is_ascii_digit()).unwrap_or(raw.len());
    let (number, unit) = raw.split_at(split);
    let number: i64 = number.parse().ok()?;
    let scale: i64 = match unit {
        "" => 1,
        "Ki" => 1 << 10,
        "Mi" => 1 << 20,
        "Gi" => 1 << 30,
        "Ti" => 1 << 40,
        "k" | "K" => 1_000,
        "M" => 1_000_000,
        "G" => 1_000_000_000,
        "T" => 1_000_000_000_000,
        _ => return None,
    };
    number.checked_mul(scale)
}

/// Memory this machine (or its cgroup, in a container) can give jobs, on Linux.
fn detected_memory() -> Option<i64> {
    let total = std::fs::read_to_string("/proc/meminfo")
        .ok()
        .and_then(|text| {
            let line = text.lines().find(|l| l.starts_with("MemTotal:"))?;
            let kib: i64 = line.split_whitespace().nth(1)?.parse().ok()?;
            kib.checked_mul(1024)
        });
    let limit = std::fs::read_to_string("/sys/fs/cgroup/memory.max")
        .ok()
        .and_then(|text| text.trim().parse::<i64>().ok());
    match (total, limit) {
        (Some(total), Some(limit)) => Some(total.min(limit)),
        (one, other) => one.or(other),
    }
}

fn hostname() -> String {
    rustix::system::uname()
        .nodename()
        .to_string_lossy()
        .into_owned()
}

impl Config {
    /// Reads the configuration through `var`, which is `std::env::var` outside tests.
    pub fn from_env(var: &dyn Fn(&str) -> Option<String>) -> Result<Self, String> {
        let get = |name: &str| {
            var(name)
                .map(|v| v.trim().to_owned())
                .filter(|v| !v.is_empty())
        };
        let flag = |name: &str| get(name).is_some_and(|v| v == "1" || v == "true");
        let allow_http = flag("IKI_ALLOW_HTTP");
        let url = server_url(&get("IKI_URL").ok_or(URL_RULE)?, allow_http)?;
        let registration_token = get("IKI_REGISTRATION_TOKEN");
        // It goes in a header: anything else in the value (a log line that was captured
        // with it, say) would fail every call, so say so now.
        if registration_token
            .as_deref()
            .is_some_and(|t| !t.bytes().all(|b| b.is_ascii_graphic()))
        {
            return Err(
                "IKI_REGISTRATION_TOKEN must be the token alone, such as iki_wreg_…, \
                 with no spaces or other lines."
                    .to_owned(),
            );
        }
        let state_file = get("IKI_WORKER_STATE").map(PathBuf::from);
        if registration_token.is_none() && state_file.is_none() {
            return Err(
                "Set IKI_REGISTRATION_TOKEN to a registration token from Compute \
                 settings (and IKI_WORKER_STATE to keep the worker's own token across restarts)."
                    .to_owned(),
            );
        }
        let plugins_dir = get("IKI_PLUGINS_DIR")
            .map(PathBuf::from)
            .ok_or("Set IKI_PLUGINS_DIR to the directory holding each plugin's code, by name.")?;
        let work_dir = get("IKI_WORK_DIR")
            .map_or_else(|| std::env::temp_dir().join("iki-worker"), PathBuf::from);
        let cpu = match get("IKI_WORKER_CPU") {
            Some(raw) => raw
                .parse::<f64>()
                .ok()
                .filter(|cpu| cpu.is_finite() && *cpu > 0.0)
                .ok_or("IKI_WORKER_CPU must be a number of cores above 0, such as 4 or 0.5.")?,
            None => std::thread::available_parallelism().map_or(1.0, |n| {
                f64::from(u32::try_from(n.get()).unwrap_or(u32::MAX))
            }),
        };
        let memory = match get("IKI_WORKER_MEMORY") {
            Some(raw) => bytes(&raw)
                .filter(|m| *m > 0)
                .ok_or("IKI_WORKER_MEMORY must be bytes, such as 16Gi or 512Mi.")?,
            None => detected_memory()
                .ok_or("Set IKI_WORKER_MEMORY to the memory jobs may use, such as 16Gi.")?,
        };
        let optional_bytes = |name: &str| -> Result<Option<i64>, String> {
            get(name)
                .map(|raw| {
                    bytes(&raw)
                        .filter(|b| *b >= 0)
                        .ok_or(format!("{name} must be bytes, such as 80Gi."))
                })
                .transpose()
        };
        let gpu_count = get("IKI_WORKER_GPUS")
            .map(|raw| {
                raw.parse::<i64>()
                    .ok()
                    .filter(|n| *n >= 0)
                    .ok_or("IKI_WORKER_GPUS must be a whole number, such as 1.")
            })
            .transpose()?;
        Ok(Self {
            url,
            registration_token,
            state_file,
            name: get("IKI_WORKER_NAME").unwrap_or_else(hostname),
            work_dir,
            plugins_dir,
            capacity: Capacity {
                cpu,
                memory,
                gpu_count,
                gpu_memory: optional_bytes("IKI_WORKER_GPU_MEMORY")?,
                scratch_disk: optional_bytes("IKI_WORKER_SCRATCH_DISK")?,
            },
            keep_job_dirs: flag("IKI_KEEP_JOB_DIRS"),
            allow_http,
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        move |name| map.get(name).cloned()
    }

    const BASE: &[(&str, &str)] = &[
        ("IKI_URL", "https://iki.example.com/"),
        ("IKI_REGISTRATION_TOKEN", "iki_wreg_x"),
        ("IKI_PLUGINS_DIR", "/plugins"),
        ("IKI_WORKER_CPU", "2"),
        ("IKI_WORKER_MEMORY", "4Gi"),
    ];

    #[test]
    fn reads_the_environment() {
        let config = Config::from_env(&env(BASE)).unwrap();
        assert_eq!(config.url, "https://iki.example.com");
        assert_eq!(config.capacity.memory, 4 << 30);
        assert!((config.capacity.cpu - 2.0).abs() < f64::EPSILON);
        assert_eq!(config.capacity.gpu_count, None);
        assert_eq!(config.state_file, None);
        assert!(!config.keep_job_dirs);
    }

    #[test]
    fn explains_what_is_missing_or_wrong() {
        let without = |name: &str| -> Vec<(&str, &str)> {
            BASE.iter().copied().filter(|(k, _)| *k != name).collect()
        };
        assert!(
            Config::from_env(&env(&without("IKI_REGISTRATION_TOKEN")))
                .unwrap_err()
                .contains("IKI_REGISTRATION_TOKEN")
        );
        assert!(
            Config::from_env(&env(&without("IKI_PLUGINS_DIR")))
                .unwrap_err()
                .contains("IKI_PLUGINS_DIR")
        );
        let mut bad = without("IKI_WORKER_MEMORY");
        bad.push(("IKI_WORKER_MEMORY", "lots"));
        assert!(Config::from_env(&env(&bad)).unwrap_err().contains("16Gi"));
    }

    #[test]
    fn plain_http_only_for_localhost() {
        let strict = |url: &str| server_url(url, false);
        assert_eq!(
            strict("http://localhost:8000/").unwrap(),
            "http://localhost:8000"
        );
        assert!(strict("http://127.0.0.1:8000").is_ok());
        assert!(strict("http://[::1]:8000").is_ok());
        assert!(strict("https://iki.example.com").is_ok());
        assert!(strict("http://iki.example.com").is_err());
        assert!(strict("http://localhost.evil.com").is_err());
        assert!(strict("http://localhost@evil.com").is_err());
        assert!(strict("iki.example.com").is_err());
        assert!(strict("ftp://localhost").is_err());
    }

    #[test]
    fn a_registration_token_is_the_token_alone() {
        let mut bad: Vec<(&str, &str)> = BASE
            .iter()
            .copied()
            .filter(|(k, _)| *k != "IKI_REGISTRATION_TOKEN")
            .collect();
        bad.push(("IKI_REGISTRATION_TOKEN", "{\"event\": \"x\"}\niki_wreg_x"));
        assert!(
            Config::from_env(&env(&bad))
                .unwrap_err()
                .contains("the token alone")
        );
        let padded: Vec<(&str, &str)> = BASE
            .iter()
            .map(|&(k, v)| {
                if k == "IKI_REGISTRATION_TOKEN" {
                    (k, " iki_wreg_x\n")
                } else {
                    (k, v)
                }
            })
            .collect();
        assert_eq!(
            Config::from_env(&env(&padded))
                .unwrap()
                .registration_token
                .as_deref(),
            Some("iki_wreg_x")
        );
    }

    #[test]
    fn plain_http_anywhere_only_when_allowed() {
        let mut dev: Vec<(&str, &str)> = BASE
            .iter()
            .copied()
            .filter(|(k, _)| *k != "IKI_URL")
            .collect();
        dev.push(("IKI_URL", "http://iki-web.iki.svc:8000"));
        assert!(
            Config::from_env(&env(&dev))
                .unwrap_err()
                .contains("https://")
        );
        dev.push(("IKI_ALLOW_HTTP", "1"));
        let config = Config::from_env(&env(&dev)).unwrap();
        assert_eq!(config.url, "http://iki-web.iki.svc:8000");
        assert!(config.allow_http);
        assert!(server_url("ftp://iki-web", true).is_err());
        assert!(server_url("http://user@iki-web", true).is_err());
        assert!(!Config::from_env(&env(BASE)).unwrap().allow_http);
    }

    #[test]
    fn reads_kubernetes_quantities() {
        assert_eq!(bytes("512Mi"), Some(512 << 20));
        assert_eq!(bytes("2G"), Some(2_000_000_000));
        assert_eq!(bytes("1024"), Some(1024));
        assert_eq!(bytes("1.5Gi"), None);
        assert_eq!(bytes("Gi"), None);
        assert_eq!(bytes("99999999999Ti"), None);
    }
}

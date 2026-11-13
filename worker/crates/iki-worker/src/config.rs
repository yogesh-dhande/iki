//! How a worker is set up: environment variables, as for GitHub's and GitLab's runners,
//! so the same image works anywhere a secret can be put in the environment.

use std::path::PathBuf;
use std::time::Duration;

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
    /// One directory per plugin, by name, holding its code. Without one, the worker
    /// fetches each plugin's code from the server and builds its environments (ADR 0059).
    pub plugins_dir: Option<PathBuf>,
    /// Where it keeps what it fetched and built: `IKI_WORKER_CACHE`, or the user's cache.
    pub cache_dir: PathBuf,
    /// What it can give one job.
    pub capacity: Capacity,
    /// Keep job directories after their jobs end, to debug a command.
    pub keep_job_dirs: bool,
    /// Whether a worker on someone's computer updates itself when the server says to
    /// (ADR 0060). `IKI_WORKER_AUTO_UPDATE=0` leaves updates to whoever installed it.
    pub auto_update: bool,
    /// `IKI_ALLOW_HTTP`: plain http:// to any host, for a development cluster
    /// whose server has no TLS. The worker warns when it starts with it.
    pub allow_http: bool,
    /// On a machine Iki started for runs (ADR 0061): the environment it was started for,
    /// whose image it runs in. It fetches only that plugin's code, and builds nothing.
    pub image: Option<InImage>,
    /// `IKI_WORKER_IDLE_EXIT`: stop after this long without a job.
    pub idle_exit: Option<Duration>,
    /// `IKI_WORKER_CLEAN_DIR`: a directory that holds only what jobs leave, such as a
    /// pod's scratch volume. A worker that runs one job after another empties it after
    /// each, so the next starts clean (ADR 0062). If its cache is in there too, it
    /// fetches its plugin's code again.
    pub clean_dir: Option<PathBuf>,
    /// `IKI_SETUP_SUCCEEDED_FILE`: on a cloud machine whose setup build's disks are then
    /// copied into a bucket (ADR 0078), the file to write once the server has taken the
    /// build as succeeded, so the machine copies them only then.
    pub setup_succeeded_file: Option<PathBuf>,
}

/// The environment a worker runs in the image of.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InImage {
    /// `plugin.environment`.
    pub environment: String,
    /// The image, pinned by its digest.
    pub image: String,
}

impl InImage {
    pub fn plugin(&self) -> &str {
        self.environment
            .split_once('.')
            .map_or("", |(plugin, _)| plugin)
    }

    pub fn name(&self) -> &str {
        self.environment.split_once('.').map_or("", |(_, env)| env)
    }

    /// `sha256:…`.
    pub fn digest(&self) -> &str {
        self.image.rsplit_once('@').map_or("", |(_, digest)| digest)
    }
}

fn in_image(environment: Option<String>, image: Option<String>) -> Result<Option<InImage>, String> {
    match (environment, image) {
        (None, None) => Ok(None),
        (Some(environment), Some(image)) => {
            let found = InImage { environment, image };
            let digest = found.digest();
            let pinned = digest.len() == 71
                && digest.starts_with("sha256:")
                && digest[7..].bytes().all(|b| b.is_ascii_hexdigit());
            if !crate::plugins::is_plugin_name(found.plugin()) || found.name().is_empty() {
                return Err(
                    "IKI_WORKER_ENVIRONMENT must be plugin.environment, such as \
                            protein.esmfold."
                        .to_owned(),
                );
            }
            if !pinned {
                return Err("IKI_WORKER_IMAGE must be pinned by its digest \
                            (registry/name@sha256:…)."
                    .to_owned());
            }
            Ok(Some(found))
        }
        _ => Err("Set both IKI_WORKER_ENVIRONMENT and IKI_WORKER_IMAGE, or neither.".to_owned()),
    }
}

/// What to do when the worker has no way to sign in.
const NOT_SIGNED_IN: &str = "Run iki-worker login https://iki.example.com (your Iki's \
    address) to sign in on this computer. A deployment's workers set IKI_URL and \
    IKI_REGISTRATION_TOKEN, to a registration token from Compute settings, instead.";

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

fn reader(var: &dyn Fn(&str) -> Option<String>) -> impl Fn(&str) -> Option<String> + '_ {
    move |name: &str| {
        var(name)
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty())
    }
}

/// How people tell the worker apart: `IKI_WORKER_NAME`, or the host name.
pub fn name(var: &dyn Fn(&str) -> Option<String>) -> String {
    reader(var)("IKI_WORKER_NAME").unwrap_or_else(hostname)
}

/// What the worker can give one job, from `IKI_WORKER_CPU` and the rest, or detected.
pub fn capacity(var: &dyn Fn(&str) -> Option<String>) -> Result<Capacity, String> {
    let get = reader(var);
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
    Ok(Capacity {
        cpu,
        memory,
        gpu_count,
        gpu_memory: optional_bytes("IKI_WORKER_GPU_MEMORY")?,
        scratch_disk: optional_bytes("IKI_WORKER_SCRATCH_DISK")?,
    })
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
        // A worker someone signed in on their computer keeps its token where `login` put it.
        let state_file = get("IKI_WORKER_STATE").map(PathBuf::from).or_else(|| {
            registration_token
                .is_none()
                .then(|| crate::login::default_state_file(var))
                .flatten()
        });
        let signed_in = state_file
            .as_deref()
            .and_then(crate::worker::load_any)
            .map(|saved| saved.url);
        if registration_token.is_none() && signed_in.is_none() {
            return Err(NOT_SIGNED_IN.to_owned());
        }
        let url = match get("IKI_URL") {
            Some(raw) => server_url(&raw, allow_http)?,
            None => server_url(&signed_in.ok_or(URL_RULE)?, allow_http)?,
        };
        let plugins_dir = get("IKI_PLUGINS_DIR").map(PathBuf::from);
        let cache_dir = get("IKI_WORKER_CACHE")
            .map(PathBuf::from)
            .or_else(|| crate::bundles::default_cache(var))
            .or_else(|| {
                plugins_dir
                    .is_some()
                    .then(|| std::env::temp_dir().join("iki-worker-cache"))
            })
            .ok_or(
                "Set IKI_WORKER_CACHE to where the worker may keep the plugins it fetches \
                 (there's no home directory), or IKI_PLUGINS_DIR to their code.",
            )?;
        let work_dir = get("IKI_WORK_DIR")
            .map_or_else(|| std::env::temp_dir().join("iki-worker"), PathBuf::from);
        let image = in_image(get("IKI_WORKER_ENVIRONMENT"), get("IKI_WORKER_IMAGE"))?;
        let clean_dir = get("IKI_WORKER_CLEAN_DIR").map(PathBuf::from);
        if let Some(dir) = &clean_dir {
            if !dir.is_absolute() || dir.parent().is_none() {
                return Err("IKI_WORKER_CLEAN_DIR must be an absolute directory, not /.".to_owned());
            }
            // Emptying it would throw away what the worker built for its environments.
            if image.is_none() && plugins_dir.is_none() && cache_dir.starts_with(dir) {
                return Err(
                    "IKI_WORKER_CACHE can't be inside IKI_WORKER_CLEAN_DIR on a worker \
                            that builds its environments: it would build them for every job."
                        .to_owned(),
                );
            }
        }
        Ok(Self {
            url,
            registration_token,
            state_file,
            name: name(var),
            work_dir,
            plugins_dir,
            cache_dir,
            capacity: capacity(var)?,
            keep_job_dirs: flag("IKI_KEEP_JOB_DIRS"),
            auto_update: !get("IKI_WORKER_AUTO_UPDATE").is_some_and(|v| v == "0" || v == "false"),
            allow_http,
            image,
            setup_succeeded_file: get("IKI_SETUP_SUCCEEDED_FILE").map(PathBuf::from),
            idle_exit: match get("IKI_WORKER_IDLE_EXIT") {
                None => None,
                Some(raw) => Some(Duration::from_secs(raw.parse().map_err(
                    |_| "IKI_WORKER_IDLE_EXIT must be a whole number of seconds, such as 300.",
                )?)),
            },
            clean_dir,
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

    #[test]
    fn a_worker_in_an_image_knows_its_environment_and_image() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let image = format!("ghcr.io/acme/fold@{digest}");
        let mut pairs = BASE.to_vec();
        pairs.push(("IKI_WORKER_ENVIRONMENT", "protein.esmfold"));
        pairs.push(("IKI_WORKER_IMAGE", &image));
        pairs.push(("IKI_WORKER_IDLE_EXIT", "300"));
        let config = Config::from_env(&env(&pairs)).unwrap();
        let found = config.image.unwrap();
        assert_eq!(
            (found.plugin(), found.name(), found.digest()),
            ("protein", "esmfold", digest.as_str())
        );
        assert_eq!(config.idle_exit, Some(Duration::from_secs(300)));
        let mut unpinned = BASE.to_vec();
        unpinned.push(("IKI_WORKER_ENVIRONMENT", "protein.esmfold"));
        unpinned.push(("IKI_WORKER_IMAGE", "ghcr.io/acme/fold:latest"));
        assert!(
            Config::from_env(&env(&unpinned))
                .unwrap_err()
                .contains("digest")
        );
        let mut half = BASE.to_vec();
        half.push(("IKI_WORKER_ENVIRONMENT", "protein.esmfold"));
        assert!(Config::from_env(&env(&half)).unwrap_err().contains("both"));
    }

    #[test]
    fn a_clean_directory_must_not_hold_what_the_worker_builds() {
        let mut pairs = BASE.to_vec();
        pairs.push(("IKI_WORKER_CLEAN_DIR", "/iki/scratch"));
        pairs.push(("IKI_WORKER_CACHE", "/var/cache/iki"));
        let config = Config::from_env(&env(&pairs)).unwrap();
        assert_eq!(config.clean_dir, Some(PathBuf::from("/iki/scratch")));
        // Without IKI_PLUGINS_DIR, it fetches its plugins and builds their environments.
        let mut inside: Vec<_> = BASE
            .iter()
            .copied()
            .filter(|(k, _)| *k != "IKI_PLUGINS_DIR")
            .collect();
        inside.push(("IKI_WORKER_CLEAN_DIR", "/iki/scratch"));
        inside.push(("IKI_WORKER_CACHE", "/iki/scratch/cache"));
        assert!(
            Config::from_env(&env(&inside))
                .unwrap_err()
                .contains("every job")
        );
        let image = format!("ghcr.io/acme/fold@sha256:{}", "a".repeat(64));
        inside.push(("IKI_WORKER_ENVIRONMENT", "protein.esmfold"));
        inside.push(("IKI_WORKER_IMAGE", &image));
        assert!(Config::from_env(&env(&inside)).is_ok());
        for odd in ["scratch", "/"] {
            let mut pairs = BASE.to_vec();
            pairs.push(("IKI_WORKER_CLEAN_DIR", odd));
            assert!(Config::from_env(&env(&pairs)).is_err(), "{odd}");
        }
    }
}

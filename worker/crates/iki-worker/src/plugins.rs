//! The plugin code a worker has, in `IKI_PLUGINS_DIR`: all it runs.
//!
//! The worker tells the server what it has when it starts, and leases only runs launched
//! with that very code (ADR 0047). What it has comes from one of two places:
//!
//! - **`plugins.json`**, from `iki deploy build --workers`: each plugin's name, version
//!   and commit, and the environments the image built, with each build's digest and
//!   prefix. A deployment's workers run this way.
//! - **Otherwise**, each directory holding a `plugin.yaml` is a plugin from a local
//!   directory, run with the host's own software. It runs only what a development
//!   server launches, whose plugins are local too.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use iki_protocol::PluginCode;
use serde::Deserialize;

use crate::say;

/// How an environment is built, and so how a command runs in it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    /// A virtual environment: its `bin` goes first on `PATH`.
    Python,
    /// A conda environment: the command runs through `micromamba run`, so its
    /// activation scripts run.
    Conda,
}

/// An environment the image built.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Built {
    pub digest: String,
    pub kind: Kind,
    pub prefix: PathBuf,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    #[serde(rename = "apiVersion")]
    api_version: String,
    kind: String,
    plugins: Vec<Listed>,
}

#[derive(Debug, Deserialize)]
struct Listed {
    name: String,
    sha: Option<String>,
    #[serde(default)]
    environments: BTreeMap<String, Built>,
}

/// What the worker has.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Have {
    /// What it tells the server.
    pub code: Vec<PluginCode>,
    /// The environments it built, by `plugin.environment`.
    built: BTreeMap<String, Built>,
    /// Whether it came from `plugins.json`, so every environment must be built.
    from_manifest: bool,
    /// Where each plugin's code is, when the worker fetched it (ADR 0059); otherwise in
    /// `IKI_PLUGINS_DIR`, by name.
    dirs: BTreeMap<String, PathBuf>,
    /// The environment whose image the worker runs in (ADR 0061), by
    /// `plugin.environment`: its commands run with the image's own software.
    image: Option<String>,
}

/// The file `iki deploy build --workers` writes in `IKI_PLUGINS_DIR`.
pub const MANIFEST: &str = "plugins.json";

/// A plugin's name, as `spec/` allows it: `protein`, `structure_prediction`.
pub fn is_plugin_name(name: &str) -> bool {
    (2..=30).contains(&name.len())
        && name.starts_with(|c: char| c.is_ascii_lowercase())
        && !name.ends_with('_')
        && !name.contains("__")
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// What `dir` holds.
pub fn discover(dir: &Path) -> Result<Have, String> {
    let manifest = dir.join(MANIFEST);
    if manifest.is_file() {
        return from_manifest(dir, &manifest);
    }
    let entries = std::fs::read_dir(dir).map_err(|error| {
        format!(
            "Couldn't read IKI_PLUGINS_DIR ({}): {error}.",
            dir.display()
        )
    })?;
    let mut code = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.join("plugin.yaml").is_file() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if !is_plugin_name(&name) {
            say::warn(
                "Skipped a plugin whose directory isn't named for it.",
                &[("path", &path.display())],
            );
            continue;
        }
        code.push(PluginCode {
            name,
            sha: None,
            environments: None,
        });
    }
    code.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(Have {
        code,
        ..Have::default()
    })
}

fn from_manifest(dir: &Path, file: &Path) -> Result<Have, String> {
    let shown = file.display();
    let text = std::fs::read(file).map_err(|error| format!("Couldn't read {shown}: {error}."))?;
    let manifest: Manifest = serde_json::from_slice(&text)
        .map_err(|error| format!("{shown} isn't what `iki deploy build` writes: {error}."))?;
    if manifest.api_version != "iki/v1" || manifest.kind != "WorkerPlugins" {
        return Err(format!(
            "{shown} is a {} {}, not an iki/v1 WorkerPlugins.",
            manifest.api_version, manifest.kind
        ));
    }
    let mut have = Have {
        from_manifest: true,
        ..Have::default()
    };
    for plugin in manifest.plugins {
        if !is_plugin_name(&plugin.name) || !dir.join(&plugin.name).is_dir() {
            return Err(format!(
                "{shown} lists {}, whose code isn't in {}.",
                plugin.name,
                dir.display()
            ));
        }
        let mut digests = serde_json::Map::new();
        for (env, built) in plugin.environments {
            // Only what's there: an image whose build failed partway claims nothing.
            if !built.prefix.is_dir() {
                say::warn(
                    "Skipped an environment that isn't built.",
                    &[
                        ("environment", &format!("{}.{env}", plugin.name)),
                        ("prefix", &built.prefix.display()),
                    ],
                );
                continue;
            }
            digests.insert(env.clone(), built.digest.clone().into());
            have.built.insert(format!("{}.{env}", plugin.name), built);
        }
        have.code.push(PluginCode {
            name: plugin.name,
            sha: plugin.sha,
            environments: Some(digests),
        });
    }
    have.code.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(have)
}

impl Have {
    /// What a worker fetched from the server and built: every environment must be built,
    /// as from `plugins.json`.
    pub fn fetched(
        code: Vec<PluginCode>,
        built: BTreeMap<String, Built>,
        dirs: BTreeMap<String, PathBuf>,
    ) -> Self {
        Self {
            code,
            built,
            from_manifest: true,
            dirs,
            image: None,
        }
    }

    /// What a worker in an environment's image has: that plugin's code, and the image.
    pub fn in_image(code: PluginCode, dir: PathBuf, environment: &str) -> Self {
        Self {
            dirs: BTreeMap::from([(code.name.clone(), dir)]),
            code: vec![code],
            built: BTreeMap::new(),
            from_manifest: true,
            image: Some(environment.to_owned()),
        }
    }

    /// Where `name`'s code is, if the worker fetched it.
    pub fn dir(&self, name: &str) -> Option<&Path> {
        self.dirs.get(name).map(PathBuf::as_path)
    }

    /// Why this worker can't run a run of `name` at `sha` in `environment`
    /// (`plugin.environment`), if it can't. Otherwise, the environment to run it in:
    /// `None` for the host's own software.
    pub fn for_run(
        &self,
        name: &str,
        sha: Option<&str>,
        environment: &str,
    ) -> Result<Option<&Built>, String> {
        let Some(plugin) = self.code.iter().find(|p| p.name == name) else {
            return Err(format!("This worker has no code for the plugin {name}."));
        };
        let shown =
            |sha: Option<&str>| sha.map_or("a local directory".to_owned(), |s| format!("{s:.12}"));
        if plugin.sha.as_deref() != sha {
            return Err(format!(
                "This worker has {name} from {}, not {}, which the run was launched with.",
                shown(plugin.sha.as_deref()),
                shown(sha)
            ));
        }
        if self.image.as_deref() == Some(environment) {
            return Ok(None);
        }
        match self.built.get(environment) {
            Some(built) => Ok(Some(built)),
            None if self.from_manifest => Err(format!(
                "This worker hasn't built the environment {environment}."
            )),
            None => Ok(None),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("iki-plugins-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn finds_local_plugins_by_their_directories() {
        let dir = scratch("local");
        for name in ["protein", "dna", "Not_A_Name", "no_manifest"] {
            std::fs::create_dir_all(dir.join(name)).unwrap();
            if name != "no_manifest" {
                std::fs::write(dir.join(name).join("plugin.yaml"), "").unwrap();
            }
        }
        let have = discover(&dir).unwrap();
        let names: Vec<&str> = have.code.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, ["dna", "protein"]);
        assert!(
            have.code
                .iter()
                .all(|p| p.sha.is_none() && p.environments.is_none())
        );
        // The host's own software.
        assert_eq!(have.for_run("dna", None, "dna.tools"), Ok(None));
        assert!(discover(&dir.join("missing")).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn reads_what_the_build_wrote() {
        let dir = scratch("built");
        let sha = "a".repeat(40);
        let built = dir.join("envs/dna/dna-tools");
        std::fs::create_dir_all(&built).unwrap();
        std::fs::create_dir_all(dir.join("dna")).unwrap();
        let digest = format!("sha256:{}", "b".repeat(64));
        let manifest = serde_json::json!({
            "apiVersion": "iki/v1",
            "kind": "WorkerPlugins",
            "plugins": [{
                "name": "dna", "version": "1.0.0", "sha": sha,
                "environments": {
                    "dna-tools": {"digest": digest, "kind": "python", "prefix": built},
                    "missing": {"digest": digest, "kind": "conda", "prefix": dir.join("nope")},
                },
            }],
        });
        std::fs::write(dir.join(MANIFEST), manifest.to_string()).unwrap();
        let have = discover(&dir).unwrap();
        assert_eq!(have.code.len(), 1);
        let listed = have.code[0].environments.clone().unwrap();
        assert_eq!(listed.keys().collect::<Vec<_>>(), ["dna-tools"]);
        let found = have
            .for_run("dna", Some(&sha), "dna.dna-tools")
            .unwrap()
            .unwrap();
        assert_eq!((found.kind, found.prefix.clone()), (Kind::Python, built));
        let missing = have.for_run("dna", Some(&sha), "dna.missing").unwrap_err();
        assert!(
            missing.contains("hasn't built the environment dna.missing"),
            "{missing}"
        );

        std::fs::write(
            dir.join(MANIFEST),
            r#"{"apiVersion": "iki/v1", "kind": "Other", "plugins": []}"#,
        )
        .unwrap();
        assert!(
            discover(&dir)
                .unwrap_err()
                .contains("not an iki/v1 WorkerPlugins")
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn plugin_names_follow_the_spec() {
        for good in ["dna", "structure_prediction", "p2"] {
            assert!(is_plugin_name(good), "{good}");
        }
        for bad in ["d", "DNA", "2dna", "dna_", "a__b", "dna-tools"] {
            assert!(!is_plugin_name(bad), "{bad}");
        }
    }

    #[test]
    fn runs_only_the_code_it_was_launched_with() {
        let have = Have {
            code: vec![PluginCode {
                name: "protein".into(),
                sha: Some("a".repeat(40)),
                environments: None,
            }],
            ..Have::default()
        };
        assert!(
            have.for_run("protein", Some(&"a".repeat(40)), "protein.t")
                .is_ok()
        );
        let other = have
            .for_run("protein", Some(&"b".repeat(40)), "protein.t")
            .unwrap_err();
        assert!(other.contains("aaaaaaaaaaaa, not bbbbbbbbbbbb"), "{other}");
        let local = have.for_run("protein", None, "protein.t").unwrap_err();
        assert!(local.contains("not a local directory"), "{local}");
        let none = have.for_run("dna", None, "dna.t").unwrap_err();
        assert!(none.contains("no code for the plugin dna"), "{none}");
    }
}

//! The plugin code a worker has, in `IKI_PLUGINS_DIR`: all it runs.
//!
//! The worker tells the server what it has when it starts, and leases only runs launched
//! with that very code (ADR 0047). What it has comes from one of two places:
//!
//! - **`plugins.json`**, from `iki deploy build --workers`: each plugin's name, version
//!   and commit, and the environments the image built, with each build's digest and
//!   prefix. A deployment's workers run this way.
//! - **Otherwise**, each directory holding a `plugin.yaml` is a plugin from a local
//!   directory, run with the host's own software, named as its `plugin.yaml` names it
//!   (`iki/protein`), whatever the directory is called. It runs only what a development
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
    /// `IKI_PLUGINS_DIR`, by label (see [`label`]).
    dirs: BTreeMap<String, PathBuf>,
    /// The environment whose image the worker runs in (ADR 0061), by
    /// `plugin.environment`: its commands run with the image's own software.
    image: Option<String>,
}

/// The file `iki deploy build --workers` writes in `IKI_PLUGINS_DIR`.
pub const MANIFEST: &str = "plugins.json";

/// A plugin's name, as `spec/` allows it (ADR 0113): a namespace and a name,
/// `iki/protein` or `acme/structure_prediction`.
pub fn is_plugin_name(name: &str) -> bool {
    name.split_once('/')
        .is_some_and(|(namespace, short)| is_part(namespace) && is_part(short))
}

/// One part of a plugin's name: 2 to 30 lower-case letters, digits and single
/// underscores, starting with a letter.
fn is_part(part: &str) -> bool {
    (2..=30).contains(&part.len())
        && part.starts_with(|c: char| c.is_ascii_lowercase())
        && !part.ends_with('_')
        && !part.contains("__")
        && part
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// A plugin's label, `namespace__name` (`iki__protein` for `iki/protein`): what names
/// it on disk, where a `/` would be another directory. `iki deploy build` names its
/// code's directory, and its environments', by it.
pub fn label(name: &str) -> String {
    name.replacen('/', "__", 1)
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
    let mut paths: Vec<PathBuf> = entries.flatten().map(|entry| entry.path()).collect();
    paths.sort();
    let mut code = Vec::new();
    let mut dirs = BTreeMap::new();
    for path in paths {
        let manifest = path.join("plugin.yaml");
        if !manifest.is_file() {
            continue;
        }
        let text = std::fs::read_to_string(&manifest).unwrap_or_default();
        let Some(name) = manifest_name(&text).filter(|name| is_plugin_name(name)) else {
            say::warn(
                "Skipped a plugin whose plugin.yaml doesn't name it as namespace/name.",
                &[("path", &path.display())],
            );
            continue;
        };
        if dirs.contains_key(&name) {
            say::warn(
                "Skipped a second directory holding the same plugin.",
                &[("plugin", &name), ("path", &path.display())],
            );
            continue;
        }
        dirs.insert(name.clone(), path);
        code.push(PluginCode {
            name,
            sha: None,
            environments: None,
            bundle: None,
        });
    }
    code.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(Have {
        code,
        dirs,
        ..Have::default()
    })
}

/// The `name` a `plugin.yaml` gives at its top level, if it gives one on a line of its
/// own: `name: iki/protein`, quoted or not, with or without a comment after it. The
/// worker reads no more YAML than that; the server checked the rest.
fn manifest_name(text: &str) -> Option<String> {
    let line = text.lines().find_map(|line| line.strip_prefix("name:"))?;
    let value = line.trim();
    let name = if let Some(rest) = value.strip_prefix('"') {
        &rest[..rest.find('"')?]
    } else if let Some(rest) = value.strip_prefix('\'') {
        &rest[..rest.find('\'')?]
    } else {
        value
            .find(" #")
            .map_or(value, |comment| &value[..comment])
            .trim_end()
    };
    Some(name.to_owned())
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
        if !is_plugin_name(&plugin.name) || !dir.join(label(&plugin.name)).is_dir() {
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
            bundle: None,
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
        // Each named as its plugin.yaml says, whatever its directory is called.
        for (directory, manifest) in [
            ("protein", Some("name: iki/protein\n")),
            ("dna", Some("name: acme/dna\n")),
            ("copy_of_dna", Some("name: acme/dna\n")),
            ("unnamed", Some("name: dna\n")),
            ("no_manifest", None),
        ] {
            std::fs::create_dir_all(dir.join(directory)).unwrap();
            if let Some(text) = manifest {
                std::fs::write(dir.join(directory).join("plugin.yaml"), text).unwrap();
            }
        }
        let have = discover(&dir).unwrap();
        let names: Vec<&str> = have.code.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, ["acme/dna", "iki/protein"]);
        // Found where they are: the first directory, by name, for a plugin in two.
        assert_eq!(have.dir("iki/protein"), Some(dir.join("protein").as_path()));
        assert_eq!(
            have.dir("acme/dna"),
            Some(dir.join("copy_of_dna").as_path())
        );
        assert!(
            have.code
                .iter()
                .all(|p| p.sha.is_none() && p.environments.is_none())
        );
        // The host's own software.
        assert_eq!(have.for_run("acme/dna", None, "acme/dna.tools"), Ok(None));
        assert!(discover(&dir.join("missing")).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn reads_what_the_build_wrote() {
        let dir = scratch("built");
        let sha = "a".repeat(40);
        let built = dir.join("envs/iki__dna/dna-tools");
        std::fs::create_dir_all(&built).unwrap();
        std::fs::create_dir_all(dir.join("iki__dna")).unwrap();
        let digest = format!("sha256:{}", "b".repeat(64));
        let manifest = serde_json::json!({
            "apiVersion": "iki/v1",
            "kind": "WorkerPlugins",
            "plugins": [{
                "name": "iki/dna", "version": "1.0.0", "sha": sha,
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
            .for_run("iki/dna", Some(&sha), "iki/dna.dna-tools")
            .unwrap()
            .unwrap();
        assert_eq!((found.kind, found.prefix.clone()), (Kind::Python, built));
        let missing = have
            .for_run("iki/dna", Some(&sha), "iki/dna.missing")
            .unwrap_err();
        assert!(
            missing.contains("hasn't built the environment iki/dna.missing"),
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
        for good in [
            "iki/protein",
            "acme/assays",
            "acme/structure_prediction",
            "p2/x9",
        ] {
            assert!(is_plugin_name(good), "{good}");
        }
        for bad in [
            "protein",
            "a/b/c",
            "Acme/x",
            "../x",
            "iki/",
            "/protein",
            "iki/d",
            "acme/dna_",
            "acme/a__b",
            "acme/dna-tools",
            "acme//dna",
            "iki/protein/../x",
        ] {
            assert!(!is_plugin_name(bad), "{bad}");
        }
    }

    #[test]
    fn a_plugin_is_named_on_disk_by_its_label() {
        assert_eq!(label("iki/protein"), "iki__protein");
        assert_eq!(
            label("acme/structure_prediction"),
            "acme__structure_prediction"
        );
    }

    #[test]
    fn reads_a_local_plugins_name_from_its_manifest() {
        for (text, name) in [
            ("apiVersion: iki/v1\nname: iki/dna\n", Some("iki/dna")),
            (
                "# a comment\nname: \"acme/lab\"  # quoted\n",
                Some("acme/lab"),
            ),
            ("name: 'acme/lab'\n", Some("acme/lab")),
            ("name: acme/lab # mine\n", Some("acme/lab")),
            ("settings:\n  name: acme/x\n", None),
            ("title: x\n", None),
        ] {
            assert_eq!(manifest_name(text).as_deref(), name, "{text}");
        }
    }

    #[test]
    fn runs_only_the_code_it_was_launched_with() {
        let have = Have {
            code: vec![PluginCode {
                name: "iki/protein".into(),
                sha: Some("a".repeat(40)),
                environments: None,
                bundle: None,
            }],
            ..Have::default()
        };
        assert!(
            have.for_run("iki/protein", Some(&"a".repeat(40)), "iki/protein.t")
                .is_ok()
        );
        let other = have
            .for_run("iki/protein", Some(&"b".repeat(40)), "iki/protein.t")
            .unwrap_err();
        assert!(other.contains("aaaaaaaaaaaa, not bbbbbbbbbbbb"), "{other}");
        let local = have
            .for_run("iki/protein", None, "iki/protein.t")
            .unwrap_err();
        assert!(local.contains("not a local directory"), "{local}");
        let none = have.for_run("iki/dna", None, "iki/dna.t").unwrap_err();
        assert!(none.contains("no code for the plugin iki/dna"), "{none}");
    }
}

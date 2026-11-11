//! The plugin code a worker has, in `IKI_PLUGINS_DIR`: all it runs.
//!
//! The worker tells the server what it has when it starts, and leases only runs launched
//! with that very code (ADR 0047). Each directory holding a `plugin.yaml` is a plugin from
//! a local directory, run with the host's own software: it runs only what a development
//! server launches, whose plugins are local too.

use std::path::Path;

use iki_protocol::PluginCode;

use crate::say;

/// A plugin's name, as `spec/` allows it: `protein`, `structure_prediction`.
fn is_plugin_name(name: &str) -> bool {
    (2..=30).contains(&name.len())
        && name.starts_with(|c: char| c.is_ascii_lowercase())
        && !name.ends_with('_')
        && !name.contains("__")
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// What `dir` holds, by name.
pub fn discover(dir: &Path) -> Result<Vec<PluginCode>, String> {
    let entries = std::fs::read_dir(dir).map_err(|error| {
        format!(
            "Couldn't read IKI_PLUGINS_DIR ({}): {error}.",
            dir.display()
        )
    })?;
    let mut found = Vec::new();
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
        found.push(PluginCode {
            name,
            sha: None,
            environments: None,
        });
    }
    found.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(found)
}

/// Why this worker can't run a run of `name` at `sha`, if it can't.
pub fn mismatch(have: &[PluginCode], name: &str, sha: Option<&str>) -> Option<String> {
    let Some(plugin) = have.iter().find(|p| p.name == name) else {
        return Some(format!("This worker has no code for the plugin {name}."));
    };
    let shown =
        |sha: Option<&str>| sha.map_or("a local directory".to_owned(), |s| format!("{s:.12}"));
    (plugin.sha.as_deref() != sha).then(|| {
        format!(
            "This worker has {name} from {}, not {}, which the run was launched with.",
            shown(plugin.sha.as_deref()),
            shown(sha)
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_plugins_by_their_directories() {
        let dir = std::env::temp_dir().join(format!("iki-plugins-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        for name in ["protein", "dna", "Not_A_Name", "no_manifest"] {
            std::fs::create_dir_all(dir.join(name)).unwrap();
            if name != "no_manifest" {
                std::fs::write(dir.join(name).join("plugin.yaml"), "").unwrap();
            }
        }
        let found = discover(&dir).unwrap();
        let names: Vec<&str> = found.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, ["dna", "protein"]);
        assert!(
            found
                .iter()
                .all(|p| p.sha.is_none() && p.environments.is_none())
        );
        assert!(discover(&dir.join("missing")).is_err());
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
        let have = [PluginCode {
            name: "protein".into(),
            sha: Some("a".repeat(40)),
            environments: None,
        }];
        assert!(mismatch(&have, "protein", Some(&"a".repeat(40))).is_none());
        let other = mismatch(&have, "protein", Some(&"b".repeat(40))).unwrap();
        assert!(other.contains("aaaaaaaaaaaa, not bbbbbbbbbbbb"), "{other}");
        assert!(
            mismatch(&have, "protein", None)
                .unwrap()
                .contains("not a local directory")
        );
        assert!(
            mismatch(&have, "dna", None)
                .unwrap()
                .contains("no code for the plugin dna")
        );
    }
}

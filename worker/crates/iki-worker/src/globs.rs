//! The glob patterns an action's `artifacts.keep` lists, matched as Iki matches them
//! (`iki_spec.globs`): against a path relative to the output directory, one segment at
//! a time. `*` and `?` match within a segment, `[...]` is a set of characters (`[!...]`
//! the rest), and a `**` segment matches any number of segments, none included.

/// Whether `pattern` matches `path`, both `/`-separated.
pub fn matches(pattern: &str, path: &str) -> bool {
    let pattern: Vec<&str> = pattern.split('/').collect();
    let path: Vec<&str> = path.split('/').collect();
    segments(&pattern, &path)
}

fn segments(pattern: &[&str], path: &[&str]) -> bool {
    match pattern.split_first() {
        None => path.is_empty(),
        Some((&"**", rest)) => (0..=path.len()).any(|i| segments(rest, &path[i..])),
        Some((head, rest)) => path
            .split_first()
            .is_some_and(|(name, more)| segment(head, name) && segments(rest, more)),
    }
}

/// One segment, `*` matching any run of characters in it.
fn segment(pattern: &str, name: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let name: Vec<char> = name.chars().collect();
    let (mut p, mut n) = (0, 0);
    // Where the last `*` was, and the name position it has matched up to.
    let mut star: Option<(usize, usize)> = None;
    while n < name.len() {
        if p < pattern.len() && pattern[p] == '*' {
            star = Some((p, n));
            p += 1;
            continue;
        }
        if p < pattern.len()
            && let Some(width) = one(&pattern[p..], name[n])
        {
            p += width;
            n += 1;
            continue;
        }
        match star {
            Some((at, matched)) => {
                p = at + 1;
                n = matched + 1;
                star = Some((at, matched + 1));
            }
            None => return false,
        }
    }
    pattern[p..].iter().all(|&c| c == '*')
}

/// If the pattern's next token matches `c`, how many of its characters that token is.
fn one(pattern: &[char], c: char) -> Option<usize> {
    match pattern[0] {
        '?' => Some(1),
        '[' => match set(pattern) {
            Some((width, matched)) => matched(c).then_some(width),
            // An unclosed `[` is itself, as fnmatch reads it.
            None => (c == '[').then_some(1),
        },
        literal => (literal == c).then_some(1),
    }
}

/// A `[...]` set at the start of `pattern`: its width, and what it matches.
fn set(pattern: &[char]) -> Option<(usize, impl Fn(char) -> bool + '_)> {
    let mut i = 1;
    let negated = matches!(pattern.get(i), Some('!'));
    if negated {
        i += 1;
    }
    let start = i;
    // A `]` first in the set is one of its characters.
    if pattern.get(i) == Some(&']') {
        i += 1;
    }
    while i < pattern.len() && pattern[i] != ']' {
        i += 1;
    }
    if i >= pattern.len() {
        return None;
    }
    let members = &pattern[start..i];
    let matched = move |c: char| {
        let mut j = 0;
        let mut found = false;
        while j < members.len() {
            if j + 2 < members.len() && members[j + 1] == '-' {
                found |= members[j] <= c && c <= members[j + 2];
                j += 3;
            } else {
                found |= members[j] == c;
                j += 1;
            }
        }
        found != negated
    };
    Some((i + 1, matched))
}

#[cfg(test)]
mod tests {
    use super::matches;

    #[test]
    fn match_a_segment_at_a_time_as_iki_does() {
        // The same cases as spec/tests/test_globs.py.
        for (pattern, path, expected) in [
            ("ranking_debug.json", "ranking_debug.json", true),
            ("ranking_debug.json", "x/ranking_debug.json", false),
            ("*.a3m", "msa.a3m", true),
            ("*.a3m", "msas/msa.a3m", false),
            ("msas/**", "msas/a.a3m", true),
            ("msas/**", "msas/deep/b.a3m", true),
            ("msas/**", "msas", true),
            ("msas/**", "other/a.a3m", false),
            ("**/*.log", "run.log", true),
            ("**/*.log", "a/b/run.log", true),
            ("model_?.pdb", "model_1.pdb", true),
            ("model_?.pdb", "model_10.pdb", false),
            ("model_[0-4].pdb", "model_3.pdb", true),
            ("model_[!0-4].pdb", "model_3.pdb", false),
            ("alignment.*", "alignment.txt", true),
            ("a*b*c", "aXbYbc", true),
            ("a*b*c", "aXbY", false),
            ("[ab", "[ab", true),
        ] {
            assert_eq!(matches(pattern, path), expected, "{pattern} ~ {path}");
        }
    }
}

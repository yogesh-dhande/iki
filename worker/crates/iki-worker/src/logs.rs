//! The command's output on its way to the server: buffered, sent in ordered chunks with
//! byte offsets, and sent again unchanged until the server has it (ADR 0043). `::progress`
//! lines are picked out on the way.

use std::sync::{Mutex, PoisonError};

/// The most sent in one request: the server takes up to 256 KiB a chunk.
pub const CHUNK: usize = 256 * 1024;
/// The most held while the server can't be reached. The server keeps 16 MiB a job, so
/// a longer outage loses only what it wouldn't have kept.
pub const MAX_PENDING: usize = 16 * 1024 * 1024;

#[derive(Debug, Default)]
struct State {
    /// Bytes the server has.
    acked: u64,
    /// What it doesn't have yet, from `acked`.
    pending: String,
    /// The length of the chunk on its way, so a retry sends exactly the same bytes.
    in_flight: Option<usize>,
    /// Bytes dropped since the last chunk, while the buffer was full.
    dropped: u64,
}

/// A job's log. Readers push; one sender takes chunks and acknowledges them.
#[derive(Debug, Default)]
pub struct Log {
    state: Mutex<State>,
}

fn boundary(text: &str, at: usize) -> usize {
    let mut end = at.min(text.len());
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    end
}

impl Log {
    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Adds what the command wrote.
    pub fn push(&self, text: &str) {
        let mut state = self.lock();
        if state.pending.len() + text.len() > MAX_PENDING {
            state.dropped += text.len() as u64;
            return;
        }
        if state.dropped > 0 {
            let note = format!(
                "[iki-worker: {} bytes of output dropped while the server couldn't be reached]\n",
                state.dropped
            );
            state.pending.push_str(&note);
            state.dropped = 0;
        }
        state.pending.push_str(text);
    }

    /// The next chunk to send, as (offset, text): the same one again until it's
    /// acknowledged.
    pub fn next_chunk(&self) -> Option<(u64, String)> {
        let mut state = self.lock();
        if state.pending.is_empty() {
            return None;
        }
        let size = state
            .in_flight
            .unwrap_or_else(|| boundary(&state.pending, CHUNK));
        state.in_flight = Some(size);
        Some((state.acked, state.pending[..size].to_owned()))
    }

    /// The server has everything before `next_offset`.
    pub fn acknowledge(&self, next_offset: u64) {
        let mut state = self.lock();
        state.in_flight = None;
        let Some(ahead) = next_offset.checked_sub(state.acked) else {
            return;
        };
        let ahead = usize::try_from(ahead).unwrap_or(usize::MAX);
        let cut = boundary(&state.pending, ahead);
        state.pending.drain(..cut);
        state.acked += cut as u64;
    }

    /// Whether everything pushed has been acknowledged.
    #[cfg(test)]
    pub fn is_sent(&self) -> bool {
        let state = self.lock();
        state.pending.is_empty() && state.dropped == 0
    }
}

/// Progress a command reported with a line `::progress 0.4 Folding chain A`, as a fraction
/// from 0 to 1 and an optional message. Percentages (`::progress 40%`) work too.
pub fn progress(line: &str) -> Option<(f64, String)> {
    let rest = line
        .trim_end_matches(['\r', '\n'])
        .strip_prefix("::progress ")?;
    let (number, message) = rest
        .trim_start()
        .split_once(' ')
        .unwrap_or((rest.trim(), ""));
    let fraction = match number.strip_suffix('%') {
        Some(percent) => percent.parse::<f64>().ok()? / 100.0,
        None => number.parse::<f64>().ok()?,
    };
    if !fraction.is_finite() {
        return None;
    }
    let message: String = message.trim().chars().take(500).collect();
    Some((fraction.clamp(0.0, 1.0), message))
}

/// Why a command says it failed, from a line `::error Record 3 has no bases`, as GitHub
/// Actions' `::error::` lines do. The worker reports the last one if the command fails.
pub fn error(line: &str) -> Option<String> {
    let text = line
        .trim_end_matches(['\r', '\n'])
        .strip_prefix("::error ")?
        .trim();
    if text.is_empty() {
        return None;
    }
    Some(text.chars().take(1000).collect())
}

/// Hides secrets' values in what a command prints, as GitHub Actions does: each line of
/// each secret becomes `***`, longest first, so a secret that holds another is hidden
/// whole. Values under three characters would hide ordinary text, so they aren't masked.
#[derive(Clone, Default)]
pub struct Mask {
    values: Vec<String>,
}

impl Mask {
    pub fn new<'a>(secrets: impl IntoIterator<Item = &'a str>) -> Self {
        let mut values: Vec<String> = secrets
            .into_iter()
            .flat_map(str::lines)
            .map(str::trim)
            .filter(|v| v.chars().count() >= 3)
            .map(str::to_owned)
            .collect();
        values.sort_by_key(|v| std::cmp::Reverse(v.len()));
        values.dedup();
        Mask { values }
    }

    pub fn apply<'a>(&self, text: &'a str) -> std::borrow::Cow<'a, str> {
        let mut masked = std::borrow::Cow::Borrowed(text);
        for value in &self.values {
            if masked.contains(value.as_str()) {
                masked = std::borrow::Cow::Owned(masked.replace(value.as_str(), "***"));
            }
        }
        masked
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hides_secrets_in_what_a_command_prints() {
        let mask = Mask::new([
            "s3cr3t-key",
            "ab",
            "-----BEGIN KEY-----\nMIIEv\n-----END KEY-----",
        ]);
        assert_eq!(mask.apply("key=s3cr3t-key&x=1"), "key=***&x=1");
        assert_eq!(mask.apply("MIIEv"), "***");
        // Too short to mask without hiding ordinary text.
        assert_eq!(mask.apply("about"), "about");
        assert!(matches!(
            mask.apply("nothing here"),
            std::borrow::Cow::Borrowed(_)
        ));
        let nested = Mask::new(["abc", "xabcx"]);
        assert_eq!(nested.apply("xabcx abc"), "*** ***");
    }

    #[test]
    fn sends_the_same_chunk_until_it_is_acknowledged() {
        let log = Log::default();
        log.push("héllo\n");
        assert_eq!(log.next_chunk(), Some((0, "héllo\n".into())));
        log.push("world\n");
        // A retry after a timeout: same bytes, so the server sees a repeat, not an overlap.
        assert_eq!(log.next_chunk(), Some((0, "héllo\n".into())));
        log.acknowledge(7);
        assert_eq!(log.next_chunk(), Some((7, "world\n".into())));
        log.acknowledge(13);
        assert_eq!(log.next_chunk(), None);
        assert!(log.is_sent());
    }

    #[test]
    fn chunks_split_on_character_boundaries() {
        let log = Log::default();
        log.push(&"é".repeat(CHUNK));
        let (_, chunk) = log.next_chunk().unwrap();
        assert_eq!(chunk.len(), CHUNK);
        log.acknowledge(chunk.len() as u64);
        assert_eq!(log.next_chunk().unwrap().0, CHUNK as u64);
    }

    #[test]
    fn a_full_buffer_drops_and_says_so() {
        let log = Log::default();
        log.push(&"x".repeat(MAX_PENDING));
        log.push("lost\n");
        assert!(!log.is_sent());
        log.acknowledge(MAX_PENDING as u64);
        log.push("next\n");
        let (_, chunk) = log.next_chunk().unwrap();
        assert!(chunk.starts_with("[iki-worker: 5 bytes of output dropped"));
        assert!(chunk.ends_with("next\n"));
    }

    #[test]
    fn an_acknowledgement_past_the_chunk_skips_ahead() {
        // The server says it already has more (a log_gap after a lost answer).
        let log = Log::default();
        log.push("abcdef");
        let _ = log.next_chunk();
        log.acknowledge(4);
        assert_eq!(log.next_chunk(), Some((4, "ef".into())));
        log.acknowledge(1); // behind: ignored
        assert_eq!(log.next_chunk(), Some((4, "ef".into())));
    }

    #[test]
    fn reads_progress_lines() {
        assert_eq!(
            progress("::progress 0.4 Folding chain A\n"),
            Some((0.4, "Folding chain A".into()))
        );
        assert_eq!(progress("::progress 40%"), Some((0.4, String::new())));
        assert_eq!(progress("::progress 7"), Some((1.0, String::new())));
        assert_eq!(progress("::progress NaN"), None);
    }

    #[test]
    fn reads_why_a_command_failed() {
        assert_eq!(
            error("::error Record 3 (pUC19) has no bases\n"),
            Some("Record 3 (pUC19) has no bases".to_owned())
        );
        assert_eq!(error("::error   "), None);
        assert_eq!(error("error: no"), None);
        assert_eq!(
            error(&format!("::error {}", "x".repeat(5000))).map(|e| e.len()),
            Some(1000)
        );
        assert_eq!(progress("::progress soon"), None);
        assert_eq!(progress("progress 0.4"), None);
    }
}

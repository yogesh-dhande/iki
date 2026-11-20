//! The command's output on its way to the server: buffered, sent in ordered chunks with
//! byte offsets, and sent again unchanged until the server has it (ADR 0043). Each chunk
//! ends where a line does, unless one line is longer than a chunk. `::progress` lines are
//! picked out on the way.
//!
//! A log read from a file (a Slurm job's, ADR 0133) also knows where in the file its lines
//! ended, and the last `::error` line before each point, so a worker that restarts can
//! carry on where the server's copy ends, reading the file from there rather than from its
//! start (`Mark`, `Marks`). Only such a log keeps these: they cost the others nothing.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, PoisonError};

use serde::{Deserialize, Serialize};

/// The most sent in one request: the server takes up to 256 KiB a chunk.
pub const CHUNK: usize = 256 * 1024;
/// The most held while the server can't be reached. The server keeps 16 MiB a job, so
/// a longer outage loses only what it wouldn't have kept.
pub const MAX_PENDING: usize = 16 * 1024 * 1024;
/// A log that keeps marks keeps one for each run of lines up to this long: a chunk ends
/// where one does, and so many short lines cost a mark each only every few KiB.
const MARK_EVERY: u64 = 4 * 1024;

/// A point in the log, and in the file it's read from: everything before `sent` bytes of
/// the log holds what the file has before `read`, and `error` is why the command said it
/// failed in the last `::error` line before it, if any (`error`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Mark {
    pub sent: u64,
    pub read: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Where a log stands, kept before each chunk is sent: what the server had, and what it
/// will have once the chunk on its way arrives. Whichever the server's copy ends at says
/// where to carry on after a restart.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Marks {
    pub acked: Mark,
    pub sending: Option<Mark>,
}

impl Marks {
    /// Where to carry on, given that the server has `end` bytes of the log (`None`: it
    /// didn't say, as a server older than `log_offset` doesn't). Unless `end` is one of the
    /// two marks, the nearest before it: the server then says it has more, and the worker
    /// skips ahead.
    pub fn resume_at(&self, end: Option<u64>) -> Mark {
        let Some(end) = end else {
            return self.acked.clone();
        };
        [self.sending.as_ref(), Some(&self.acked)]
            .into_iter()
            .flatten()
            .filter(|mark| mark.sent <= end)
            .max_by_key(|mark| mark.sent)
            .cloned()
            .unwrap_or_default()
    }
}

/// Where a run of pushes ends: in the log, in the file it was read from (unchanged for the
/// worker's own lines), and the last `::error` line up to there.
#[derive(Debug, Clone)]
struct Segment {
    end: u64,
    read: u64,
    error: Option<Arc<str>>,
}

impl Segment {
    fn mark(&self) -> Mark {
        Mark {
            sent: self.end,
            read: self.read,
            error: self.error.as_deref().map(str::to_owned),
        }
    }
}

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
    /// Whether it keeps marks (`Log::keep_marks`).
    marking: bool,
    /// For a log that keeps marks: where the pending pushes end, in order, a run of short
    /// ones in one.
    segments: VecDeque<Segment>,
    /// How far the file has been read.
    read: u64,
    /// The last `::error` line the command printed.
    error: Option<Arc<str>>,
    /// The last segment the server has all of.
    mark: Mark,
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

    /// Keeps marks from now on, before anything is pushed: for a log read from a file that
    /// a restarted worker reads again (`marks`).
    pub fn keep_marks(&self) {
        let mut state = self.lock();
        debug_assert!(state.pending.is_empty());
        state.marking = true;
    }

    /// Starts the log at `mark`, before anything is pushed: the server has `mark.sent`
    /// bytes, which hold the file up to `mark.read`.
    pub fn resume(&self, mark: Mark) {
        let mut state = self.lock();
        debug_assert!(state.pending.is_empty());
        state.acked = mark.sent;
        state.read = mark.read;
        state.error = mark.error.as_deref().map(Arc::from);
        state.mark = mark;
    }

    /// Adds a line of the worker's own.
    pub fn push(&self, text: &str) {
        let mut state = self.lock();
        let read = state.read;
        Self::add(&mut state, text, read);
    }

    /// Adds what the command wrote, read from a file up to `read`.
    pub fn push_read(&self, text: &str, read: u64) {
        let mut state = self.lock();
        if state.marking
            && let Some(why) = error(text)
        {
            state.error = Some(Arc::from(why));
        }
        Self::add(&mut state, text, read);
    }

    fn add(state: &mut State, text: &str, read: u64) {
        state.read = state.read.max(read);
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
        if !state.marking {
            return;
        }
        let end = state.acked + state.pending.len() as u64;
        // A run of short pushes shares a segment, unless the chunk on its way ends where
        // the segment does now.
        let sending_to = state.acked + state.in_flight.unwrap_or(0) as u64;
        let count = state.segments.len();
        let start = count
            .checked_sub(2)
            .map_or(state.acked, |before| state.segments[before].end);
        let (read, error) = (state.read, state.error.clone());
        match state.segments.back_mut() {
            Some(last) if last.end > sending_to && end - start <= MARK_EVERY => {
                last.end = end;
                last.read = read;
                last.error = error;
            }
            _ => state.segments.push_back(Segment { end, read, error }),
        }
    }

    /// The next chunk to send, as (offset, text): the same one again until it's
    /// acknowledged. It ends where a line ends (for a log that keeps marks, where a
    /// segment does), unless one is longer than a chunk.
    pub fn next_chunk(&self) -> Option<(u64, String)> {
        let mut state = self.lock();
        if state.pending.is_empty() {
            return None;
        }
        let size = state.in_flight.unwrap_or_else(|| {
            let fits = if state.marking {
                state
                    .segments
                    .iter()
                    .map(|s| s.end - state.acked)
                    .take_while(|&end| end <= CHUNK as u64)
                    .last()
                    .and_then(|end| usize::try_from(end).ok())
            } else if state.pending.len() <= CHUNK {
                Some(state.pending.len())
            } else {
                state.pending.as_bytes()[..CHUNK]
                    .iter()
                    .rposition(|&b| b == b'\n')
                    .map(|at| at + 1)
            };
            fits.unwrap_or_else(|| boundary(&state.pending, CHUNK))
        });
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
        let mut reached = None;
        while state.segments.front().is_some_and(|s| s.end <= state.acked) {
            reached = state.segments.pop_front();
        }
        if let Some(segment) = reached {
            state.mark = segment.mark();
        }
    }

    /// Where the log stands, for a log that keeps marks: what the server has, and, with a
    /// chunk on its way that ends where a segment does, what it will have.
    pub fn marks(&self) -> Marks {
        let state = self.lock();
        let sending = state.in_flight.and_then(|size| {
            let end = state.acked + size as u64;
            state
                .segments
                .iter()
                .find(|s| s.end == end)
                .map(Segment::mark)
        });
        Marks {
            acked: state.mark.clone(),
            sending,
        }
    }

    /// Whether everything pushed has been acknowledged.
    #[cfg(test)]
    pub fn is_sent(&self) -> bool {
        let state = self.lock();
        state.pending.is_empty() && state.dropped == 0
    }

    /// How many segments it holds.
    #[cfg(test)]
    fn segments(&self) -> usize {
        self.lock().segments.len()
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

    /// Where `raw`, a stream's bytes so far, can be cut, for what's before it to be masked
    /// and written while the rest waits for more: no secret, or the start of one that more
    /// bytes might finish, crosses it, nor does a character.
    fn cut(&self, raw: &[u8]) -> usize {
        let longest = self.values.first().map_or(0, String::len);
        let mut cut = raw.len().saturating_sub(longest.saturating_sub(1));
        loop {
            let before = cut;
            cut = whole_characters(raw, cut);
            // Not inside a secret: before the first one that crosses it.
            if let Some(start) = self
                .values
                .iter()
                .filter_map(|value| {
                    let value = value.as_bytes();
                    (cut.saturating_sub(value.len() - 1)..cut)
                        .find(|&at| raw[at..].starts_with(value))
                })
                .min()
            {
                cut = start;
            }
            if cut == before {
                return cut;
            }
        }
    }
}

/// Where the last whole character of `raw` before `cut` ends: `cut`, unless a character
/// starts before it and ends after it, or isn't all there yet.
fn whole_characters(raw: &[u8], cut: usize) -> usize {
    let mut start = cut;
    while start > 0 && cut - start < 3 && raw[start - 1] & 0xC0 == 0x80 {
        start -= 1;
    }
    let Some(lead) = start.checked_sub(1) else {
        return cut;
    };
    let width = match raw[lead] {
        0xC0..=0xDF => 2,
        0xE0..=0xEF => 3,
        0xF0..=0xF7 => 4,
        _ => 1,
    };
    if lead + width > cut { lead } else { cut }
}

/// What a command prints, masked as it's read, piece by piece. A piece that ends a line is
/// masked whole. One that doesn't (a line longer than a read) is masked up to where no
/// secret could cross into the next piece, and the rest waits for it, so a secret is
/// hidden wherever the reads split it.
pub struct Masking {
    mask: Mask,
    held: Vec<u8>,
}

impl Masking {
    pub fn new(mask: Mask) -> Self {
        Self {
            mask,
            held: Vec::new(),
        }
    }

    /// The next piece the stream holds. Returns the masked text to write, if there's any
    /// yet, and how many of the stream's bytes it holds.
    pub fn piece(&mut self, bytes: &[u8]) -> Option<(String, u64)> {
        self.held.extend_from_slice(bytes);
        let cut = if self.held.ends_with(b"\n") {
            self.held.len()
        } else {
            self.mask.cut(&self.held)
        };
        self.take(cut)
    }

    /// The stream ended: the rest.
    pub fn end(&mut self) -> Option<(String, u64)> {
        self.take(self.held.len())
    }

    fn take(&mut self, cut: usize) -> Option<(String, u64)> {
        if cut == 0 {
            return None;
        }
        let raw: Vec<u8> = self.held.drain(..cut).collect();
        let text = String::from_utf8_lossy(&raw);
        Some((self.mask.apply(&text).into_owned(), cut as u64))
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
    fn a_chunk_ends_where_a_push_does() {
        let log = Log::default();
        let line = format!("{}\n", "x".repeat(CHUNK / 2 - 1));
        log.push(&line);
        log.push(&line);
        log.push(&line);
        // Two whole lines fit in a chunk; a third would split.
        let (_, chunk) = log.next_chunk().unwrap();
        assert_eq!(chunk.len(), 2 * line.len());
    }

    fn at(sent: u64, read: u64) -> Mark {
        Mark {
            sent,
            read,
            error: None,
        }
    }

    fn marking() -> Log {
        let log = Log::default();
        log.keep_marks();
        log
    }

    #[test]
    fn marks_say_how_much_of_the_file_the_server_has() {
        let log = marking();
        log.push_read("one\n", 4);
        log.push("iki-worker: a note\n");
        log.push_read("two\n", 8);
        assert_eq!(log.marks(), Marks::default());
        let (offset, chunk) = log.next_chunk().unwrap();
        let end = offset + chunk.len() as u64;
        // On its way: once it's there, the server has the file up to byte 8.
        let marks = log.marks();
        assert_eq!(marks.acked, Mark::default());
        assert_eq!(marks.sending, Some(at(end, 8)));
        log.acknowledge(end);
        assert_eq!(log.marks().acked, at(end, 8));
        assert_eq!(log.marks().sending, None);
        // Part of a push isn't a mark: the last whole one is.
        log.push_read("three\n", 14);
        let _ = log.next_chunk();
        log.acknowledge(end + 2);
        assert_eq!(log.marks().acked, at(end, 8));
    }

    #[test]
    fn a_restarted_log_carries_on_where_the_server_has_it() {
        let marks = Marks {
            acked: at(10, 6),
            sending: Some(at(30, 26)),
        };
        // The server got the chunk on its way, or didn't.
        assert_eq!(marks.resume_at(Some(30)), at(30, 26));
        assert_eq!(marks.resume_at(Some(10)), at(10, 6));
        // Somewhere else: the nearest before it, and the server says to skip ahead.
        assert_eq!(marks.resume_at(Some(20)), at(10, 6));
        assert_eq!(marks.resume_at(Some(5)), Mark::default());
        // A server that doesn't say: what it last acknowledged.
        assert_eq!(marks.resume_at(None), at(10, 6));
        let log = marking();
        log.resume(at(30, 26));
        log.push_read("rest\n", 31);
        assert_eq!(log.next_chunk(), Some((30, "rest\n".into())));
        log.acknowledge(35);
        assert_eq!(log.marks().acked, at(35, 31));
    }

    #[test]
    fn a_mark_keeps_why_the_command_said_it_failed() {
        let log = marking();
        log.push_read("::error Record 3 (pUC19) has no bases\n", 39);
        log.push_read("cleaning up\n", 51);
        let (offset, chunk) = log.next_chunk().unwrap();
        log.acknowledge(offset + chunk.len() as u64);
        let acked = log.marks().acked;
        assert_eq!(
            acked.error.as_deref(),
            Some("Record 3 (pUC19) has no bases")
        );
        assert_eq!(acked.read, 51);
        // In the note a restarted worker reads...
        let kept: Marks =
            serde_json::from_slice(&serde_json::to_vec(&log.marks()).unwrap()).unwrap();
        assert_eq!(kept.acked, acked);
        // ...and in the marks after it, until the command says otherwise.
        let again = marking();
        again.resume(kept.resume_at(Some(acked.sent)));
        again.push_read("still cleaning up\n", 69);
        let (offset, chunk) = again.next_chunk().unwrap();
        again.acknowledge(offset + chunk.len() as u64);
        assert_eq!(again.marks().acked.error, acked.error);
        // A note from before marks kept errors reads as having none.
        let old: Marks =
            serde_json::from_str(r#"{"acked": {"sent": 10, "read": 6}, "sending": null}"#).unwrap();
        assert_eq!(old.acked, at(10, 6));
    }

    #[test]
    fn only_a_log_that_keeps_marks_pays_for_them() {
        // Short lines, as a command printing a count per record does.
        let lines = 8192;
        let plain = Log::default();
        let marked = marking();
        for n in 0..lines {
            plain.push(&format!("{n:07}\n"));
            marked.push_read(&format!("{n:07}\n"), (n + 1) * 8);
        }
        assert_eq!(plain.segments(), 0);
        // A segment each few KiB, not a line: 64 KiB of lines in 16.
        let bytes = usize::try_from(lines * 8).unwrap();
        assert!(
            marked.segments() <= bytes.div_ceil(4096),
            "{}",
            marked.segments()
        );
        // Both still send every line, each chunk ending where a line does.
        for log in [&plain, &marked] {
            let mut sent = String::new();
            while let Some((offset, chunk)) = log.next_chunk() {
                assert!(chunk.ends_with('\n'));
                log.acknowledge(offset + chunk.len() as u64);
                sent.push_str(&chunk);
            }
            assert_eq!(sent.len(), bytes);
        }
        assert_eq!(marked.marks().acked, at(lines * 8, lines * 8));
    }

    #[test]
    fn a_chunk_on_its_way_ends_its_segment() {
        let log = marking();
        log.push_read("one\n", 4);
        let (_, chunk) = log.next_chunk().unwrap();
        // Pushed while it's on its way: a segment of its own, so the mark on its way stays
        // where the chunk ends.
        log.push_read("two\n", 8);
        assert_eq!(log.marks().sending, Some(at(4, 4)));
        log.acknowledge(chunk.len() as u64);
        assert_eq!(log.marks().acked, at(4, 4));
        let (offset, chunk) = log.next_chunk().unwrap();
        assert_eq!((offset, chunk.as_str()), (4, "two\n"));
    }

    /// Feeds `text` to a `Masking` in pieces of `size` bytes, as `job::each_line` reads a
    /// line longer than it reads at once, and returns what it writes and how many bytes
    /// that holds.
    fn masked_in_pieces(mask: Mask, text: &[u8], size: usize) -> (String, u64) {
        let mut masking = Masking::new(mask);
        let (mut out, mut held) = (String::new(), 0);
        for piece in text.chunks(size) {
            if let Some((written, bytes)) = masking.piece(piece) {
                out.push_str(&written);
                held += bytes;
            }
        }
        if let Some((written, bytes)) = masking.end() {
            out.push_str(&written);
            held += bytes;
        }
        (out, held)
    }

    #[test]
    fn a_secret_split_between_reads_is_still_hidden() {
        let secret = "iki-sentinel-6f1c2d";
        let size = 64 * 1024;
        for before in [1, 10, secret.len() - 1] {
            // A long line with the secret across where one read ends and the next begins.
            let mut line = "x".repeat(size - before);
            line.push_str(secret);
            line.push_str(&"y".repeat(100));
            line.push('\n');
            let (out, held) = masked_in_pieces(Mask::new([secret]), line.as_bytes(), size);
            assert!(!out.contains(secret), "{before} bytes before the cut");
            assert!(out.contains("x***y"));
            assert_eq!(held, line.len() as u64);
        }
        // Across three reads, too, and with no line end before the stream does.
        let line = format!("{}{secret}", "z".repeat(5));
        let (out, held) = masked_in_pieces(Mask::new([secret]), line.as_bytes(), 7);
        assert_eq!(out, "zzzzz***");
        assert_eq!(held, line.len() as u64);
    }

    #[test]
    fn a_character_split_between_reads_stays_whole() {
        // "Ångström" in a line longer than a read, cut inside its multibyte characters.
        let line = format!("{}Ångström\n", "a".repeat(3));
        for size in 1..line.len() {
            let (out, held) = masked_in_pieces(Mask::default(), line.as_bytes(), size);
            assert_eq!(out, line, "pieces of {size}");
            assert_eq!(held, line.len() as u64);
        }
    }

    #[test]
    fn a_line_that_fits_a_read_is_written_at_once() {
        let mut masking = Masking::new(Mask::new(["s3cr3t-key"]));
        assert_eq!(
            masking.piece(b"key=s3cr3t-key\n"),
            Some(("key=***\n".to_owned(), 15))
        );
        // Part of a line that might end in a secret waits for the rest.
        assert_eq!(masking.piece(b"key=s3cr"), None);
        assert_eq!(masking.piece(b"3t-key"), Some(("key=".to_owned(), 4)));
        assert_eq!(masking.end(), Some(("***".to_owned(), 10)));
        assert_eq!(masking.end(), None);
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

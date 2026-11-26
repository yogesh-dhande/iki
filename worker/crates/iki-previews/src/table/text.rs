//! CSV and TSV, read as the server's importer (`spreadsheets/workbooks.py`) reads them:
//! the same encodings, in the same order, and the same delimiter for a file whose
//! delimiter isn't given.

use super::{
    IMPORTER_MAX_CELL, IMPORTER_MAX_COLUMNS, OLE, OLE_MESSAGE, Problems, ROW_LIMIT, Table, grid,
    thousands,
};

/// How much of the file's start the delimiter is found from, as the importer reads it.
const SNIFF_BYTES: usize = 64 * 1024;
/// The delimiters a file's own may be, in the importer's order.
const DELIMITERS: [char; 4] = [',', ';', '\t', '|'];

pub(super) fn read(
    bytes: &[u8],
    delimiter: u8,
    max_rows: usize,
    max_columns: usize,
    complete: bool,
) -> Result<Table, String> {
    if bytes.is_empty() && complete {
        return Err("It's empty.".to_owned());
    }
    if bytes.starts_with(OLE) {
        return Err(OLE_MESSAGE.to_owned());
    }
    if bytes.starts_with(b"PK\x03\x04") {
        return Err(
            "It's a workbook (a ZIP file), not CSV text: open it as a workbook.".to_owned(),
        );
    }
    let (text, encoding, mut problems) = decode(bytes, complete)?;
    let delimiter = match delimiter {
        0 => {
            let head = &bytes[..bytes.len().min(SNIFF_BYTES)];
            let (head, _, _) = decode(head, false)?;
            u8::try_from(sniff(&head)).unwrap_or(b',')
        }
        d if d.is_ascii() && d != b'"' && d != b'\n' && d != b'\r' => d,
        _ => {
            return Err(
                "The delimiter must be one ASCII character other than a quote or a line end."
                    .to_owned(),
            );
        }
    };
    let max_rows = max_rows.min(ROW_LIMIT);
    let (mut rows, reached_end) = records(&text, delimiter, max_rows + 1)?;
    let mut more_rows = false;
    if !reached_end || rows.len() > max_rows {
        more_rows = true;
        rows.truncate(max_rows);
    } else if !complete {
        // The input ends where the page stopped reading: its last record may be cut.
        rows.pop();
        more_rows = true;
    }
    if !more_rows {
        // As the importer's sample does, the rows end at the last with anything in it.
        while rows
            .last()
            .is_some_and(|row| row.iter().all(|cell| cell.trim().is_empty()))
        {
            rows.pop();
        }
    }
    if let Some(first) = rows.first_mut().and_then(|row| row.first_mut())
        && let Some(rest) = first.strip_prefix('\u{feff}')
    {
        *first = rest.to_owned();
    }
    check(&rows, &mut problems);
    let shown = grid(rows, max_columns);
    Ok(Table {
        sheets: Vec::new(),
        sheet: 0,
        rows: shown.rows,
        first_row: 1,
        first_column: 1,
        columns: shown.columns,
        more_rows: more_rows || shown.fewer_rows,
        more_columns: shown.more_columns,
        cut_cells: shown.cut_cells,
        encoding: encoding.to_owned(),
        problems: problems.finish(),
    })
}

/// What the importer would say about the rows kept.
fn check(rows: &[Vec<String>], problems: &mut Problems) {
    let header = rows.first().map_or(0, Vec::len);
    for (index, row) in rows.iter().enumerate() {
        let number = thousands(index + 1);
        if index > 0 && row.len() > header {
            problems.add(format!(
                "Row {number} has {} cells, more than the first row's {}.",
                thousands(row.len()),
                thousands(header)
            ));
        }
        if row.len() > IMPORTER_MAX_COLUMNS {
            problems.add(format!(
                "Row {number} has over {} columns, more than Iki imports.",
                thousands(IMPORTER_MAX_COLUMNS)
            ));
        }
        if row
            .iter()
            .any(|cell| cell.chars().count() > IMPORTER_MAX_CELL)
        {
            problems.add(format!(
                "Row {number} has a cell of over {} characters, more than Iki imports.",
                thousands(IMPORTER_MAX_CELL)
            ));
        }
    }
}

/// The text, how it was decoded, and any bytes that weren't text in it. As the importer
/// does: a UTF-8 byte order mark, a UTF-16 one, then UTF-8 if it all is, else
/// Windows-1252. Where `complete` is false, a character cut at the end is left out.
#[allow(clippy::too_many_lines)] // Each encoding the importer tries, in its order.
fn decode(bytes: &[u8], complete: bool) -> Result<(String, &'static str, Problems), String> {
    let mut problems = Problems::default();
    if let Some(rest) = bytes.strip_prefix(b"\xef\xbb\xbf") {
        return Ok(match std::str::from_utf8(rest) {
            Ok(text) => (text.to_owned(), "UTF-8", problems),
            Err(error) if !complete && error.error_len().is_none() => {
                let valid = &rest[..error.valid_up_to()];
                (
                    String::from_utf8_lossy(valid).into_owned(),
                    "UTF-8",
                    problems,
                )
            }
            Err(error) => {
                problems.add(format!(
                    "Byte {} isn't UTF-8 text, though the file starts as UTF-8: the importer \
                     will refuse it. Save it as CSV UTF-8.",
                    thousands(error.valid_up_to() + 3)
                ));
                (
                    String::from_utf8_lossy(rest).into_owned(),
                    "UTF-8",
                    problems,
                )
            }
        });
    }
    let utf16 = if bytes.starts_with(b"\xff\xfe") {
        Some(true)
    } else if bytes.starts_with(b"\xfe\xff") {
        Some(false)
    } else {
        None
    };
    if let Some(little) = utf16 {
        let body = &bytes[2..];
        let units = body.chunks_exact(2).map(|pair| {
            let pair = [pair[0], pair[1]];
            if little {
                u16::from_le_bytes(pair)
            } else {
                u16::from_be_bytes(pair)
            }
        });
        let mut text = String::with_capacity(body.len() / 2);
        let mut bad = 0usize;
        let decoded: Vec<_> = char::decode_utf16(units).collect();
        let last = decoded.len();
        for (i, unit) in decoded.into_iter().enumerate() {
            match unit {
                Ok(c) => text.push(c),
                // A surrogate cut off by the end of what was read.
                Err(_) if !complete && i + 1 == last => {}
                Err(_) => {
                    bad += 1;
                    text.push('\u{fffd}');
                }
            }
        }
        if bad > 0 {
            problems.add(format!(
                "{} of its characters aren't UTF-16 text: the importer will refuse it. Save it \
                 as CSV UTF-8.",
                thousands(bad)
            ));
        }
        return Ok((text, if little { "UTF-16LE" } else { "UTF-16BE" }, problems));
    }
    if bytes.iter().take(8).any(|&b| b == 0) {
        return Err(
            "It isn't text or a workbook: import an .xlsx workbook or a CSV file.".to_owned(),
        );
    }
    match std::str::from_utf8(bytes) {
        Ok(text) => return Ok((text.to_owned(), "UTF-8", problems)),
        Err(error) if !complete && error.error_len().is_none() => {
            let valid = &bytes[..error.valid_up_to()];
            return Ok((
                String::from_utf8_lossy(valid).into_owned(),
                "UTF-8",
                problems,
            ));
        }
        Err(_) => {}
    }
    let mut text = String::with_capacity(bytes.len());
    let mut first_bad = None;
    for (at, &b) in bytes.iter().enumerate() {
        if let Some(c) = windows_1252(b) {
            text.push(c);
        } else {
            first_bad.get_or_insert(at);
            text.push('\u{fffd}');
        }
    }
    if let Some(at) = first_bad {
        problems.add(format!(
            "Byte {} isn't UTF-8 or Windows-1252 text: the importer will refuse it. Save it as \
             CSV UTF-8.",
            thousands(at + 1)
        ));
    }
    Ok((text, "windows-1252", problems))
}

/// A Windows-1252 byte's character: Latin-1, but for 0x80–0x9F, five of which aren't
/// characters at all (as Python's `cp1252` codec, which the importer uses, has them).
fn windows_1252(b: u8) -> Option<char> {
    const HIGH: [u16; 32] = [
        0x20AC, 0, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021, 0x02C6, 0x2030, 0x0160, 0x2039,
        0x0152, 0, 0x017D, 0, 0, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014, 0x02DC,
        0x2122, 0x0161, 0x203A, 0x0153, 0, 0x017E, 0x0178,
    ];
    match b {
        0x80..=0x9f => match HIGH[usize::from(b - 0x80)] {
            0 => None,
            code => char::from_u32(u32::from(code)),
        },
        _ => Some(char::from(b)),
    }
}

/// Up to `limit` records, each its fields, with a blank line an empty record as Python's
/// `csv` module (and so the importer) reads it; and whether the text ended.
fn records(text: &str, delimiter: u8, limit: usize) -> Result<(Vec<Vec<String>>, bool), String> {
    let bytes = text.as_bytes();
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .delimiter(delimiter)
        .from_reader(bytes);
    let mut rows = Vec::new();
    let mut record = csv::ByteRecord::new();
    loop {
        if rows.len() >= limit {
            return Ok((rows, false));
        }
        let more = reader
            .read_byte_record(&mut record)
            .map_err(|error| format!("Its row {} can't be read: {error}.", rows.len() + 1))?;
        // The csv crate skips blank lines, starting a record where they start; Python's csv
        // module reads each as an empty row.
        let start = record
            .position()
            .and_then(|p| usize::try_from(p.byte()).ok())
            .unwrap_or(bytes.len());
        for _ in 0..blank_lines(bytes, start) {
            if rows.len() >= limit {
                return Ok((rows, false));
            }
            rows.push(Vec::new());
        }
        if !more {
            return Ok((rows, true));
        }
        if rows.len() >= limit {
            return Ok((rows, false));
        }
        rows.push(
            record
                .iter()
                .map(|field| String::from_utf8_lossy(field).into_owned())
                .collect(),
        );
    }
}

/// How many blank lines start at `from`: line ends (CR LF, CR or LF) before anything
/// else. An LF just after a CR belongs to it, whether that CR ended the record before
/// or a blank line.
fn blank_lines(bytes: &[u8], from: usize) -> usize {
    let mut count = 0;
    for at in from..bytes.len() {
        match bytes[at] {
            b'\r' => count += 1,
            b'\n' if at == 0 || bytes[at - 1] != b'\r' => count += 1,
            b'\n' => {}
            _ => break,
        }
    }
    count
}

/// The delimiter of a file whose delimiter isn't given, found from its start as the
/// importer finds it: Python's `csv.Sniffer` among comma, semicolon, tab and bar, over its
/// first 20 lines that aren't blank, and failing that whichever of those splits the first
/// line most (a comma if none does). This mirrors `Sniffer._guess_quote_and_delimiter`
/// then `Sniffer._guess_delimiter` step for step; the one difference is that Python's `\w`
/// is approximated by alphanumeric characters and `_`.
fn sniff(head: &str) -> char {
    let lines: Vec<&str> = split_lines(head)
        .into_iter()
        .take(20)
        .filter(|line| !line.trim().is_empty())
        .collect();
    if lines.is_empty() {
        return ',';
    }
    let data: Vec<char> = lines.join("\n").chars().collect();
    if let Some(found) = quoted_delimiter(&data) {
        return found;
    }
    if let Some(found) = consistent_delimiter(&lines) {
        return found;
    }
    let first = lines[0];
    let mut best = ',';
    let mut most = 0;
    for d in DELIMITERS {
        let count = first.matches(d).count();
        if count > most {
            best = d;
            most = count;
        }
    }
    best
}

/// Python's `str.splitlines()`.
fn split_lines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((at, c)) = chars.next() {
        let breaks = matches!(
            c,
            '\n' | '\r'
                | '\u{b}'
                | '\u{c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if breaks {
            lines.push(&text[start..at]);
            let mut next = at + c.len_utf8();
            if c == '\r' && chars.peek().is_some_and(|&(_, n)| n == '\n') {
                chars.next();
                next += 1;
            }
            start = next;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

fn is_quote(c: char) -> bool {
    c == '"' || c == '\''
}

/// `[^\w\n"']`.
fn is_delimiter_class(c: char) -> bool {
    !(c.is_alphanumeric() || c == '_' || c == '\n' || is_quote(c))
}

/// `Sniffer._guess_quote_and_delimiter`'s delimiter: the first of its patterns that
/// matches anywhere, `,".*?",`, then `^".*?",`, then `,".*?"$`, and the delimiter of the
/// most of its matches, if one of the four.
#[allow(clippy::too_many_lines)] // Three regular expressions, by hand.
fn quoted_delimiter(data: &[char]) -> Option<char> {
    let n = data.len();
    // Where each quote is followed by a given character, for the patterns' lazy `.*?`.
    let mut followed: std::collections::HashMap<(char, char), Vec<usize>> =
        std::collections::HashMap::new();
    let mut before_class: std::collections::HashMap<char, Vec<usize>> =
        std::collections::HashMap::new();
    for m in 0..n {
        if is_quote(data[m]) {
            let next = data.get(m + 1).copied().unwrap_or('\0');
            if m + 1 == n {
                followed.entry((data[m], '\n')).or_default().push(m);
            } else {
                followed.entry((data[m], next)).or_default().push(m);
            }
            if m + 1 < n && is_delimiter_class(next) {
                before_class.entry(data[m]).or_default().push(m);
            }
        }
    }
    let after = |list: Option<&Vec<usize>>, from: usize| -> Option<usize> {
        let list = list?;
        let at = list.partition_point(|&m| m < from);
        list.get(at).copied()
    };
    // The quote at `k` opens a field (after a delimiter and an optional space at `i`).
    let opening = |i: usize| -> Option<(char, usize)> {
        let d = data[i];
        if !is_delimiter_class(d) {
            return None;
        }
        let k = if data.get(i + 1) == Some(&' ') {
            i + 2
        } else {
            i + 1
        };
        let q = *data.get(k)?;
        is_quote(q).then_some((d, k))
    };
    let mut counts: Vec<(char, usize)> = Vec::new();
    let mut count = |d: char| {
        if DELIMITERS.contains(&d) {
            match counts.iter_mut().find(|(c, _)| *c == d) {
                Some((_, n)) => *n += 1,
                None => counts.push((d, 1)),
            }
        }
    };
    let mut matched = false;
    // `(?P<delim>[^\w\n"'])(?P<space> ?)(?P<quote>["']).*?(?P=quote)(?P=delim)`
    let mut i = 0;
    while i < n {
        if let Some((d, k)) = opening(i)
            && let Some(m) = after(followed.get(&(data[k], d)), k + 1)
        {
            matched = true;
            count(d);
            i = m + 2;
            continue;
        }
        i += 1;
    }
    if !matched {
        // `(?:^|\n)(?P<quote>["']).*?(?P=quote)(?P<delim>[^\w\n"'])(?P<space> ?)`
        let mut i = 0;
        while i < n {
            let k = if (i == 0 || data[i - 1] == '\n') && is_quote(data[i]) {
                Some(i)
            } else if data[i] == '\n' && data.get(i + 1).copied().is_some_and(is_quote) {
                Some(i + 1)
            } else {
                None
            };
            if let Some(k) = k
                && let Some(m) = after(before_class.get(&data[k]), k + 1)
            {
                matched = true;
                count(data[m + 1]);
                i = if data.get(m + 2) == Some(&' ') {
                    m + 3
                } else {
                    m + 2
                };
                continue;
            }
            i += 1;
        }
    }
    if !matched {
        // `(?P<delim>[^\w\n"'])(?P<space> ?)(?P<quote>["']).*?(?P=quote)(?:$|\n)`
        let mut i = 0;
        while i < n {
            if let Some((d, k)) = opening(i)
                && let Some(m) = after(followed.get(&(data[k], '\n')), k + 1)
            {
                count(d);
                i = m + 1;
                continue;
            }
            i += 1;
        }
    }
    // `max(delims, key=delims.get)`: the first of the most.
    let mut best: Option<(char, usize)> = None;
    for &(d, c) in &counts {
        if best.is_none_or(|(_, b)| c > b) {
            best = Some((d, c));
        }
    }
    best.map(|(d, _)| d)
}

/// `Sniffer._guess_delimiter`: the delimiter that's in each line the same number of times,
/// or nearly each (90%), over chunks of ten lines; with several, the first of comma, tab
/// and semicolon among them.
fn consistent_delimiter(lines: &[&str]) -> Option<char> {
    let total_lines = lines.len();
    let chunk = total_lines.min(10);
    // For each delimiter, how many lines had it each number of times, in the order those
    // numbers were first seen.
    let mut frequencies: Vec<Vec<(usize, usize)>> = vec![Vec::new(); DELIMITERS.len()];
    let mut found: Vec<(char, (usize, i64))> = Vec::new();
    let mut iteration = 0usize;
    let (mut start, mut end) = (0usize, chunk);
    while start < total_lines {
        iteration += 1;
        for line in &lines[start..end.min(total_lines)] {
            for (d, table) in DELIMITERS.iter().zip(frequencies.iter_mut()) {
                let freq = line.matches(*d).count();
                match table.iter_mut().find(|(f, _)| *f == freq) {
                    Some((_, n)) => *n += 1,
                    None => table.push((freq, 1)),
                }
            }
        }
        let mut modes: Vec<(char, (usize, i64))> = Vec::new();
        for (d, table) in DELIMITERS.iter().zip(&frequencies) {
            if table.len() == 1 && table[0].0 == 0 {
                continue;
            }
            let mut mode = table[0];
            for &item in &table[1..] {
                if item.1 > mode.1 {
                    mode = item;
                }
            }
            let others: usize = table.iter().map(|item| item.1).sum::<usize>() - mode.1;
            let count = i64::try_from(mode.1).unwrap_or(i64::MAX)
                - i64::try_from(others).unwrap_or(i64::MAX);
            modes.push((*d, (mode.0, count)));
        }
        #[allow(clippy::cast_precision_loss)] // At most 20 lines.
        let total = (chunk * iteration).min(total_lines) as f64;
        let mut consistency = 1.0f64;
        while found.is_empty() && consistency >= 0.9 {
            for &(d, (freq, count)) in &modes {
                #[allow(clippy::cast_precision_loss)]
                if freq > 0 && count > 0 && count as f64 / total >= consistency {
                    found.push((d, (freq, count)));
                }
            }
            consistency -= 0.01;
        }
        if found.len() == 1 {
            return Some(found[0].0);
        }
        start = end;
        end += chunk;
    }
    if found.is_empty() {
        return None;
    }
    for preferred in [',', '\t', ';'] {
        if found.iter().any(|(d, _)| *d == preferred) {
            return Some(preferred);
        }
    }
    // Sorted by (mode, delimiter): the greatest.
    found
        .iter()
        .max_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)))
        .map(|(d, _)| *d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sniffs_as_python_does() {
        assert_eq!(sniff("a,b,c\n1,2,3\n"), ',');
        assert_eq!(sniff("a;b;c\n1;2;3\n"), ';');
        assert_eq!(sniff("a\tb\tc\n1\t2\t3\n"), '\t');
        assert_eq!(sniff("a|b|c\n1|2|3\n"), '|');
        // A quoted field between semicolons decides it, commas inside or not.
        assert_eq!(sniff("x;\"1,5\";y\n"), ';');
        // European decimals: semicolons are in each line the same number of times.
        assert_eq!(sniff("Sample;Conc\nA1;1,5\nA2;2,25\nA3;10\n"), ';');
        // Nothing consistent: whichever splits the first line most.
        assert_eq!(sniff("a,b;c;d\nx\n"), ';');
        assert_eq!(sniff("plain words\n"), ',');
        assert_eq!(sniff(""), ',');
    }

    #[test]
    fn decodes_windows_1252() {
        let (text, encoding, problems) = decode(b"caf\xe9 \x80 \x93x\x94", true).unwrap();
        assert_eq!(text, "caf\u{e9} \u{20ac} \u{201c}x\u{201d}");
        assert_eq!(encoding, "windows-1252");
        assert!(problems.listed.is_empty());
        let (_, _, problems) = decode(b"a\x81b\xff", true).unwrap();
        assert_eq!(problems.listed.len(), 1);
    }

    #[test]
    fn counts_blank_lines() {
        assert_eq!(blank_lines(b"a\r\n\r\nb", 2), 1);
        assert_eq!(blank_lines(b"a\n\n\nb", 2), 2);
        assert_eq!(blank_lines(b"a\r\rb", 2), 1);
        assert_eq!(blank_lines(b"\n\na", 0), 2);
        let rows = |text: &str| records(text, b',', 100).unwrap().0;
        assert_eq!(rows("a\n\nb"), [vec!["a"], vec![], vec!["b"]]);
        assert_eq!(rows("a\r\n\r\nb\r\n"), [vec!["a"], vec![], vec!["b"]]);
        assert_eq!(rows("\n\na"), [vec![], vec![], vec!["a"]]);
        assert_eq!(rows("a\n\n"), [vec!["a"], vec![]]);
        assert_eq!(rows("a\r\rb"), [vec!["a"], vec![], vec!["b"]]);
    }
}

//! mzML (HUPO-PSI's mzML 1.1), read a spectrum at a time. An indexed file (`indexedmzML`)
//! says at its end where its index is (`<indexListOffset>`), and the index says where each
//! spectrum and chromatogram starts, so the page reads the end, then the index, then one
//! spectrum's bytes: never the whole file. A file without an index is scanned from its start,
//! as far as the page read. Binary arrays are base64, plain or zlib-compressed, of 32- or
//! 64-bit floats (or integers); MS-Numpress isn't read, and says so.
//!
//! Parameters are read by their accession in the PSI-MS controlled vocabulary, wherever in
//! the element they are, except those of a precursor or product (only a selected ion's m/z,
//! MS:1000744, is read there) and of a binary array (its kind, number type and compression).
//! Parameters a spectrum takes from a `referenceableParamGroup` aren't read: the group is
//! in the file's header, not in the spectrum's bytes.
//!
//! A `<!DOCTYPE` is refused: it can declare entities that expand without bound, and mzML
//! has none.

use std::borrow::Cow;

use base64::Engine as _;
use base64::engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig};
use quick_xml::events::{BytesStart, Event};
use quick_xml::{Reader, XmlVersion};
use serde::Serialize;

/// The most entries an index lists.
pub const MAX_ENTRIES: usize = 2_000_000;
/// The most bytes one binary array may decode to: 32 million 64-bit values.
pub const MAX_ARRAY_BYTES: usize = 256 * 1024 * 1024;
/// The most bytes a spectrum's or chromatogram's arrays may decode to, together.
pub const MAX_ELEMENT_BYTES: usize = 512 * 1024 * 1024;
/// How deeply elements may nest. mzML's go about ten deep.
pub const MAX_DEPTH: usize = 64;
/// How much of a file's end [`index_offset`] looks at.
pub const TAIL: usize = 64 * 1024;

/// Where each spectrum and chromatogram starts, in the order the file lists them.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Index {
    /// Its spectra.
    pub spectra: Vec<Entry>,
    /// Its chromatograms, such as its total ion current.
    pub chromatograms: Vec<Entry>,
    /// Whether entries were left out: past [`MAX_ENTRIES`], or past the bytes read when
    /// scanning a file that has no index.
    pub truncated: bool,
    /// How many spectra `<spectrumList count>` says it has, if it was read.
    pub declared_spectra: Option<usize>,
}

/// One spectrum or chromatogram.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Entry {
    /// Its `id`, such as `controllerType=0 controllerNumber=1 scan=1`.
    pub id: String,
    /// Where its element starts, in bytes from the file's start.
    pub offset: u64,
}

/// A spectrum or chromatogram, ready to plot.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Element {
    /// A mass spectrum: m/z against intensity.
    Spectrum(Spectrum),
    /// A chromatogram: time against intensity.
    Chromatogram(Chromatogram),
}

/// One spectrum.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Spectrum {
    /// Its `id`.
    pub id: String,
    /// Its `index`, from 0.
    pub index: Option<usize>,
    /// Its MS level (MS:1000511).
    pub ms_level: Option<u32>,
    /// When it was scanned (MS:1000016), in minutes, whatever unit the file used.
    pub scan_start_minutes: Option<f64>,
    /// `positive` or `negative` (MS:1000130, MS:1000129), if said.
    pub polarity: Option<String>,
    /// Whether it's centroided (MS:1000127) rather than profile (MS:1000128), if said.
    pub centroided: Option<bool>,
    /// Its base peak's m/z and intensity (MS:1000504, MS:1000505), if recorded.
    pub base_peak_mz: Option<f64>,
    /// The base peak's intensity.
    pub base_peak_intensity: Option<f64>,
    /// Its total ion current (MS:1000285), if recorded.
    pub total_ion_current: Option<f64>,
    /// Its precursors' selected ions' m/z (MS:1000744), for MSⁿ spectra.
    pub precursor_mz: Vec<f64>,
    /// The m/z values.
    pub mz: Vec<f64>,
    /// Their intensities.
    pub intensity: Vec<f64>,
    /// How many points its arrays have (`defaultArrayLength`).
    pub points: usize,
    /// Whether points were left out past the limit the page gave.
    pub truncated: bool,
}

/// One chromatogram.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Chromatogram {
    /// Its `id`, such as `TIC`.
    pub id: String,
    /// What it is, such as `total ion current chromatogram` (MS:1000235), if said.
    pub chromatogram_type: Option<String>,
    /// Its times, in minutes.
    pub time_minutes: Vec<f64>,
    /// Their intensities.
    pub intensity: Vec<f64>,
    /// How many points its arrays have.
    pub points: usize,
    /// Whether points were left out.
    pub truncated: bool,
}

const DOCTYPE: &str = "This file declares a <!DOCTYPE>, which previews refuse: it can define \
                       entities that expand without bound, and mzML has none.";

/// Base64 as mzML writers write it: padded or not.
const BASE64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new()
        .with_decode_padding_mode(DecodePaddingMode::Indifferent)
        .with_decode_allow_trailing_bits(true),
);

/// Where the index starts, from a file's last bytes (`<indexListOffset>n</indexListOffset>`).
#[must_use]
pub fn index_offset(tail: &[u8]) -> Option<u64> {
    const OPEN: &[u8] = b"<indexListOffset>";
    let tail = &tail[tail.len().saturating_sub(TAIL)..];
    let at = tail
        .windows(OPEN.len())
        .rposition(|window| window == OPEN)?;
    let rest = &tail[at + OPEN.len()..];
    let end = rest.iter().position(|byte| *byte == b'<')?;
    if !rest[end..].starts_with(b"</indexListOffset>") {
        return None;
    }
    std::str::from_utf8(&rest[..end]).ok()?.trim().parse().ok()
}

fn reader(bytes: &[u8]) -> Reader<&[u8]> {
    let mut reader = Reader::from_reader(bytes);
    let config = reader.config_mut();
    config.trim_text(false);
    config.check_end_names = true;
    config.expand_empty_elements = false;
    reader
}

/// Whether a parse error is only the buffer ending part-way through: no tag closes after it.
fn cut_off(bytes: &[u8], at: u64) -> bool {
    let at = usize::try_from(at).unwrap_or(usize::MAX).min(bytes.len());
    let rest = &bytes[at..];
    rest.len() <= 4 || !rest.contains(&b'>')
}

fn local_name(element: &BytesStart) -> String {
    element.local_name().as_ref().to_owned()
}

/// An attribute's value, its entities replaced.
fn attribute(element: &BytesStart, name: &str) -> Result<Option<String>, String> {
    for attribute in element.attributes() {
        let attribute = attribute.map_err(|error| {
            format!(
                "The <{}> element's attributes can't be read: {error}.",
                local_name(element)
            )
        })?;
        if attribute.key.local_name().as_ref() == name {
            return attribute
                .normalized_value(XmlVersion::Implicit1_0)
                .map(|value| Some(value.into_owned()))
                .map_err(|error| {
                    format!(
                        "The <{}> element's {name} attribute can't be read: {error}.",
                        local_name(element)
                    )
                });
        }
    }
    Ok(None)
}

/// Reads an index, from its offset to the file's end.
///
/// # Errors
///
/// A message for a person if it isn't an mzML index.
pub fn read_index(bytes: &[u8]) -> Result<Index, String> {
    let mut reader = reader(bytes);
    let mut index = Index {
        spectra: Vec::new(),
        chromatograms: Vec::new(),
        truncated: false,
        declared_spectra: None,
    };
    let mut opened = false;
    let mut closed = false;
    let mut list = List::Other;
    let mut offset: Option<(String, String)> = None;
    loop {
        let event = match reader.read_event() {
            Ok(event) => event,
            Err(_) if cut_off(bytes, reader.error_position()) && opened => break,
            Err(error) => {
                return Err(format!(
                    "The mzML index isn't well-formed XML at byte {} of it: {error}.",
                    count_u64(reader.error_position())
                ));
            }
        };
        match event {
            Event::DocType(_) => return Err(DOCTYPE.to_owned()),
            Event::Start(element) | Event::Empty(element) => {
                let name = local_name(&element);
                if !opened {
                    if name != "indexList" {
                        return Err(format!(
                            "This isn't an mzML index: it starts with <{name}>, not \
                             <indexList>. The file's <indexListOffset> may be wrong."
                        ));
                    }
                    opened = true;
                    continue;
                }
                match name.as_str() {
                    "index" => {
                        list = match attribute(&element, "name")?.as_deref() {
                            Some("spectrum") => List::Spectra,
                            Some("chromatogram") => List::Chromatograms,
                            _ => List::Other,
                        };
                    }
                    "offset" => {
                        let id = attribute(&element, "idRef")?.unwrap_or_default();
                        offset = Some((id, String::new()));
                    }
                    _ => {}
                }
            }
            Event::Text(text) => {
                if let Some((id, digits)) = &mut offset {
                    offset_digits(id, digits, &text.xml10_content())?;
                }
            }
            Event::GeneralRef(_) | Event::CData(_) => {
                if let Some((id, _)) = &offset {
                    return Err(format!(
                        "The index's offset for \"{id}\" isn't a byte offset: it has markup in it."
                    ));
                }
            }
            Event::End(element) => match element.local_name().as_ref() {
                "offset" => {
                    if let Some((id, digits)) = offset.take()
                        && !add_entry(&mut index, &list, id, &digits)?
                    {
                        index.truncated = true;
                        closed = true;
                        break;
                    }
                }
                "index" => list = List::Other,
                "indexList" => {
                    closed = true;
                    break;
                }
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
    }
    if !opened {
        return Err(
            "This isn't an mzML index: it has no <indexList>. The file's <indexListOffset> may \
             be wrong."
                .to_owned(),
        );
    }
    if !closed {
        index.truncated = true;
    }
    Ok(index)
}

/// Adds text to an `<offset>`'s digits, up to a sane length.
fn offset_digits(id: &str, digits: &mut String, text: &str) -> Result<(), String> {
    digits.push_str(text);
    if digits.len() > 64 {
        return Err(format!(
            "The index's offset for \"{id}\" isn't a byte offset: it's {} characters long.",
            count(digits.len())
        ));
    }
    Ok(())
}

/// Which of the index's lists an `<offset>` is in.
enum List {
    Spectra,
    Chromatograms,
    Other,
}

/// Adds an `<offset>` to the index's list; false if the index is full.
fn add_entry(index: &mut Index, list: &List, id: String, digits: &str) -> Result<bool, String> {
    let at = digits.trim().parse::<u64>().map_err(|_| {
        format!(
            "The index's offset for \"{id}\" isn't a byte offset: \"{}\".",
            digits.trim()
        )
    })?;
    if matches!(list, List::Other) {
        return Ok(true);
    }
    if index_len(index) >= MAX_ENTRIES {
        return Ok(false);
    }
    let entries = match list {
        List::Spectra => &mut index.spectra,
        _ => &mut index.chromatograms,
    };
    entries.push(Entry { id, offset: at });
    Ok(true)
}

fn index_len(index: &Index) -> usize {
    index.spectra.len() + index.chromatograms.len()
}

/// Finds the spectra and chromatograms that start in `bytes`, the start of a file without
/// an index; `complete` says whether it's the whole file.
///
/// When it isn't, the buffer may end part-way through an element, and `truncated` says
/// whether it ended before `</mzML>`: more entries may follow. When it is, nothing follows,
/// so only [`MAX_ENTRIES`] truncates; a file cut short is read as far as it goes.
///
/// # Errors
///
/// A message for a person if it isn't mzML.
pub fn scan(bytes: &[u8], complete: bool) -> Result<Index, String> {
    let (skipped, body) = match bytes.strip_prefix(b"\xEF\xBB\xBF") {
        Some(body) => (3u64, body),
        None => (0, bytes),
    };
    if body.iter().all(u8::is_ascii_whitespace) {
        return Err("The file is empty.".to_owned());
    }
    let mut reader = reader(body);
    let mut index = Index {
        spectra: Vec::new(),
        chromatograms: Vec::new(),
        truncated: false,
        declared_spectra: None,
    };
    let mut root = false;
    let mut ended = false;
    let mut full = false;
    let mut depth = 0usize;
    loop {
        let at = reader.buffer_position() + skipped;
        let event = match reader.read_event() {
            Ok(event) => event,
            Err(_) if root && cut_off(body, reader.error_position()) => break,
            Err(_) if !complete && cut_off(body, reader.error_position()) => {
                return Err(format!(
                    "The {} bytes read end before the file's <mzML> element starts.",
                    count(body.len())
                ));
            }
            Err(error) => {
                return Err(format!(
                    "This file isn't well-formed XML at byte {}: {error}.",
                    count_u64(reader.error_position() + skipped)
                ));
            }
        };
        let (element, opens) = match event {
            Event::DocType(_) => return Err(DOCTYPE.to_owned()),
            Event::Start(element) => (element, true),
            Event::Empty(element) => (element, false),
            Event::End(element) => {
                depth = depth.saturating_sub(1);
                if element.local_name().as_ref() == "mzML" {
                    ended = true;
                    break;
                }
                continue;
            }
            Event::Eof => break,
            _ => continue,
        };
        if opens {
            depth += 1;
            if depth > MAX_DEPTH {
                return Err(format!(
                    "This file's elements nest more than {MAX_DEPTH} deep, at byte {}; mzML's \
                     go about ten deep.",
                    count_u64(at)
                ));
            }
        }
        let name = local_name(&element);
        if !root {
            if name != "mzML" && name != "indexedmzML" {
                return Err(format!(
                    "This isn't mzML: its first element is <{name}>, not <mzML>."
                ));
            }
            root = true;
            continue;
        }
        match name.as_str() {
            "spectrumList" => {
                index.declared_spectra =
                    attribute(&element, "count")?.and_then(|count| count.trim().parse().ok());
            }
            "spectrum" | "chromatogram" => {
                if index_len(&index) >= MAX_ENTRIES {
                    full = true;
                    break;
                }
                let entry = Entry {
                    id: attribute(&element, "id")?.unwrap_or_default(),
                    offset: at,
                };
                if name == "spectrum" {
                    index.spectra.push(entry);
                } else {
                    index.chromatograms.push(entry);
                }
            }
            _ => {}
        }
    }
    if !root {
        return Err("This isn't mzML: it has no <mzML> element.".to_owned());
    }
    index.truncated = full || (!complete && !ended);
    Ok(index)
}

/// What a binary array holds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Role {
    Mz,
    Intensity,
    Time,
}

impl Role {
    fn name(self) -> &'static str {
        match self {
            Role::Mz => "m/z",
            Role::Intensity => "intensity",
            Role::Time => "time",
        }
    }
}

/// How a binary array's values are stored.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Number {
    F32,
    F64,
    I32,
    I64,
    /// One previews don't read, by its name.
    Other(&'static str),
}

/// One `<binaryDataArray>`, as it's read.
#[derive(Default)]
struct Array<'a> {
    length: Option<usize>,
    role: Option<Role>,
    number: Option<Number>,
    zlib: bool,
    numpress: Option<String>,
    /// For a time array: how many minutes its unit is.
    minutes_per_unit: Option<f64>,
    unit: Option<String>,
    group: bool,
    text: Vec<Cow<'a, str>>,
}

#[derive(Default)]
struct Parsed {
    ms_level: Option<u32>,
    scan_start_minutes: Option<f64>,
    scan_started: bool,
    polarity: Option<String>,
    centroided: Option<bool>,
    base_peak_mz: Option<f64>,
    base_peak_intensity: Option<f64>,
    total_ion_current: Option<f64>,
    precursor_mz: Vec<f64>,
    chromatogram_type: Option<String>,
    mz: Option<Vec<f64>>,
    intensity: Option<Vec<f64>>,
    time: Option<Vec<f64>>,
    decoded_bytes: usize,
}

/// How many minutes a unit of time is, by its Unit Ontology accession or name.
fn minutes_per(accession: Option<&str>, name: Option<&str>) -> Option<f64> {
    match (accession, name) {
        (Some("UO:0000010"), _) | (None, Some("second")) => Some(1.0 / 60.0),
        (Some("UO:0000031"), _) | (None, Some("minute")) => Some(1.0),
        (Some("UO:0000032"), _) | (None, Some("hour")) => Some(60.0),
        (Some("UO:0000028"), _) | (None, Some("millisecond")) => Some(1.0 / 60_000.0),
        _ => None,
    }
}

/// Chromatogram types (children of MS:1000626), for those that don't end "chromatogram".
const CHROMATOGRAM_TYPES: [&str; 12] = [
    "MS:1000235",
    "MS:1000627",
    "MS:1000628",
    "MS:1000810",
    "MS:1000811",
    "MS:1000812",
    "MS:1000813",
    "MS:1001472",
    "MS:1001473",
    "MS:1001474",
    "MS:1003019",
    "MS:1003020",
];

fn numpress(accession: &str) -> Option<&'static str> {
    Some(match accession {
        "MS:1002312" => "MS-Numpress linear prediction",
        "MS:1002313" => "MS-Numpress positive integer",
        "MS:1002314" => "MS-Numpress short logged float",
        "MS:1002746" => "MS-Numpress linear prediction, then zlib",
        "MS:1002747" => "MS-Numpress positive integer, then zlib",
        "MS:1002748" => "MS-Numpress short logged float, then zlib",
        _ => return None,
    })
}

/// Reads the spectrum or chromatogram that `bytes` start with, keeping at most `max_points`.
///
/// # Errors
///
/// A message for a person if it isn't one, or its arrays can't be read.
pub fn read_element(bytes: &[u8], max_points: usize) -> Result<Element, String> {
    let mut reader = reader(bytes);
    let (root, empty) = root(&mut reader, bytes)?;
    let kind = local_name(&root);
    let spectrum = match kind.as_str() {
        "spectrum" => true,
        "chromatogram" => false,
        _ => {
            return Err(format!(
                "This isn't a spectrum or chromatogram: it starts with <{kind}>. The offset it \
                 was read from may be wrong."
            ));
        }
    };
    let id = attribute(&root, "id")?.unwrap_or_default();
    let index = attribute(&root, "index")?.and_then(|index| index.trim().parse().ok());
    let declared = attribute(&root, "defaultArrayLength")?;
    let Some(points) = declared
        .as_deref()
        .and_then(|n| n.trim().parse::<usize>().ok())
    else {
        return Err(format!(
            "The {kind} \"{id}\" doesn't say how many points it has: its defaultArrayLength is \
             {}.",
            declared.map_or_else(|| "missing".to_owned(), |n| format!("\"{n}\""))
        ));
    };
    let mut walk = Walk {
        kind,
        id,
        spectrum,
        points,
        parsed: Parsed::default(),
        depth: 1,
        precursor: 0,
        selected_ion: false,
        array: None,
        binary: false,
    };
    if !empty {
        walk.through(&mut reader, bytes)?;
    }
    walk.into_element(index, max_points)
}

fn not_well_formed(reader: &Reader<&[u8]>, error: &quick_xml::Error) -> String {
    format!(
        "This spectrum or chromatogram isn't well-formed XML at byte {} of it: {error}.",
        count_u64(reader.error_position())
    )
}

/// The element `bytes` start with, and whether it's empty (`<spectrum ... />`).
fn root<'a>(reader: &mut Reader<&'a [u8]>, bytes: &[u8]) -> Result<(BytesStart<'a>, bool), String> {
    loop {
        match reader.read_event() {
            Ok(Event::Start(element)) => return Ok((element, false)),
            Ok(Event::Empty(element)) => return Ok((element, true)),
            Ok(Event::DocType(_)) => return Err(DOCTYPE.to_owned()),
            Ok(Event::Text(text)) if text.xml10_content().trim().is_empty() => {}
            Ok(Event::Comment(_) | Event::Decl(_) | Event::PI(_)) => {}
            Ok(Event::Eof) => {
                return Err(
                    "There's no spectrum or chromatogram here: the bytes are empty.".to_owned(),
                );
            }
            Ok(_) => {
                return Err(
                    "This isn't a spectrum or chromatogram: it doesn't start with <spectrum> or \
                     <chromatogram>. The offset it was read from may be wrong."
                        .to_owned(),
                );
            }
            Err(_) if cut_off(bytes, reader.error_position()) => {
                return Err(format!(
                    "The spectrum or chromatogram is cut short: the {} bytes read end inside \
                     its start tag.",
                    count(bytes.len())
                ));
            }
            Err(error) => return Err(not_well_formed(reader, &error)),
        }
    }
}

/// A walk through a spectrum's or chromatogram's elements, to its end tag.
struct Walk<'a> {
    kind: String,
    id: String,
    spectrum: bool,
    points: usize,
    parsed: Parsed,
    depth: usize,
    /// How many `<precursor>` or `<product>` elements it's in.
    precursor: usize,
    selected_ion: bool,
    array: Option<Array<'a>>,
    binary: bool,
}

impl<'a> Walk<'a> {
    fn through(&mut self, reader: &mut Reader<&'a [u8]>, bytes: &[u8]) -> Result<(), String> {
        loop {
            let event = match reader.read_event() {
                Ok(event) => event,
                Err(_) if cut_off(bytes, reader.error_position()) => {
                    return Err(cut_short(&self.kind, &self.id, bytes.len()));
                }
                Err(error) => return Err(not_well_formed(reader, &error)),
            };
            match event {
                Event::Start(element) => self.open(&element, true)?,
                Event::Empty(element) => self.open(&element, false)?,
                Event::Text(text) => {
                    if self.binary
                        && let Some(array) = &mut self.array
                    {
                        array.text.push(text.into_inner());
                    }
                }
                Event::GeneralRef(_) | Event::CData(_) if self.binary => {
                    return Err(format!(
                        "A binary array of the {} \"{}\" has markup in its base64.",
                        self.kind, self.id
                    ));
                }
                Event::End(element) => {
                    self.close(element.local_name().as_ref())?;
                    if self.depth == 0 {
                        return Ok(());
                    }
                }
                Event::DocType(_) => return Err(DOCTYPE.to_owned()),
                Event::Eof => return Err(cut_short(&self.kind, &self.id, bytes.len())),
                _ => {}
            }
        }
    }

    fn open(&mut self, element: &BytesStart, opens: bool) -> Result<(), String> {
        if opens {
            self.depth += 1;
            if self.depth > MAX_DEPTH {
                return Err(format!(
                    "The {} \"{}\" nests elements more than {MAX_DEPTH} deep; mzML's go about \
                     ten deep.",
                    self.kind, self.id
                ));
            }
        }
        match local_name(element).as_str() {
            "binaryDataArray" => {
                self.array = Some(Array {
                    length: attribute(element, "arrayLength")?.and_then(|n| n.trim().parse().ok()),
                    ..Array::default()
                });
                if !opens {
                    self.finish_array()?;
                }
            }
            "binary" => self.binary = opens,
            "precursor" | "product" if opens => self.precursor += 1,
            "selectedIon" if opens => self.selected_ion = true,
            "referenceableParamGroupRef" => {
                if let Some(array) = &mut self.array {
                    array.group = true;
                }
            }
            // A child of the chromatogram, if it's at depth 2 once open.
            "cvParam" => self.param(element, self.depth + usize::from(!opens) == 2)?,
            _ => {}
        }
        Ok(())
    }

    fn close(&mut self, name: &str) -> Result<(), String> {
        self.depth = self.depth.saturating_sub(1);
        match name {
            "binary" => self.binary = false,
            "precursor" | "product" => self.precursor = self.precursor.saturating_sub(1),
            "selectedIon" => self.selected_ion = false,
            "binaryDataArray" => self.finish_array()?,
            _ => {}
        }
        Ok(())
    }

    fn finish_array(&mut self) -> Result<(), String> {
        match self.array.take() {
            Some(array) => finish_array(
                array,
                self.spectrum,
                self.points,
                &self.kind,
                &self.id,
                &mut self.parsed,
            ),
            None => Ok(()),
        }
    }

    /// A `<cvParam>`: the array's, a precursor's, or the spectrum's or chromatogram's own.
    fn param(&mut self, element: &BytesStart, child: bool) -> Result<(), String> {
        let accession = attribute(element, "accession")?.unwrap_or_default();
        let value = || -> Result<Option<f64>, String> {
            Ok(attribute(element, "value")?
                .and_then(|value| value.trim().parse::<f64>().ok())
                .filter(|value| value.is_finite()))
        };
        if let Some(array) = &mut self.array {
            return array_param(array, &accession, element);
        }
        let parsed = &mut self.parsed;
        if self.precursor > 0 {
            if self.selected_ion
                && accession == "MS:1000744"
                && let Some(mz) = value()?
            {
                parsed.precursor_mz.push(mz);
            }
            return Ok(());
        }
        match accession.as_str() {
            "MS:1000511" => {
                parsed.ms_level =
                    attribute(element, "value")?.and_then(|level| level.trim().parse().ok());
            }
            "MS:1000016" if !parsed.scan_started => {
                parsed.scan_started = true;
                let unit = attribute(element, "unitAccession")?;
                let unit_name = attribute(element, "unitName")?;
                parsed.scan_start_minutes = value()?
                    .zip(minutes_per(unit.as_deref(), unit_name.as_deref()))
                    .map(|(time, minutes)| time * minutes);
            }
            "MS:1000130" => parsed.polarity = Some("positive".to_owned()),
            "MS:1000129" => parsed.polarity = Some("negative".to_owned()),
            "MS:1000127" => parsed.centroided = Some(true),
            "MS:1000128" => parsed.centroided = Some(false),
            "MS:1000504" => parsed.base_peak_mz = value()?,
            "MS:1000505" => parsed.base_peak_intensity = value()?,
            "MS:1000285" => parsed.total_ion_current = value()?,
            _ if !self.spectrum && child => {
                let label = attribute(element, "name")?.unwrap_or_default();
                if CHROMATOGRAM_TYPES.contains(&accession.as_str())
                    || label.ends_with(" chromatogram")
                {
                    parsed.chromatogram_type =
                        Some(if label.is_empty() { accession } else { label });
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn into_element(self, index: Option<usize>, max_points: usize) -> Result<Element, String> {
        let Walk {
            id,
            spectrum,
            points,
            parsed,
            ..
        } = self;
        if !spectrum {
            let (time, intensity, truncated) = pair(
                parsed.time,
                parsed.intensity,
                points,
                (Role::Time, Role::Intensity),
                &id,
            )?;
            let (time_minutes, intensity, cut) = keep_extremes(time, intensity, max_points);
            return Ok(Element::Chromatogram(Chromatogram {
                id,
                chromatogram_type: parsed.chromatogram_type,
                time_minutes,
                intensity,
                points,
                truncated: truncated || cut,
            }));
        }
        let (mz, intensity, truncated) = pair(
            parsed.mz,
            parsed.intensity,
            points,
            (Role::Mz, Role::Intensity),
            &id,
        )?;
        let (mz, intensity, cut) = if parsed.centroided == Some(true) {
            keep_most_intense(mz, intensity, max_points)
        } else {
            keep_extremes(mz, intensity, max_points)
        };
        Ok(Element::Spectrum(Spectrum {
            id,
            index,
            ms_level: parsed.ms_level,
            scan_start_minutes: parsed.scan_start_minutes,
            polarity: parsed.polarity,
            centroided: parsed.centroided,
            base_peak_mz: parsed.base_peak_mz,
            base_peak_intensity: parsed.base_peak_intensity,
            total_ion_current: parsed.total_ion_current,
            precursor_mz: parsed.precursor_mz,
            mz,
            intensity,
            points,
            truncated: truncated || cut,
        }))
    }
}

fn cut_short(kind: &str, id: &str, read: usize) -> String {
    format!(
        "The {kind} \"{id}\" is cut short: the {} bytes read end before its </{kind}>.",
        count(read)
    )
}

/// A binary array's own parameters.
fn array_param(array: &mut Array, accession: &str, element: &BytesStart) -> Result<(), String> {
    match accession {
        "MS:1000514" => array.role = Some(Role::Mz),
        "MS:1000515" => array.role = Some(Role::Intensity),
        "MS:1000595" => {
            array.role = Some(Role::Time);
            let unit = attribute(element, "unitAccession")?;
            let name = attribute(element, "unitName")?;
            array.minutes_per_unit = minutes_per(unit.as_deref(), name.as_deref());
            array.unit = name.or(unit);
        }
        "MS:1000521" => array.number = Some(Number::F32),
        "MS:1000523" => array.number = Some(Number::F64),
        "MS:1000519" => array.number = Some(Number::I32),
        "MS:1000522" => array.number = Some(Number::I64),
        "MS:1000520" => array.number = Some(Number::Other("16-bit floats")),
        "MS:1000574" => array.zlib = true,
        "MS:1000576" => array.zlib = false,
        other => {
            if let Some(method) = numpress(other) {
                array.numpress = Some(method.to_owned());
            }
        }
    }
    Ok(())
}

/// Decodes a finished array if its element needs it.
fn finish_array(
    array: Array,
    spectrum: bool,
    points: usize,
    kind: &str,
    id: &str,
    parsed: &mut Parsed,
) -> Result<(), String> {
    let Some(role) = array.role else {
        return Ok(());
    };
    let slot = match (role, spectrum) {
        (Role::Mz, true) => &mut parsed.mz,
        (Role::Intensity, _) => &mut parsed.intensity,
        (Role::Time, false) => &mut parsed.time,
        _ => return Ok(()),
    };
    if slot.is_some() {
        return Ok(());
    }
    let what = format!("{} array of the {kind} \"{id}\"", role.name());
    let expected = array.length.unwrap_or(points);
    let mut values = decode(&array, expected, &what, parsed.decoded_bytes)?;
    parsed.decoded_bytes += values.len() * 8;
    if role == Role::Time {
        let Some(minutes) = array.minutes_per_unit else {
            return Err(format!(
                "The {what} {}, so its times can't be put in minutes.",
                array.unit.map_or_else(
                    || "doesn't say its unit".to_owned(),
                    |unit| format!("is in \"{unit}\", which previews don't know")
                )
            ));
        };
        if (minutes - 1.0).abs() > f64::EPSILON {
            for value in &mut values {
                *value *= minutes;
            }
        }
    }
    *slot = Some(values);
    Ok(())
}

/// A binary array's values.
fn decode(array: &Array, expected: usize, what: &str, so_far: usize) -> Result<Vec<f64>, String> {
    if let Some(method) = &array.numpress {
        return Err(format!(
            "The {what} is compressed with {method} (MS-Numpress), which previews don't read \
             yet."
        ));
    }
    let width = match array.number {
        Some(Number::F32 | Number::I32) => 4,
        Some(Number::F64 | Number::I64) => 8,
        Some(Number::Other(name)) => {
            return Err(format!(
                "The {what} holds {name}, which previews don't read."
            ));
        }
        None => {
            return Err(format!(
                "The {what} doesn't say what its numbers are (32- or 64-bit floats or integers){}.",
                if array.group {
                    "; it may take that from a referenceableParamGroup, which previews don't read"
                } else {
                    ""
                }
            ));
        }
    };
    let size = expected
        .checked_mul(width)
        .filter(|size| *size <= MAX_ARRAY_BYTES)
        .filter(|size| {
            so_far.saturating_add(expected.saturating_mul(8)) <= MAX_ELEMENT_BYTES && *size > 0
                || expected == 0
        })
        .ok_or_else(|| {
            format!(
                "The {what} has {} points, more than a preview reads ({} bytes of arrays).",
                count(expected),
                count(MAX_ARRAY_BYTES)
            )
        })?;
    let mut encoded = Vec::with_capacity(array.text.iter().map(|part| part.len()).sum());
    for part in &array.text {
        encoded.extend(part.bytes().filter(|byte| !byte.is_ascii_whitespace()));
    }
    // Uncompressed, the base64 can only be so long; don't decode more than that.
    if !array.zlib && encoded.len() / 4 * 3 > size + 3 {
        return Err(format!(
            "The {what} has {} bytes of data, but its {} points need {}.",
            count(encoded.len() / 4 * 3),
            count(expected),
            count(size)
        ));
    }
    let raw = BASE64
        .decode(&encoded)
        .map_err(|error| format!("The {what} isn't valid base64: {error}."))?;
    let raw = if array.zlib {
        // One byte more than it needs, so an array that's too long is seen as one.
        miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(&raw, size.saturating_add(1))
            .map_err(|error| match error.status {
                miniz_oxide::inflate::TINFLStatus::HasMoreOutput => format!(
                    "The {what} inflates to more than the {} bytes its {} points need.",
                    count(size),
                    count(expected)
                ),
                _ => format!("The {what} isn't valid zlib data ({:?}).", error.status),
            })?
    } else {
        raw
    };
    if raw.len() != size {
        return Err(if raw.len() % width == 0 {
            format!(
                "The {what} has {} values, but it should have {} (its defaultArrayLength).",
                count(raw.len() / width),
                count(expected)
            )
        } else {
            format!(
                "The {what} is {} bytes, not a whole number of {width}-byte values.",
                count(raw.len())
            )
        });
    }
    #[allow(clippy::cast_precision_loss)]
    let values = raw
        .chunks_exact(width)
        .map(|chunk| match (array.number, chunk) {
            (Some(Number::F32), [a, b, c, d]) => f64::from(f32::from_le_bytes([*a, *b, *c, *d])),
            (Some(Number::I32), [a, b, c, d]) => f64::from(i32::from_le_bytes([*a, *b, *c, *d])),
            (Some(Number::F64), bytes) => f64::from_le_bytes(bytes.try_into().unwrap_or([0; 8])),
            (_, bytes) => i64::from_le_bytes(bytes.try_into().unwrap_or([0; 8])) as f64,
        })
        .collect();
    Ok(values)
}

/// Pairs an element's two arrays, leaving out points that aren't finite numbers.
fn pair(
    x: Option<Vec<f64>>,
    y: Option<Vec<f64>>,
    points: usize,
    (x_role, y_role): (Role, Role),
    id: &str,
) -> Result<(Vec<f64>, Vec<f64>, bool), String> {
    let missing = |role: Role| {
        format!(
            "\"{id}\" has {} points but no {} array.",
            count(points),
            role.name()
        )
    };
    let (x, y) = match (x, y) {
        (Some(x), Some(y)) => (x, y),
        (None, None) if points == 0 => return Ok((Vec::new(), Vec::new(), false)),
        (None, _) => return Err(missing(x_role)),
        (_, None) => return Err(missing(y_role)),
    };
    if x.len() != y.len() {
        return Err(format!(
            "\"{id}\" has {} {} values but {} {} values.",
            count(x.len()),
            x_role.name(),
            count(y.len()),
            y_role.name()
        ));
    }
    if x.iter().chain(&y).all(|value| value.is_finite()) {
        return Ok((x, y, false));
    }
    let (x, y): (Vec<f64>, Vec<f64>) = x
        .into_iter()
        .zip(y)
        .filter(|(x, y)| x.is_finite() && y.is_finite())
        .unzip();
    Ok((x, y, true))
}

/// For profile spectra and chromatograms: the lowest and highest point of each of
/// `max / 2` runs of points, in order, so peaks and the baseline both stay.
fn keep_extremes(x: Vec<f64>, y: Vec<f64>, max: usize) -> (Vec<f64>, Vec<f64>, bool) {
    let n = x.len();
    if n <= max {
        return (x, y, false);
    }
    let buckets = (max / 2).max(usize::from(max > 0));
    let mut out_x = Vec::with_capacity(max);
    let mut out_y = Vec::with_capacity(max);
    let bound = |bucket: usize| -> usize {
        let at = (bucket as u128 * n as u128) / buckets as u128;
        usize::try_from(at).unwrap_or(n)
    };
    for bucket in 0..buckets {
        let (start, end) = (bound(bucket), bound(bucket + 1).min(n));
        if start >= end {
            continue;
        }
        let run = &y[start..end];
        let (mut low, mut high) = (0, 0);
        for (offset, value) in run.iter().enumerate() {
            if *value < run[low] {
                low = offset;
            }
            if *value > run[high] {
                high = offset;
            }
        }
        let mut keep = if max == 1 {
            vec![high]
        } else {
            vec![low, high]
        };
        keep.sort_unstable();
        keep.dedup();
        for offset in keep {
            out_x.push(x[start + offset]);
            out_y.push(y[start + offset]);
        }
    }
    (out_x, out_y, true)
}

/// For centroided spectra: the `max` most intense peaks, in m/z order.
fn keep_most_intense(x: Vec<f64>, y: Vec<f64>, max: usize) -> (Vec<f64>, Vec<f64>, bool) {
    let n = x.len();
    if n <= max {
        return (x, y, false);
    }
    if max == 0 {
        return (Vec::new(), Vec::new(), true);
    }
    let mut order: Vec<usize> = (0..n).collect();
    order.select_nth_unstable_by(max - 1, |a, b| y[*b].total_cmp(&y[*a]));
    order.truncate(max);
    order.sort_unstable_by(|a, b| x[*a].total_cmp(&x[*b]).then(a.cmp(b)));
    let out_x = order.iter().map(|at| x[*at]).collect();
    let out_y = order.iter().map(|at| y[*at]).collect();
    (out_x, out_y, true)
}

fn count_u64(value: u64) -> String {
    count(usize::try_from(value).unwrap_or(usize::MAX))
}

/// A count with thousands separators, as people read them.
fn count(value: usize) -> String {
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    fn encode_f64(values: &[f64]) -> Vec<u8> {
        values.iter().flat_map(|v| v.to_le_bytes()).collect()
    }

    fn encode_f32(values: &[f32]) -> Vec<u8> {
        values.iter().flat_map(|v| v.to_le_bytes()).collect()
    }

    fn b64(bytes: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(bytes)
    }

    fn zlib(bytes: &[u8]) -> Vec<u8> {
        miniz_oxide::deflate::compress_to_vec_zlib(bytes, 6)
    }

    fn array(params: &[&str], data: &str) -> String {
        let params = params.iter().fold(String::new(), |mut out, p| {
            write!(
                out,
                "<cvParam cvRef=\"MS\" accession=\"{p}\" name=\"\" value=\"\"/>"
            )
            .unwrap();
            out
        });
        format!(
            "<binaryDataArray encodedLength=\"{}\">{params}<binary>{data}</binary></binaryDataArray>",
            data.len()
        )
    }

    fn spectrum(points: usize, params: &str, arrays: &[String]) -> String {
        format!(
            "<spectrum index=\"4\" id=\"scan=5\" defaultArrayLength=\"{points}\">{params}\
             <binaryDataArrayList count=\"2\">{}</binaryDataArrayList></spectrum>",
            arrays.concat()
        )
    }

    fn read_spectrum(xml: &str, max: usize) -> Spectrum {
        match read_element(xml.as_bytes(), max).unwrap() {
            Element::Spectrum(spectrum) => spectrum,
            Element::Chromatogram(_) => unreachable!("a chromatogram"),
        }
    }

    #[test]
    fn finds_the_index_offset_in_a_files_tail() {
        let tail = b"</indexList>\n  <indexListOffset>24498</indexListOffset>\n  <fileChecksum>8a9</fileChecksum>\n</indexedmzML>\n";
        assert_eq!(index_offset(tail), Some(24498));
        assert_eq!(
            index_offset(b"<indexListOffset> 12 </indexListOffset>"),
            Some(12)
        );
        assert_eq!(index_offset(b"</mzML>"), None);
        assert_eq!(index_offset(b"<indexListOffset>-1</indexListOffset>"), None);
        assert_eq!(
            index_offset(b"<indexListOffset>99999999999999999999999</indexListOffset>"),
            None
        );
        assert_eq!(index_offset(b"<indexListOffset>12"), None);
        // The last of several wins: an edited file appends.
        assert_eq!(
            index_offset(
                b"<indexListOffset>1</indexListOffset><indexListOffset>2</indexListOffset>"
            ),
            Some(2)
        );
    }

    #[test]
    fn reads_an_index_and_unescapes_its_ids() {
        let xml = br#"<indexList count="2">
            <index name="spectrum"><offset idRef="a &amp; b">10</offset><offset idRef="c">20</offset></index>
            <index name="chromatogram"><offset idRef="TIC">30</offset></index>
          </indexList>
          <indexListOffset>5</indexListOffset></indexedmzML>"#;
        let index = read_index(xml).unwrap();
        assert_eq!(
            index.spectra,
            vec![
                Entry {
                    id: "a & b".to_owned(),
                    offset: 10
                },
                Entry {
                    id: "c".to_owned(),
                    offset: 20
                },
            ]
        );
        assert_eq!(
            index.chromatograms,
            vec![Entry {
                id: "TIC".to_owned(),
                offset: 30
            }]
        );
        assert!(!index.truncated);
        assert_eq!(index.declared_spectra, None);
    }

    #[test]
    fn an_index_offset_that_misses_says_so() {
        let error = read_index(b"spectrum index=\"0\" id=\"x\">").unwrap_err();
        assert!(error.contains("<indexListOffset> may be wrong"), "{error}");
        let error = read_index(b"<run>").unwrap_err();
        assert!(error.contains("starts with <run>"), "{error}");
        let error =
            read_index(b"<indexList><index name=\"spectrum\"><offset idRef=\"x\">12a</offset>")
                .unwrap_err();
        assert!(error.contains("\"x\"") && error.contains("12a"), "{error}");
    }

    #[test]
    fn a_cut_index_is_truncated() {
        let index = read_index(
            b"<indexList><index name=\"spectrum\"><offset idRef=\"x\">12</offset><offset idR",
        )
        .unwrap();
        assert_eq!(index.spectra.len(), 1);
        assert!(index.truncated);
    }

    const HEAD: &str = "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<indexedmzML xmlns=\"http://psi.hupo.org/ms/mzml\">\n<mzML><run id=\"r\">\
                        <spectrumList count=\"3\">";

    #[test]
    fn scans_for_spectra_and_chromatograms() {
        let xml = format!(
            "{HEAD}<spectrum index=\"0\" id=\"s0\" defaultArrayLength=\"0\"/>\n<spectrum index=\"1\" id=\"s&quot;1\" defaultArrayLength=\"0\"></spectrum>\
             </spectrumList><chromatogramList count=\"1\"><chromatogram id=\"TIC\" defaultArrayLength=\"0\"/></chromatogramList></run></mzML>\
             <indexList/></indexedmzML>"
        );
        let index = scan(xml.as_bytes(), true).unwrap();
        assert_eq!(index.declared_spectra, Some(3));
        assert_eq!(index.spectra.len(), 2);
        assert_eq!(index.spectra[1].id, "s\"1");
        for entry in index.spectra.iter().chain(&index.chromatograms) {
            let at = usize::try_from(entry.offset).unwrap();
            assert!(
                xml[at..].starts_with("<spectrum ") || xml[at..].starts_with("<chromatogram "),
                "{entry:?}"
            );
        }
        assert_eq!(index.chromatograms[0].id, "TIC");
        assert!(!index.truncated);
    }

    #[test]
    fn scanning_a_prefix_says_it_was_cut_wherever_it_ends() {
        let xml = format!(
            "{HEAD}<spectrum id=\"s0\" defaultArrayLength=\"1\"><binaryDataArrayList><binaryDataArray><binary>AAAAAAAAAAA=</binary>\
             </binaryDataArray></binaryDataArrayList></spectrum><spectrum id=\"s1\" defaultArrayLength=\"0\"/></spectrumList></run></mzML>"
        );
        let whole = scan(xml.as_bytes(), false).unwrap();
        assert!(!whole.truncated);
        assert_eq!(whole.spectra.len(), 2);
        let first = usize::try_from(whole.spectra[0].offset).unwrap();
        for end in first..xml.len() - 1 {
            let index = scan(&xml.as_bytes()[..end], false)
                .map_err(|error| format!("cut at {end}: {error}"))
                .unwrap();
            assert!(index.truncated, "cut at {end}");
            assert!(index.spectra.len() <= 2);
        }
        // Cut inside a multi-byte character, too.
        let accented = format!("{HEAD}<spectrum id=\"é\"");
        let cut = &accented.as_bytes()[..accented.len() - 2];
        assert!(scan(cut, false).unwrap().truncated);
    }

    #[test]
    fn a_complete_file_is_only_truncated_past_the_limit() {
        let xml = format!("{HEAD}<spectrum id=\"s0\" defaultArrayLength=\"0\"/>");
        let index = scan(xml.as_bytes(), true).unwrap();
        assert!(!index.truncated);
        assert_eq!(index.spectra.len(), 1);
    }

    #[test]
    fn scanning_refuses_what_isnt_mzml() {
        assert!(scan(b"", true).unwrap_err().contains("empty"));
        assert!(
            scan(b"<html><body/></html>", true)
                .unwrap_err()
                .contains("<html>")
        );
        assert!(
            scan(b"not xml at all", true)
                .unwrap_err()
                .contains("no <mzML>")
        );
        let error = scan(b"<?xml version=\"1.0\"?><!DOCTYPE lolz [<!ENTITY lol \"lol\"><!ENTITY lol2 \"&lol;&lol;&lol;\">]><mzML>&lol2;</mzML>", true).unwrap_err();
        assert!(error.contains("DOCTYPE"), "{error}");
        let error = scan(b"<mzML><run></spectrumList></run></mzML>", true).unwrap_err();
        assert!(error.contains("byte"), "{error}");
    }

    #[test]
    fn deep_nesting_is_refused() {
        let xml = format!("<mzML>{}", "<a>".repeat(10_000));
        assert!(scan(xml.as_bytes(), false).unwrap_err().contains("nest"));
        let xml = format!(
            "<spectrum id=\"x\" defaultArrayLength=\"0\">{}",
            "<a>".repeat(10_000)
        );
        assert!(
            read_element(xml.as_bytes(), 10)
                .unwrap_err()
                .contains("nest")
        );
    }

    #[test]
    fn reads_a_64_bit_zlib_spectrum_and_its_parameters() {
        let mz = [100.5, 200.25, 300.125];
        let intensity = [10.0f32, 1000.0, 5.0];
        let xml = spectrum(
            3,
            "<cvParam accession=\"MS:1000511\" value=\"2\"/><cvParam accession=\"MS:1000129\"/>\
             <cvParam accession=\"MS:1000127\"/><cvParam accession=\"MS:1000504\" value=\"200.25\"/>\
             <cvParam accession=\"MS:1000505\" value=\"1000\"/><cvParam accession=\"MS:1000285\" value=\"1015\"/>\
             <scanList><scan><cvParam accession=\"MS:1000016\" value=\"90\" unitAccession=\"UO:0000010\" unitName=\"second\"/></scan></scanList>\
             <precursorList><precursor><isolationWindow><cvParam accession=\"MS:1000827\" value=\"445.3\"/></isolationWindow>\
             <selectedIonList><selectedIon><cvParam accession=\"MS:1000744\" value=\"445.34\"/><cvParam accession=\"MS:1000511\" value=\"9\"/></selectedIon></selectedIonList>\
             </precursor></precursorList>",
            &[
                array(
                    &["MS:1000523", "MS:1000574", "MS:1000514"],
                    &b64(&zlib(&encode_f64(&mz))),
                ),
                array(
                    &["MS:1000521", "MS:1000576", "MS:1000515"],
                    &b64(&encode_f32(&intensity)),
                ),
            ],
        );
        let spectrum = read_spectrum(&xml, 100);
        assert_eq!(spectrum.id, "scan=5");
        assert_eq!(spectrum.index, Some(4));
        assert_eq!(
            spectrum.ms_level,
            Some(2),
            "a precursor's parameters aren't the spectrum's"
        );
        assert_eq!(spectrum.polarity.as_deref(), Some("negative"));
        assert_eq!(spectrum.centroided, Some(true));
        assert_eq!(spectrum.base_peak_mz, Some(200.25));
        assert_eq!(spectrum.base_peak_intensity, Some(1000.0));
        assert_eq!(spectrum.total_ion_current, Some(1015.0));
        assert_eq!(spectrum.scan_start_minutes, Some(1.5));
        assert_eq!(spectrum.precursor_mz, vec![445.34]);
        assert_eq!(spectrum.mz, mz.to_vec());
        assert_eq!(spectrum.intensity, vec![10.0, 1000.0, 5.0]);
        assert_eq!(spectrum.points, 3);
        assert!(!spectrum.truncated);
    }

    #[test]
    fn reads_integer_arrays_and_base64_with_line_breaks() {
        let mz: Vec<u8> = [1i32, 2, 3].iter().flat_map(|v| v.to_le_bytes()).collect();
        let intensity: Vec<u8> = [7i64, -8, 9].iter().flat_map(|v| v.to_le_bytes()).collect();
        let wrapped = b64(&intensity)
            .as_bytes()
            .chunks(4)
            .fold(String::new(), |mut out, c| {
                write!(out, "{}\n ", std::str::from_utf8(c).unwrap()).unwrap();
                out
            });
        let xml = spectrum(
            3,
            "",
            &[
                array(&["MS:1000519", "MS:1000514"], &b64(&mz)),
                array(&["MS:1000522", "MS:1000515"], &wrapped),
            ],
        );
        let spectrum = read_spectrum(&xml, 100);
        assert_eq!(spectrum.mz, vec![1.0, 2.0, 3.0]);
        assert_eq!(spectrum.intensity, vec![7.0, -8.0, 9.0]);
    }

    #[test]
    fn reads_a_chromatogram_in_seconds_as_minutes() {
        let time = [0.0, 30.0, 60.0, 90.0];
        let intensity = [1.0, 4.0, 2.0, 0.5];
        let xml = format!(
            "<chromatogram index=\"0\" id=\"SRM SIC 445.3,678.9\" defaultArrayLength=\"4\">\
             <cvParam cvRef=\"MS\" accession=\"MS:1001473\" name=\"selected reaction monitoring chromatogram\" value=\"\"/>\
             <precursor><isolationWindow><cvParam accession=\"MS:1000827\" value=\"445.3\"/></isolationWindow></precursor>\
             <binaryDataArrayList count=\"2\">\
             <binaryDataArray><cvParam accession=\"MS:1000523\"/><cvParam accession=\"MS:1000574\"/>\
             <cvParam accession=\"MS:1000595\" unitAccession=\"UO:0000010\" unitName=\"second\"/><binary>{}</binary></binaryDataArray>\
             <binaryDataArray><cvParam accession=\"MS:1000523\"/><cvParam accession=\"MS:1000576\"/>\
             <cvParam accession=\"MS:1000515\"/><binary>{}</binary></binaryDataArray>\
             </binaryDataArrayList></chromatogram><chromatogram id=\"next\" defaultArr",
            b64(&zlib(&encode_f64(&time))),
            b64(&encode_f64(&intensity)),
        );
        let Element::Chromatogram(chromatogram) = read_element(xml.as_bytes(), 100).unwrap() else {
            unreachable!("a spectrum");
        };
        assert_eq!(chromatogram.id, "SRM SIC 445.3,678.9");
        assert_eq!(
            chromatogram.chromatogram_type.as_deref(),
            Some("selected reaction monitoring chromatogram")
        );
        assert_eq!(chromatogram.time_minutes, vec![0.0, 0.5, 1.0, 1.5]);
        assert_eq!(chromatogram.intensity, intensity.to_vec());
        let json =
            String::from_utf8(crate::to_json(Ok(Element::Chromatogram(chromatogram)))).unwrap();
        assert!(json.starts_with("{\"kind\":\"chromatogram\""), "{json}");
    }

    #[test]
    fn a_time_array_without_a_unit_is_an_error() {
        let xml = format!(
            "<chromatogram id=\"TIC\" defaultArrayLength=\"1\"><binaryDataArray><cvParam accession=\"MS:1000523\"/>\
             <cvParam accession=\"MS:1000595\"/><binary>{0}</binary></binaryDataArray>\
             <binaryDataArray><cvParam accession=\"MS:1000523\"/><cvParam accession=\"MS:1000515\"/><binary>{0}</binary></binaryDataArray></chromatogram>",
            b64(&encode_f64(&[1.0]))
        );
        let error = read_element(xml.as_bytes(), 10).unwrap_err();
        assert!(error.contains("doesn't say its unit"), "{error}");
    }

    #[test]
    fn numpress_is_refused_by_name() {
        for accession in [
            "MS:1002312",
            "MS:1002313",
            "MS:1002314",
            "MS:1002746",
            "MS:1002747",
            "MS:1002748",
        ] {
            let xml = spectrum(
                1,
                "",
                &[
                    array(&[accession, "MS:1000514", "MS:1000523"], "AAAA"),
                    array(&["MS:1000515", "MS:1000523"], "AAAA"),
                ],
            );
            let error = read_element(xml.as_bytes(), 10).unwrap_err();
            assert!(
                error.contains("MS-Numpress") && error.contains("m/z array"),
                "{error}"
            );
        }
    }

    #[test]
    fn malformed_arrays_are_errors_that_say_which() {
        let good = b64(&encode_f64(&[1.0, 2.0]));
        let cases = [
            (
                array(&["MS:1000523", "MS:1000514"], "!!!notbase64"),
                "isn't valid base64",
            ),
            (
                array(&["MS:1000523", "MS:1000514"], &b64(&encode_f64(&[1.0]))),
                "has 1 values, but it should have 2",
            ),
            (
                array(&["MS:1000523", "MS:1000514"], &b64(&[0u8; 12])),
                "not a whole number of 8-byte values",
            ),
            (
                array(
                    &["MS:1000523", "MS:1000514"],
                    &b64(&encode_f64(&[1.0; 100])),
                ),
                "need 16",
            ),
            (
                array(&["MS:1000523", "MS:1000574", "MS:1000514"], &good),
                "isn't valid zlib",
            ),
            (
                array(&["MS:1000514"], &good),
                "doesn't say what its numbers are",
            ),
            (array(&["MS:1000520", "MS:1000514"], &good), "16-bit floats"),
        ];
        for (mz, message) in cases {
            let xml = spectrum(2, "", &[mz, array(&["MS:1000523", "MS:1000515"], &good)]);
            let error = read_element(xml.as_bytes(), 10).unwrap_err();
            assert!(error.contains(message), "{message}: {error}");
            assert!(
                error.contains("m/z array of the spectrum \"scan=5\""),
                "{error}"
            );
        }
        let xml = spectrum(2, "", &[array(&["MS:1000523", "MS:1000515"], &good)]);
        assert!(
            read_element(xml.as_bytes(), 10)
                .unwrap_err()
                .contains("no m/z array")
        );
    }

    #[test]
    fn a_zlib_bomb_is_refused_without_inflating_it() {
        // 64 MiB of zeros compresses to about 64 KiB; the spectrum says it has 4 points.
        let bomb = zlib(&vec![0u8; 64 * 1024 * 1024]);
        assert!(bomb.len() < 100_000);
        let xml = spectrum(
            4,
            "",
            &[
                array(&["MS:1000523", "MS:1000574", "MS:1000514"], &b64(&bomb)),
                array(&["MS:1000523", "MS:1000515"], &b64(&encode_f64(&[1.0; 4]))),
            ],
        );
        let error = read_element(xml.as_bytes(), 10).unwrap_err();
        assert!(
            error.contains("inflates to more than the 32 bytes its 4 points need"),
            "{error}"
        );
    }

    #[test]
    fn absurd_lengths_are_refused_before_decoding() {
        for points in ["4000000000", "18446744073709551615", "-1", "many"] {
            let xml = format!(
                "<spectrum id=\"x\" defaultArrayLength=\"{points}\"><binaryDataArray><cvParam accession=\"MS:1000523\"/><cvParam accession=\"MS:1000514\"/><binary>AAAA</binary></binaryDataArray></spectrum>"
            );
            assert!(read_element(xml.as_bytes(), 10).is_err(), "{points}");
        }
        let xml = "<spectrum id=\"x\"></spectrum>";
        assert!(
            read_element(xml.as_bytes(), 10)
                .unwrap_err()
                .contains("defaultArrayLength is missing")
        );
    }

    #[test]
    fn an_element_cut_short_says_so() {
        let xml = spectrum(
            2,
            "",
            &[array(
                &["MS:1000523", "MS:1000514"],
                &b64(&encode_f64(&[1.0, 2.0])),
            )],
        );
        for end in [20, xml.len() / 2, xml.len() - 3] {
            let error = read_element(&xml.as_bytes()[..end], 10).unwrap_err();
            assert!(error.contains("cut short"), "{end}: {error}");
        }
    }

    #[test]
    fn what_isnt_a_spectrum_is_refused() {
        assert!(read_element(b"", 10).unwrap_err().contains("empty"));
        assert!(
            read_element(b"   <run id=\"x\">", 10)
                .unwrap_err()
                .contains("starts with <run>")
        );
        assert!(
            read_element(b"pectrum id=\"x\">", 10)
                .unwrap_err()
                .contains("may be wrong")
        );
        assert!(
            read_element(b"<!DOCTYPE x><spectrum/>", 10)
                .unwrap_err()
                .contains("DOCTYPE")
        );
    }

    #[test]
    fn an_empty_spectrum_has_no_points() {
        let spectrum = read_spectrum(
            "<spectrum index=\"2\" id=\"scan=21\" defaultArrayLength=\"0\"/>",
            10,
        );
        assert!(spectrum.mz.is_empty() && !spectrum.truncated);
    }

    #[test]
    fn non_finite_points_are_left_out() {
        let xml = spectrum(
            3,
            "",
            &[
                array(
                    &["MS:1000523", "MS:1000514"],
                    &b64(&encode_f64(&[1.0, f64::NAN, 3.0])),
                ),
                array(
                    &["MS:1000523", "MS:1000515"],
                    &b64(&encode_f64(&[1.0, 2.0, f64::INFINITY])),
                ),
            ],
        );
        let spectrum = read_spectrum(&xml, 10);
        assert_eq!(spectrum.mz, vec![1.0]);
        assert!(spectrum.truncated);
        assert_eq!(spectrum.points, 3);
    }

    #[test]
    fn downsampling_a_profile_keeps_each_runs_extremes() {
        let n = 10_000;
        let x: Vec<f64> = (0..n).map(f64::from).collect();
        let mut y = vec![1.0; 10_000];
        y[4321] = 500.0; // a narrow peak
        y[777] = -3.0; // a dip
        let (kept_x, kept_y, cut) = keep_extremes(x, y, 100);
        assert!(cut);
        assert!(kept_x.len() <= 100);
        assert!(kept_x.windows(2).all(|pair| pair[0] < pair[1]), "in order");
        assert!(kept_x.contains(&4321.0) && kept_y.contains(&500.0));
        assert!(kept_x.contains(&777.0));
        assert_eq!(keep_extremes(vec![1.0, 2.0], vec![3.0, 4.0], 0).0.len(), 0);
        assert_eq!(
            keep_extremes(vec![1.0, 2.0, 3.0], vec![3.0, 9.0, 4.0], 1).1,
            vec![9.0]
        );
    }

    #[test]
    fn downsampling_centroids_keeps_the_most_intense_in_mz_order() {
        let x = vec![100.0, 200.0, 300.0, 400.0, 500.0];
        let y = vec![5.0, 50.0, 1.0, 40.0, 30.0];
        let (kept_x, kept_y, cut) = keep_most_intense(x, y, 3);
        assert!(cut);
        assert_eq!(kept_x, vec![200.0, 400.0, 500.0]);
        assert_eq!(kept_y, vec![50.0, 40.0, 30.0]);
    }

    #[test]
    fn the_page_limit_downsamples_and_says_so() {
        let mz: Vec<f64> = (0..1000).map(|i| 100.0 + f64::from(i)).collect();
        let intensity: Vec<f64> = (0..1000).map(|i| f64::from(i % 97)).collect();
        let xml = spectrum(
            1000,
            "<cvParam accession=\"MS:1000128\"/>",
            &[
                array(&["MS:1000523", "MS:1000514"], &b64(&encode_f64(&mz))),
                array(&["MS:1000523", "MS:1000515"], &b64(&encode_f64(&intensity))),
            ],
        );
        let spectrum = read_spectrum(&xml, 64);
        assert!(spectrum.truncated);
        assert!(spectrum.mz.len() <= 64 && spectrum.mz.len() >= 32);
        assert_eq!(spectrum.points, 1000);
        assert!(spectrum.intensity.contains(&96.0));
    }

    #[test]
    fn every_truncation_of_an_element_is_handled() {
        let xml = spectrum(
            3,
            "<cvParam accession=\"MS:1000511\" value=\"1\"/>",
            &[
                array(
                    &["MS:1000523", "MS:1000574", "MS:1000514"],
                    &b64(&zlib(&encode_f64(&[1.0, 2.0, 3.0]))),
                ),
                array(
                    &["MS:1000521", "MS:1000515"],
                    &b64(&encode_f32(&[1.0, 2.0, 3.0])),
                ),
            ],
        );
        for end in 0..xml.len() {
            assert!(read_element(&xml.as_bytes()[..end], 10).is_err(), "{end}");
        }
        assert!(read_element(xml.as_bytes(), 10).is_ok());
    }
}

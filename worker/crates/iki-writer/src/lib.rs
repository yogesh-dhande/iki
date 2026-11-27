//! Write an Iki plugin's export format in Rust.
//!
//! A plugin declares a writer in its `plugin.yaml`, and the writer is a WebAssembly module
//! built from a Rust crate in the plugin (ADR 0230). Iki runs it in a sandbox with no
//! imports: no clock, no files, no network. It hands the module the export, then each
//! record, then the end of the file, and the module answers each with the bytes it adds to
//! the file and notes on what the format couldn't hold. This crate is that ABI as a trait.
//!
//! ```
//! use iki_writer::{Part, Record, Refusal, Writer};
//!
//! /// One line per sample: its HRID and its concentration in ng/µL.
//! #[derive(Default)]
//! struct SampleSheet;
//!
//! impl Writer for SampleSheet {
//!     fn record(&mut self, record: &Record) -> Result<Part, Refusal> {
//!         let Some(concentration) = record.quantity("concentration") else {
//!             return Err(Refusal::new(format!("{} has no concentration.", record.hrid)));
//!         };
//!         let line = format!("{},{}\r\n", record.hrid, concentration.value);
//!         Ok(Part::bytes(line))
//!     }
//! }
//!
//! iki_writer::writer!(SampleSheet);
//! ```
//!
//! [`writer!`] generates the module's exports, so a crate has one writer. Its state lives
//! in the module for one file: [`Writer::begin`] starts from `Default`, and
//! [`Writer::end`] ends it. Each call returns a [`Part`], or a [`Refusal`] when the writer
//! can't write that record in its format.
//!
//! # The ABI
//!
//! ABI 1. All of the exports' arguments and results are wasm `i32`s.
//!
//! | export                             | what it does                                                     |
//! | ---------------------------------- | ---------------------------------------------------------------- |
//! | `memory`                           | The module's one memory.                                         |
//! | `iki_abi() -> i32`                 | 1.                                                               |
//! | `iki_input(len) -> ptr`            | A fresh buffer of `len` bytes, into which Iki writes a request.  |
//! | `iki_begin() -> status`            | Starts a file. The request is an [`Export`].                     |
//! | `iki_record() -> status`           | Writes one record. The request is a [`Record`].                  |
//! | `iki_end() -> status`              | Finishes the file. The request is `{}`.                          |
//! | `iki_output()`, `iki_output_len()` | The bytes this call adds to the file.                            |
//! | `iki_notes()`, `iki_notes_len()`   | This call's notes, as JSON.                                      |
//!
//! Status 0 means written; 1 means refused. The notes are a JSON array of
//! `{"code", "message", "role"}`, `role` left out when a note isn't about one role. A
//! refusal's notes are exactly `[{"code":"refused","message":"…"}]`, and it adds no
//! bytes. Requests are canonical JSON (keys sorted, no insignificant whitespace, UTF-8),
//! at most [`MAX_REQUEST_BYTES`]. A request that isn't one is Iki's bug, not the
//! record's, so the module panics, which is a trap under `panic = "abort"`.
//!
//! # Testing a writer
//!
//! Build the crate as a `cdylib` and an `rlib`, and its unit tests run natively: call the
//! writer's methods directly, or drive it through the same code the exports run with
//! [`testing::Session`], which also checks what Iki would read back.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// The JSON crate whose values [`Record::roles`] and [`Reference::fields`] hold, so a
/// writer can use them without depending on it itself.
pub use serde_json;

/// The ABI this crate's modules speak, which `iki_abi` returns.
pub const ABI: i32 = 1;

/// The most a request may be: 64 MiB.
pub const MAX_REQUEST_BYTES: usize = 64 * 1024 * 1024;

/// A writer: turns records into the bytes of a file, one call at a time.
///
/// Iki calls [`begin`](Self::begin) once, [`record`](Self::record) once per record in
/// the export's order, then [`end`](Self::end). Each returns the bytes it adds to the file
/// and its notes. A file that holds one record per file (`many: zip`) is a `begin`, a
/// `record` and an `end` per record, and each `begin` starts from `Default` again.
pub trait Writer: Default {
    /// Starts a file: a header, say. Writes nothing unless overridden.
    ///
    /// # Errors
    ///
    /// A [`Refusal`] when this writer can't write this export.
    fn begin(&mut self, _export: &Export) -> Result<Part, Refusal> {
        Ok(Part::default())
    }

    /// Writes one record.
    ///
    /// # Errors
    ///
    /// A [`Refusal`] when this record can't be written in this format, saying why.
    fn record(&mut self, record: &Record) -> Result<Part, Refusal>;

    /// Finishes the file: a footer, or a total. Writes nothing unless overridden.
    ///
    /// # Errors
    ///
    /// A [`Refusal`] when the file can't be finished, saying why.
    fn end(&mut self) -> Result<Part, Refusal> {
        Ok(Part::default())
    }
}

/// What a call adds to the file: its bytes and its notes.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Part {
    /// The bytes added to the file, after the previous call's.
    pub bytes: Vec<u8>,
    /// What the file couldn't hold as it was given, or what was changed to fit.
    pub notes: Vec<Note>,
}

impl Part {
    /// A part of these bytes, with no notes.
    #[must_use]
    pub fn bytes(bytes: impl Into<Vec<u8>>) -> Self {
        Self {
            bytes: bytes.into(),
            notes: Vec::new(),
        }
    }

    /// Adds a note about the record as a whole.
    #[must_use]
    pub fn note(mut self, code: impl Into<String>, message: impl Into<String>) -> Self {
        self.notes.push(Note::new(code, message));
        self
    }

    /// Adds a note about one of the writer's roles, such as a value rounded to fit.
    #[must_use]
    pub fn role_note(
        mut self,
        role: impl Into<String>,
        code: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        self.notes.push(Note::for_role(role, code, message));
        self
    }
}

/// A note on what the file couldn't hold, for the person exporting it. Iki lists it with
/// the download, and records it in the export's audit event.
///
/// `code` is short and stable, such as `rounded`; `refused` is the one a refusal gives.
/// Iki takes at most 1,000 notes per record, of 500 characters each.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Note {
    /// What kind of note it is, for machines.
    pub code: String,
    /// What happened, for a person.
    pub message: String,
    /// The writer's role it's about, if it's about one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
}

impl Note {
    /// A note about the record as a whole.
    #[must_use]
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            role: None,
        }
    }

    /// A note about one of the writer's roles.
    #[must_use]
    pub fn for_role(
        role: impl Into<String>,
        code: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            role: Some(role.into()),
        }
    }
}

/// Why a writer can't write a record, or a file, in its format. Iki refuses the whole
/// export, naming the record and quoting the message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    message: String,
}

impl Refusal {
    /// A refusal, with a message for the person exporting: what's wrong, and with what.
    #[must_use]
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    /// What's wrong.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for Refusal {}

/// The export a file is for, as [`Writer::begin`] gets it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Export {
    /// The ABI Iki speaks: 1.
    pub abi: u32,
    /// The writer's qualified ID, such as `acme/liquid_handling.echo-picklist`.
    pub writer: String,
    /// The qualified ID of the format the file is, such as `iki/core.csv`.
    pub format: String,
    /// How many records the file will hold.
    pub records: u64,
}

/// One record at one revision, as [`Writer::record`] gets it.
///
/// Its values are the roles its type maps to the writer's, as the API gives them: text,
/// enums, dates (`YYYY-MM-DD`) and times (ISO 8601, in UTC with `Z`) as strings; integers;
/// floats; booleans; quantities as `{"value", "unit"}`, already converted to the role's
/// declared unit; and references as the referenced record, one step deep. A role with no
/// value, or one the type doesn't map, isn't there. Nor is a reference the person
/// exporting can't read; its role is named in [`withheld`](Self::withheld) instead.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Record {
    /// Its UUID.
    pub id: String,
    /// Its human-readable ID, such as `TRF-000042`.
    pub hrid: String,
    /// Its name.
    pub name: String,
    /// Its qualified type, such as `acme/liquid_handling.Transfer`.
    #[serde(rename = "type")]
    pub r#type: String,
    /// The revision being written, from 1.
    pub revision: u64,
    /// When that revision was made, in UTC, such as `2026-11-20T09:14:03Z`.
    pub revised_at: String,
    /// The writer's roles' values, by role.
    pub roles: Map<String, Value>,
    /// The roles whose references the person exporting can't read.
    pub withheld: Vec<String>,
}

impl Record {
    /// A role's value as the API gives it.
    #[must_use]
    pub fn value(&self, role: &str) -> Option<&Value> {
        self.roles.get(role)
    }

    /// A text, enum, date or time role's value.
    #[must_use]
    pub fn text(&self, role: &str) -> Option<&str> {
        text(&self.roles, role)
    }

    /// An integer role's value.
    #[must_use]
    pub fn integer(&self, role: &str) -> Option<i64> {
        integer(&self.roles, role)
    }

    /// A number role's value, integer or float.
    #[must_use]
    pub fn number(&self, role: &str) -> Option<f64> {
        number(&self.roles, role)
    }

    /// A boolean role's value.
    #[must_use]
    pub fn boolean(&self, role: &str) -> Option<bool> {
        boolean(&self.roles, role)
    }

    /// A quantity role's value, in the unit the role declares.
    #[must_use]
    pub fn quantity(&self, role: &str) -> Option<Quantity> {
        quantity(&self.roles, role)
    }

    /// A reference role's record.
    #[must_use]
    pub fn reference(&self, role: &str) -> Option<Reference> {
        Reference::deserialize(self.roles.get(role)?).ok()
    }

    /// Whether a reference role's record is one the person exporting can't read.
    #[must_use]
    pub fn is_withheld(&self, role: &str) -> bool {
        self.withheld.iter().any(|name| name == role)
    }
}

/// A quantity: a value in a unit.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Quantity {
    /// The value.
    pub value: f64,
    /// Its unit, as a UCUM code, such as `uL` or `ng/uL`.
    pub unit: String,
}

/// The record a reference role refers to, read as it was when the referring revision was
/// its record's latest.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Reference {
    /// Its UUID.
    pub id: String,
    /// Its human-readable ID.
    pub hrid: String,
    /// Its name.
    pub name: String,
    /// Its qualified type.
    #[serde(rename = "type")]
    pub r#type: String,
    /// Its revision as read.
    pub revision: u64,
    /// Its fields' values, by field, as the API gives them.
    pub fields: Map<String, Value>,
}

impl Reference {
    /// A field's value as the API gives it.
    #[must_use]
    pub fn value(&self, field: &str) -> Option<&Value> {
        self.fields.get(field)
    }

    /// A text, enum, date or time field's value.
    #[must_use]
    pub fn text(&self, field: &str) -> Option<&str> {
        text(&self.fields, field)
    }

    /// An integer field's value.
    #[must_use]
    pub fn integer(&self, field: &str) -> Option<i64> {
        integer(&self.fields, field)
    }

    /// A number field's value, integer or float.
    #[must_use]
    pub fn number(&self, field: &str) -> Option<f64> {
        number(&self.fields, field)
    }

    /// A boolean field's value.
    #[must_use]
    pub fn boolean(&self, field: &str) -> Option<bool> {
        boolean(&self.fields, field)
    }

    /// A quantity field's value, in the field's unit.
    #[must_use]
    pub fn quantity(&self, field: &str) -> Option<Quantity> {
        quantity(&self.fields, field)
    }
}

fn text<'a>(values: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    values.get(key)?.as_str()
}

fn integer(values: &Map<String, Value>, key: &str) -> Option<i64> {
    values.get(key)?.as_i64()
}

fn number(values: &Map<String, Value>, key: &str) -> Option<f64> {
    values.get(key)?.as_f64()
}

fn boolean(values: &Map<String, Value>, key: &str) -> Option<bool> {
    values.get(key)?.as_bool()
}

fn quantity(values: &Map<String, Value>, key: &str) -> Option<Quantity> {
    let value = values.get(key)?.as_object()?;
    Some(Quantity {
        value: value.get("value")?.as_f64()?,
        unit: value.get("unit")?.as_str()?.to_owned(),
    })
}

/// Generates a module's exports for a [`Writer`]: `writer!(MyWriter);`, once per crate.
///
/// The exports are the ABI's (see the crate's documentation). A second `writer!` in the
/// same crate fails to link, as two exports of one name would.
#[macro_export]
macro_rules! writer {
    ($writer:ty) => {
        #[allow(unsafe_code)] // `no_mangle` alone: the exports' names. No unsafe blocks.
        const _: () = {
            ::std::thread_local! {
                static HOST: ::std::cell::RefCell<$crate::__private::Host<$writer>> =
                    const { ::std::cell::RefCell::new($crate::__private::Host::new()) };
            }

            /// The ABI this module speaks.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_abi() -> i32 {
                $crate::ABI
            }

            /// A buffer of `len` bytes for the next request.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_input(len: usize) -> *mut u8 {
                HOST.with_borrow_mut(|host| host.input(len))
            }

            /// Starts a file.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_begin() -> i32 {
                HOST.with_borrow_mut($crate::__private::Host::begin)
            }

            /// Writes one record.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_record() -> i32 {
                HOST.with_borrow_mut($crate::__private::Host::record)
            }

            /// Finishes the file.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_end() -> i32 {
                HOST.with_borrow_mut($crate::__private::Host::end)
            }

            /// Where the bytes the last call added are.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_output() -> *const u8 {
                HOST.with_borrow($crate::__private::Host::output)
            }

            /// How many bytes the last call added.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_output_len() -> usize {
                HOST.with_borrow($crate::__private::Host::output_len)
            }

            /// Where the last call's notes are.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_notes() -> *const u8 {
                HOST.with_borrow($crate::__private::Host::notes)
            }

            /// How long the last call's notes are.
            #[unsafe(no_mangle)]
            pub extern "C" fn iki_notes_len() -> usize {
                HOST.with_borrow($crate::__private::Host::notes_len)
            }
        };
    };
}

/// What [`writer!`]'s exports run. Not part of the API: use the macro.
#[doc(hidden)]
pub mod __private {
    use super::{Export, MAX_REQUEST_BYTES, Note, Part, Record, Refusal, Writer};

    /// A module's state: the writer for the file being written, the request last written
    /// and the last call's bytes and notes.
    pub struct Host<W> {
        writer: Option<W>,
        input: Vec<u8>,
        output: Vec<u8>,
        notes: Vec<u8>,
    }

    impl<W: Writer> Default for Host<W> {
        fn default() -> Self {
            Self::new()
        }
    }

    impl<W: Writer> Host<W> {
        /// A module that hasn't started a file.
        #[must_use]
        pub const fn new() -> Self {
            Self {
                writer: None,
                input: Vec::new(),
                output: Vec::new(),
                notes: Vec::new(),
            }
        }

        /// A fresh buffer of `len` bytes for the next request, at most one byte more than
        /// a request may be, so that a longer one is seen to be too long.
        pub fn input(&mut self, len: usize) -> *mut u8 {
            self.input = vec![0; len.min(MAX_REQUEST_BYTES + 1)];
            self.input.as_mut_ptr()
        }

        /// The buffer's bytes, as Iki writes them through the module's memory.
        pub fn input_bytes(&mut self) -> &mut [u8] {
            &mut self.input
        }

        /// Starts a file from a fresh writer, with the export last written.
        pub fn begin(&mut self) -> i32 {
            let export: Export = self.request("begin");
            // A trap: Iki's bug, not the export's.
            assert!(
                i64::from(export.abi) == i64::from(super::ABI),
                "iki_begin: Iki asked for ABI {}, not 1.",
                export.abi
            );
            let writer = self.writer.insert(W::default());
            let result = writer.begin(&export);
            self.answer(result)
        }

        /// Writes the record last written.
        pub fn record(&mut self) -> i32 {
            let record: Record = self.request("record");
            #[allow(clippy::panic)] // A trap: Iki's bug, not the record's.
            let Some(writer) = self.writer.as_mut() else {
                panic!("iki_record: no file was started with iki_begin.");
            };
            let result = writer.record(&record);
            self.answer(result)
        }

        /// Finishes the file, and drops the writer.
        pub fn end(&mut self) -> i32 {
            let _: serde_json::Map<String, serde_json::Value> = self.request("end");
            #[allow(clippy::panic)] // A trap: Iki's bug, not the export's.
            let Some(mut writer) = self.writer.take() else {
                panic!("iki_end: no file was started with iki_begin.");
            };
            let result = writer.end();
            self.answer(result)
        }

        /// Where the last call's bytes are.
        #[must_use]
        pub fn output(&self) -> *const u8 {
            self.output.as_ptr()
        }

        /// How many bytes the last call added.
        #[must_use]
        pub fn output_len(&self) -> usize {
            self.output.len()
        }

        /// Where the last call's notes are.
        #[must_use]
        pub fn notes(&self) -> *const u8 {
            self.notes.as_ptr()
        }

        /// How long the last call's notes are.
        #[must_use]
        pub fn notes_len(&self) -> usize {
            self.notes.len()
        }

        /// The last call's bytes.
        #[must_use]
        pub fn output_bytes(&self) -> &[u8] {
            &self.output
        }

        /// The last call's notes, as JSON.
        #[must_use]
        pub fn notes_bytes(&self) -> &[u8] {
            &self.notes
        }

        /// Reads the request last written, and frees its buffer, along with the previous
        /// call's answer.
        #[allow(clippy::panic)] // A trap: a request that isn't one is Iki's bug.
        fn request<T: serde::de::DeserializeOwned>(&mut self, call: &str) -> T {
            let bytes = std::mem::take(&mut self.input);
            self.output = Vec::new();
            self.notes = Vec::new();
            assert!(
                bytes.len() <= MAX_REQUEST_BYTES,
                "iki_{call}: the request is over {} MiB.",
                MAX_REQUEST_BYTES >> 20
            );
            serde_json::from_slice(&bytes)
                .unwrap_or_else(|error| panic!("iki_{call}: the request isn't one: {error}."))
        }

        /// Keeps a call's answer for Iki to read, and gives its status.
        fn answer(&mut self, result: Result<Part, Refusal>) -> i32 {
            let (status, bytes, notes) = match result {
                Ok(part) => (0, part.bytes, part.notes),
                Err(refusal) => (1, Vec::new(), vec![Note::new("refused", refusal.message)]),
            };
            self.output = bytes;
            self.notes = notes_json(&notes);
            status
        }
    }

    /// Notes as the ABI gives them: `[{"code","message","role"?}]`.
    #[must_use]
    pub fn notes_json(notes: &[Note]) -> Vec<u8> {
        // Strings and an optional string always serialize.
        serde_json::to_vec(notes).unwrap_or_else(|_| b"[]".to_vec())
    }
}

pub mod testing {
    //! Drive a writer natively, through the same code its module's exports run.
    //!
    //! ```
    //! # use iki_writer::{Export, Part, Record, Refusal, Writer};
    //! # #[derive(Default)]
    //! # struct Lines;
    //! # impl Writer for Lines {
    //! #     fn record(&mut self, record: &Record) -> Result<Part, Refusal> {
    //! #         Ok(Part::bytes(format!("{}\n", record.hrid)))
    //! #     }
    //! # }
    //! use iki_writer::testing::Session;
    //! use serde_json::json;
    //!
    //! let mut session = Session::<Lines>::new();
    //! let begun = session.begin(&json!({
    //!     "abi": 1, "format": "iki/core.text", "records": 1, "writer": "acme/lines.lines",
    //! }));
    //! assert_eq!(begun.status, 0);
    //! let written = session.record(&json!({
    //!     "hrid": "SMP-000007", "id": "0b6f6d1e-6c38-4c5e-9a39-2f8f0d1c8e21",
    //!     "name": "Liver lysate 7", "revised_at": "2026-11-20T09:14:03Z",
    //!     "revision": 2, "roles": {}, "type": "acme/samples.Sample", "withheld": [],
    //! }));
    //! assert_eq!(written.output, b"SMP-000007\n");
    //! assert!(written.notes.is_empty());
    //! assert_eq!(session.end().status, 0);
    //! ```

    use super::{__private::Host, Note, Writer};

    /// A writer's module, run natively: each call writes its request into the input
    /// buffer, as Iki does, and runs what the export runs.
    pub struct Session<W> {
        host: Host<W>,
    }

    /// What one call gave back, as Iki reads it from the module.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Call {
        /// 0 written, 1 refused.
        pub status: i32,
        /// The bytes the call added to the file.
        pub output: Vec<u8>,
        /// The call's notes, read back from the JSON the module gives.
        pub notes: Vec<Note>,
        /// That JSON.
        pub notes_json: String,
    }

    impl Call {
        /// The refusal's message, if the call was refused.
        #[must_use]
        pub fn refusal(&self) -> Option<&str> {
            match (self.status, self.notes.as_slice()) {
                (1, [note]) if note.code == "refused" => Some(&note.message),
                _ => None,
            }
        }

        /// The bytes the call added, as text.
        #[must_use]
        pub fn text(&self) -> String {
            String::from_utf8_lossy(&self.output).into_owned()
        }
    }

    impl<W: Writer> Default for Session<W> {
        fn default() -> Self {
            Self::new()
        }
    }

    impl<W: Writer> Session<W> {
        /// A module that hasn't started a file.
        #[must_use]
        pub const fn new() -> Self {
            Self { host: Host::new() }
        }

        /// Starts a file with an export's request.
        pub fn begin(&mut self, export: &impl serde::Serialize) -> Call {
            self.write(export);
            let status = self.host.begin();
            self.call(status)
        }

        /// Writes a record's request.
        pub fn record(&mut self, record: &impl serde::Serialize) -> Call {
            self.write(record);
            let status = self.host.record();
            self.call(status)
        }

        /// Finishes the file.
        pub fn end(&mut self) -> Call {
            self.write_bytes(b"{}");
            let status = self.host.end();
            self.call(status)
        }

        /// Writes a request's bytes as given, as Iki does before a call.
        pub fn write_bytes(&mut self, request: &[u8]) {
            self.host.input(request.len());
            self.host.input_bytes().copy_from_slice(request);
        }

        /// The module's state, to make the export calls by hand after
        /// [`write_bytes`](Self::write_bytes).
        pub fn host(&mut self) -> &mut Host<W> {
            &mut self.host
        }

        fn write(&mut self, request: &impl serde::Serialize) {
            // A value that doesn't serialize is a test's own mistake.
            let bytes = serde_json::to_vec(request).unwrap_or_default();
            self.write_bytes(&bytes);
        }

        fn call(&self, status: i32) -> Call {
            let notes_json = String::from_utf8_lossy(self.host.notes_bytes()).into_owned();
            Call {
                status,
                output: self.host.output_bytes().to_vec(),
                notes: serde_json::from_str(&notes_json).unwrap_or_default(),
                notes_json,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::{Call, Session};
    use super::{__private::notes_json, Export, Note, Part, Record, Refusal, Writer};
    use serde_json::{Value, json};

    /// A pick list: a header, a line per transfer, a count at the end.
    #[derive(Default)]
    struct PickList {
        format: String,
        lines: u64,
    }

    impl Writer for PickList {
        fn begin(&mut self, export: &Export) -> Result<Part, Refusal> {
            if export.records > 1536 {
                return Err(Refusal::new("A pick list holds at most 1,536 transfers."));
            }
            self.format.clone_from(&export.format);
            Ok(Part::bytes(
                "Source Well,Destination Well,Transfer Volume\r\n",
            ))
        }

        fn record(&mut self, record: &Record) -> Result<Part, Refusal> {
            if record.is_withheld("source_plate") {
                return Err(Refusal::new(format!(
                    "{}'s source plate isn't a record you can read.",
                    record.hrid
                )));
            }
            let Some(volume) = record.quantity("volume") else {
                return Err(Refusal::new(format!("{} has no volume.", record.hrid)));
            };
            let source = record.text("source_well").unwrap_or("");
            let destination = record.text("destination_well").unwrap_or("");
            self.lines += 1;
            let mut part = Part::bytes(format!("{source},{destination},{}\r\n", volume.value));
            if record.text("note").is_none() {
                part = part.note("no_note", "The transfer has no note.");
            }
            if volume.value.fract() != 0.0 {
                part = part.role_note("volume", "rounded", "Echo volumes are whole nL.");
            }
            Ok(part)
        }

        fn end(&mut self) -> Result<Part, Refusal> {
            Ok(Part::bytes(format!("# {} transfers\r\n", self.lines)))
        }
    }

    fn export(records: u64) -> Value {
        json!({
            "abi": 1,
            "format": "iki/core.csv",
            "records": records,
            "writer": "acme/liquid_handling.echo-picklist",
        })
    }

    fn transfer(roles: &Value, withheld: &[&str]) -> Value {
        json!({
            "hrid": "TRF-000042",
            "id": "6f1c1b7e-2a6d-4c1e-9b0a-3f6f5f9b2d41",
            "name": "Dose-response, compound 12",
            "revised_at": "2026-11-20T09:14:03Z",
            "revision": 3,
            "roles": roles,
            "type": "acme/liquid_handling.Transfer",
            "withheld": withheld,
        })
    }

    fn plate() -> Value {
        json!({
            "fields": {
                "barcode": "CP-0412",
                "columns": 24,
                "dead_volume": {"value": 2.5, "unit": "uL"},
                "label": "Compound source 1",
                "sealed": false,
                "working_volume": 12,
            },
            "hrid": "PLT-000412",
            "id": "2f0c9a43-61b8-4a1e-8d2e-0c9b0a7c5e10",
            "name": "Compound source 1",
            "revision": 7,
            "type": "acme/plates.Plate",
        })
    }

    #[test]
    fn a_file_is_begin_records_and_end() {
        let mut session = Session::<PickList>::new();
        let begun = session.begin(&export(2));
        assert_eq!(begun.status, 0);
        assert_eq!(
            begun.text(),
            "Source Well,Destination Well,Transfer Volume\r\n"
        );
        assert_eq!(begun.notes_json, "[]");
        let roles = json!({
            "note": "top up",
            "source_plate": plate(),
            "source_well": "A1",
            "destination_well": "B3",
            "volume": {"value": 25, "unit": "nL"},
        });
        let first = session.record(&transfer(&roles, &[]));
        assert_eq!(first.status, 0);
        assert_eq!(first.text(), "A1,B3,25\r\n");
        assert_eq!(first.notes_json, "[]");
        let second = session.record(&transfer(&roles, &[]));
        assert_eq!(second.text(), "A1,B3,25\r\n");
        let ended = session.end();
        assert_eq!(ended.status, 0);
        assert_eq!(ended.text(), "# 2 transfers\r\n");
    }

    #[test]
    fn notes_leave_out_a_role_they_are_not_about() {
        let mut session = Session::<PickList>::new();
        session.begin(&export(1));
        let roles = json!({
            "source_well": "P24",
            "destination_well": "H12",
            "volume": {"value": 2.5, "unit": "nL"},
        });
        let written = session.record(&transfer(&roles, &[]));
        assert_eq!(written.status, 0);
        assert_eq!(
            written.notes_json,
            r#"[{"code":"no_note","message":"The transfer has no note."},{"code":"rounded","message":"Echo volumes are whole nL.","role":"volume"}]"#
        );
        assert_eq!(
            written.notes,
            vec![
                Note::new("no_note", "The transfer has no note."),
                Note::for_role("volume", "rounded", "Echo volumes are whole nL."),
            ]
        );
    }

    #[test]
    fn a_refusal_is_status_1_with_only_the_refused_note() {
        let mut session = Session::<PickList>::new();
        session.begin(&export(1));
        let roles = json!({"source_well": "A1", "destination_well": "A2"});
        let refused = session.record(&transfer(&roles, &["source_plate"]));
        assert_eq!(refused.status, 1);
        assert!(refused.output.is_empty());
        assert_eq!(
            refused.notes_json,
            r#"[{"code":"refused","message":"TRF-000042's source plate isn't a record you can read."}]"#
        );
        assert_eq!(
            refused.refusal(),
            Some("TRF-000042's source plate isn't a record you can read.")
        );
    }

    #[test]
    fn a_refused_begin_writes_nothing() {
        let mut session = Session::<PickList>::new();
        let refused = session.begin(&export(2000));
        assert_eq!(refused.status, 1);
        assert!(refused.output.is_empty());
        assert_eq!(
            refused.refusal(),
            Some("A pick list holds at most 1,536 transfers.")
        );
    }

    #[test]
    fn each_call_replaces_the_last_ones_answer() {
        let mut session = Session::<PickList>::new();
        session.begin(&export(1));
        let roles = json!({"source_well": "A1", "destination_well": "A2"});
        let refused = session.record(&transfer(&roles, &[]));
        assert_eq!(refused.status, 1);
        let ended = session.end();
        assert_eq!(ended.text(), "# 0 transfers\r\n");
        assert_eq!(ended.notes_json, "[]");
    }

    #[test]
    fn each_begin_starts_from_a_fresh_writer() {
        let mut session = Session::<PickList>::new();
        let roles = json!({
            "note": "n",
            "source_well": "A1",
            "destination_well": "A2",
            "volume": {"value": 5, "unit": "nL"},
        });
        session.begin(&export(1));
        session.record(&transfer(&roles, &[]));
        assert_eq!(session.end().text(), "# 1 transfers\r\n");
        session.begin(&export(1));
        assert_eq!(session.end().text(), "# 0 transfers\r\n");
    }

    #[test]
    fn requests_parse_as_iki_writes_them() {
        // Canonical JSON: keys sorted, no whitespace.
        let request = br#"{"hrid":"TRF-000042","id":"6f1c1b7e-2a6d-4c1e-9b0a-3f6f5f9b2d41","name":"Dose-response, compound 12","revised_at":"2026-11-20T09:14:03Z","revision":3,"roles":{"count":96,"dispensed_on":"2026-11-19","done":true,"plate":{"fields":{"barcode":"CP-0412","dead_volume":{"unit":"uL","value":2.5},"label":"Compound source 1","sealed":false,"working_volume":12},"hrid":"PLT-000412","id":"2f0c9a43-61b8-4a1e-8d2e-0c9b0a7c5e10","name":"Compound source 1","revision":7,"type":"acme/plates.Plate"},"ratio":0.75,"volume":{"unit":"uL","value":2.5}},"type":"acme/liquid_handling.Transfer","withheld":["destination_plate"]}"#;
        let record: Record = serde_json::from_slice(request).unwrap();
        assert_eq!(record.r#type, "acme/liquid_handling.Transfer");
        assert_eq!(record.revision, 3);
        assert_eq!(record.revised_at, "2026-11-20T09:14:03Z");
        assert_eq!(record.integer("count"), Some(96));
        assert_eq!(record.number("count"), Some(96.0));
        assert_eq!(record.number("ratio"), Some(0.75));
        assert_eq!(record.integer("ratio"), None);
        assert_eq!(record.boolean("done"), Some(true));
        assert_eq!(record.text("dispensed_on"), Some("2026-11-19"));
        assert_eq!(record.text("count"), None);
        let volume = record.quantity("volume").unwrap();
        assert_eq!((volume.value, volume.unit.as_str()), (2.5, "uL"));
        assert_eq!(record.quantity("ratio"), None);
        assert_eq!(record.text("absent"), None);
        assert!(record.is_withheld("destination_plate"));
        assert!(!record.is_withheld("plate"));
        assert_eq!(record.reference("destination_plate"), None);
        assert_eq!(record.reference("volume"), None);
        let plate = record.reference("plate").unwrap();
        assert_eq!(plate.hrid, "PLT-000412");
        assert_eq!(plate.r#type, "acme/plates.Plate");
        assert_eq!(plate.revision, 7);
        assert_eq!(plate.text("label"), Some("Compound source 1"));
        assert_eq!(plate.integer("working_volume"), Some(12));
        assert_eq!(plate.number("working_volume"), Some(12.0));
        assert_eq!(plate.boolean("sealed"), Some(false));
        assert_eq!(plate.quantity("dead_volume").map(|q| q.value), Some(2.5));
        assert_eq!(plate.text("labware_type"), None);

        let export: Export = serde_json::from_slice(
            br#"{"abi":1,"format":"iki/core.csv","records":96,"writer":"acme/liquid_handling.echo-picklist"}"#,
        )
        .unwrap();
        assert_eq!(export.writer, "acme/liquid_handling.echo-picklist");
        assert_eq!(export.records, 96);
    }

    #[test]
    fn the_exports_read_the_bytes_iki_writes() {
        let mut session = Session::<PickList>::new();
        let request = br#"{"abi":1,"format":"iki/core.csv","records":1,"writer":"acme/liquid_handling.echo-picklist"}"#;
        let pointer = session.host().input(request.len());
        assert!(!pointer.is_null());
        session.host().input_bytes().copy_from_slice(request);
        assert_eq!(session.host().begin(), 0);
        assert_eq!(
            session.host().output_len(),
            "Source Well,Destination Well,Transfer Volume\r\n".len()
        );
        assert_eq!(session.host().notes_bytes(), b"[]");
        assert_eq!(session.host().notes_len(), 2);
    }

    #[test]
    fn notes_json_leaves_out_a_missing_role() {
        assert_eq!(notes_json(&[]), b"[]");
        assert_eq!(
            notes_json(&[
                Note::new("withheld", "Left out."),
                Note::for_role("volume", "rounded", "1.2345 µL is written as 1.23 µL.")
            ]),
            r#"[{"code":"withheld","message":"Left out."},{"code":"rounded","message":"1.2345 µL is written as 1.23 µL.","role":"volume"}]"#.as_bytes()
        );
    }

    #[test]
    fn parts_gather_their_notes() {
        let part = Part::bytes("A;x\r\n")
            .note("one", "First.")
            .role_note("volume", "rounded", "Second.");
        assert_eq!(part.bytes, b"A;x\r\n");
        assert_eq!(part.notes.len(), 2);
        assert_eq!(part.notes[1].role.as_deref(), Some("volume"));
        assert_eq!(Part::default(), Part::bytes(Vec::new()));
        assert_eq!(Refusal::new("No.").to_string(), "No.");
    }

    #[test]
    #[should_panic(expected = "iki_record: the request isn't one")]
    fn a_malformed_record_traps() {
        let mut session = Session::<PickList>::new();
        session.begin(&export(1));
        // `withheld` is missing.
        session.record(&json!({
            "hrid": "TRF-000042", "id": "x", "name": "n",
            "revised_at": "2026-11-20T09:14:03Z", "revision": 1, "roles": {},
            "type": "t",
        }));
    }

    #[test]
    #[should_panic(expected = "iki_begin: the request isn't one")]
    fn a_request_that_is_not_json_traps() {
        let mut session = Session::<PickList>::new();
        session.write_bytes(b"{\"abi\":1");
        session.host().begin();
    }

    #[test]
    #[should_panic(expected = "Iki asked for ABI 2, not 1")]
    fn another_abi_traps() {
        let mut session = Session::<PickList>::new();
        session.begin(&json!({"abi": 2, "format": "f", "records": 1, "writer": "w"}));
    }

    #[test]
    #[should_panic(expected = "iki_record: no file was started with iki_begin")]
    fn a_record_before_begin_traps() {
        let mut session = Session::<PickList>::new();
        session.record(&transfer(&json!({}), &[]));
    }

    #[test]
    #[should_panic(expected = "iki_end: no file was started with iki_begin")]
    fn an_end_after_end_traps() {
        let mut session = Session::<PickList>::new();
        session.begin(&export(1));
        session.end();
        session.end();
    }

    #[test]
    #[should_panic(expected = "iki_end: the request isn't one")]
    fn an_end_request_must_be_an_object() {
        let mut session = Session::<PickList>::new();
        session.begin(&export(1));
        session.write_bytes(b"[]");
        session.host().end();
    }

    #[test]
    #[should_panic(expected = "the request is over 64 MiB")]
    fn a_request_over_64_mib_traps() {
        let mut session = Session::<PickList>::new();
        let pointer = session.host().input(super::MAX_REQUEST_BYTES + 10);
        assert!(!pointer.is_null());
        assert_eq!(
            session.host().input_bytes().len(),
            super::MAX_REQUEST_BYTES + 1
        );
        session.host().begin();
    }

    #[test]
    fn a_call_says_when_it_was_refused() {
        let written = Call {
            status: 0,
            output: Vec::new(),
            notes: vec![Note::new("refused", "Looks like one.")],
            notes_json: String::new(),
        };
        assert_eq!(written.refusal(), None);
    }
}

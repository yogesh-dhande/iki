//! The exports `writer!` generates, called natively as Iki calls them in the module.

use iki_writer::{Part, Record, Refusal, Writer};

/// Writes each record's HRID and volume, and refuses one with no volume.
#[derive(Default)]
struct Volumes {
    written: usize,
}

impl Writer for Volumes {
    fn record(&mut self, record: &Record) -> Result<Part, Refusal> {
        let Some(volume) = record.quantity("volume") else {
            return Err(Refusal::new(format!("{} has no volume.", record.hrid)));
        };
        self.written += 1;
        Ok(
            Part::bytes(format!("{},{}\n", record.hrid, volume.value)).role_note(
                "volume",
                "unit",
                format!("In {}.", volume.unit),
            ),
        )
    }

    fn end(&mut self) -> Result<Part, Refusal> {
        Ok(Part::bytes(format!("{} written\n", self.written)))
    }
}

iki_writer::writer!(Volumes);

// The exports by their symbols, as the module's host finds them: `writer!` doesn't name
// them in Rust.
#[allow(unsafe_code)] // Declarations of this binary's own exports.
unsafe extern "C" {
    safe fn iki_abi() -> i32;
    safe fn iki_input(len: usize) -> *mut u8;
    safe fn iki_begin() -> i32;
    safe fn iki_record() -> i32;
    safe fn iki_end() -> i32;
    safe fn iki_output() -> *const u8;
    safe fn iki_output_len() -> usize;
    safe fn iki_notes() -> *const u8;
    safe fn iki_notes_len() -> usize;
}

/// Writes a request as Iki does: into the buffer `iki_input` gives, through memory.
#[allow(unsafe_code)] // Only the host side, standing in for the module's memory.
fn write(request: &str) {
    let pointer = iki_input(request.len());
    // SAFETY: `iki_input` gave a buffer of exactly this many bytes, which nothing else
    // holds until the next call.
    unsafe { std::ptr::copy_nonoverlapping(request.as_ptr(), pointer, request.len()) };
}

/// Reads a call's bytes and notes, as Iki does through memory.
#[allow(unsafe_code)] // Only the host side, standing in for the module's memory.
fn read() -> (String, String) {
    // SAFETY: the module keeps both buffers, of these lengths, until the next call.
    let (output, notes) = unsafe {
        (
            std::slice::from_raw_parts(iki_output(), iki_output_len()),
            std::slice::from_raw_parts(iki_notes(), iki_notes_len()),
        )
    };
    (
        String::from_utf8_lossy(output).into_owned(),
        String::from_utf8_lossy(notes).into_owned(),
    )
}

#[test]
fn the_exports_speak_abi_1() {
    assert_eq!(iki_abi(), 1);

    write(r#"{"abi":1,"format":"iki/core.csv","records":2,"writer":"acme/lab.volumes"}"#);
    assert_eq!(iki_begin(), 0);
    assert_eq!(read(), (String::new(), "[]".to_owned()));

    write(
        r#"{"hrid":"TRF-000001","id":"6f1c1b7e-2a6d-4c1e-9b0a-3f6f5f9b2d41","latest":true,"name":"Top-up","revised_at":"2026-11-20T09:14:03Z","revision":1,"roles":{"volume":{"unit":"uL","value":12.5}},"type":"acme/lab.Transfer","withheld":[]}"#,
    );
    assert_eq!(iki_record(), 0);
    assert_eq!(
        read(),
        (
            "TRF-000001,12.5\n".to_owned(),
            r#"[{"code":"unit","message":"In uL.","role":"volume"}]"#.to_owned()
        )
    );

    write(
        r#"{"hrid":"TRF-000002","id":"9a0d2c55-0f5e-4f7b-a7a4-6b3f1d9c2e88","latest":true,"name":"Empty","revised_at":"2026-11-20T09:15:00Z","revision":1,"roles":{},"type":"acme/lab.Transfer","withheld":[]}"#,
    );
    assert_eq!(iki_record(), 1);
    assert_eq!(
        read(),
        (
            String::new(),
            r#"[{"code":"refused","message":"TRF-000002 has no volume."}]"#.to_owned()
        )
    );

    write("{}");
    assert_eq!(iki_end(), 0);
    assert_eq!(read(), ("1 written\n".to_owned(), "[]".to_owned()));
}

//! The DNA record's fixed-condition measurement in isolation, for consumers that
//! only need this function. Build for wasm32-unknown-unknown (no host imports).
#![deny(unsafe_code)]

#[path = "../src/oligo.rs"]
pub mod oligo;

use std::cell::RefCell;

thread_local! {
    static INPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

/// A caller writes the single strand's ASCII bytes here, then calls the measurement.
/// An input larger than the full iki-wasm message limit (64 MiB) has no buffer.
#[allow(unsafe_code)] // Named WebAssembly export; no unsafe operations.
#[unsafe(no_mangle)]
pub extern "C" fn iki_input(len: usize) -> *mut u8 {
    INPUT.with_borrow_mut(|input| {
        input.clear();
        if len > 64 * 1024 * 1024 {
            return std::ptr::null_mut();
        }
        input.resize(len, 0);
        input.as_mut_ptr()
    })
}

/// Degrees Celsius to one decimal place, or NaN when the sequence has no estimate.
#[allow(unsafe_code)] // Named WebAssembly export; no unsafe operations.
#[unsafe(no_mangle)]
pub extern "C" fn iki_melting_temperature_value() -> f64 {
    INPUT.with_borrow(|input| oligo::melting_temperature(input).unwrap_or(f64::NAN))
}

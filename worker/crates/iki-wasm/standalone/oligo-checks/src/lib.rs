//! The primer checks in isolation: the hairpins and dimers an oligo forms, how stable they
//! are, and how warm it melts in the buffer a reaction runs in.
//!
//! Built for wasm32-unknown-unknown with no host imports, so the DNA plugin's action
//! archive carries it and runs it under Wasmtime, and the browser runs the same bytes.

#[path = "../../../src/oligo.rs"]
pub mod oligo;
#[path = "../../../src/structures.rs"]
pub mod structures;
#[path = "../../../src/thermodynamics.rs"]
pub mod thermodynamics;

/// The most a request may be: a plate of primers is far smaller.
pub const MAX_REQUEST: usize = 64 * 1024 * 1024;

#[allow(unsafe_code)] // The exported names, with no unsafe blocks.
mod exports {
    use std::cell::RefCell;

    thread_local! {
        static INPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
        static OUTPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    /// A buffer of `len` bytes for the request, and where it is in the module's memory.
    /// A request larger than [`super::MAX_REQUEST`] has no buffer.
    #[unsafe(no_mangle)]
    extern "C" fn iki_input(len: usize) -> *mut u8 {
        INPUT.with_borrow_mut(|input| {
            input.clear();
            if len > super::MAX_REQUEST {
                return std::ptr::null_mut();
            }
            input.resize(len, 0);
            input.as_mut_ptr()
        })
    }

    /// Checks the request's oligos, and the pairs it asks about.
    #[unsafe(no_mangle)]
    extern "C" fn iki_check_oligos() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::structures::check_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Where the last answer's JSON is.
    #[unsafe(no_mangle)]
    extern "C" fn iki_output() -> *const u8 {
        OUTPUT.with_borrow(Vec::as_ptr)
    }

    /// How long it is.
    #[unsafe(no_mangle)]
    extern "C" fn iki_output_len() -> usize {
        OUTPUT.with_borrow(Vec::len)
    }
}

//! The plasmid plugin's molecular biology in isolation: where primers bind a template,
//! what a pair of them amplifies, and what restriction enzymes cut a molecule into.
//!
//! Built for wasm32-unknown-unknown with no host imports, so the plugin's action archive
//! carries it and runs it under Wasmtime, and the browser runs the same bytes.

#[path = "../../../src/digest.rs"]
pub mod digest;
#[path = "../../../src/priming.rs"]
pub mod priming;
#[path = "../../../src/thermodynamics.rs"]
pub mod thermodynamics;

/// The most a request may be: a plasmid, its primers and its enzymes are far smaller.
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

    /// Places the request's primers on its template.
    #[unsafe(no_mangle)]
    extern "C" fn iki_place_primers() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::priming::place_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Works out what the request's sites amplify.
    #[unsafe(no_mangle)]
    extern "C" fn iki_pcr_products() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::priming::products_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Measures the request's duplex.
    #[unsafe(no_mangle)]
    extern "C" fn iki_duplex_temperature() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::priming::temperature_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Cuts the request's molecule with its enzymes.
    #[unsafe(no_mangle)]
    extern "C" fn iki_digest() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::digest::cut_json(&bytes);
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

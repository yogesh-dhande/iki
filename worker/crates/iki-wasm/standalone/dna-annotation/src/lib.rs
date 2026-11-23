//! The sequence annotation export for the Plasmid action archive.

#[path = "../../../src/annotation.rs"]
mod annotation;

#[allow(unsafe_code)] // The exported names, with no unsafe blocks.
mod exports {
    use std::cell::RefCell;

    thread_local! {
        static INPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
        static OUTPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    #[unsafe(no_mangle)]
    extern "C" fn iki_input(len: usize) -> *mut u8 {
        INPUT.with_borrow_mut(|input| {
            *input = vec![0; len.min(super::annotation::MAX_REQUEST + 1)];
            input.as_mut_ptr()
        })
    }

    #[unsafe(no_mangle)]
    extern "C" fn iki_annotate_dna_sequence() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::annotation::annotate_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    #[unsafe(no_mangle)]
    extern "C" fn iki_output() -> *const u8 {
        OUTPUT.with_borrow(Vec::as_ptr)
    }

    #[unsafe(no_mangle)]
    extern "C" fn iki_output_len() -> usize {
        OUTPUT.with_borrow(Vec::len)
    }
}

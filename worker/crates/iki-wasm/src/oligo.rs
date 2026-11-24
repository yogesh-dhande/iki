//! The melting temperature of a supplied, unmodified DNA strand and its exact
//! complement under the fixed conditions of the DNA sequence record.

const STRANDS: f64 = 50e-9; // mol/L, total.
const SODIUM: f64 = 0.05; // mol/L.
const GAS_CONSTANT: f64 = 1.987; // cal/(mol·K), as in the reference.

fn terminal(base: u8) -> Option<(f64, f64)> {
    match base.to_ascii_uppercase() {
        b'A' | b'T' => Some((2.3, 4.1)),
        b'C' | b'G' => Some((0.1, -2.8)),
        _ => None,
    }
}

// Each doublet and its reverse complement have the same enthalpy (kcal/mol)
// and entropy (cal/(mol·K)) in this perfectly matched duplex.
fn neighbours(left: u8, right: u8) -> Option<(f64, f64)> {
    Some(
        match (left.to_ascii_uppercase(), right.to_ascii_uppercase()) {
            (b'A', b'A') | (b'T', b'T') => (-7.9, -22.2),
            (b'A', b'T') => (-7.2, -20.4),
            (b'T', b'A') => (-7.2, -21.3),
            (b'C', b'A') | (b'T', b'G') => (-8.5, -22.7),
            (b'G', b'T') | (b'A', b'C') => (-8.4, -22.4),
            (b'C', b'T') | (b'A', b'G') => (-7.8, -21.0),
            (b'G', b'A') | (b'T', b'C') => (-8.2, -22.2),
            (b'C', b'G') => (-10.6, -27.2),
            (b'G', b'C') => (-9.8, -24.4),
            (b'G', b'G') | (b'C', b'C') => (-8.0, -19.9),
            _ => return None,
        },
    )
}

/// Estimate the melting temperature in degrees Celsius, rounded to one decimal.
/// ASCII A, C, G and T (case insensitive) are valid; at least two bases are needed.
///
/// The nearest-neighbour parameters and initiation terms are those of `SantaLucia`
/// (1998), Table 2, equations 3 and 8: <https://doi.org/10.1073/pnas.95.4.1460>.
/// Assumes sodium alone at 50 mM and a total strand concentration of 50 nM.
/// A self-complementary strand uses the full concentration and symmetry term.
#[must_use]
pub fn melting_temperature(bases: &[u8]) -> Option<f64> {
    let count = u32::try_from(bases.len()).ok()?;
    if count < 2 {
        return None;
    }

    let (left_h, left_s) = terminal(*bases.first()?)?;
    let (right_h, right_s) = terminal(*bases.last()?)?;
    let mut enthalpy = left_h + right_h;
    let mut entropy = left_s + right_s;
    for pair in bases.windows(2) {
        let (h, s) = neighbours(pair[0], pair[1])?;
        enthalpy += h;
        entropy += s;
    }

    let symmetric = count.is_multiple_of(2)
        && bases
            .iter()
            .zip(bases.iter().rev())
            .take(bases.len() / 2)
            .all(|(&a, &b)| {
                matches!(
                    (a.to_ascii_uppercase(), b.to_ascii_uppercase()),
                    (b'A', b'T') | (b'T', b'A') | (b'C', b'G') | (b'G', b'C')
                )
            });
    let log_concentration = if symmetric {
        entropy -= 1.4;
        STRANDS.ln()
    } else {
        (STRANDS / 4.0).ln()
    };
    entropy += 0.368 * f64::from(count - 1) * SODIUM.ln();
    let celsius = 1000.0 * enthalpy / (entropy + GAS_CONSTANT * log_concentration) - 273.15;
    Some((celsius * 10.0).round_ties_even() / 10.0)
}

#[cfg(test)]
mod tests {
    use super::melting_temperature;

    #[test]
    fn matches_the_published_record_convention_including_palindromes() {
        // Independent Biopython DNA_NN3 reference at the conditions above. See
        // SantaLucia (1998), https://doi.org/10.1073/pnas.95.4.1460, equations 3 and 8.
        for (bases, expected) in [
            (b"GTAAAACGACGGCCAGT".as_slice(), 49.2), // Existing M13/pUC forward record.
            (b"CAGGAAACAGCTATGAC".as_slice(), 43.6), // Existing M13/pUC reverse record.
            (b"gAaTtC".as_slice(), -20.4), // Self-complementary; count concentration once.
        ] {
            assert_eq!(melting_temperature(bases), Some(expected));
        }
    }

    #[test]
    fn reports_no_estimate_for_an_undefined_input() {
        for bases in [b"".as_slice(), b"A", b"ANG", b"ACU", b"C G", &[0xff, 0xfe]] {
            assert_eq!(melting_temperature(bases), None);
        }
    }
}

//! Writes liquid transfers as a Tecan worklist (`.gwl`), which Freedom EVOware and
//! FluentControl run: each transfer is an aspirate (`A`), a dispense (`D`) and a wash or
//! tip change (`W`), one record per line, ending in CR LF.
//!
//! ```text
//! A;Compound source 1;CP-0412;384 Well Greiner PP;1;;2.5;;;;
//! D;Assay plate 7;AP-0007;96 Well Corning Flat;18;;2.5;;;;
//! W;
//! ```
//!
//! After the record letter come RackLabel (the labware's label on the worktable), RackID
//! (its barcode), RackType (its labware definition), Position, TubeID, Volume in µL,
//! LiquidClass, TipType, TipMask and ForcedRackType. Positions number the wells down the
//! columns from 1: A1 is 1, B1 is 2, and A2 follows the first column's last row.

use iki_writer::{Note, Part, Record, Refusal, Writer};

/// The writer: it keeps nothing between transfers, and writes no header or footer.
#[derive(Debug, Default)]
pub struct TecanWorklist;

iki_writer::writer!(TecanWorklist);

impl Writer for TecanWorklist {
    fn record(&mut self, record: &Record) -> Result<Part, Refusal> {
        let mut notes = Vec::new();
        let source = Labware::read(record, Side::Source, &mut notes)?;
        let destination = Labware::read(record, Side::Destination, &mut notes)?;
        let volume = volume(record, &mut notes)?;
        let liquid_class = field(
            record.text("liquid_class").unwrap_or(""),
            "liquid_class",
            "The liquid class",
            &mut notes,
        );
        let mut text = source.line('A', &volume, &liquid_class);
        text.push_str(&destination.line('D', &volume, &liquid_class));
        text.push_str("W;\r\n");
        Ok(Part {
            bytes: text.into_bytes(),
            notes,
        })
    }
}

/// A plate's wells: 8 rows by 12 columns, or 16 by 24.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Plate {
    /// A 96-well plate: rows A to H, columns 1 to 12.
    Wells96,
    /// A 384-well plate: rows A to P, columns 1 to 24.
    Wells384,
}

impl Plate {
    /// The plate the `wells` field's value names.
    #[must_use]
    pub fn named(value: &str) -> Option<Self> {
        match value {
            "plate_96" => Some(Self::Wells96),
            "plate_384" => Some(Self::Wells384),
            _ => None,
        }
    }

    /// How many rows it has.
    #[must_use]
    pub const fn rows(self) -> u32 {
        match self {
            Self::Wells96 => 8,
            Self::Wells384 => 16,
        }
    }

    /// How many columns it has.
    #[must_use]
    pub const fn columns(self) -> u32 {
        match self {
            Self::Wells96 => 12,
            Self::Wells384 => 24,
        }
    }

    /// How many wells it has.
    #[must_use]
    pub const fn wells(self) -> u32 {
        self.rows() * self.columns()
    }
}

/// A well's position on a plate, as a worklist numbers it: from 1, down each column, then
/// on to the next. `well` is a row letter and a column, such as `A1`, `a01` or `P24`.
///
/// # Errors
///
/// A message, after "The source well" or "The destination well", when `well` isn't a well
/// name or isn't on the plate.
pub fn position(well: &str, plate: Plate) -> Result<u32, String> {
    let Some((row, column)) = parse_well(well) else {
        return Err(format!(
            "{well:?} isn't a well name, such as A1 or H12: a row letter, then a column number."
        ));
    };
    if row >= plate.rows() || column == 0 || column > plate.columns() {
        let letter = char::from(b'A' + u8::try_from(row).unwrap_or(0));
        let last = char::from(b'A' + u8::try_from(plate.rows() - 1).unwrap_or(0));
        return Err(format!(
            "{letter}{column} isn't a well of a {}-well plate, whose rows run A to {last} and columns 1 to {}.",
            plate.wells(),
            plate.columns()
        ));
    }
    Ok((column - 1) * plate.rows() + row + 1)
}

/// A well name's row, from 0 for A, and its column, as written.
fn parse_well(well: &str) -> Option<(u32, u32)> {
    let well = well.trim();
    let letter = *well.as_bytes().first()?;
    if !letter.is_ascii_alphabetic() {
        return None;
    }
    let digits = &well[1..];
    if digits.is_empty() || digits.len() > 3 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let column = digits.parse().ok()?;
    Some((u32::from(letter.to_ascii_uppercase() - b'A'), column))
}

/// Which end of a transfer.
#[derive(Debug, Clone, Copy)]
enum Side {
    Source,
    Destination,
}

impl Side {
    const fn name(self) -> &'static str {
        match self {
            Self::Source => "source",
            Self::Destination => "destination",
        }
    }

    const fn plate_role(self) -> &'static str {
        match self {
            Self::Source => "source_plate",
            Self::Destination => "destination_plate",
        }
    }

    const fn well_role(self) -> &'static str {
        match self {
            Self::Source => "source_well",
            Self::Destination => "destination_well",
        }
    }

    const fn verb(self) -> &'static str {
        match self {
            Self::Source => "aspirate from",
            Self::Destination => "dispense into",
        }
    }
}

/// One end of a transfer, as the worklist names it.
struct Labware {
    label: String,
    barcode: String,
    rack_type: String,
    position: u32,
}

impl Labware {
    fn read(record: &Record, side: Side, notes: &mut Vec<Note>) -> Result<Self, Refusal> {
        let name = side.name();
        let role = side.plate_role();
        if record.is_withheld(role) {
            return Err(Refusal::new(format!(
                "The {name} plate isn't a record you can read, so the worklist can't say where to {}.",
                side.verb()
            )));
        }
        let Some(plate) = record.reference(role) else {
            return Err(Refusal::new(format!("The transfer has no {name} plate.")));
        };
        let hrid = &plate.hrid;
        let label = plate.text("label").unwrap_or("");
        if label.trim().is_empty() {
            return Err(Refusal::new(format!(
                "The {name} plate {hrid} has no label, which the worklist needs to find it on the worktable."
            )));
        }
        let wells = match plate.text("wells") {
            Some(value) => Plate::named(value).ok_or_else(|| {
                Refusal::new(format!(
                    "The {name} plate {hrid} has {value:?} wells; this worklist writes 96-well (plate_96) and 384-well (plate_384) plates."
                ))
            })?,
            None => {
                return Err(Refusal::new(format!(
                    "The {name} plate {hrid} doesn't say whether it has 96 or 384 wells."
                )));
            }
        };
        let Some(well) = record.text(side.well_role()) else {
            return Err(Refusal::new(format!("The transfer has no {name} well.")));
        };
        let position = position(well, wells)
            .map_err(|problem| Refusal::new(format!("The {name} well {problem}")))?;
        Ok(Self {
            label: field(label, role, &format!("The {name} plate's label"), notes),
            barcode: field(
                plate.text("barcode").unwrap_or(""),
                role,
                &format!("The {name} plate's barcode"),
                notes,
            ),
            rack_type: field(
                plate.text("labware_type").unwrap_or(""),
                role,
                &format!("The {name} plate's labware type"),
                notes,
            ),
            position,
        })
    }

    /// The aspirate or dispense line for this end of the transfer.
    fn line(&self, letter: char, volume: &str, liquid_class: &str) -> String {
        format!(
            "{letter};{};{};{};{};;{volume};{liquid_class};;;\r\n",
            self.label, self.barcode, self.rack_type, self.position
        )
    }
}

/// A text field as the worklist can hold it: a `;` would end the field, and a CR or LF the
/// line, so each becomes `_`, and a note says so.
fn field(value: &str, role: &str, what: &str, notes: &mut Vec<Note>) -> String {
    let breaks = |c: char| matches!(c, '\r' | '\n');
    let semicolon = value.contains(';');
    let line_break = value.contains(breaks);
    if !semicolon && !line_break {
        return value.to_owned();
    }
    let written: String = value
        .chars()
        .map(|c| if c == ';' || breaks(c) { '_' } else { c })
        .collect();
    let found = match (semicolon, line_break) {
        (true, true) => "a semicolon and a line break",
        (true, false) => "a semicolon",
        _ => "a line break",
    };
    notes.push(Note::for_role(
        role,
        "replaced",
        format!(
            "{what} {value:?} has {found}, which would break the worklist's line, so it's written as {written:?}."
        ),
    ));
    written
}

/// The transfer's volume in µL, as the worklist writes it: to 0.01 µL.
fn volume(record: &Record, notes: &mut Vec<Note>) -> Result<String, Refusal> {
    let Some(volume) = record.quantity("volume") else {
        return Err(Refusal::new("The transfer has no volume."));
    };
    if volume.unit != "uL" {
        return Err(Refusal::new(format!(
            "The transfer volume is in {:?}, and a worklist's volumes are in µL (uL).",
            volume.unit
        )));
    }
    let value = volume.value;
    if !value.is_finite() || value <= 0.0 {
        return Err(Refusal::new(format!(
            "The transfer volume is {value} µL: a transfer has to move more than 0 µL."
        )));
    }
    let (written, rounded) = hundredths(value);
    if written == "0" {
        return Err(Refusal::new(format!(
            "Transfer volume {value} µL would be written as 0 µL: worklist volumes are written to 0.01 µL."
        )));
    }
    if rounded {
        notes.push(Note::for_role(
            "volume",
            "rounded",
            format!(
                "Transfer volume {value} µL is written as {written} µL: worklist volumes are written to 0.01 µL."
            ),
        ));
    }
    Ok(written)
}

/// A positive number to two decimal places, half up, as the shortest decimal with no
/// exponent and no trailing zeros, and whether that rounded it. It rounds the number as
/// it's written (its shortest decimal, as the API gives it), so 1.005 is 1.01, not the
/// 1.00 its nearest double, 1.00499999999999989…, would round to.
#[must_use]
pub fn hundredths(value: f64) -> (String, bool) {
    // Display never writes an exponent, and writes the shortest decimal that reads back.
    let shown = value.to_string();
    let (whole, fraction) = shown.split_once('.').unwrap_or((&shown, ""));
    if fraction.len() <= 2 {
        return (shown, false);
    }
    let mut digits: Vec<u8> = whole
        .bytes()
        .chain(fraction.bytes().take(2))
        .map(|b| b - b'0')
        .collect();
    if fraction.as_bytes()[2] >= b'5' {
        let mut carry = true;
        for digit in digits.iter_mut().rev() {
            if *digit == 9 {
                *digit = 0;
            } else {
                *digit += 1;
                carry = false;
                break;
            }
        }
        if carry {
            digits.insert(0, 1);
        }
    }
    let split = digits.len() - 2;
    let text = |digits: &[u8]| -> String { digits.iter().map(|d| char::from(b'0' + d)).collect() };
    let whole = text(&digits[..split]);
    let fraction = text(&digits[split..]);
    let fraction = fraction.trim_end_matches('0');
    let written = if fraction.is_empty() {
        whole
    } else {
        format!("{whole}.{fraction}")
    };
    (written, true)
}

#[cfg(test)]
mod tests {
    use super::{Plate, TecanWorklist, hundredths, position};
    use iki_writer::serde_json::{self, Value, json};
    use iki_writer::testing::{Call, Session};
    use iki_writer::{Note, Writer};

    fn plate(hrid: &str, fields: &Value) -> Value {
        json!({
            "fields": fields,
            "hrid": hrid,
            "id": "2f0c9a43-61b8-4a1e-8d2e-0c9b0a7c5e10",
            "name": hrid,
            "revision": 4,
            "type": "acme/liquid_handling.Plate",
        })
    }

    /// A 384-well compound source plate.
    fn compound_plate() -> Value {
        plate(
            "PLT-000412",
            &json!({
                "barcode": "CP-0412",
                "label": "Compound source 1",
                "labware_type": "384 Well Greiner PP",
                "wells": "plate_384",
            }),
        )
    }

    /// A 96-well assay plate.
    fn assay_plate() -> Value {
        plate(
            "PLT-000007",
            &json!({
                "barcode": "AP-0007",
                "label": "Assay plate 7",
                "labware_type": "96 Well Corning Flat",
                "wells": "plate_96",
            }),
        )
    }

    fn roles() -> Value {
        json!({
            "destination_plate": assay_plate(),
            "destination_well": "B3",
            "source_plate": compound_plate(),
            "source_well": "A1",
            "volume": {"unit": "uL", "value": 2.5},
        })
    }

    fn transfer(hrid: &str, roles: &Value, withheld: &[&str]) -> Value {
        json!({
            "hrid": hrid,
            "id": "6f1c1b7e-2a6d-4c1e-9b0a-3f6f5f9b2d41",
            "latest": true,
            "name": "Dose-response, compound 12",
            "revised_at": "2026-11-20T09:14:03Z",
            "revision": 3,
            "roles": roles,
            "type": "acme/liquid_handling.Transfer",
            "withheld": withheld,
        })
    }

    fn write(roles: &Value) -> Call {
        write_withheld(roles, &[])
    }

    fn write_withheld(roles: &Value, withheld: &[&str]) -> Call {
        let mut session = Session::<TecanWorklist>::new();
        let begun = session.begin(&json!({
            "abi": 1,
            "format": "acme/liquid_handling.gwl",
            "records": 1,
            "writer": "acme/liquid_handling.tecan-worklist",
        }));
        assert_eq!((begun.status, begun.output.len()), (0, 0));
        session.record(&transfer("TRF-000042", roles, withheld))
    }

    fn with(mut roles: Value, path: &[&str], value: Value) -> Value {
        let (last, parents) = path.split_last().unwrap();
        let mut target = &mut roles;
        for key in parents {
            target = &mut target[*key];
        }
        target[*last] = value;
        roles
    }

    fn without(mut roles: Value, path: &[&str]) -> Value {
        let (last, parents) = path.split_last().unwrap();
        let mut target = &mut roles;
        for key in parents {
            target = &mut target[*key];
        }
        target.as_object_mut().unwrap().remove(*last);
        roles
    }

    fn refusal(roles: &Value) -> String {
        let call = write(roles);
        assert_eq!(call.status, 1, "{}", call.text());
        assert!(call.output.is_empty());
        call.refusal().unwrap().to_owned()
    }

    #[test]
    fn positions_run_down_the_columns() {
        let p96 = Plate::Wells96;
        assert_eq!(position("A1", p96), Ok(1));
        assert_eq!(position("B1", p96), Ok(2));
        assert_eq!(position("H1", p96), Ok(8));
        assert_eq!(position("A2", p96), Ok(9));
        assert_eq!(position("B3", p96), Ok(18));
        assert_eq!(position("D7", p96), Ok(52));
        assert_eq!(position("A12", p96), Ok(89));
        assert_eq!(position("H12", p96), Ok(96));
        let p384 = Plate::Wells384;
        assert_eq!(position("A1", p384), Ok(1));
        assert_eq!(position("P1", p384), Ok(16));
        assert_eq!(position("A2", p384), Ok(17));
        assert_eq!(position("B3", p384), Ok(34));
        assert_eq!(position("H12", p384), Ok(184));
        assert_eq!(position("P24", p384), Ok(384));
    }

    #[test]
    fn well_names_are_read_as_people_write_them() {
        assert_eq!(position("a01", Plate::Wells96), Ok(1));
        assert_eq!(position("h012", Plate::Wells96), Ok(96));
        assert_eq!(position(" C05 ", Plate::Wells96), Ok(35));
        assert_eq!(position("p24", Plate::Wells384), Ok(384));
    }

    #[test]
    fn a_name_that_is_not_a_well_is_refused() {
        for well in [
            "", "A", "1A", "12", "AA1", "A1B", "A-1", "A 1", "A1234", "Ä1", "A1.5",
        ] {
            let problem = position(well, Plate::Wells96).unwrap_err();
            assert_eq!(
                problem,
                format!(
                    "{well:?} isn't a well name, such as A1 or H12: a row letter, then a column number."
                )
            );
        }
    }

    #[test]
    fn a_well_off_the_plate_is_refused() {
        assert_eq!(
            position("I1", Plate::Wells96).unwrap_err(),
            "I1 isn't a well of a 96-well plate, whose rows run A to H and columns 1 to 12."
        );
        assert_eq!(
            position("A13", Plate::Wells96).unwrap_err(),
            "A13 isn't a well of a 96-well plate, whose rows run A to H and columns 1 to 12."
        );
        assert_eq!(
            position("a00", Plate::Wells96).unwrap_err(),
            "A0 isn't a well of a 96-well plate, whose rows run A to H and columns 1 to 12."
        );
        assert_eq!(
            position("Q1", Plate::Wells384).unwrap_err(),
            "Q1 isn't a well of a 384-well plate, whose rows run A to P and columns 1 to 24."
        );
        assert_eq!(
            position("P25", Plate::Wells384).unwrap_err(),
            "P25 isn't a well of a 384-well plate, whose rows run A to P and columns 1 to 24."
        );
        assert_eq!(
            position("z1", Plate::Wells384).unwrap_err(),
            "Z1 isn't a well of a 384-well plate, whose rows run A to P and columns 1 to 24."
        );
    }

    #[test]
    fn a_transfer_is_an_aspirate_a_dispense_and_a_wash() {
        let call = write(&roles());
        assert_eq!(call.status, 0);
        assert_eq!(
            call.text(),
            "A;Compound source 1;CP-0412;384 Well Greiner PP;1;;2.5;;;;\r\n\
             D;Assay plate 7;AP-0007;96 Well Corning Flat;18;;2.5;;;;\r\n\
             W;\r\n"
        );
        assert!(call.notes.is_empty());
        assert_eq!(call.notes_json, "[]");
    }

    #[test]
    fn a_file_of_transfers_has_no_header_or_footer() {
        let mut session = Session::<TecanWorklist>::new();
        let begun = session.begin(&json!({
            "abi": 1,
            "format": "acme/liquid_handling.gwl",
            "records": 3,
            "writer": "acme/liquid_handling.tecan-worklist",
        }));
        assert_eq!(
            (begun.status, begun.text(), begun.notes_json.as_str()),
            (0, String::new(), "[]")
        );
        let doses = [("A1", "B3", 2.5), ("P24", "H12", 10.0), ("C5", "A1", 0.5)];
        let mut file = String::new();
        for (index, (source, destination, microlitres)) in doses.into_iter().enumerate() {
            let roles = with(
                with(
                    with(roles(), &["source_well"], json!(source)),
                    &["destination_well"],
                    json!(destination),
                ),
                &["volume"],
                json!({"unit": "uL", "value": microlitres}),
            );
            let roles = with(roles, &["liquid_class"], json!("DMSO Free Single"));
            let call = session.record(&transfer(&format!("TRF-00004{index}"), &roles, &[]));
            assert_eq!(call.status, 0);
            file.push_str(&call.text());
        }
        let ended = session.end();
        assert_eq!(
            (ended.status, ended.text(), ended.notes_json.as_str()),
            (0, String::new(), "[]")
        );
        assert_eq!(
            file,
            "A;Compound source 1;CP-0412;384 Well Greiner PP;1;;2.5;DMSO Free Single;;;\r\n\
             D;Assay plate 7;AP-0007;96 Well Corning Flat;18;;2.5;DMSO Free Single;;;\r\n\
             W;\r\n\
             A;Compound source 1;CP-0412;384 Well Greiner PP;384;;10;DMSO Free Single;;;\r\n\
             D;Assay plate 7;AP-0007;96 Well Corning Flat;96;;10;DMSO Free Single;;;\r\n\
             W;\r\n\
             A;Compound source 1;CP-0412;384 Well Greiner PP;67;;0.5;DMSO Free Single;;;\r\n\
             D;Assay plate 7;AP-0007;96 Well Corning Flat;1;;0.5;DMSO Free Single;;;\r\n\
             W;\r\n"
        );
    }

    #[test]
    fn optional_fields_are_left_empty() {
        let roles = without(
            without(roles(), &["source_plate", "fields", "barcode"]),
            &["destination_plate", "fields", "labware_type"],
        );
        let call = write(&roles);
        assert_eq!(call.status, 0);
        assert_eq!(
            call.text(),
            "A;Compound source 1;;384 Well Greiner PP;1;;2.5;;;;\r\n\
             D;Assay plate 7;AP-0007;;18;;2.5;;;;\r\n\
             W;\r\n"
        );
        assert!(call.notes.is_empty());
    }

    #[test]
    fn a_plate_that_is_withheld_is_refused() {
        let roles = without(roles(), &["source_plate"]);
        let call = write_withheld(&roles, &["source_plate"]);
        assert_eq!(call.status, 1);
        assert_eq!(
            call.refusal(),
            Some(
                "The source plate isn't a record you can read, so the worklist can't say where to aspirate from."
            )
        );
        let roles = without(super::tests::roles(), &["destination_plate"]);
        let call = write_withheld(&roles, &["destination_plate"]);
        assert_eq!(
            call.refusal(),
            Some(
                "The destination plate isn't a record you can read, so the worklist can't say where to dispense into."
            )
        );
        assert_eq!(
            call.notes_json,
            r#"[{"code":"refused","message":"The destination plate isn't a record you can read, so the worklist can't say where to dispense into."}]"#
        );
    }

    #[test]
    fn a_missing_required_value_is_refused() {
        assert_eq!(
            refusal(&without(roles(), &["source_plate"])),
            "The transfer has no source plate."
        );
        assert_eq!(
            refusal(&without(roles(), &["destination_plate"])),
            "The transfer has no destination plate."
        );
        assert_eq!(
            refusal(&without(roles(), &["source_well"])),
            "The transfer has no source well."
        );
        assert_eq!(
            refusal(&without(roles(), &["destination_well"])),
            "The transfer has no destination well."
        );
        assert_eq!(
            refusal(&without(roles(), &["volume"])),
            "The transfer has no volume."
        );
        assert_eq!(
            refusal(&without(roles(), &["source_plate", "fields", "label"])),
            "The source plate PLT-000412 has no label, which the worklist needs to find it on the worktable."
        );
        assert_eq!(
            refusal(&with(
                roles(),
                &["destination_plate", "fields", "label"],
                json!("  ")
            )),
            "The destination plate PLT-000007 has no label, which the worklist needs to find it on the worktable."
        );
        assert_eq!(
            refusal(&without(roles(), &["source_plate", "fields", "wells"])),
            "The source plate PLT-000412 doesn't say whether it has 96 or 384 wells."
        );
        assert_eq!(
            refusal(&with(
                roles(),
                &["destination_plate", "fields", "wells"],
                json!("plate_1536")
            )),
            "The destination plate PLT-000007 has \"plate_1536\" wells; this worklist writes 96-well (plate_96) and 384-well (plate_384) plates."
        );
    }

    #[test]
    fn a_well_that_is_not_on_its_plate_is_refused() {
        assert_eq!(
            refusal(&with(roles(), &["source_well"], json!("Q1"))),
            "The source well Q1 isn't a well of a 384-well plate, whose rows run A to P and columns 1 to 24."
        );
        // P24 is on the source plate, but not on the 96-well assay plate.
        assert_eq!(
            refusal(&with(roles(), &["destination_well"], json!("P24"))),
            "The destination well P24 isn't a well of a 96-well plate, whose rows run A to H and columns 1 to 12."
        );
        assert_eq!(
            refusal(&with(roles(), &["destination_well"], json!("B-3"))),
            "The destination well \"B-3\" isn't a well name, such as A1 or H12: a row letter, then a column number."
        );
    }

    #[test]
    fn a_volume_must_be_positive_and_in_microlitres() {
        assert_eq!(
            refusal(&with(
                roles(),
                &["volume"],
                json!({"unit": "uL", "value": 0})
            )),
            "The transfer volume is 0 µL: a transfer has to move more than 0 µL."
        );
        assert_eq!(
            refusal(&with(
                roles(),
                &["volume"],
                json!({"unit": "uL", "value": -5.0})
            )),
            "The transfer volume is -5 µL: a transfer has to move more than 0 µL."
        );
        assert_eq!(
            refusal(&with(
                roles(),
                &["volume"],
                json!({"unit": "nL", "value": 2500})
            )),
            "The transfer volume is in \"nL\", and a worklist's volumes are in µL (uL)."
        );
        assert_eq!(
            refusal(&with(
                roles(),
                &["volume"],
                json!({"unit": "uL", "value": 0.004})
            )),
            "Transfer volume 0.004 µL would be written as 0 µL: worklist volumes are written to 0.01 µL."
        );
    }

    #[test]
    fn a_volume_finer_than_a_hundredth_is_rounded_and_noted() {
        let call = write(&with(
            roles(),
            &["volume"],
            json!({"unit": "uL", "value": 1.2345}),
        ));
        assert_eq!(call.status, 0);
        assert_eq!(
            call.text(),
            "A;Compound source 1;CP-0412;384 Well Greiner PP;1;;1.23;;;;\r\n\
             D;Assay plate 7;AP-0007;96 Well Corning Flat;18;;1.23;;;;\r\n\
             W;\r\n"
        );
        assert_eq!(
            call.notes,
            vec![Note::for_role(
                "volume",
                "rounded",
                "Transfer volume 1.2345 µL is written as 1.23 µL: worklist volumes are written to 0.01 µL."
            )]
        );
        assert_eq!(
            call.notes_json,
            r#"[{"code":"rounded","message":"Transfer volume 1.2345 µL is written as 1.23 µL: worklist volumes are written to 0.01 µL.","role":"volume"}]"#
        );
    }

    #[test]
    fn volumes_are_written_to_a_hundredth_without_trailing_zeros() {
        let cases = [
            (10.0, "10", false),
            (2.5, "2.5", false),
            (1.23, "1.23", false),
            (0.5, "0.5", false),
            (1500.0, "1500", false),
            (0.01, "0.01", false),
            (1.2345, "1.23", true),
            (1.005, "1.01", true),
            (0.125, "0.13", true),
            (2.499, "2.5", true),
            (9.999, "10", true),
            (99.995, "100", true),
            (0.005, "0.01", true),
            (0.004, "0", true),
            (0.1 + 0.2, "0.3", true),
            (12.0004, "12", true),
            (1e-7, "0", true),
            (1e21, "1000000000000000000000", false),
        ];
        for (value, written, rounded) in cases {
            assert_eq!(hundredths(value), (written.to_owned(), rounded), "{value}");
        }
    }

    #[test]
    fn semicolons_and_line_breaks_are_replaced_and_noted() {
        let roles = with(
            with(
                with(
                    roles(),
                    &["source_plate", "fields", "label"],
                    json!("Compound source; row 1"),
                ),
                &["destination_plate", "fields", "barcode"],
                json!("AP-0007\r\n"),
            ),
            &["liquid_class"],
            json!("Water;Free\nDispense"),
        );
        let call = write(&roles);
        assert_eq!(call.status, 0);
        assert_eq!(
            call.text(),
            "A;Compound source_ row 1;CP-0412;384 Well Greiner PP;1;;2.5;Water_Free_Dispense;;;\r\n\
             D;Assay plate 7;AP-0007__;96 Well Corning Flat;18;;2.5;Water_Free_Dispense;;;\r\n\
             W;\r\n"
        );
        assert_eq!(
            call.notes,
            vec![
                Note::for_role(
                    "source_plate",
                    "replaced",
                    "The source plate's label \"Compound source; row 1\" has a semicolon, which would break the worklist's line, so it's written as \"Compound source_ row 1\"."
                ),
                Note::for_role(
                    "destination_plate",
                    "replaced",
                    "The destination plate's barcode \"AP-0007\\r\\n\" has a line break, which would break the worklist's line, so it's written as \"AP-0007__\"."
                ),
                Note::for_role(
                    "liquid_class",
                    "replaced",
                    "The liquid class \"Water;Free\\nDispense\" has a semicolon and a line break, which would break the worklist's line, so it's written as \"Water_Free_Dispense\"."
                ),
            ]
        );
    }

    #[test]
    fn a_labware_type_with_a_semicolon_is_noted_on_its_plate() {
        let roles = with(
            roles(),
            &["destination_plate", "fields", "labware_type"],
            json!("96 Well;Flat"),
        );
        let call = write(&roles);
        assert!(
            call.text()
                .contains("D;Assay plate 7;AP-0007;96 Well_Flat;18;;2.5;;;;\r\n")
        );
        assert_eq!(call.notes.len(), 1);
        assert_eq!(call.notes[0].role.as_deref(), Some("destination_plate"));
        assert_eq!(call.notes[0].code, "replaced");
    }

    #[test]
    fn a_refusal_carries_no_notes_from_before_it() {
        // The label would be noted, but the volume refuses the transfer.
        let roles = with(
            with(
                roles(),
                &["source_plate", "fields", "label"],
                json!("Rack;1"),
            ),
            &["volume"],
            json!({"unit": "uL", "value": 0}),
        );
        let call = write(&roles);
        assert_eq!(call.status, 1);
        assert_eq!(call.notes.len(), 1);
        assert_eq!(call.notes[0].code, "refused");
    }

    #[test]
    fn the_writer_can_be_called_directly() {
        let record = serde_json::from_value(transfer("TRF-000042", &roles(), &[])).unwrap();
        let part = TecanWorklist.record(&record).unwrap();
        assert!(part.bytes.starts_with(b"A;Compound source 1;CP-0412;"));
    }
}

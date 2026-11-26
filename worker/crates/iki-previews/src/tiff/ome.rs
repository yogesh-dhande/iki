//! OME-XML (the OME Data Model's schema, 2016-06), as OME-TIFF keeps it in its first
//! IFD's `ImageDescription`: its first `Image`'s `Pixels`, channels and `TiffData`.

use quick_xml::XmlVersion;
use quick_xml::events::{BytesStart, Event};

use super::{Channel, Ome, Plane};

/// The most planes listed from `TiffData`.
const MAX_PLANES: usize = 100_000;
/// The most channels listed.
const MAX_CHANNELS: usize = 10_000;

fn attribute(e: &BytesStart<'_>, name: &str) -> Option<String> {
    e.attributes().flatten().find_map(|a| {
        (a.key.local_name().as_ref() == name)
            .then(|| {
                a.normalized_value(XmlVersion::Implicit1_0)
                    .ok()
                    .map(std::borrow::Cow::into_owned)
            })
            .flatten()
    })
}

fn number<T: std::str::FromStr>(e: &BytesStart<'_>, name: &str) -> Option<T> {
    attribute(e, name).and_then(|v| v.trim().parse().ok())
}

/// One `TiffData`: its first IFD, first plane and how many planes, as given.
struct TiffData {
    ifd: Option<usize>,
    c: u32,
    z: u32,
    t: u32,
    count: Option<usize>,
}

/// What the OME-XML says, or `None` if it isn't OME-XML this reads (or declares a
/// document type, which OME-XML never does).
pub(super) fn parse(xml: &str) -> Option<Ome> {
    let mut reader = quick_xml::Reader::from_str(xml);
    reader.config_mut().expand_empty_elements = true;
    let mut images = 0usize;
    let mut in_pixels = false;
    let mut ome: Option<Ome> = None;
    let mut tiff_data = Vec::new();
    loop {
        match reader.read_event().ok()? {
            Event::DocType(_) => return None,
            Event::Start(e) => match e.local_name().as_ref() {
                "Image" => images += 1,
                "Pixels" if images == 1 && ome.is_none() => {
                    in_pixels = true;
                    let physical_size_x = number::<f64>(&e, "PhysicalSizeX");
                    ome = Some(Ome {
                        size_x: number(&e, "SizeX")?,
                        size_y: number(&e, "SizeY")?,
                        size_c: number(&e, "SizeC").unwrap_or(1),
                        size_z: number(&e, "SizeZ").unwrap_or(1),
                        size_t: number(&e, "SizeT").unwrap_or(1),
                        dimension_order: attribute(&e, "DimensionOrder")
                            .unwrap_or_else(|| "XYZCT".to_owned()),
                        channels: Vec::new(),
                        planes: Vec::new(),
                        physical_size_x,
                        physical_size_y: number(&e, "PhysicalSizeY"),
                        physical_size_unit: attribute(&e, "PhysicalSizeXUnit")
                            .or_else(|| physical_size_x.map(|_| "\u{b5}m".to_owned())),
                    });
                }
                "Channel" if in_pixels => {
                    if let Some(ome) = ome.as_mut()
                        && ome.channels.len() < MAX_CHANNELS
                    {
                        // Colours are signed 32-bit RGBA; some writers write them unsigned.
                        #[allow(clippy::cast_possible_truncation)]
                        let color = number::<i64>(&e, "Color")
                            .filter(|c| (i64::from(i32::MIN)..=i64::from(u32::MAX)).contains(c))
                            .map(|c| c as i32);
                        ome.channels.push(Channel {
                            name: attribute(&e, "Name"),
                            color,
                        });
                    }
                }
                "TiffData" if in_pixels && tiff_data.len() < MAX_PLANES => {
                    tiff_data.push(TiffData {
                        ifd: number(&e, "IFD"),
                        c: number(&e, "FirstC").unwrap_or(0),
                        z: number(&e, "FirstZ").unwrap_or(0),
                        t: number(&e, "FirstT").unwrap_or(0),
                        count: number(&e, "PlaneCount"),
                    });
                }
                _ => {}
            },
            Event::End(e) => match e.local_name().as_ref() {
                "Pixels" => in_pixels = false,
                "Image" if ome.is_some() => break,
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
    }
    let mut ome = ome?;
    ome.planes = planes(&ome, &tiff_data);
    Some(ome)
}

/// Each `TiffData`'s planes: from its first (c, z, t), on in the dimension order, one IFD
/// each. As the schema has it, `IFD` is 0 if not given, and `PlaneCount` 1 if `IFD` is
/// given, else every plane.
fn planes(ome: &Ome, tiff_data: &[TiffData]) -> Vec<Plane> {
    let sizes = [
        ('C', ome.size_c.max(1)),
        ('Z', ome.size_z.max(1)),
        ('T', ome.size_t.max(1)),
    ];
    // The order Z, C and T vary in, fastest first, as `XYZCT` says.
    let mut order: Vec<(char, u32)> = ome
        .dimension_order
        .chars()
        .filter_map(|d| sizes.iter().find(|(name, _)| *name == d).copied())
        .collect();
    if order.len() != 3 {
        order = vec![sizes[1], sizes[0], sizes[2]];
    }
    let total = sizes.iter().map(|(_, n)| u64::from(*n)).product::<u64>();
    let rank = |c: u32, z: u32, t: u32| -> u64 {
        let mut rank = 0u64;
        let mut stride = 1u64;
        for (name, size) in &order {
            let at = match name {
                'C' => c,
                'Z' => z,
                _ => t,
            };
            rank += u64::from(at) * stride;
            stride *= u64::from(*size);
        }
        rank
    };
    let mut out = Vec::new();
    for data in tiff_data {
        let first_ifd = data.ifd.unwrap_or(0);
        let count = data.count.unwrap_or(if data.ifd.is_some() {
            1
        } else {
            usize::try_from(total).unwrap_or(usize::MAX)
        });
        let start = rank(data.c, data.z, data.t);
        for k in 0..count {
            let r = start + k as u64;
            if r >= total || out.len() >= MAX_PLANES {
                break;
            }
            let mut rest = r;
            let (mut c, mut z, mut t) = (0, 0, 0);
            for (name, size) in &order {
                let at = u32::try_from(rest % u64::from(*size)).unwrap_or(0);
                rest /= u64::from(*size);
                match name {
                    'C' => c = at,
                    'Z' => z = at,
                    _ => t = at,
                }
            }
            out.push(Plane {
                c,
                z,
                t,
                ifd: first_ifd + k,
            });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_first_images_pixels() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">
  <Image ID="Image:0" Name="well A1">
    <Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" SizeX="512" SizeY="256"
            SizeC="2" SizeZ="3" SizeT="1" PhysicalSizeX="0.325" PhysicalSizeY="0.325">
      <Channel ID="Channel:0:0" Name="DAPI" Color="65535"/>
      <Channel ID="Channel:0:1" Name="GFP" Color="-16776961"/>
      <TiffData/>
    </Pixels>
  </Image>
  <Image ID="Image:1"><Pixels SizeX="1" SizeY="1" DimensionOrder="XYZCT"/></Image>
</OME>"#;
        let ome = parse(xml).unwrap();
        assert_eq!(
            (ome.size_x, ome.size_y, ome.size_c, ome.size_z),
            (512, 256, 2, 3)
        );
        assert_eq!(ome.physical_size_unit.as_deref(), Some("\u{b5}m"));
        assert_eq!(ome.channels[1].name.as_deref(), Some("GFP"));
        assert_eq!(ome.channels[1].color, Some(-16_776_961));
        assert_eq!(ome.planes.len(), 6);
        // XYCZT: C varies fastest.
        assert_eq!(
            ome.planes[1],
            Plane {
                c: 1,
                z: 0,
                t: 0,
                ifd: 1
            }
        );
        assert_eq!(
            ome.planes[2],
            Plane {
                c: 0,
                z: 1,
                t: 0,
                ifd: 2
            }
        );
    }

    #[test]
    fn refuses_a_document_type() {
        let xml = r#"<?xml version="1.0"?><!DOCTYPE OME [<!ENTITY a "aaaa">]><OME><Image><Pixels SizeX="1" SizeY="1"/></Image></OME>"#;
        assert_eq!(parse(xml), None);
    }
}

"""What a file says about itself besides when (ADR 0224): its instrument, serial number,
operator, software and protocol, and a SAM or BAM file's read groups.

Real files where their licences let them be checked in (server/tests/data/abif and fcs,
and the plate_assays plugin's MARS export); otherwise each file is built from its format's
specification, as test_file_acquired.py's are, with the same builders."""

import random
import struct
import time
from pathlib import Path

import pytest
from test_file_acquired import (
    ABIF_CHAR,
    ABIF_PSTRING,
    MIB,
    CountingSource,
    PaddedSource,
    camera_exif,
    camera_ifd0,
    canto_pairs,
    czi_metadata_xml,
    gradient,
    make_ab1,
    make_bam,
    make_czi,
    make_fcs,
    make_jcamp,
    make_mzml,
    make_sam,
    make_tiff,
    realistic_ab1,
    run_tags,
    trace_tags,
)

from iki_server.files import acquired, metadata
from iki_server.files.acquired import BytesSource, Fact, FileMetadata

DATA = Path(__file__).parent / "data"
MARS = Path(__file__).parents[2] / "plugins/plate_assays/tests/data"
ABIF_CSTRING = 19


def read(name: str, data: bytes) -> FileMetadata:
    from iki_server.entities import registry

    found = registry.file_formats().by_extension(name)
    reader = next((fmt.reader for fmt in found if fmt.reader), None)
    return acquired.read_metadata(reader, BytesSource(data))


def facts(name: str, data: bytes) -> dict[str, str]:
    return {key: fact.value for key, fact in read(name, data).facts.items()}


def stated_in(name: str, data: bytes) -> dict[str, str]:
    return {key: fact.stated_in for key, fact in read(name, data).facts.items()}


# ABIF.


def test_a_3730xl_trace_says_which_instrument_ran_it() -> None:
    """Biopython's 3730.ab1, from a 3730xl at 1st BASE: tags read with a hex editor."""
    found = read("3730.ab1", (DATA / "abif/3730.ab1").read_bytes())
    assert {k: f.value for k, f in found.facts.items()} == {
        "instrument": "3730xl",
        "instrument_name": "ABI-3730-XL-1404-021",
        "serial": "1404-021",
        "software": "3.0",
        "protocol": "LongSeq50_POP7_2",
    }
    assert found.facts["instrument"].stated_in == "ABIF tag HCFG 3 (official instrument name)"
    assert found.facts["serial"].stated_in == (
        "ABIF tag HCFG 4 (instrument parameters), SerialNumber"
    )
    # Its run name says the same: Run_ABI-3730-XL_2009-12-12_09-56_0048.
    assert found.acquired is not None
    assert found.acquired.at == "2009-12-12T09:56:53.00"


def abif_tags(*extra: tuple[bytes, int, int, int, bytes]) -> bytes:
    return make_ab1([*trace_tags(200), *run_tags(), *extra])


def test_abif_without_hcfg_gives_its_model_number() -> None:
    assert stated_in("a.ab1", realistic_ab1()) == {"instrument": "ABIF tag MODL 1 (model number)"}
    assert facts("a.ab1", realistic_ab1()) == {"instrument": "3730"}


def test_abif_reads_pstrings_cstrings_and_short_ones_in_the_entry() -> None:
    data = abif_tags(
        (b"MCHN", 1, ABIF_PSTRING, 1, b"\x10WILMAR-21372-006"),
        (b"HCFG", 3, ABIF_CSTRING, 1, b"3130\x00"),
        (b"RPrN", 1, ABIF_CSTRING, 1, b"Z_BDTV31_36cm-POP7-BDx_Long\x00"),
        (b"SVER", 1, ABIF_PSTRING, 1, b"\x033.1"),
    )
    assert facts("a.ab1", data) == {
        "instrument": "3130",
        "instrument_name": "WILMAR-21372-006",
        "software": "3.1",
        "protocol": "Z_BDTV31_36cm-POP7-BDx_Long",
    }


@pytest.mark.parametrize(
    "parameters",
    [
        b"UnitID=3;CPUBoard=ECPU500;ArraySize=96;\x00",
        b"SerialNumber=1404-021;SerialNumber=1404-022;\x00",
        b"SerialNumber=;\x00",
    ],
)
def test_abif_without_one_clear_serial_has_none(parameters: bytes) -> None:
    data = abif_tags((b"HCFG", 4, ABIF_CSTRING, 1, parameters))
    assert "serial" not in facts("a.ab1", data)


def test_abif_tags_given_twice_or_malformed_are_unknown_and_the_rest_still_read() -> None:
    data = abif_tags(
        (b"HCFG", 3, ABIF_CSTRING, 1, b"3730xl\x00"),
        (b"HCFG", 3, ABIF_CSTRING, 1, b"3500xl\x00"),
        (b"MODL", 1, ABIF_CHAR, 1, b"3730"),
        # Its length says more than it has.
        (b"MCHN", 1, ABIF_PSTRING, 1, b"\x40ABI-3730"),
        # Not ASCII, as ABIF's strings are.
        (b"RPrN", 1, ABIF_CSTRING, 1, "Séquençage\x00".encode("latin-1")),
        # Control characters are no name.
        (b"SVER", 1, ABIF_PSTRING, 1, b"\x043\x07.0"),
    )
    found = read("a.ab1", data)
    assert {k: f.value for k, f in found.facts.items()} == {"instrument": "3730"}
    assert found.acquired is not None


def test_abifs_user_is_who_made_the_plate_so_isnt_its_operator() -> None:
    data = abif_tags((b"User", 1, ABIF_PSTRING, 1, b"\x081st BASE"))
    assert "operator" not in facts("a.ab1", data)


def test_abif_strings_past_their_bound_arent_read() -> None:
    data = abif_tags((b"RPrN", 1, ABIF_CSTRING, 1, b"x" * 5000 + b"\x00"))
    assert "protocol" not in facts("a.ab1", data)


# FCS.


def test_a_cytek_aurora_file_says_its_cytometer_serial_and_operator() -> None:
    """fcsparser's Cytek Aurora header (FCS 3.1), as SpectroFlo wrote it."""
    found = read("aurora.fcs", (DATA / "fcs/cytek-aurora-header.fcs").read_bytes())
    assert {k: (f.value, f.stated_in) for k, f in found.facts.items()} == {
        "instrument": ("Aurora", "FCS keyword $CYT"),
        "serial": ("N0354", "FCS keyword $CYTSN"),
        "operator": ("Admin", "FCS keyword $OP"),
    }
    assert found.acquired is not None
    assert found.acquired.at == "2020-07-22T18:39:40.59"


def test_a_cyflow_cube_8_file_says_who_ran_it_though_its_time_is_malformed() -> None:
    """fcsparser's Sysmex CyFlow Cube 8 file (FCS 3.0). Its $BTIM, 09:42:05:509, is
    neither FCS 3.0's hundredths nor sixtieths, so its date isn't known: its facts are."""
    found = read("cube.fcs", (DATA / "fcs/cyflow-cube-8.fcs").read_bytes())
    assert {k: f.value for k, f in found.facts.items()} == {
        "instrument": "Cube_15",
        "serial": "0131011431",
        "operator": "USER",
    }
    assert found.acquired is None


def test_fcs_20_has_no_serial_keyword() -> None:
    data = make_fcs("FCS2.0", canto_pairs(date="14-MAR-2026", btim="09:21:07"))
    assert facts("a.fcs", data) == {"instrument": "FACSCanto II", "operator": "jdoe"}


def test_fcs_keywords_given_twice_differently_are_unknown() -> None:
    data = make_fcs("FCS3.1", canto_pairs(extra=[("$OP", "asmith"), ("$CYT", "FACSCanto II")]))
    assert facts("a.fcs", data) == {"instrument": "FACSCanto II", "serial": "V96300123"}


def test_fcs_values_without_a_readable_name_are_unknown() -> None:
    pairs = [(k, v) for k, v in canto_pairs() if k not in ("$CYT", "$OP")]
    data = make_fcs("FCS3.1", [*pairs, ("$CYT", "   "), ("$OP", "j\x01doe")])
    assert facts("a.fcs", data) == {"serial": "V96300123"}
    long = make_fcs("FCS3.1", [*pairs, ("$CYT", "C" * 300)])
    assert "instrument" not in facts("a.fcs", long)


def test_fcs_without_the_keywords_states_nothing() -> None:
    pairs = [(k, v) for k, v in canto_pairs() if k not in ("$CYT", "$CYTSN", "$OP")]
    found = read("a.fcs", make_fcs("FCS3.1", pairs))
    assert found.facts == {}
    assert found.acquired is not None


# mzML.

ORBITRAP_PARAMS = (
    '<referenceableParamGroupList count="1"><referenceableParamGroup id="CommonInstrumentParams">'
    '<cvParam cvRef="MS" accession="MS:1003029" name="Orbitrap Eclipse" value=""/>'
    '<cvParam cvRef="MS" accession="MS:1000529" name="instrument serial number" value="FSN40152"/>'
    "</referenceableParamGroup></referenceableParamGroupList>"
)
SOFTWARE = (
    '<softwareList count="2"><software id="Xcalibur" version="4.4.16.14">'
    '<cvParam cvRef="MS" accession="MS:1001455" name="acquisition software" value=""/>'
    '<cvParam cvRef="MS" accession="MS:1000532" name="Xcalibur" value=""/></software>'
    '<software id="pwiz" version="3.0.23240"><cvParam cvRef="MS" accession="MS:1000615" '
    'name="ProteoWizard software" value=""/></software></softwareList>'
)


def mzml(configurations: str, before: str = ORBITRAP_PARAMS + SOFTWARE, run: str = "IC1") -> bytes:
    """An mzML 1.1 file as msconvert writes a Thermo run: the instrument's parameters in a
    group its configurations refer to, and the acquisition software."""
    data = make_mzml(
        f'id="QC_HeLa_01" defaultInstrumentConfigurationRef="{run}" '
        'startTimeStamp="2026-03-14T09:21:07Z"',
        spectra=3,
    ).decode()
    start = data.index("    <instrumentConfigurationList")
    end = data.index("</instrumentConfigurationList>\n") + len("</instrumentConfigurationList>\n")
    listed = f'    {before}<instrumentConfigurationList count="2">{configurations}'
    return (data[:start] + listed + "</instrumentConfigurationList>\n" + data[end:]).encode()


def configuration(id: str, *params: str, software: str = "Xcalibur") -> str:
    ref = f'<softwareRef ref="{software}"/>' if software else ""
    return (
        f'<instrumentConfiguration id="{id}">{"".join(params)}<componentList count="3">'
        '<source order="1"><cvParam cvRef="MS" accession="MS:1000073" '
        'name="electrospray ionization"/></source><analyzer order="2"><cvParam cvRef="MS" '
        'accession="MS:1000484" name="orbitrap"/></analyzer><detector order="3"><cvParam '
        'cvRef="MS" accession="MS:1000624" name="inductive detector"/></detector>'
        f"</componentList>{ref}</instrumentConfiguration>"
    )


GROUP = '<referenceableParamGroupRef ref="CommonInstrumentParams"/>'


def test_mzml_reads_the_default_configurations_instrument_serial_and_software() -> None:
    data = mzml(configuration("IC1", GROUP) + configuration("IC2", GROUP))
    found = read("qc.mzML", data)
    assert {k: (f.value, f.stated_in) for k, f in found.facts.items()} == {
        "instrument": (
            "Orbitrap Eclipse",
            "mzML instrument configuration “IC1”, MS:1003029",
        ),
        "serial": ("FSN40152", "mzML instrument configuration “IC1”, MS:1000529"),
        "software": (
            "Xcalibur 4.4.16.14",
            "mzML software that mzML instrument configuration “IC1” names",
        ),
    }
    assert found.acquired is not None


def test_mzml_reads_the_runs_default_configuration_not_the_first() -> None:
    lumos = '<cvParam cvRef="MS" accession="MS:1002732" name="Orbitrap Fusion Lumos" value=""/>'
    data = mzml(configuration("IC1", lumos) + configuration("IC2", GROUP), run="IC2")
    assert facts("qc.mzML", data)["instrument"] == "Orbitrap Eclipse"
    data = mzml(configuration("IC1", lumos) + configuration("IC2", GROUP), run="IC1")
    assert facts("qc.mzML", data) == {
        "instrument": "Orbitrap Fusion Lumos",
        "software": "Xcalibur 4.4.16.14",
    }


def test_mzml_takes_only_an_instrument_model_term_for_the_instrument() -> None:
    # Ion optics and customizations may be at the same level: they're not the model.
    optics = '<cvParam cvRef="MS" accession="MS:1000221" name="magnetic deflection" value=""/>'
    custom = '<cvParam cvRef="MS" accession="MS:1000032" name="customization" value="FAIMS Pro"/>'
    assert "instrument" not in facts("a.mzML", mzml(configuration("IC1", optics, custom)))
    # A term the vocabulary doesn't have isn't taken for one.
    unknown = '<cvParam cvRef="MS" accession="MS:9999999" name="Orbitrap Zeta" value=""/>'
    assert "instrument" not in facts("a.mzML", mzml(configuration("IC1", unknown)))


def test_mzml_takes_the_instrument_model_terms_value_for_a_model_it_doesnt_have() -> None:
    generic = '<cvParam cvRef="MS" accession="MS:1000031" name="instrument model" value="{v}"/>'
    data = mzml(configuration("IC1", generic.format(v="Astral Zoom")))
    assert facts("a.mzML", data)["instrument"] == "Astral Zoom"
    data = mzml(configuration("IC1", generic.format(v="")))
    assert "instrument" not in facts("a.mzML", data)


def test_mzml_with_two_different_models_states_none() -> None:
    lumos = '<cvParam cvRef="MS" accession="MS:1002732" name="Orbitrap Fusion Lumos" value=""/>'
    assert "instrument" not in facts("a.mzML", mzml(configuration("IC1", GROUP, lumos)))


def test_mzml_vendor_terms_give_way_to_the_model_under_them() -> None:
    thermo = (
        '<cvParam cvRef="MS" accession="MS:1000483" '
        'name="Thermo Fisher Scientific instrument model" value=""/>'
    )
    assert facts("a.mzML", mzml(configuration("IC1", thermo, GROUP)))["instrument"] == (
        "Orbitrap Eclipse"
    )


def test_mzml_software_needs_a_reference_and_one_software_term() -> None:
    assert "software" not in facts("a.mzML", mzml(configuration("IC1", GROUP, software="")))
    assert "software" not in facts("a.mzML", mzml(configuration("IC1", GROUP, software="Missing")))
    two = SOFTWARE.replace(
        'name="Xcalibur" value=""/>',
        'name="Xcalibur" value=""/><cvParam cvRef="MS" accession="MS:1000534" name="MassLynx"/>',
    )
    data = mzml(configuration("IC1", GROUP), before=ORBITRAP_PARAMS + two)
    assert "software" not in facts("a.mzML", data)
    unversioned = SOFTWARE.replace(' version="4.4.16.14"', "")
    data = mzml(configuration("IC1", GROUP), before=ORBITRAP_PARAMS + unversioned)
    assert facts("a.mzML", data)["software"] == "Xcalibur"


def test_mzml_referring_to_a_missing_group_states_no_instrument() -> None:
    missing = '<referenceableParamGroupRef ref="NotThere"/>'
    assert "instrument" not in facts("a.mzML", mzml(configuration("IC1", missing)))


def test_mzml_contacts_arent_operators() -> None:
    contact = (
        '<fileDescription><contact><cvParam cvRef="MS" accession="MS:1000586" '
        'name="contact name" value="William Pennington"/></contact></fileDescription>'
    )
    data = mzml(configuration("IC1", GROUP), before=contact + ORBITRAP_PARAMS + SOFTWARE)
    assert "operator" not in facts("a.mzML", data)


def test_mzml_other_than_11_states_nothing() -> None:
    data = mzml(configuration("IC1", GROUP)).replace(b'version="1.1.0"', b'version="1.0.0"')
    assert facts("a.mzML", data) == {}
    other = mzml(configuration("IC1", GROUP)).replace(b"psi.hupo.org/ms/mzml", b"example.org/x")
    assert facts("a.mzML", other) == {}


def test_mzml_whose_header_doesnt_end_in_the_first_mib_states_nothing() -> None:
    data = mzml(configuration("IC1", GROUP), before=ORBITRAP_PARAMS + SOFTWARE + " " * MIB)
    assert facts("a.mzML", data) == {}


def test_mzml_with_a_dtd_states_nothing_and_expands_no_entity() -> None:
    laughs = (
        '<?xml version="1.0"?><!DOCTYPE mzML [<!ENTITY a "aaaaaaaaaa">'
        + "".join(
            f'<!ENTITY {"b" * i}x "{f"&{'b' * (i - 1)}x;" * 10 if i > 1 else "&a;" * 10}">'
            for i in range(1, 9)
        )
        + "]>"
    )
    data = mzml(configuration("IC1", GROUP)).replace(
        b'<?xml version="1.0" encoding="utf-8"?>', laughs.encode()
    )
    started = time.monotonic()
    assert facts("a.mzML", data) == {}
    assert time.monotonic() - started < 1


def test_mzml_reads_one_window_of_a_big_file() -> None:
    source = CountingSource(PaddedSource(mzml(configuration("IC1", GROUP)), 50 * MIB))
    found = acquired.read_metadata("mzml", source)
    assert found.facts and found.acquired is not None
    assert source.calls == 1
    assert source.asked <= MIB


# JCAMP-DX.


def test_jcamp_says_its_spectrometer() -> None:
    data = make_jcamp(["##LONGDATE=2026/03/14 09:21:07"]).replace(
        b"##SPECTROMETER/DATA SYSTEM=FT-IR", b"##SPECTROMETER/DATA SYSTEM= Bruker ALPHA II, OPUS"
    )
    assert read("a.jdx", data).facts == {
        "instrument": Fact("Bruker ALPHA II, OPUS", "JCAMP-DX ##SPECTROMETER/DATA SYSTEM")
    }


def test_jcamp_non_ascii_spectrometer_is_unknown() -> None:
    data = make_jcamp(["##DATE=2026/03/14"]).replace(
        b"##SPECTROMETER/DATA SYSTEM=FT-IR", "##SPECTROMETER/DATA SYSTEM=Spectrom\u00e8tre".encode()
    )
    found = read("a.jdx", data)
    assert found.facts == {}
    assert found.acquired is not None


# OME-TIFF.


def ome(
    image: str = '<InstrumentRef ID="Instrument:0"/><ExperimenterRef ID="Experimenter:1"/>',
    experimenters: str = (
        '<Experimenter ID="Experimenter:0" FirstName="Quinn" LastName="Adeyemi"/>'
        '<Experimenter ID="Experimenter:1" FirstName="Mira" LastName="Okafor" UserName="mokafor"/>'
    ),
    microscope: str = (
        '<Microscope Manufacturer="Nikon" Model="Eclipse Ti2-E" SerialNumber="541023" '
        'Type="Inverted"/>'
    ),
    prefix: str = '<?xml version="1.0" encoding="UTF-8"?>',
) -> str:
    """OME-XML as Bio-Formats writes it, in the schema's order: experimenters, instruments,
    then the images, the first of which refers to both."""
    return (
        f"{prefix}"
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" '
        'Creator="OME Bio-Formats 8.1.0">'
        f"{experimenters}"
        f'<Instrument ID="Instrument:0">{microscope}<Objective ID="Objective:0:0" '
        'NominalMagnification="20"/></Instrument>'
        f'<Image ID="Image:0" Name="well A1, field 1">'
        f"<AcquisitionDate>2026-03-14T09:21:07.250</AcquisitionDate>{image}"
        '<Pixels BigEndian="false" DimensionOrder="XYCZT" ID="Pixels:0" SizeC="1" SizeT="1" '
        'SizeX="64" SizeY="64" SizeZ="1" Type="uint8"/></Image>'
        '<Image ID="Image:1" Name="well A1, field 2"><InstrumentRef ID="Instrument:0"/>'
        '<ExperimenterRef ID="Experimenter:0"/></Image></OME>'
    )


def ome_tiff(xml: str) -> bytes:
    return make_tiff("<", camera_ifd0(xml), None, gradient())


def test_ome_tiff_says_its_microscope_and_who_imaged_it() -> None:
    found = read("plate.ome.tif", ome_tiff(ome()))
    assert {k: (f.value, f.stated_in) for k, f in found.facts.items()} == {
        "instrument": ("Eclipse Ti2-E", "OME-XML Microscope Model"),
        "manufacturer": ("Nikon", "OME-XML Microscope Manufacturer"),
        "serial": ("541023", "OME-XML Microscope SerialNumber"),
        "operator": ("Mira Okafor", "OME-XML Experimenter name"),
    }
    assert found.acquired is not None and found.acquired.source == "ome-tiff"


def test_ome_tiff_operator_falls_back_to_the_user_name() -> None:
    xml = ome(experimenters='<Experimenter ID="Experimenter:1" UserName="mokafor"/>')
    assert read("a.ome.tif", ome_tiff(xml)).facts["operator"] == Fact(
        "mokafor", "OME-XML Experimenter UserName"
    )


def test_ome_tiff_without_references_states_nothing() -> None:
    found = read("a.ome.tif", ome_tiff(ome(image="")))
    assert found.facts == {}
    assert found.acquired is not None


def test_ome_tiff_with_a_dtd_states_nothing() -> None:
    xml = ome(
        prefix='<?xml version="1.0"?><!DOCTYPE OME [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    )
    assert (
        read("a.ome.tif", ome_tiff(xml.replace('Model="Eclipse Ti2-E"', 'Model="&x;"'))).facts == {}
    )


def test_a_plain_tiff_states_nothing() -> None:
    found = read("gel.tif", make_tiff("<", camera_ifd0(), camera_exif(), gradient()))
    assert found.facts == {}
    assert found.acquired is not None


# CZI.


def test_czi_says_its_microscope_and_operator() -> None:
    found = read("well-B3.czi", make_czi(czi_metadata_xml()))
    assert {k: (f.value, f.stated_in) for k, f in found.facts.items()} == {
        "instrument": ("Celldiscoverer 7", "CZI Microscope Name"),
        "operator": ("Mira Okafor", "CZI Information/User DisplayName"),
    }


def test_czi_reads_zen_2s_system_and_a_manufacturers_model_first() -> None:
    old = '<Microscope Id="Microscope:0"><System>LSM 700, AxioObserver</System></Microscope>'
    xml = czi_metadata_xml(microscope=old, reference='<MicroscopeRef Id="Microscope:0"/>')
    assert read("a.czi", make_czi(xml)).facts["instrument"] == Fact(
        "LSM 700, AxioObserver", "CZI Microscope System"
    )
    made = (
        '<Microscope Id="Microscope:1" Name="Axio Observer 7"><Manufacturer>'
        "<Manufacturer>Carl Zeiss</Manufacturer><Model>Axio Observer 7 materials</Model>"
        "<SerialNumber>3849001234</SerialNumber></Manufacturer></Microscope>"
    )
    assert facts("a.czi", make_czi(czi_metadata_xml(microscope=made))) == {
        "instrument": "Axio Observer 7 materials",
        "manufacturer": "Carl Zeiss",
        "serial": "3849001234",
        "operator": "Mira Okafor",
    }


def test_czi_the_document_saver_isnt_the_operator() -> None:
    assert "operator" not in facts("a.czi", make_czi(czi_metadata_xml(user="")))
    names = "<User><FirstName>Mira</FirstName><LastName>Okafor</LastName></User>"
    assert facts("a.czi", make_czi(czi_metadata_xml(user=names)))["operator"] == "Mira Okafor"


def test_czi_without_a_microscope_reference_has_no_instrument() -> None:
    assert "instrument" not in facts("a.czi", make_czi(czi_metadata_xml(reference="")))
    other = '<MicroscopeRef Id="Microscope:9"/>'
    assert "instrument" not in facts("a.czi", make_czi(czi_metadata_xml(reference=other)))


def test_czi_stops_reading_after_its_information() -> None:
    xml = czi_metadata_xml(after="<Layers>" + "<Layer/>" * 400_000 + "</Layers>")
    source = CountingSource(BytesSource(make_czi(xml, image=bytes(8 * MIB))))
    found = acquired.read_metadata("czi", source)
    assert found.facts and found.acquired is not None
    assert source.calls <= 3
    assert source.asked <= 2 * MIB


@pytest.mark.parametrize(
    "broken",
    [
        lambda data: b"ZISRAWFILX" + data[10:],
        lambda data: data.replace(b"ZISRAWMETADATA", b"ZISRAWSUBBLOCK"),
        lambda data: data.replace(b"<ImageDocument>", b"<OME>").replace(
            b"</ImageDocument>", b"</OME>"
        ),
    ],
)
def test_czi_that_isnt_states_nothing(broken: object) -> None:
    data = make_czi(czi_metadata_xml())
    assert callable(broken)
    assert read("a.czi", broken(data)) == FileMetadata()


def test_czi_whose_metadata_position_or_size_is_wrong_states_nothing() -> None:
    data = bytearray(make_czi(czi_metadata_xml()))
    data[32 + 60 : 32 + 68] = struct.pack("<q", len(data) + 4096)
    assert read("a.czi", bytes(data)) == FileMetadata()
    assert read("a.czi", make_czi(czi_metadata_xml(), xml_size=2**31 - 1)) == FileMetadata()
    assert read("a.czi", make_czi(czi_metadata_xml(), xml_size=-5)) == FileMetadata()


def test_czi_with_a_dtd_expands_nothing() -> None:
    xml = '<?xml version="1.0"?><!DOCTYPE ImageDocument [<!ENTITY a "x">]>' + czi_metadata_xml()
    assert read("a.czi", make_czi(xml)) == FileMetadata()


def test_czi_nested_past_any_format_states_nothing() -> None:
    deep = "<A>" * 2000 + "</A>" * 2000
    xml = czi_metadata_xml(user=f"<User><DisplayName>Mira Okafor</DisplayName>{deep}</User>")
    assert read("a.czi", make_czi(xml)) == FileMetadata()


# SAM and BAM read groups.


@pytest.mark.parametrize("make", [make_sam, make_bam])
def test_sam_and_bam_keep_each_read_group(make: object) -> None:
    assert callable(make)
    found = read("a.bam" if make is make_bam else "a.sam", make(["2026-03-15T10:00:00", None]))
    assert [dict(group) for group in found.read_groups] == [
        {
            "ID": "HV7KCDSX5.1",
            "DT": "2026-03-15T10:00:00",
            "LB": "lib1",
            "PL": "ILLUMINA",
            "PU": "HV7KCDSX5.1.ACGTACGT",
            "SM": "NA12878",
        },
        {
            "ID": "HV7KCDSX5.2",
            "LB": "lib1",
            "PL": "ILLUMINA",
            "PU": "HV7KCDSX5.1.ACGTACGT",
            "SM": "NA12878",
        },
    ]
    assert found.acquired is not None and found.facts == {}


def sam(*read_groups: str) -> bytes:
    lines = ["@HD\tVN:1.6", "@SQ\tSN:chr1\tLN:248956422", *read_groups, "@PG\tID:bwa\tPN:bwa"]
    return ("\n".join(lines) + "\nr001\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\t*\n").encode()


def test_read_groups_keep_the_standard_tags_and_leave_out_others() -> None:
    found = read("a.sam", sam("@RG\tID:A\tSM:S1\tzz:a lab's own\tPM:NovaSeq 6000\tDS:Tumour, FFPE"))
    assert [dict(g) for g in found.read_groups] == [
        {"ID": "A", "DS": "Tumour, FFPE", "PM": "NovaSeq 6000", "SM": "S1"}
    ]


@pytest.mark.parametrize(
    "groups",
    [
        ["@RG\tID:A\tSM:S1", "@RG\tID:A\tSM:S2"],
        ["@RG\tSM:S1"],
        ["@RG\tID:A\tSM:S1\tSM:S2"],
        ["@RG\tID:A\tSM"],
        ["@RG\tID:A\tSM:"],
        ["@RG\tID:A\tDS:" + "d" * 1025],
    ],
)
def test_read_groups_that_cant_all_be_kept_are_none(groups: list[str]) -> None:
    found = read("a.sam", sam("@RG\tID:fine\tSM:S0", *groups))
    assert found.read_groups == ()


def test_a_header_that_isnt_utf8_has_no_read_groups_but_its_date() -> None:
    data = sam("@RG\tID:A\tDT:2026-03-14", "@RG\tID:B\tSM:\xe9chantillon").replace(
        b"\xc3\xa9", b"\xe9"
    )
    found = read("a.sam", data)
    assert found.read_groups == ()
    assert found.acquired is not None


def test_more_read_groups_than_are_kept_are_none() -> None:
    many = [f"@RG\tID:lane{i}\tSM:S{i}" for i in range(metadata.MAX_READ_GROUPS + 1)]
    assert read("a.sam", sam(*many)).read_groups == ()
    assert len(read("a.sam", sam(*many[:-1])).read_groups) == metadata.MAX_READ_GROUPS


# Plate reader exports.


def test_a_mars_export_says_its_test_and_user() -> None:
    """The plate_assays plugin's PHERAstar export (from Benchling's allotropy, MIT)."""
    found = read("qc.csv", (MARS / "mars-384-qc.csv").read_bytes())
    assert {k: (f.value, f.stated_in) for k, f in found.facts.items()} == {
        "operator": ("USER", "BMG MARS export header, User"),
        "protocol": ("472 ABS 384 QC", "BMG MARS export header, Test name"),
    }
    # 03/03/2016 could be March 3 or 3 March.
    assert found.acquired is None


def test_a_mars_export_in_windows_1252_with_semicolons() -> None:
    header = (
        "User: José Ruiz;Path: C:\\MARS;Test run no.: 12\r\n"
        "Test name: Bradford 595;Date: 14.03.2026\r\n"
    )
    found = facts("a.csv", (header + "\r\nRaw Data (595)\r\n").encode("cp1252"))
    assert found == {"operator": "José Ruiz", "protocol": "Bradford 595"}


@pytest.mark.parametrize(
    "text",
    [
        "sample,od600\nA1,0.31\n",
        "User: someone,Path: x\nTest name: y\n",
        "Test name: y,Test run no.: 1\nUser: someone\n",
        'User: a,Test run no.: 1\n"Test name: unterminated\n',
    ],
)
def test_other_csvs_state_nothing(text: str) -> None:
    assert read("a.csv", text.encode()) == FileMetadata()


# What's kept.


def test_values_are_checked_as_people_would_read_them() -> None:
    assert metadata.clean_value("  LSRFortessa ") == "LSRFortessa"
    for bad in ("", "   ", "a\nb", "a\x00b", "bad \ufffd", "x" * 256):
        assert metadata.clean_value(bad) is None


def test_the_first_statement_of_a_fact_is_kept() -> None:
    found = metadata.Facts()
    found.add("instrument", None, "nowhere")
    found.add("instrument", " ", "blank")
    found.add("instrument", "3730xl", "HCFG 3")
    found.add("instrument", "3730", "MODL 1")
    assert found.found() == {"instrument": Fact("3730xl", "HCFG 3")}
    with pytest.raises(KeyError):
        found.add("colour", "red", "nowhere")


def test_stored_facts_from_elsewhere_are_checked() -> None:
    good = {"instrument": {"value": "Aurora", "stated_in": "FCS keyword $CYT"}}
    assert metadata.clean_stored(good) == good
    assert metadata.clean_stored(None) == {}
    bad: object
    for bad in (
        [],
        {"colour": {"value": "red", "stated_in": "x"}},
        {"instrument": {"value": "Aurora"}},
        {"instrument": {"value": "Aurora", "stated_in": "x", "extra": 1}},
        {"instrument": {"value": 7, "stated_in": "x"}},
        {"instrument": {"value": " Aurora", "stated_in": "x"}},
        {"instrument": {"value": "Aurora", "stated_in": ""}},
    ):
        with pytest.raises(ValueError, match="fact"):
            metadata.clean_stored(bad)


def test_read_groups_from_elsewhere_are_checked() -> None:
    good = [{"ID": "A", "SM": "S1"}]
    assert metadata.clean_read_groups(good) == good
    assert metadata.clean_read_groups(None) == []
    bad_groups: tuple[object, ...] = (
        {},
        [1],
        [{"SM": "S1"}],
        [{"ID": "A", "zz": "x"}],
        [{"ID": "A"}, {"ID": "A"}],
    )
    for bad in bad_groups:
        with pytest.raises(ValueError, match="read group"):
            metadata.clean_read_groups(bad)


def test_the_search_text_holds_each_value_once_and_is_bounded() -> None:
    stored = {"instrument": {"value": "Aurora", "stated_in": "x"}}
    groups = [{"ID": "L1", "SM": "NA12878", "DT": "2026-03-14"}, {"ID": "L2", "SM": "NA12878"}]
    assert metadata.search_text(stored, groups) == "Aurora\nL1\nNA12878\nL2"
    big = [{"ID": f"{i:04d}" + "x" * 1000} for i in range(512)]
    assert len(metadata.search_text({}, big)) <= metadata.MAX_SEARCH


def test_parsing_keeps_only_whats_asked_and_refuses_dtds() -> None:
    root, stopped = metadata.parse_xml(
        [b"<a x='1'><b>one</b><c><b>two</b></c></a>"], lambda path: path in {("a",), ("a", "b")}
    )
    assert root is not None and not stopped
    assert [(e.name, e.text) for e in root.children] == [("b", "one")]
    with pytest.raises(metadata.XmlError, match="DTD"):
        metadata.parse_xml([b"<!DOCTYPE a><a/>"], lambda path: True)
    with pytest.raises(metadata.XmlError):
        metadata.parse_xml([b"<a>&unknown;</a>"], lambda path: True)
    with pytest.raises(metadata.XmlError, match="more"):
        metadata.parse_xml([b"<a>" + b"<b/>" * 30_000 + b"</a>"], lambda path: True)


# Every reader, against cut and corrupted files: facts are values people can read, or none.


def fact_fixtures() -> dict[str, tuple[str, bytes]]:
    return {
        "abif": ("a.ab1", (DATA / "abif/3730.ab1").read_bytes()),
        "fcs": ("a.fcs", (DATA / "fcs/cytek-aurora-header.fcs").read_bytes()),
        "mzml": ("a.mzML", mzml(configuration("IC1", GROUP))),
        "ome-tiff": ("a.ome.tif", ome_tiff(ome())),
        "czi": ("a.czi", make_czi(czi_metadata_xml())),
        "sam": ("a.sam", sam("@RG\tID:A\tSM:S1\tLB:L1")),
        "mars": ("a.csv", (MARS / "mars-384-qc.csv").read_bytes()),
    }


@pytest.mark.parametrize("fixture", list(fact_fixtures()))
def test_cut_or_corrupted_files_give_only_readable_facts(fixture: str) -> None:
    name, data = fact_fixtures()[fixture]

    def check(found: FileMetadata) -> None:
        for key, fact in found.facts.items():
            assert key in metadata.FACTS
            assert metadata.clean_value(fact.value) == fact.value
        for group in found.read_groups:
            assert metadata.read_group(group.items()) == dict(group)

    for cut in range(0, len(data), max(1, len(data) // 200)):
        check(read(name, data[:cut]))
    randomly = random.Random(fixture)  # noqa: S311 - made-up corruption, not secrets
    for _ in range(200):
        corrupted = bytearray(data)
        for _ in range(randomly.randint(1, 8)):
            corrupted[randomly.randrange(len(corrupted))] = randomly.randrange(256)
        check(read(name, bytes(corrupted)))

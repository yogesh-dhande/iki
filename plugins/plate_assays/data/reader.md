# Importing an endpoint plate

Use a 96- or 384-well plate and export a single raw absorbance endpoint in
BMG LABTECH MARS. In MicroPlate View, select the raw channel and the **OD**
display. Include the run header, choose comma-separated CSV with a decimal
point and leave unmeasured wells empty. The file must contain one block
labelled `Raw Data (<wavelength>)`, with its wavelength in nanometres.
MARS also offers milli-OD and percent transmission. The default OD export
has no per-cell unit label, so check the display before export.
[BMG LABTECH MARS manual, sections 3.2 and 3.4](https://medicine.umich.edu/sites/default/files/PHERAstar_data%20analysis.pdf).
[MARS CSV export settings](https://help.benchling.com/hc/en-us/articles/29043817765517-BMG-LabTech-MARS-Configuration-Guide).

Assign each measured well to a sample ID or a named control and specify its
replicate number. For a dilution series, record the concentration and its
unit for each well. The layout uses A1–H12 for 96 wells and A1–P24 for 384
wells. Each measured cell joins by its labelled row and column. A zero is a
reading. An empty cell has no reading. The preview gives the original file
row and well for every match and identifies repeated coordinates, missing
readings and readings without assignments. Resolve the issues before commit.
[ANSI/SLAS microplate standards](https://www.slas.org/education/ansi-slas-microplate-standards/).

The signal is dimensionless absorbance, displayed as AU. The original
reading and channel stay with the measurement for review.
[IUPAC definition of absorbance](https://doi.org/10.1351/goldbook.A00028).

## Normalized activity

For deposited percent activity, place `Normalized activity (%)` alone on line one, then the numbered column headings and the labelled plate rows. Each populated cell contains the signed percentage as deposited. Individual well records store it as **Normalized activity (%)**. The fitter retains this unit. [Tox21 example and source](curve-example.md).

## Example files

- [PHERAstar QC plate](../tests/data/mars-384-qc.csv) contains the public MARS
  export of the 384-well `472 ABS 384 QC` run at 484 nm. Its measurements and
  empty wells are preserved. The instrument's directory was redacted and line
  ends normalized. The sample assignments in the parser test are synthetic.
  [Original export](https://github.com/Benchling-Open-Source/allotropy/blob/5bcc4c92b5eba5b2b1febe6efbc98bab6a3b5685/tests/parsers/bmg_mars/testdata/16-03-03%2016-54-03%20472%20ABS%20384%20QC.csv).
  [MIT licence](../tests/data/allotropy-LICENSE.txt).
- [96-well dilution series](../tests/data/mars-96-series.csv) is entirely
  synthetic. A1 through A8 represent a decreasing series at 1, 2, 4, 8, 16,
  32, 64 and 128 nM. The generating curve has a midpoint of 12 nM, an upper
  asymptote of 1 AU, a lower asymptote of 0.1 AU and a Hill slope of magnitude
  1. Fixed offsets of +0.003, −0.005, +0.007, −0.004, +0.006, −0.006, +0.003
     and −0.002 AU model measurement scatter. The readings are rounded to six
     decimal places. A9 is the blank control at 0.1 AU and A10 the reference
     control at 1 AU. All ten occupy replicate 1.

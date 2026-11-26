# The SDF importer's test files

- `pubchem-aspirin-caffeine.sdf` is PubChem's own SDF of two compound
  records, aspirin (CID 2244) and caffeine (CID 2519). PubChem's PUG REST
  service gave it on 25 November 2026, from
  `https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/2244,2519/SDF`,
  and it's kept byte for byte. A compound record's standardised structure and
  its `PUBCHEM_` fields are computed by the NCBI. Some of the fields have
  values of several lines, such as `PUBCHEM_COORDINATE_TYPE` and
  `PUBCHEM_BONDANNOTATIONS`. Information created by the U.S. government is in
  the public domain, and the NCBI places no restrictions on the use or
  distribution of its molecular data.
  [NCBI's policies](https://www.ncbi.nlm.nih.gov/home/about/policies/)
- `stockroom.sdf` was written for these tests by RDKit, from the structures'
  SMILES. It is no one else's data.

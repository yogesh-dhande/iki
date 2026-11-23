# Virtual gel

Import `@ikisci/science/virtual-gel` to register `<iki-virtual-gel>` and
load `@ikisci/ui/tokens.css` and `@ikisci/science/virtual-gel.css`.
Assign `VirtualGelData` to the element's `data` property. Reassign it when the
agarose percentage, lane order or band sizes change.

```js
import "@ikisci/science/virtual-gel";
import "@ikisci/ui/tokens.css";
import "@ikisci/science/virtual-gel.css";

const gel = document.createElement("iki-virtual-gel");
gel.data = {
  label: "Example fragment comparison",
  agarosePercent: 1.5,
  lanes: [
    { ladder: "neb-100-bp" },
    {
      label: "Synthetic sample",
      fragments: [{ sizeBp: 1200 }, { sizeBp: 550 }, { sizeBp: 550 }],
    },
  ],
};
document.body.append(gel);
```

- `label` is an optional figure caption. `agarosePercent` is percent agarose
  from 0.5 to 3, inclusive. `GEL_AGAROSE_RANGE` exports these limits.
- A ladder lane has a `ladder` ID and an optional display label.
  `GEL_LADDERS` lists the catalog numbers, source URLs and published band
  sizes and masses. Both NEB ladders show one band for the adjacent 500 and 517 bp
  fragments, with their two sizes and combined mass in the details.
- A sample lane has a `label` and `fragments`. Each fragment supplies an integer
  `sizeBp` greater than zero. Equal sizes within a lane share a band.
  For known masses, provide a nonnegative `massNg` on every fragment, or give the
  lane a nonnegative `totalMassNg` to apportion among equimolar fragments by length.
  Otherwise the sample lane uses relative band weights proportional to fragment
  length and count, and its table cells have no mass in ng. Known band masses
  share a brightness scale across lanes.

The gel shows relative migration of supplied linear DNA fragment sizes. It
interpolates effective size ranges in log space and uses a near-semilog middle
with compressed ends outside that range. Positions are schematic and stay in
base-pair size order across lanes.
[Southern's comparison of sizing curves](<https://doi.org/10.1016/0003-2697(79)90235-5>) ·
[Thermo Fisher agarose resolution ranges, Table 1](https://assets.thermofisher.com/TFS-Assets/LSG/manuals/MAN0012614_Gen_Recommend_DNA_Electrophoresis_UG.pdf)

The ladder sizes and approximate band masses use the manufacturer's 500 ng
reference load.
[NEB 1 kb DNA Ladder, N3232](https://www.neb.com/products/n3232-1-kb-dna-ladder) ·
[NEB 100 bp DNA Ladder, N3231](https://www.neb.com/products/n3231-100-bp-dna-ladder)

[Run the virtual gel example](virtual-gel.html)

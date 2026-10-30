# Claim-check pack

This pack contains 40 short factual claims in four categories: astronomy, SI units, hydrology, and
oceanography. Each category has five calibration and five held-out claims. Each split is balanced
at 10 true and 10 false claims, and every label links to an official primary source.

The source pages used for the labels are:

- NASA: [Solar System Facts](https://science.nasa.gov/solar-system/solar-system-facts/),
  [About the Planets](https://science.nasa.gov/solar-system/planets/), and
  [Sun Facts](https://science.nasa.gov/sun/facts/)
- NIST: [Definitions of SI Base Units](https://www.nist.gov/si-redefinition/definitions-si-base-units)
- USGS: [How Much Water Is There on Earth?](https://www.usgs.gov/water-science-school/science/how-much-water-there-earth),
  [What Is the Earth's Water Cycle?](https://www.usgs.gov/faqs/what-earths-water-cycle)
- NOAA: [Sea Water](https://www.noaa.gov/jetstream/ocean/sea-water)

The checked-in `source_url` for each item points directly to the page used to verify its label.
Refresh the deterministic JSONL files with:

```bash
python3 packs/claim-check/generate.py
```

The labels were checked against these pages on 2026-10-30. This is a small synthetic benchmark,
not an exhaustive fact-checking dataset or evidence that a model is reliable for high-stakes claims.
Its 20 held-out examples are too few for stable comparative performance estimates.

#!/usr/bin/env python3
"""Generate and validate the source-linked factual-claim pack."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


PACK_DIR = Path(__file__).resolve().parent

# Claims and labels were checked against the linked primary-source pages on 2026-10-30.
# Each pair is: id, category, split, truth, input, source URL.
ITEMS = [
    ("cal-astro-01", "astronomy", "calibration", True, "The solar system has eight planets.", "https://science.nasa.gov/solar-system/planets/"),
    ("cal-astro-02", "astronomy", "calibration", False, "Pluto is one of the eight planets in the solar system.", "https://science.nasa.gov/solar-system/planets/"),
    ("cal-astro-03", "astronomy", "calibration", True, "Mercury, Venus, Earth, and Mars are the four terrestrial planets in our solar system.", "https://science.nasa.gov/solar-system/planets/"),
    ("cal-astro-04", "astronomy", "calibration", False, "Uranus and Neptune are classified as gas giants by NASA.", "https://science.nasa.gov/solar-system/planets/"),
    ("cal-astro-05", "astronomy", "calibration", False, "Jupiter is one of the four innermost terrestrial planets.", "https://science.nasa.gov/solar-system/planets/"),
    ("held-astro-01", "astronomy", "heldout", True, "NASA says the solar system formed about 4.6 billion years ago.", "https://science.nasa.gov/solar-system/solar-system-facts/"),
    ("held-astro-02", "astronomy", "heldout", True, "The Sun contains more than 99 percent of the matter available in the early solar nebula, according to NASA.", "https://science.nasa.gov/solar-system/solar-system-facts/"),
    ("held-astro-03", "astronomy", "heldout", True, "Mercury and Venus are the only two planets in our solar system without moons.", "https://science.nasa.gov/solar-system/solar-system-facts/"),
    ("held-astro-04", "astronomy", "heldout", False, "NASA describes the asteroid belt as the remains of a single planet that exploded.", "https://science.nasa.gov/solar-system/solar-system-facts/"),
    ("held-astro-05", "astronomy", "heldout", False, "The Sun is a planet rather than the solar system's star.", "https://science.nasa.gov/sun/facts/"),

    ("cal-si-01", "si_units", "calibration", True, "One mole contains exactly 6.02214076 × 10^23 specified elementary entities.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("cal-si-02", "si_units", "calibration", False, "The SI meter is defined using a fixed speed of light of 299,792,457 meters per second.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("cal-si-03", "si_units", "calibration", True, "The SI second uses the cesium-133 transition frequency fixed at 9,192,631,770 hertz.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("cal-si-04", "si_units", "calibration", False, "The SI kilogram is still defined by the mass of a particular platinum-iridium cylinder.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("cal-si-05", "si_units", "calibration", False, "The SI candela is defined using monochromatic radiation at 540 hertz.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("held-si-01", "si_units", "heldout", True, "The SI meter is defined by fixing the speed of light in vacuum at exactly 299,792,458 meters per second.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("held-si-02", "si_units", "heldout", True, "The SI kilogram is defined using the exact Planck constant 6.62607015 × 10^-34 joule seconds.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("held-si-03", "si_units", "heldout", True, "The kelvin definition fixes the Boltzmann constant at 1.380649 × 10^-23 joules per kelvin.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("held-si-04", "si_units", "heldout", False, "The SI ampere is defined using an elementary charge of 1.602176634 × 10^-18 coulombs.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),
    ("held-si-05", "si_units", "heldout", False, "One mole is defined as exactly 6.02214076 × 10^22 elementary entities.", "https://www.nist.gov/si-redefinition/definitions-si-base-units"),

    ("cal-hydro-01", "hydrology", "calibration", True, "More than 96 percent of Earth's total water is saline, according to the USGS water-distribution estimate.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("cal-hydro-02", "hydrology", "calibration", True, "Over 68 percent of Earth's freshwater is stored in ice caps, glaciers, and permanent snow.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("cal-hydro-03", "hydrology", "calibration", False, "Rivers contain about one percent of all water on Earth.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("cal-hydro-04", "hydrology", "calibration", False, "Fresh groundwater accounts for about 30 percent of Earth's total water supply.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("cal-hydro-05", "hydrology", "calibration", True, "The USGS says sunlight and gravity drive the continual movement of water on Earth.", "https://www.usgs.gov/faqs/what-earths-water-cycle"),
    ("held-hydro-01", "hydrology", "heldout", True, "About 30 percent of Earth's freshwater is groundwater.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("held-hydro-02", "hydrology", "heldout", True, "The USGS estimate puts the atmosphere's share of Earth's total water at about 0.001 percent.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("held-hydro-03", "hydrology", "heldout", False, "Rivers store more water than groundwater does.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("held-hydro-04", "hydrology", "heldout", False, "Ten percent of Earth's total water supply is freshwater.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),
    ("held-hydro-05", "hydrology", "heldout", False, "Rivers contain more water than the atmosphere, according to the USGS comparison.", "https://www.usgs.gov/water-science-school/science/how-much-water-there-earth"),

    ("cal-ocean-01", "oceanography", "calibration", True, "Typical seawater contains close to 35 grams of dissolved salts per liter.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("cal-ocean-02", "oceanography", "calibration", True, "Of the five ocean basins, the Atlantic is the saltiest on average.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("cal-ocean-03", "oceanography", "calibration", True, "NOAA identifies the Red Sea and Persian Gulf region as ocean areas with salinity around 40 parts per thousand.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("cal-ocean-04", "oceanography", "calibration", False, "Seawater with 35 parts per thousand salinity freezes at about 0 degrees Celsius.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("cal-ocean-05", "oceanography", "calibration", False, "Ice floats because it is denser than liquid water.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("held-ocean-01", "oceanography", "heldout", True, "Adding salt lowers water's freezing temperature.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("held-ocean-02", "oceanography", "heldout", True, "NOAA says ice floats because its density is lower than liquid water's density.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("held-ocean-03", "oceanography", "heldout", False, "Seawater density decreases as salinity increases.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("held-ocean-04", "oceanography", "heldout", False, "NOAA says ocean salinity is greatest near the equator because rainfall adds salt to surface water.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
    ("held-ocean-05", "oceanography", "heldout", False, "Older sea ice contains about ten times as much salt as seawater.", "https://www.noaa.gov/jetstream/ocean/sea-water"),
]


def write_corpus() -> None:
    groups = {"calibration": [], "heldout": []}
    ids_by_split: dict[str, set[str]] = {}
    inputs_by_split: dict[str, set[str]] = {}
    for item_id, category, split, truth, statement, source_url in ITEMS:
        if split not in groups:
            raise ValueError(f"Unknown split {split!r} for {item_id}")
        if urlparse(source_url).scheme != "https":
            raise ValueError(f"Source must use HTTPS: {item_id}")
        groups[split].append({
            "id": item_id,
            "category": category,
            "input": statement,
            "label": {"truth": truth, "source_url": source_url},
        })

    all_ids = [item["id"] for group in groups.values() for item in group]
    categories = {item[1] for item in ITEMS}
    if len(ITEMS) != 40 or len(all_ids) != len(set(all_ids)):
        raise ValueError("Expected exactly 40 items with unique ids")
    for split, rows in groups.items():
        if len(rows) != 20:
            raise ValueError(f"Expected 20 {split} items, got {len(rows)}")
        counts = Counter(row["label"]["truth"] for row in rows)
        if counts != {True: 10, False: 10}:
            raise ValueError(f"{split} must be balanced true/false, got {dict(counts)}")
        if {row["category"] for row in rows} != categories:
            raise ValueError(f"{split} is missing one or more categories")
        category_counts = Counter(row["category"] for row in rows)
        if category_counts != {category: 5 for category in categories}:
            raise ValueError(f"{split} must have five examples per category, got {dict(category_counts)}")
        ids_by_split[split] = {row["id"] for row in rows}
        inputs_by_split[split] = {row["input"] for row in rows}
        if len(inputs_by_split[split]) != len(rows):
            raise ValueError(f"{split} contains duplicate claim text")
    if ids_by_split["calibration"] & ids_by_split["heldout"]:
        raise ValueError("Calibration and held-out ids overlap")
    if inputs_by_split["calibration"] & inputs_by_split["heldout"]:
        raise ValueError("Calibration and held-out statements overlap")
    all_rows = groups["calibration"] + groups["heldout"]
    for category in categories:
        truths = Counter(row["label"]["truth"] for row in all_rows if row["category"] == category)
        if truths != {True: 5, False: 5}:
            raise ValueError(f"{category} must have five true and five false claims, got {dict(truths)}")

    for split, rows in groups.items():
        path = PACK_DIR / f"{split}.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        print(f"wrote {len(rows)} items to {path.name}")


if __name__ == "__main__":
    write_corpus()

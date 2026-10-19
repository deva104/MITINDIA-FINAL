"""Generate data/mock_vahan_registry.xlsx: 100 vehicles, deterministic.

    python scripts/generate_registry.py

Every person is fictitious and every policy number is obviously fake
(DEMO-...). The one fixed row is MH12AB1234, the synthetic motor bundle's
vehicle (tests/fixtures/Synthetic Motor Claim Test Bundle.pdf). Rows built
from real test documents are never written here (the repo is public): they
live in the gitignored data/private_registry_rows.json, or in the
REGISTRY_PRIVATE_ROWS env var, and registry.load() adds them at startup.
"""
import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parents[1] / "data" / "mock_vahan_registry.xlsx"
TODAY = date(2026, 10, 19)
MOBILE = "918408016464"
COLUMNS = [
    "plate", "owner_name", "owner_mobile", "make", "model", "variant", "colour",
    "body_type", "fuel", "registration_date", "rto", "chassis_last5", "engine_last5",
    "insurer", "policy_number", "policy_type", "policy_start", "policy_end", "idv",
    "ncb_percent", "city",
]
STATES = {  # state code: (cities, RTO numbers)
    "MH": (["Pune", "Mumbai", "Nagpur"], [12, 14, 1, 2, 31]),
    "KA": (["Bengaluru", "Mysuru"], [1, 3, 5, 51, 9]),
    "DL": (["New Delhi"], [1, 3, 4, 8, 10]),
    "TN": (["Chennai", "Coimbatore"], [1, 9, 22, 37]),
    "GJ": (["Ahmedabad", "Surat"], [1, 5, 18]),
    "KL": (["Kochi", "Thiruvananthapuram"], [7, 1, 11]),
    "TS": (["Hyderabad"], [9, 7, 8]),
    "UP": (["Lucknow", "Noida"], [32, 16, 14]),
    "RJ": (["Jaipur"], [14, 45]),
    "WB": (["Kolkata"], [2, 6, 20]),
}
CARS = [  # make, model, variants, body, fuel options
    ("Maruti Suzuki", "Swift", ["LXI", "VXI", "ZXI"], "hatchback", ["petrol", "cng"]),
    ("Maruti Suzuki", "Baleno", ["Delta", "Zeta", "Alpha"], "hatchback", ["petrol"]),
    ("Maruti Suzuki", "Dzire", ["VXI", "ZXI"], "sedan", ["petrol", "cng"]),
    ("Hyundai", "Creta", ["E", "S", "SX"], "suv", ["petrol", "diesel"]),
    ("Hyundai", "i20", ["Magna", "Sportz", "Asta"], "hatchback", ["petrol"]),
    ("Hyundai", "Verna", ["S", "SX"], "sedan", ["petrol", "diesel"]),
    ("Tata", "Nexon", ["Smart", "Pure", "Creative"], "suv", ["petrol", "diesel", "electric"]),
    ("Tata", "Punch", ["Pure", "Adventure"], "suv", ["petrol"]),
    ("Mahindra", "XUV700", ["MX", "AX5", "AX7"], "suv", ["diesel", "petrol"]),
    ("Mahindra", "Scorpio-N", ["Z4", "Z8"], "suv", ["diesel"]),
    ("Honda", "City", ["V", "VX", "ZX"], "sedan", ["petrol"]),
    ("Honda", "Amaze", ["S", "VX"], "sedan", ["petrol"]),
    ("Toyota", "Innova Crysta", ["GX", "VX"], "muv", ["diesel"]),
    ("Kia", "Seltos", ["HTE", "HTK", "GTX"], "suv", ["petrol", "diesel"]),
    ("Kia", "Sonet", ["HTE", "HTX"], "suv", ["petrol"]),
]
COLOURS = ["white", "silver", "grey", "black", "red", "blue", "brown", "maroon"]
INSURERS = [
    ("ICICI Lombard General Insurance", "ICL"), ("HDFC ERGO General Insurance", "HDF"),
    ("Bajaj Allianz General Insurance", "BAJ"), ("Tata AIG General Insurance", "TAG"),
    ("The New India Assurance", "NIA"), ("United India Insurance", "UII"),
    ("SBI General Insurance", "SBI"), ("Go Digit General Insurance", "DIG"),
    ("IFFCO Tokio General Insurance", "IFT"), ("Royal Sundaram General Insurance", "RSG"),
]
FIRST = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Kabir", "Rohan", "Arjun", "Nikhil", "Pranav",
         "Siddharth", "Ananya", "Diya", "Isha", "Kavya", "Meera", "Neha", "Pooja", "Riya",
         "Sneha", "Tanvi", "Farhan", "Imran", "Joseph", "Gurpreet", "Lakshmi", "Venkat"]
LAST = ["Sharma", "Verma", "Iyer", "Nair", "Reddy", "Patel", "Shah", "Kulkarni", "Deshpande",
        "Menon", "Pillai", "Banerjee", "Chatterjee", "Gupta", "Agarwal", "Singh", "Khan",
        "Fernandes", "Rao", "Joshi", "Mehta", "Bose", "Naidu", "Gill"]

BUNDLE_ROW = {
    "plate": "MH12AB1234", "owner_name": "RAJESH KUMAR SHARMA", "owner_mobile": MOBILE,
    "make": "Maruti Suzuki", "model": "Swift", "variant": "VXI", "colour": "silver",
    "body_type": "sedan", "fuel": "petrol", "registration_date": "2019-03-22", "rto": "MH12",
    "chassis_last5": "23456", "engine_last5": "76543",
    "insurer": "The New India Assurance", "policy_number": "MOT-NIA-2023-01948271",
    # A long-term policy: active on the bundle's accident date (10-03-2024)
    # and today, so the journey can run on this plate.
    "policy_type": "comprehensive", "policy_start": "2024-01-01", "policy_end": "2026-12-31",
    "idv": 410000, "ncb_percent": 35, "city": "Pune",
}


def plate_for(rng, state, used):
    cities, rtos = STATES[state]
    while True:
        series = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(rng.choice([1, 2, 2])))
        plate = f"{state}{rng.choice(rtos):02d}{series}{rng.randint(1000, 9999)}"
        if plate not in used:
            return plate, rng.choice(cities)


def bh_plate(rng, used):
    while True:
        letters = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(2))
        plate = f"{rng.choice([21, 22, 23, 24])}BH{rng.randint(1000, 9999)}{letters}"
        if plate not in used:
            return plate, rng.choice(["Pune", "Bengaluru", "New Delhi", "Hyderabad"])


def policy(rng, kind, insurer_code, serial):
    if kind == "expired":
        end = TODAY - timedelta(days=rng.randint(20, 400))
    elif kind == "renewed":
        end = TODAY + timedelta(days=365 - rng.randint(1, 45))
    else:
        end = TODAY + timedelta(days=rng.randint(30, 330))
    start = end - timedelta(days=364)
    policy_type = "third_party_only" if kind == "third_party" else "comprehensive"
    number = f"DEMO-{insurer_code}-{start.year}-{serial:06d}"
    return policy_type, start.isoformat(), end.isoformat(), number


def rows():
    rng = random.Random(20261019)
    used = {BUNDLE_ROW["plate"]}
    kinds = ["active"] * 69 + ["expired"] * 10 + ["third_party"] * 10 + ["renewed"] * 10
    rng.shuffle(kinds)
    states = [state for state in STATES for _ in range(9)] + ["MH", "KA", "DL", "TN"]
    rng.shuffle(states)
    out = [dict(BUNDLE_ROW)]
    for index, kind in enumerate(kinds):
        if index % 20 == 7:  # a few BH-series plates
            plate, city = bh_plate(rng, used)
        else:
            plate, city = plate_for(rng, states[index % len(states)], used)
        used.add(plate)
        make, model, variants, body, fuels = rng.choice(CARS)
        insurer, code = rng.choice(INSURERS)
        policy_type, start, end, number = policy(rng, kind, code, 100 + index)
        registered = date(rng.randint(2015, 2025), rng.randint(1, 12), rng.randint(1, 28))
        idv = rng.randrange(300000, 1800000, 5000)
        out.append({
            "plate": plate,
            "owner_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "owner_mobile": MOBILE,
            "make": make, "model": model, "variant": rng.choice(variants),
            "colour": rng.choice(COLOURS), "body_type": body, "fuel": rng.choice(fuels),
            "registration_date": registered.isoformat(),
            "rto": plate[:4] if not plate[2:4] == "BH" else "BH",
            "chassis_last5": f"{rng.randint(0, 99999):05d}",
            "engine_last5": f"{rng.randint(0, 99999):05d}",
            "insurer": insurer, "policy_number": number, "policy_type": policy_type,
            "policy_start": start, "policy_end": end, "idv": idv,
            "ncb_percent": rng.choice([0, 20, 25, 35, 45, 50]), "city": city,
        })
    return out


def main():
    data = rows()
    assert len(data) == 100 and len({row["plate"] for row in data}) == 100
    book = Workbook()
    sheet = book.active
    sheet.title = "registry"
    sheet.append(COLUMNS)
    for row in data:
        sheet.append([row[column] for column in COLUMNS])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    book.save(OUT)
    print(f"wrote {len(data)} rows to {OUT}")


if __name__ == "__main__":
    main()

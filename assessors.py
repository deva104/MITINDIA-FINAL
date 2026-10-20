"""Mock claim assessors and their assignment.

Fictitious assessors in the registry's cities. A claim goes to an assessor
in the vehicle's registered city, round robin by how many claims each
already holds; with nobody in that city, to the region's assessors; with no
region either, to anyone. Every assessor's phone is ASSESSOR_CONTACT (one
demo number for all). A claim always gets an assessor: when the database
cannot be read, the choice is made from this list.
"""
import os

import db

ASSESSORS = (
    {"id": "AS-PUN-01", "name": "Meera Kulkarni", "city": "Pune"},
    {"id": "AS-PUN-02", "name": "Rohit Deshmukh", "city": "Pune"},
    {"id": "AS-MUM-01", "name": "Farah Shaikh", "city": "Mumbai"},
    {"id": "AS-BLR-01", "name": "Karthik Raghavan", "city": "Bengaluru"},
    {"id": "AS-DEL-01", "name": "Simran Kaur", "city": "New Delhi"},
    {"id": "AS-CHE-01", "name": "Lakshmi Narayanan", "city": "Chennai"},
    {"id": "AS-DMN-01", "name": "Hetal Desai", "city": "Daman"},
    {"id": "AS-CHD-01", "name": "Gurpreet Sandhu", "city": "Chandigarh"},
    {"id": "AS-PBL-01", "name": "Arun Mondal", "city": "Port Blair"},
    {"id": "AS-GTK-01", "name": "Pema Bhutia", "city": "Gangtok"},
    {"id": "AS-IMF-01", "name": "Ranjita Ningthoujam", "city": "Imphal"},
)
# A city with no assessor of its own goes to its region's.
REGIONS = {
    "west": ("Pune", "Mumbai", "Daman", "Silvassa", "Diu", "Ahmedabad", "Surat", "Nashik",
             "Nagpur", "Goa", "Panaji"),
    "north": ("New Delhi", "Delhi", "Chandigarh", "Gurugram", "Noida", "Jaipur", "Lucknow",
              "Shimla", "Dehradun", "Jammu", "Srinagar", "Amritsar", "Ludhiana"),
    "south": ("Bengaluru", "Chennai", "Port Blair", "Hyderabad", "Kochi", "Coimbatore",
              "Mysuru", "Puducherry", "Thiruvananthapuram", "Visakhapatnam"),
    "east": ("Gangtok", "Imphal", "Kolkata", "Guwahati", "Shillong", "Agartala", "Aizawl",
             "Kohima", "Itanagar", "Bhubaneswar", "Patna", "Ranchi", "Siliguri"),
}
REGION_OF = {city.lower(): region for region, cities in REGIONS.items() for city in cities}


def contact():
    return os.getenv("ASSESSOR_CONTACT", "").strip()


def seed():
    db.assessors_seed(ASSESSORS)


def _pool(everyone, city):
    city = str(city or "").strip().lower()
    local = [item for item in everyone if str(item["city"]).lower() == city]
    if local:
        return local
    region = REGION_OF.get(city)
    regional = [item for item in everyone
                if region and REGION_OF.get(str(item["city"]).lower()) == region]
    return regional or list(everyone)


def assign(city):
    """{"id", "name", "city", "phone"} for a new claim; never None."""
    try:
        seed()
        everyone = db.assessors_list() or list(ASSESSORS)
        counts = db.assessor_assignment_counts()
    except Exception as error:
        # The claim still gets an assessor; round robin resumes with the database.
        print(f"[assessors] database unavailable ({type(error).__name__}); "
              "choosing from the built-in list", flush=True)
        everyone, counts = list(ASSESSORS), {}
    pool = _pool(everyone, city)
    chosen = min(pool, key=lambda item: (counts.get(item["id"], 0), item["id"]))
    return {"id": chosen["id"], "name": chosen["name"], "city": chosen["city"],
            "phone": contact()}

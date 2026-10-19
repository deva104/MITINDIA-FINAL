"""Mock claim assessors and their assignment.

Six fictitious assessors in the registry's main cities. A claim goes to an
assessor in the vehicle's registered city, round robin by how many claims
each already holds; with nobody in that city, to anyone. Every assessor's
phone is ASSESSOR_CONTACT (one demo number for all).
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
)


def contact():
    return os.getenv("ASSESSOR_CONTACT", "").strip()


def seed():
    db.assessors_seed(ASSESSORS)


def assign(city):
    """{"id", "name", "city", "phone"} for a new claim."""
    seed()
    everyone = db.assessors_list() or list(ASSESSORS)
    local = [item for item in everyone if str(item["city"]).lower() == str(city or "").lower()]
    pool = local or everyone
    counts = db.assessor_assignment_counts()
    chosen = min(pool, key=lambda item: (counts.get(item["id"], 0), item["id"]))
    return {**chosen, "phone": contact()}

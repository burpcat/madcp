# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# madhu/names.py
"""
Canonical name pools for MadCP — madhu.

KRISHNAS  — the 24 tier names, in order from highest to lowest.
            Used by the tier registry. Not used for worker naming.

All other pools are for worker agent naming, assigned per tier:
  Hamsa (leaf, v0) → RISHIS
  Intermediate tiers (future) → HEROES, GRAHA, GUARDIANS, PEETHAS, VAHANAS

Leaf-tier workers are lowercased at generation time by the naming service.
For v0, Hamsa is the deepest active tier, so all Hamsa workers are lowercase
(e.g. "vasishtha", not "Vasishtha").

Lineage path format (implemented at Stage 11):
  {Xx}{Xx}-{agent-name}
  where Xx = first two letters of each ancestor tier's first word.
  Example: Adi Purusha → Hamsa → vasishtha becomes AdHa-vasishtha.
"""

KRISHNAS: list[str] = [
    "Adi Purusha",
    "Sanaka",
    "Varaha",
    "Narada",
    "Nara-Narayana",
    "Kapila",
    "Dattatreya",
    "Yajna",
    "Rishabha",
    "Prithu",
    "Matsya",
    "Kurma",
    "Dhanvantari",
    "Mohini",
    "Narasimha",
    "Vamana",
    "Parashurama",
    "Vedavyasa",
    "Rama",
    "Balarama",
    "Krishna",
    "Buddha",
    "Kalki",
    "Hamsa",
]

# ---------------------------------------------------------------------------
# Worker naming pools
# ---------------------------------------------------------------------------

HEROES: list[str] = [
    "Rama", "Yudhishthira", "Arjuna", "Lakshmana", "Bhima",
    "Nakula", "Sahadeva", "Bharata", "Shatrughna", "Hanuman",
]

GRAHA: list[str] = [
    "Surya", "Chandra", "Brihaspati", "Budha", "Shukra",
    "Mangala", "Shani", "Rahu", "Ketu",
]

GUARDIANS: list[str] = [
    "Indra", "Varuna", "Yama", "Agni", "Vayu",
    "Kubera", "Ishana", "Nirriti",
]

RISHIS: list[str] = [
    "Sanaka", "Sananda", "Sanatana", "Vasishtha",
    "Vishwamitra", "Agastya", "Atri", "Bharadwaja",
]

PEETHAS: list[str] = [
    "Meru", "Kailash", "Mandara", "Himalayas",
    "Varanasi", "Ujjain", "Ayodhya", "Vindhya",
]

VAHANAS: list[str] = [
    "Garuda", "Nandi", "Hamsa", "Makara",
    "Simha", "Vyaghra", "Vrishabha", "Mushika",
]

"""Offline fixture source. No network. Use it to verify the database, scoring
and alert formatting work before you point anything at a live site:

    python run.py --source sample --mode buy --dry-run

The Leon in here is the real listing from Biel, so you can see how it scores.
"""
from __future__ import annotations

from .base import Source, register
from ..models import Listing

FIXTURES = [
    {
        "id": "fx-1", "title": "Urgent Seat Leon FR 1.8 TFSI",
        "body": ("Seat Leon Fr 1.8 Tfsi 160cv. Essence Manuelle. "
                 "Expertisé le 23.06.23. Sièges chauffants. 2 jeux de pneus "
                 "été/hiver. Jantes homologués. Vente cause achat de van "
                 "aménagé. À essayer de suite. Roule très bien, pas de frais "
                 "à prévoir. Factures des travaux effectués à l'appui. "
                 "Prête à l'expertise. Prix négociable."),
        "price": 3700, "km": 195000, "year": 2011, "make": "seat",
        "model": "leon", "region": "Biel/Bienne, BE", "photos": 3,
        "seller": "private",
    },
    {
        "id": "fx-2", "title": "VW Golf 6 GTI, ab MFK",
        "body": ("Verkaufe meinen gepflegten Golf 6 GTI. Ab MFK, unfallfrei, "
                 "Serviceheft lückenlos, 2 Satz Reifen auf Felgen, "
                 "Standheizung, neue Bremsen vorne und hinten, Zahnriemen "
                 "bei 120000 km gemacht, Rechnungen vorhanden."),
        "price": 11500, "km": 138000, "year": 2012, "make": "vw",
        "model": "golf", "region": "Bern", "photos": 12, "seller": "private",
    },
    {
        "id": "fx-3", "title": "Golf VI 2.0 TSI GTI MFK abgelaufen muss weg",
        "body": "Wegen Umzug. MFK abgelaufen. Motor läuft gut.",
        "price": 6900, "km": 152000, "year": 2011, "make": "vw",
        "model": "golf", "region": "Solothurn", "photos": 2, "seller": "private",
    },
    {
        "id": "fx-4", "title": "VW Golf 6 GTI DSG",
        "body": ("Golf GTI DSG in sehr gutem Zustand, ab MFK, ein Vorbesitzer, "
                 "alle Services in der Garage gemacht, Winterräder dabei, "
                 "Xenon, Navi, Sitzheizung, sehr sparsam für die Leistung."),
        "price": 12900, "km": 129000, "year": 2013, "make": "vw",
        "model": "golf", "region": "Fribourg", "photos": 9, "seller": "private",
    },
    {
        "id": "fx-5", "title": "Golf 6 GTI Edition 35",
        "body": "Occasion, guter Zustand, ab Service, Preis verhandelbar.",
        "price": 13500, "km": 141000, "year": 2012, "make": "vw",
        "model": "golf", "region": "Biel/Bienne, BE", "photos": 5,
        "seller": "private",
    },
    {
        "id": "fx-6", "title": "Golf 6 GTI - Garage Muster AG",
        "body": "Ab MFK, 12 Monate Garantie, Eintausch möglich, Finanzierung.",
        "price": 14900, "km": 118000, "year": 2012, "make": "vw",
        "model": "golf", "region": "Bern", "photos": 20, "seller": "dealer",
    },
    {
        "id": "fx-7", "title": "Golf 6 GTI à vendre",
        "body": ("Très belle voiture, expertisée du jour, 4 pneus neufs, "
                 "prix négociable, cause double emploi. Voiture très propre, "
                 "aucun frais à prévoir, jantes en très bon état."),
        "price": 15900, "km": 134000, "year": 2012, "make": "vw",
        "model": "golf", "region": "Biel/Bienne, BE", "photos": 4,
        "seller": "private",
    },
]


@register
class Sample(Source):
    name = "sample"

    def fetch(self):
        return FIXTURES

    def parse(self, r: dict) -> Listing:
        return Listing(
            source=self.name, external_id=r["id"], url=f"https://example.invalid/{r['id']}",
            title=r["title"], body=r["body"], price_chf=r["price"], km=r["km"],
            year=r["year"], make=r["make"], model=r["model"],
            seller_type=r["seller"], region=r["region"], photo_count=r["photos"],
            raw=r,
        )

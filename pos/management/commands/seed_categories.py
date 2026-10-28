from django.core.management.base import BaseCommand
from pos.models import SuperCategory, BusinessType

DATA = {
    "retail-commerce": ("Retail & Commerce", [
        ("retail-store","Retail Stores"),("grocery-supermarket","Grocery & Supermarkets"),
        ("fashion-apparel","Fashion & Apparel"),("pharmacy","Pharmacies"),("electronics","Electronics"),
    ]),
    "food-hospitality": ("Food & Hospitality", [
        ("restaurant-cafe","Restaurants & Cafés"),("fast-food","Fast Food"),
        ("bakery","Bakery"),("juice-beverage","Juice & Beverage"),
        ("ice-cream-dessert","Ice Cream & Dessert"),("catering","Catering"),
        ("food-truck-takeaway","Food Truck & Takeaway"),
    ]),
    "healthcare-pharmacy": ("Healthcare & Pharmacy", [
        ("clinic","Clinics"),("medical-center","Medical Centers"),
        ("health-store","Health Stores"),
    ]),
    "beauty-personal-services": ("Beauty & Personal Services", [
        ("salon","Salons"),("spa","Spa & Wellness"),("barber","Barbers"),
    ]),
    "wholesale-distribution": ("Wholesale & Distribution", [
        ("wholesale","Wholesale"),("distribution","Distribution"),
        ("beverage-agency","Beverage Agencies"),
    ]),
}

class Command(BaseCommand):
    help = "Seed POS super categories and business types"

    def handle(self, *args, **kwargs):
        for sid, (label, types) in DATA.items():
            sc, _ = SuperCategory.objects.update_or_create(
                id=sid, defaults={"label": label, "is_active": True}
            )
            for tid, tlabel in types:
                BusinessType.objects.update_or_create(
                    id=tid, defaults={"label": tlabel, "super_category": sc, "is_active": True}
                )
        self.stdout.write(self.style.SUCCESS("Categories seeded."))

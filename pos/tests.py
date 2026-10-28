import os
import re
import smtplib
import subprocess
import sys
from datetime import timedelta
from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import call_command
from django.core.management.base import CommandError
from io import StringIO
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import SuperCategory, BusinessType, User, Customer


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class Base(TestCase):
    def setUp(self):
        cache.clear()  # reset rate-limit counters between tests
        sc = SuperCategory.objects.create(id="food", label="Food")
        BusinessType.objects.create(id="bakery", label="Bakery", super_category=sc)
        self.c = APIClient()

    # --- helpers -------------------------------------------------------
    def signup_verify(self, email):
        r = self.c.post("/api/auth/signup", {"full_name": "T", "email": email, "password": "password123",
                        "confirm_password": "password123"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        code = re.search(r"code is (\d{6})", mail.outbox[-1].body).group(1)
        return self.c.post("/api/auth/verify-otp", {"email": email, "otp": code}, format="json")

    def login_client(self, email, password="password123"):
        r = APIClient().post("/api/auth/login", {"email": email, "password": password}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['token']}")
        return c

    def make_user(self, email):
        r = self.signup_verify(email)
        self.assertEqual(r.status_code, 200, r.content)
        User.objects.filter(email=email).update(approval_status="approved")   # what the admin does
        c = self.login_client(email)
        r = c.post("/api/business/onboarding", {"super_category_id": "food", "business_type_id": "bakery",
                   "name": email}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return c

    def product(self, c, price=500, **extra):
        r = c.post("/api/me/products", {"name": "Tea", "price_cents": price, **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def table(self, c, name="T1"):
        r = c.post("/api/me/tables", {"name": name}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def staff(self, owner, email, role="cashier", permissions=None, verify=True):
        body = {"full_name": "S", "email": email, "password": "password123", "role": role, "phone": "0300"}
        if permissions is not None:
            body["permissions"] = permissions
        r = owner.post("/api/me/staff", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        sid = r.json()["id"]
        if verify:
            code = re.search(r"code is (\d{6})", mail.outbox[-1].body).group(1)
            self.assertEqual(owner.post(f"/api/me/staff/{sid}/verify", {"otp": code}, format="json").status_code, 200)
            return self.login_client(email), sid
        return None, sid


class AuthAndApprovalTests(Base):
    def test_new_signup_waits_for_admin(self):
        r = self.signup_verify("a@x.com")
        self.assertEqual(r.json()["status"], "pending_approval")
        self.assertNotIn("token", r.json())
        r = self.c.post("/api/auth/login", {"email": "a@x.com", "password": "password123"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "pending_approval"))

    def test_signup_login_rules(self):
        self.make_user("a@x.com")
        self.assertEqual(self.c.post("/api/auth/signup", {"full_name": "T", "email": "a@x.com",
                         "password": "password123"}, format="json").status_code, 409)
        self.assertEqual(self.c.post("/api/auth/login", {"email": "a@x.com", "password": "nope"},
                         format="json").status_code, 401)

    def test_unverified_cannot_login(self):
        self.c.post("/api/auth/signup", {"full_name": "T", "email": "u@x.com", "password": "password123"}, format="json")
        r = self.c.post("/api/auth/login", {"email": "u@x.com", "password": "password123"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "email_unverified"))

    def test_missing_token_is_401(self):
        self.assertEqual(self.c.get("/api/auth/me").status_code, 401)

    def test_expiry_blocks_login_and_live_sessions(self):
        c = self.make_user("a@x.com")
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        User.objects.filter(email="a@x.com").update(access_expires_at=timezone.now() - timedelta(minutes=1))
        r = c.get("/api/auth/me")
        self.assertEqual((r.status_code, r.json()["error"]), (401, "access_expired"))
        r = self.c.post("/api/auth/login", {"email": "a@x.com", "password": "password123"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "access_expired"))
        # extending the period brings them back
        User.objects.filter(email="a@x.com").update(access_expires_at=timezone.now() + timedelta(days=5))
        self.assertEqual(c.get("/api/auth/me").status_code, 200)

    def test_rejected_cannot_login(self):
        self.signup_verify("a@x.com")
        User.objects.filter(email="a@x.com").update(approval_status="rejected")
        r = self.c.post("/api/auth/login", {"email": "a@x.com", "password": "password123"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "rejected"))

    def test_staff_follow_the_owners_expiry(self):
        owner = self.make_user("o@x.com")
        cashier, _ = self.staff(owner, "c@x.com")
        self.assertEqual(cashier.get("/api/me/products").status_code, 200)
        User.objects.filter(email="o@x.com").update(access_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(cashier.get("/api/me/products").status_code, 401)

    def test_admin_approves_with_days_and_emails_login_link(self):
        self.signup_verify("a@x.com")
        u = User.objects.get(email="a@x.com")
        admin_user = User.objects.create_superuser("root@x.com", "pw12345678", "Root")
        c = Client()
        c.force_login(admin_user)
        mail.outbox.clear()
        r = c.post("/admin/pos/user/", {"action": "approve_20_days", "_selected_action": [str(u.pk)]})
        self.assertEqual(r.status_code, 302, r.content[:200])
        u.refresh_from_db()
        self.assertEqual(u.approval_status, "approved")
        self.assertTrue(timezone.now() + timedelta(days=19) < u.access_expires_at < timezone.now() + timedelta(days=21))
        self.assertIn("/login", mail.outbox[-1].body)
        self.assertEqual(mail.outbox[-1].to, ["a@x.com"])
        self.assertEqual(self.login_client("a@x.com").get("/api/auth/me").status_code, 200)

    def test_admin_edit_form_sets_expiry_and_sends_email(self):
        self.signup_verify("a@x.com")
        u = User.objects.get(email="a@x.com")
        admin_user = User.objects.create_superuser("root@x.com", "pw12345678", "Root")
        c = Client(); c.force_login(admin_user)
        mail.outbox.clear()
        r = c.post(f"/admin/pos/user/{u.pk}/change/", {
            "email": "a@x.com", "full_name": "T", "phone": "", "email_verified": "on", "is_active": "on",
            "approval_status": "approved", "access_days": "10", "access_expires_at_0": "", "access_expires_at_1": ""})
        self.assertEqual(r.status_code, 302, r.content[:300])
        u.refresh_from_db()
        self.assertEqual(u.approval_status, "approved")
        self.assertIsNotNone(u.access_expires_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_demo_request_and_login_throttle(self):
        r = self.c.post("/api/demo-requests", {"name": "Z", "phone": "0300", "business_type": "Bakery"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.c.post("/api/demo-requests", {"name": ""}, format="json").status_code, 400)
        codes = [self.c.post("/api/auth/login", {"email": "n@x.com", "password": "bad"}, format="json").status_code
                 for _ in range(12)]
        self.assertEqual((codes[0], codes[-1]), (401, 429))


class CheckoutTests(Base):
    def test_totals_tax_discount_stock_and_restock(self):
        a = self.make_user("a@x.com")
        a.patch("/api/business/profile", {"tax_rate_bps": 1000}, format="json")
        pid = self.product(a, 1000, track_stock=True, stock_quantity=5)
        o = a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 3}], "discount_cents": 500},
                   format="json").json()
        self.assertEqual((o["subtotal_cents"], o["discount_cents"], o["tax_cents"], o["total_cents"]),
                         (3000, 500, 250, 2750))
        stock = lambda: a.get("/api/me/products").json()[0]["stock_quantity"]
        self.assertEqual(stock(), 2)
        r = a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 3}]}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "insufficient_stock"))
        r = a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 2}, {"product_id": pid, "quantity": 2}]}, format="json")
        self.assertEqual(r.status_code, 409)
        a.patch(f"/api/me/orders/{o['id']}", {"status": "refunded"}, format="json")
        self.assertEqual(stock(), 5)
        o2 = a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 4}], "pay_now": False}, format="json").json()
        self.assertEqual(stock(), 1)
        a.patch(f"/api/me/orders/{o2['id']}", {"status": "cancelled"}, format="json")
        self.assertEqual(stock(), 5)

    def test_validation_and_isolation(self):
        a, b = self.make_user("a@x.com"), self.make_user("b@x.com")
        pid = self.product(a)
        self.assertEqual(a.post("/api/me/orders", {"items": []}, format="json").status_code, 400)
        self.assertEqual(a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": -1}]}, format="json").status_code, 400)
        self.assertEqual(a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}], "discount_cents": 501}, format="json").status_code, 400)
        self.assertEqual(a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}], "order_type": "teleport"}, format="json").status_code, 400)
        o = a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}]}, format="json").json()
        self.assertEqual(b.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}]}, format="json").status_code, 400)
        self.assertEqual(b.get(f"/api/me/orders/{o['id']}").status_code, 404)
        self.assertEqual(b.get("/api/me/orders").json(), [])

    def test_order_types_are_saved(self):
        a = self.make_user("a@x.com")
        pid = self.product(a)
        item = [{"product_id": pid, "quantity": 1}]
        take = a.post("/api/me/orders", {"items": item, "order_type": "takeaway"}, format="json").json()
        self.assertEqual(take["order_type"], "takeaway")
        dl = a.post("/api/me/orders", {"items": item, "order_type": "delivery", "delivery_address": "12 Main St",
                    "customer_phone": "0301"}, format="json").json()
        self.assertEqual((dl["order_type"], dl["delivery_address"], dl["customer_phone"]), ("delivery", "12 Main St", "0301"))
        # dine-in needs a table
        self.assertEqual(a.post("/api/me/orders", {"items": item, "order_type": "dine_in"}, format="json").json()["error"], "table_required")
        tid = self.table(a)
        di = a.post("/api/me/orders", {"items": item, "order_type": "dine_in", "table_id": tid}, format="json").json()
        self.assertEqual((di["order_type"], di["table_name"], di["status"]), ("dine_in", "T1", "paid"))
        # takeaway never stores a table
        self.assertIsNone(a.post("/api/me/orders", {"items": item, "order_type": "takeaway", "table_id": tid}, format="json").json()["table"])

    def test_table_occupied_until_paid_or_cancelled(self):
        a = self.make_user("a@x.com")
        pid, tid = self.product(a), self.table(a)
        item = [{"product_id": pid, "quantity": 1}]
        o = a.post("/api/me/orders", {"items": item, "order_type": "dine_in", "table_id": tid, "pay_now": False}, format="json").json()
        tables = a.get("/api/me/tables").json()
        self.assertTrue(tables[0]["occupied"])
        self.assertEqual(tables[0]["order"]["order_number"], o["order_number"])
        r = a.post("/api/me/orders", {"items": item, "order_type": "dine_in", "table_id": tid}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "table_occupied"))
        self.assertEqual(a.delete(f"/api/me/tables/{tid}").status_code, 409)   # can't delete a busy table
        a.patch(f"/api/me/orders/{o['id']}", {"status": "paid", "payment_method": "card"}, format="json")
        self.assertFalse(a.get("/api/me/tables").json()[0]["occupied"])
        self.assertEqual(a.post("/api/me/orders", {"items": item, "order_type": "dine_in", "table_id": tid}, format="json").status_code, 201)

    def test_payment_methods_cash_tender_and_change(self):
        a = self.make_user("a@x.com")
        pid = self.product(a, 1000)
        item = [{"product_id": pid, "quantity": 1}]
        r = a.post("/api/me/orders", {"items": item, "payment_method": "cash", "tendered_cents": 500}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "insufficient_tender"))
        self.assertEqual(a.get("/api/me/orders").json(), [])          # nothing saved on failure
        o = a.post("/api/me/orders", {"items": item, "payment_method": "cash", "tendered_cents": 1500}, format="json").json()
        self.assertEqual((o["payment_method"], o["tendered_cents"], o["change_cents"]), ("cash", 1500, 500))
        o = a.post("/api/me/orders", {"items": item, "payment_method": "cash"}, format="json").json()
        self.assertEqual((o["tendered_cents"], o["change_cents"]), (1000, 0))
        self.assertEqual(a.post("/api/me/orders", {"items": item, "payment_method": "card"}, format="json").json()["payment_method"], "card")
        self.assertEqual(a.post("/api/me/orders", {"items": item, "payment_method": "other"}, format="json").json()["payment_method"], "other")
        self.assertEqual(a.post("/api/me/orders", {"items": item, "payment_method": "bitcoin"}, format="json").status_code, 400)

    def test_phone_creates_or_links_customer(self):
        a = self.make_user("a@x.com")
        pid = self.product(a)
        item = [{"product_id": pid, "quantity": 1}]
        o1 = a.post("/api/me/orders", {"items": item, "customer_phone": "0300111"}, format="json").json()
        o2 = a.post("/api/me/orders", {"items": item, "customer_phone": "0300111"}, format="json").json()
        self.assertEqual(o1["customer"], o2["customer"])
        self.assertEqual(Customer.objects.count(), 1)
        cust = a.get("/api/me/customers").json()[0]
        self.assertEqual((cust["phone"], cust["order_count"], cust["total_spent"], cust["name"]), ("0300111", 2, 1000, "0300111"))
        # a later order that includes the name fills in the placeholder name
        a.post("/api/me/orders", {"items": item, "customer_phone": "0300111", "customer_name": "Ali Khan"}, format="json")
        self.assertEqual(a.get("/api/me/customers").json()[0]["name"], "Ali Khan")
        a.post("/api/me/orders", {"items": item, "customer_phone": "0300111", "customer_name": "Someone Else"}, format="json")
        self.assertEqual(a.get("/api/me/customers").json()[0]["name"], "Ali Khan")   # a real name is never overwritten
        d = a.get(f"/api/me/customers/{o1['customer']}").json()
        self.assertEqual((len(d["orders"]), d["average_order_cents"]), (4, 500))

    def test_order_filters(self):
        a = self.make_user("a@x.com")
        pid, tid = self.product(a), self.table(a, "Window")
        item = [{"product_id": pid, "quantity": 1}]
        a.post("/api/me/orders", {"items": item, "order_type": "takeaway", "payment_method": "cash"}, format="json")
        a.post("/api/me/orders", {"items": item, "order_type": "delivery", "payment_method": "card", "customer_phone": "0777"}, format="json")
        a.post("/api/me/orders", {"items": item, "order_type": "dine_in", "table_id": tid, "pay_now": False}, format="json")
        n = lambda qs: len(a.get("/api/me/orders" + qs).json())
        self.assertEqual((n(""), n("?order_type=delivery"), n("?status=open"), n("?payment_method=card")), (3, 1, 1, 1))
        self.assertEqual((n("?q=window"), n("?q=0777"), n("?q=zzz")), (1, 1, 0))
        today = timezone.localtime().strftime("%Y-%m-%d")
        self.assertEqual((n(f"?date_from={today}&date_to={today}"), n("?date_from=2000-01-01&date_to=2000-01-02")), (3, 0))

    def test_sold_product_cannot_be_deleted_and_duplicate_category(self):
        a = self.make_user("a@x.com")
        pid = self.product(a)
        a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}]}, format="json")
        self.assertEqual(a.delete(f"/api/me/products/{pid}").status_code, 409)
        self.assertEqual(a.post("/api/me/categories", {"name": "Drinks"}, format="json").status_code, 201)
        self.assertEqual(a.post("/api/me/categories", {"name": "drinks"}, format="json").status_code, 409)

    def test_product_extra_fields(self):
        a = self.make_user("a@x.com")
        pid = self.product(a, 900, cost_cents=400, barcode="123", unit="kg", description="Fresh", sku="T-1")
        p = a.get("/api/me/products").json()[0]
        self.assertEqual((p["cost_cents"], p["barcode"], p["unit"], p["description"]), (400, "123", "kg", "Fresh"))
        a.patch(f"/api/me/products/{pid}", {"cost_cents": 450, "unit": "box"}, format="json")
        p = a.get("/api/me/products").json()[0]
        self.assertEqual((p["cost_cents"], p["unit"]), (450, "box"))
        self.assertEqual(a.patch(f"/api/me/products/{pid}", {"cost_cents": -1}, format="json").status_code, 400)


class ReportTests(Base):
    def test_profit_expenses_and_analytics(self):
        a = self.make_user("a@x.com")
        pid = self.product(a, 1000, cost_cents=400)
        a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 2}], "order_type": "delivery",
               "payment_method": "card"}, format="json")
        # changing cost later must NOT rewrite past profit
        a.patch(f"/api/me/products/{pid}", {"cost_cents": 900}, format="json")
        self.assertEqual(a.post("/api/me/expenses", {"title": "Rent", "category": "Rent", "amount_cents": 300}, format="json").status_code, 201)
        f = a.get("/api/me/finance?days=7").json()
        self.assertEqual((f["revenue_cents"], f["cogs_cents"], f["gross_profit_cents"]), (2000, 800, 1200))
        self.assertEqual((f["expenses_cents"], f["net_profit_cents"]), (300, 900))
        self.assertEqual(f["expenses_by_category"][0]["category"], "Rent")
        self.assertEqual(sum(d["profit_cents"] for d in f["daily"]), 900)
        an = a.get("/api/me/analytics?days=7").json()
        self.assertEqual(len(an["by_hour"]), 24)
        self.assertEqual(an["by_type"][0]["order_type"], "delivery")
        self.assertEqual(an["top_products"][0]["profit_cents"], 1200)
        self.assertIn("revenue_change_pct", an)
        exp = a.get("/api/me/expenses?days=7").json()
        self.assertEqual(len(exp), 1)
        self.assertEqual(a.delete(f"/api/me/expenses/{exp[0]['id']}").status_code, 200)
        self.assertEqual(a.get("/api/me/finance?days=7").json()["expenses_cents"], 0)

    def test_refund_removes_revenue_and_custom_range(self):
        a = self.make_user("a@x.com")
        pid = self.product(a, 1000)
        o = a.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}]}, format="json").json()
        self.assertEqual(a.get("/api/me/dashboard").json()["total_revenue_cents"], 1000)
        a.patch(f"/api/me/orders/{o['id']}", {"status": "refunded"}, format="json")
        self.assertEqual(a.get("/api/me/dashboard").json()["total_revenue_cents"], 0)
        today = timezone.localtime().strftime("%Y-%m-%d")
        self.assertEqual(a.get(f"/api/me/analytics?date_from={today}&date_to={today}").status_code, 200)


class TableAndStaffTests(Base):
    def test_only_owner_edits_floor_plan(self):
        owner = self.make_user("o@x.com")
        tid = self.table(owner)
        cashier, _ = self.staff(owner, "c@x.com")
        self.assertEqual(cashier.get("/api/me/tables").status_code, 200)       # needed to pick a table at checkout
        self.assertEqual(cashier.post("/api/me/tables", {"name": "X"}, format="json").status_code, 403)
        self.assertEqual(cashier.patch(f"/api/me/tables/{tid}", {"name": "Y"}, format="json").status_code, 403)
        self.assertEqual(cashier.put("/api/me/tables/layout", {"tables": []}, format="json").status_code, 403)
        manager, _ = self.staff(owner, "m@x.com", role="manager")
        self.assertEqual(manager.post("/api/me/tables", {"name": "X"}, format="json").status_code, 403)

    def test_owner_can_arrange_and_edit_tables(self):
        a = self.make_user("a@x.com")
        t1, t2 = self.table(a, "T1"), self.table(a, "T2")
        self.assertEqual(a.post("/api/me/tables", {"name": "t1"}, format="json").status_code, 409)
        self.assertEqual(a.put("/api/me/tables/layout", {"tables": [{"id": t1, "x": 500, "y": 300}, {"id": t2, "x": 10, "y": 20}]},
                         format="json").status_code, 200)
        rows = {t["name"]: t for t in a.get("/api/me/tables").json()}
        self.assertEqual((rows["T1"]["x"], rows["T1"]["y"], rows["T2"]["x"]), (500, 300, 10))
        r = a.patch(f"/api/me/tables/{t1}", {"name": "Window", "seats": 6, "shape": "round", "section": "Patio"}, format="json").json()
        self.assertEqual((r["name"], r["seats"], r["shape"], r["section"]), ("Window", 6, "round", "Patio"))
        self.assertEqual(a.delete(f"/api/me/tables/{t2}").status_code, 200)

    def test_tables_are_isolated_between_businesses(self):
        a, b = self.make_user("a@x.com"), self.make_user("b@x.com")
        tid = self.table(a)
        self.assertEqual(b.get("/api/me/tables").json(), [])
        self.assertEqual(b.patch(f"/api/me/tables/{tid}", {"name": "Hack"}, format="json").status_code, 404)
        pid = self.product(b)
        r = b.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}], "order_type": "dine_in", "table_id": tid}, format="json")
        self.assertEqual(r.json()["error"], "table_required")

    def test_staff_needs_email_code_before_login(self):
        owner = self.make_user("o@x.com")
        _, sid = self.staff(owner, "c@x.com", verify=False)
        r = APIClient().post("/api/auth/login", {"email": "c@x.com", "password": "password123"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "email_unverified"))
        self.assertEqual(owner.post(f"/api/me/staff/{sid}/verify", {"otp": "000000"}, format="json").status_code, 400)
        code = re.search(r"code is (\d{6})", mail.outbox[-1].body).group(1)
        self.assertEqual(owner.post(f"/api/me/staff/{sid}/verify", {"otp": code}, format="json").status_code, 200)
        self.assertEqual(self.login_client("c@x.com").get("/api/auth/me").status_code, 200)

    def test_per_user_permissions_are_enforced(self):
        owner = self.make_user("o@x.com")
        pid = self.product(owner)
        item = [{"product_id": pid, "quantity": 1}]
        # picks exactly: checkout + orders (no finance, no inventory, no refunds)
        c, sid = self.staff(owner, "c@x.com", permissions=["checkout", "orders"])
        me = c.get("/api/auth/me").json()["business"]
        self.assertEqual(sorted(me["permissions"]), ["checkout", "orders"])
        o = c.post("/api/me/orders", {"items": item}, format="json")
        self.assertEqual(o.status_code, 201)
        self.assertEqual(c.get("/api/me/orders").status_code, 200)
        for method, url in [("get", "/api/me/dashboard"), ("get", "/api/me/finance"), ("get", "/api/me/analytics"),
                            ("get", "/api/me/staff"), ("get", "/api/me/stock-movements"), ("get", "/api/me/expenses"),
                            ("patch", "/api/business/profile"), ("get", f"/api/me/customers/{sid}")]:
            self.assertEqual(getattr(c, method)(url).status_code, 403, url)
        self.assertEqual(c.post("/api/me/products", {"name": "X", "price_cents": 1}, format="json").status_code, 403)
        self.assertEqual(c.patch(f"/api/me/orders/{o.json()['id']}", {"status": "refunded"}, format="json").status_code, 403)
        # owner grants refunds + finance -> works immediately
        owner.patch(f"/api/me/staff/{sid}", {"permissions": ["checkout", "orders", "refunds", "finance"]}, format="json")
        self.assertEqual(c.get("/api/me/finance").status_code, 200)
        self.assertEqual(c.patch(f"/api/me/orders/{o.json()['id']}", {"status": "refunded"}, format="json").status_code, 200)
        # a checkout-less staff member cannot sell
        c2, _ = self.staff(owner, "d@x.com", permissions=["orders"])
        self.assertEqual(c2.post("/api/me/orders", {"items": item}, format="json").status_code, 403)

    def test_owner_only_permissions_cannot_be_granted(self):
        owner = self.make_user("o@x.com")
        c, sid = self.staff(owner, "c@x.com", permissions=["checkout", "users", "settings", "tables_edit"])
        self.assertEqual(c.get("/api/auth/me").json()["business"]["permissions"], ["checkout"])
        self.assertEqual(c.get("/api/me/staff").status_code, 403)
        self.assertEqual(c.post("/api/me/tables", {"name": "X"}, format="json").status_code, 403)

    def test_role_defaults_and_cannot_create_second_business(self):
        owner = self.make_user("o@x.com")
        c, _ = self.staff(owner, "c@x.com")                      # no permissions sent -> cashier defaults
        perms = c.get("/api/auth/me").json()["business"]["permissions"]
        self.assertIn("checkout", perms); self.assertNotIn("finance", perms)
        m, _ = self.staff(owner, "m@x.com", role="manager")
        self.assertIn("finance", m.get("/api/auth/me").json()["business"]["permissions"])
        r = c.post("/api/business/onboarding", {"super_category_id": "food", "business_type_id": "bakery", "name": "Hack"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_staff_isolated_and_deactivation(self):
        a, b = self.make_user("a@x.com"), self.make_user("b@x.com")
        pid = self.product(a)
        cb, sid = self.staff(b, "cb@x.com")
        self.assertEqual(cb.get("/api/me/products").json(), [])
        self.assertEqual(cb.post("/api/me/orders", {"items": [{"product_id": pid, "quantity": 1}]}, format="json").status_code, 400)
        self.assertEqual(a.patch(f"/api/me/staff/{sid}", {"is_active": False}, format="json").status_code, 404)
        self.assertEqual(b.patch(f"/api/me/staff/{sid}", {"is_active": False}, format="json").status_code, 200)
        self.assertEqual(cb.get("/api/me/products").status_code, 401)
        r = self.c.post("/api/auth/login", {"email": "cb@x.com", "password": "password123"}, format="json")
        self.assertEqual(r.status_code, 401)


class FailingBackend(BaseEmailBackend):
    """Pretends the SMTP server rejected our login (e.g. wrong Gmail password)."""
    def send_messages(self, email_messages):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")


class EmailDeliveryTests(Base):
    def test_code_email_has_a_clean_subject_and_the_code(self):
        self.signup_verify("a@x.com")
        self.assertEqual(mail.outbox[0].subject, "Your POS email verification code")
        self.assertRegex(mail.outbox[0].body, r"code is \d{6}")

    @override_settings(EMAIL_BACKEND="pos.tests.FailingBackend")
    def test_failed_delivery_returns_502_leaves_no_account_and_logs_the_reason(self):
        with self.assertLogs("pos.utils", level="ERROR") as logs:
            r = self.c.post("/api/auth/signup", {"full_name": "T", "email": "f@x.com", "password": "password123"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (502, "email_delivery_failed"))
        self.assertFalse(User.objects.filter(email="f@x.com").exists())          # so they can simply try again
        self.assertIn("SMTPAuthenticationError", "\n".join(logs.output))          # the real reason is visible
        self.assertNotIn("password123", "\n".join(logs.output))

    def test_test_email_command_success_and_failure(self):
        out = StringIO()
        call_command("test_email", "me@x.com", stdout=out)
        self.assertEqual(mail.outbox[-1].to, ["me@x.com"])
        self.assertIn("Sent to me@x.com", out.getvalue())
        with override_settings(EMAIL_BACKEND="pos.tests.FailingBackend"):
            out = StringIO()
            with self.assertRaises(CommandError):
                call_command("test_email", "me@x.com", stdout=out)
        self.assertIn("App Password", out.getvalue())                             # tells the user what to do

    def test_email_settings_are_derived_sensibly(self):
        def settings_for(**env):
            code = ("import os,django;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';django.setup();"
                    "from django.conf import settings as s;"
                    "print(s.EMAIL_BACKEND.rsplit('.',2)[-2],s.EMAIL_USE_SSL,s.EMAIL_USE_TLS,s.DEFAULT_FROM_EMAIL,s.EMAIL_HOST_PASSWORD)")
            base = {k: v for k, v in os.environ.items() if not k.startswith(("EMAIL_", "DEFAULT_FROM"))}
            return subprocess.run([sys.executable, "-c", code], env={**base, **env, "PYTHONPATH": "."},
                                  capture_output=True, text=True, check=True).stdout.split()
        # nothing configured -> console (codes are printed in the terminal)
        self.assertEqual(settings_for()[0], "console")
        # Gmail: SMTP is switched on automatically, From = login address, spaces in the app password removed
        r = settings_for(EMAIL_HOST="smtp.gmail.com", EMAIL_HOST_USER="me@gmail.com", EMAIL_HOST_PASSWORD="abcd efgh ijkl mnop")
        self.assertEqual(r, ["smtp", "False", "True", "me@gmail.com", "abcdefghijklmnop"])
        # port 465 means SSL, not STARTTLS
        r = settings_for(EMAIL_HOST="smtp.example.com", EMAIL_PORT="465")
        self.assertEqual((r[0], r[1], r[2]), ("smtp", "True", "False"))
        # an explicit EMAIL_BACKEND is still respected
        r = settings_for(EMAIL_HOST="smtp.example.com", EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
        self.assertEqual(r[0], "console")


class DotenvLoaderTests(TestCase):
    """The .env file is written by hand on Windows, so the loader must survive its quirks."""

    def load(self, text, encoding="utf-8"):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from config.settings import _load_dotenv
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_bytes(text.encode(encoding))
            with mock.patch.dict(os.environ, {}, clear=True):
                names = _load_dotenv(p)
                return names, dict(os.environ)

    def test_notepad_bom_crlf_comments_quotes_and_export(self):
        text = ("\ufeffFIRST=one\r\n# a comment\r\n\r\nEMAIL_HOST=smtp.gmail.com   # my gmail\r\n"
                "EMAIL_HOST_USER = me@gmail.com\r\nEMAIL_HOST_PASSWORD=\"ab cd#ef\"\r\n"
                "export EMAIL_PORT=587\r\nQUOTED='single # kept'\r\nEMPTY=\r\n")
        names, env = self.load(text)
        self.assertEqual(env["FIRST"], "one")                      # BOM did not corrupt the first key
        self.assertEqual(env["EMAIL_HOST"], "smtp.gmail.com")      # trailing comment removed
        self.assertEqual(env["EMAIL_HOST_USER"], "me@gmail.com")   # spaces around = are fine
        self.assertEqual(env["EMAIL_HOST_PASSWORD"], "ab cd#ef")   # quotes protect # and spaces
        self.assertEqual(env["EMAIL_PORT"], "587")                 # export prefix
        self.assertEqual(env["QUOTED"], "single # kept")
        self.assertEqual(env["EMPTY"], "")
        self.assertIn("EMAIL_HOST", names)

    def test_a_real_environment_variable_beats_the_file(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from config.settings import _load_dotenv
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("EMAIL_HOST=from-file\n")
            with mock.patch.dict(os.environ, {"EMAIL_HOST": "from-system"}, clear=True):
                _load_dotenv(p)
                self.assertEqual(os.environ["EMAIL_HOST"], "from-system")

    def test_missing_file_is_fine(self):
        from pathlib import Path
        from config.settings import _load_dotenv
        self.assertEqual(_load_dotenv(Path("/nonexistent/.env")), [])

from datetime import timedelta, datetime, time
from django.db import transaction, IntegrityError
from django.db.models import Sum, Count, F, Q, Max
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import (Customer, Order, OrderItem, Payment, Product, StockMovement, StaffMember,
                     User, DiningTable, Expense, EmailOTP)
from .permissions import HasPerm
from .serializers import (BusinessSerializer, CustomerSerializer, OrderSerializer, ProductSerializer,
                          StockMovementSerializer, StaffSerializer, DiningTableSerializer, ExpenseSerializer)
from .tenant import business_of, perms_of, STAFF_PERMS, ROLE_DEFAULTS, MANAGER, CASHIER
from .authentication import revoke_all_sessions
from .utils import create_otp, verify_otp, OTP_MAX_ATTEMPTS

PAY_METHODS = ("cash", "card", "other")
ORDER_TYPES = ("dine_in", "takeaway", "delivery")


class BusinessMixin:
    permission_classes = [HasPerm]

    def biz(self, request):
        return business_of(request.user)


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _err(code, status=400, **extra):
    return Response({"error": code, **extra}, status=status)


def change_stock(business, product, delta, reason, user=None, order=None):
    """Atomically move stock and write the audit row. Caller must be in a transaction."""
    Product.objects.filter(id=product.id).update(stock_quantity=F("stock_quantity") + delta)
    product.refresh_from_db(fields=["stock_quantity"])
    StockMovement.objects.create(business=business, product=product, change=delta,
                                 balance_after=product.stock_quantity, reason=reason,
                                 order=order, user=user)


def next_order_number(business):
    n = business.orders.count() + 1
    while business.orders.filter(order_number=f"#{n:05d}").exists():
        n += 1
    return f"#{n:05d}"


def parse_day(s, end=False):
    try:
        d = datetime.strptime(str(s), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(datetime.combine(d, time.max if end else time.min))


def local_midnight(days_ago=0):
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=days_ago)


def report_range(request, default_days):
    """(start, end, label_days). Accepts ?date_from=&date_to= or ?days=N."""
    f, t = parse_day(request.query_params.get("date_from")), parse_day(request.query_params.get("date_to"), True)
    if f and t and f <= t:
        return f, t, max(1, (t.date() - f.date()).days + 1)
    days = min(366, max(1, _int(request.query_params.get("days"), default_days)))
    return local_midnight(days - 1), timezone.now(), days


def make_payment(order, method, tendered_cents):
    """Returns an error Response or None. Cash: tendered must cover the total (blank = exact)."""
    if method not in PAY_METHODS:
        return _err("invalid_payment_method")
    tendered = order.total_cents
    if method == "cash" and tendered_cents:
        tendered = _int(tendered_cents)
        if tendered is None or tendered < order.total_cents:
            return _err("insufficient_tender", required=order.total_cents)
    Payment.objects.create(order=order, method=method, amount_cents=order.total_cents,
                           tendered_cents=tendered if method == "cash" else 0)
    return None


# ------------------------------------------------------------------ orders
class OrderListCreateView(BusinessMixin, APIView):
    read_perm = ("orders", "checkout")
    write_perm = ("checkout",)

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        q = request.query_params
        qs = b.orders.select_related("customer", "created_by").prefetch_related("items__product", "payments")
        if q.get("status"):
            qs = qs.filter(status=q["status"])
        if q.get("order_type"):
            qs = qs.filter(order_type=q["order_type"])
        if q.get("payment_method"):
            qs = qs.filter(payments__method=q["payment_method"])
        if q.get("customer"):
            qs = qs.filter(customer_id=q["customer"])
        f, t = parse_day(q.get("date_from")), parse_day(q.get("date_to"), True)
        if f:
            qs = qs.filter(created_at__gte=f)
        if t:
            qs = qs.filter(created_at__lte=t)
        if q.get("q"):
            s = q["q"].strip()
            qs = qs.filter(Q(order_number__icontains=s) | Q(customer__name__icontains=s) |
                           Q(customer_phone__icontains=s) | Q(table_name__icontains=s))
        limit = min(2000, max(1, _int(q.get("limit"), 300)))
        return Response(OrderSerializer(qs.distinct().order_by("-created_at")[:limit], many=True).data)

    @transaction.atomic
    def post(self, request):
        """Checkout. Prices come from the DB, never from the client."""
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        d = request.data
        items = d.get("items")
        if not isinstance(items, list) or not items:
            return _err("empty_cart")
        method = d.get("payment_method", "cash")
        if method not in PAY_METHODS:
            return _err("invalid_payment_method")
        order_type = d.get("order_type", "takeaway")
        if order_type not in ORDER_TYPES:
            return _err("invalid_order_type")

        table = None
        if order_type == "dine_in":
            table = DiningTable.objects.select_for_update().filter(id=d.get("table_id"), business=b).first() \
                if str(d.get("table_id", "")).count("-") == 4 else None
            if not table:
                return _err("table_required")
            if b.orders.filter(table=table, status="open").exists():
                return _err("table_occupied", 409, table=table.name)

        phone = str(d.get("customer_phone", "") or "").strip()[:30]
        customer = None
        if d.get("customer_id"):
            customer = Customer.objects.filter(id=d["customer_id"], business=b).first()
            if not customer:
                return _err("invalid_customer")
        elif phone:   # phone given at checkout -> find or create the customer record
            nm = str(d.get("customer_name", "") or "").strip()[:120]
            customer = Customer.objects.filter(business=b, phone=phone).first()
            if not customer:
                customer = Customer.objects.create(business=b, name=nm or phone, phone=phone)
            elif nm and customer.name == customer.phone:   # earlier order had only a phone: fill in the name now
                customer.name = nm
                customer.save(update_fields=["name"])
        if customer and not phone:
            phone = customer.phone

        merged = {}
        for it in items:
            if not isinstance(it, dict):
                return _err("invalid_input")
            qty = _int(it.get("quantity", 1))
            if qty is None or qty < 1 or qty > 999:
                return _err("invalid_input")
            pid = str(it.get("product_id"))
            merged[pid] = merged.get(pid, 0) + qty

        lines, subtotal = [], 0
        for pid, qty in merged.items():
            p = Product.objects.select_for_update().filter(id=pid, business=b, is_active=True).first() \
                if pid.count("-") == 4 else None
            if not p:
                return _err("invalid_product")
            if p.track_stock and p.stock_quantity < qty:
                return _err("insufficient_stock", 409, product=p.name, available=p.stock_quantity)
            lines.append((p, qty))
            subtotal += p.price_cents * qty

        discount = _int(d.get("discount_cents", 0), 0)
        if discount < 0 or discount > subtotal:
            return _err("invalid_discount")
        taxable = subtotal - discount
        tax = (taxable * b.tax_rate_bps + 5000) // 10000
        total = taxable + tax

        pay_now = d.get("pay_now", True)
        order = None
        for _ in range(5):  # retry on order-number race
            try:
                with transaction.atomic():
                    order = Order.objects.create(
                        business=b, customer=customer, order_number=next_order_number(b),
                        status="paid" if pay_now else "open", order_type=order_type,
                        table=table, table_name=table.name if table else "",
                        customer_phone=phone,
                        delivery_address=str(d.get("delivery_address", "") or "").strip()[:300]
                        if order_type == "delivery" else "",
                        created_by=request.user, subtotal_cents=subtotal,
                        discount_cents=discount, tax_cents=tax, total_cents=total,
                    )
                break
            except IntegrityError:
                continue
        if not order:
            return _err("try_again", 409)

        OrderItem.objects.bulk_create([
            OrderItem(order=order, product=p, quantity=q, unit_price_cents=p.price_cents,
                      unit_cost_cents=p.cost_cents) for p, q in lines
        ])
        for p, q in lines:
            if p.track_stock:
                change_stock(b, p, -q, f"sale {order.order_number}", request.user, order)
        if pay_now:
            bad = make_payment(order, method, d.get("tendered_cents"))
            if bad:
                transaction.set_rollback(True)
                return bad
        order = (Order.objects.prefetch_related("items__product", "payments")
                 .select_related("customer", "created_by").get(id=order.id))
        return Response(OrderSerializer(order).data, status=201)


class OrderDetailView(BusinessMixin, APIView):
    read_perm = ("orders", "checkout")
    write_perm = ("orders",)

    def get_obj(self, request, pk):
        return Order.objects.filter(id=pk, business=self.biz(request)).prefetch_related(
            "items__product", "payments").select_related("customer", "created_by").first()

    def get(self, request, pk):
        o = self.get_obj(request, pk)
        return Response(OrderSerializer(o).data) if o else _err("not_found", 404)

    ALLOWED = {"open": {"paid", "cancelled"}, "paid": {"refunded"}, "cancelled": set(), "refunded": set()}

    @transaction.atomic
    def patch(self, request, pk):
        o = self.get_obj(request, pk)
        if not o:
            return _err("not_found", 404)
        new = request.data.get("status")
        if new not in self.ALLOWED[o.status]:
            return _err("invalid_transition")
        if new == "refunded" and "refunds" not in perms_of(request.user):
            return _err("forbidden", 403)
        if new == "paid":
            bad = make_payment(o, request.data.get("payment_method", "cash"), request.data.get("tendered_cents"))
            if bad:
                transaction.set_rollback(True)
                return bad
        o.status = new
        o.save(update_fields=["status"])
        if new in ("cancelled", "refunded"):
            for it in o.items.select_related("product"):
                if it.product.track_stock:
                    change_stock(o.business, it.product, it.quantity, f"{new} {o.order_number}", request.user, o)
        return Response(OrderSerializer(self.get_obj(request, pk)).data)


# --------------------------------------------------------------- customers
def customer_qs(b):
    return b.customers.annotate(
        order_count=Count("orders", filter=Q(orders__status="paid")),
        total_spent=Sum("orders__total_cents", filter=Q(orders__status="paid")),
        last_order_at=Max("orders__created_at", filter=Q(orders__status="paid")),
    )


class CustomerListCreateView(BusinessMixin, APIView):
    read_perm = ("customers", "checkout")
    write_perm = ("customers", "checkout")

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        qs = customer_qs(b)
        s = request.query_params.get("q", "").strip()
        if s:
            qs = qs.filter(Q(name__icontains=s) | Q(phone__icontains=s) | Q(email__icontains=s))
        sort = {"spent": "-total_spent", "orders": "-order_count", "recent": "-last_order_at",
                "newest": "-created_at"}.get(request.query_params.get("sort"), "name")
        return Response(CustomerSerializer(qs.order_by(sort, "name"), many=True).data)

    def post(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        name = str(request.data.get("name", "")).strip()
        if not 1 <= len(name) <= 120:
            return _err("invalid_input")
        c = Customer.objects.create(
            business=b, name=name,
            phone=str(request.data.get("phone", "")).strip()[:30],
            email=str(request.data.get("email", "")).strip()[:254],
            address=str(request.data.get("address", "")).strip()[:300],
            notes=str(request.data.get("notes", "")).strip()[:500])
        return Response(CustomerSerializer(c).data, status=201)


class CustomerDetailView(BusinessMixin, APIView):
    read_perm = ("customers",)

    def get_obj(self, request, pk):
        b = self.biz(request)
        return customer_qs(b).filter(id=pk).first() if b else None

    def get(self, request, pk):
        c = self.get_obj(request, pk)
        if not c:
            return _err("not_found", 404)
        orders = (c.orders.select_related("created_by").prefetch_related("items__product", "payments")
                  .order_by("-created_at")[:50])
        data = CustomerSerializer(c).data
        data["average_order_cents"] = (c.total_spent or 0) // c.order_count if c.order_count else 0
        data["orders"] = OrderSerializer(orders, many=True).data
        return Response(data)

    def patch(self, request, pk):
        c = self.get_obj(request, pk)
        if not c:
            return _err("not_found", 404)
        if "name" in request.data:
            name = str(request.data["name"]).strip()
            if not name:
                return _err("invalid_input")
            c.name = name[:120]
        for f, n in (("phone", 30), ("email", 254), ("address", 300), ("notes", 500)):
            if f in request.data:
                setattr(c, f, str(request.data[f] or "").strip()[:n])
        c.save()
        return Response(CustomerSerializer(self.get_obj(request, pk)).data)

    def delete(self, request, pk):
        c = self.get_obj(request, pk)
        if c:
            c.delete()
        return Response({"ok": True})


# ------------------------------------------------------------------ tables
class TableListCreateView(BusinessMixin, APIView):
    read_perm = ("checkout", "floor_plan", "tables_edit")
    write_perm = ("tables_edit",)

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        busy = {o.table_id: o for o in b.orders.filter(status="open", table__isnull=False)}
        out = []
        for t in b.tables.order_by("section", "name"):
            row = dict(DiningTableSerializer(t).data)
            o = busy.get(t.id)
            row["occupied"] = bool(o)
            row["order"] = {"id": str(o.id), "order_number": o.order_number, "total_cents": o.total_cents,
                            "since": o.created_at} if o else None
            out.append(row)
        return Response(out)

    def post(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        name = str(request.data.get("name", "")).strip()[:40]
        if not name:
            return _err("invalid_input")
        if b.tables.filter(name__iexact=name).exists():
            return _err("duplicate_name", 409)
        shape = request.data.get("shape", "rect")
        if shape not in ("rect", "round"):
            return _err("invalid_input")
        n = b.tables.count()
        t = DiningTable.objects.create(
            business=b, name=name, shape=shape,
            section=(str(request.data.get("section", "")).strip() or "Dining room")[:40],
            seats=min(50, max(1, _int(request.data.get("seats"), 4))),
            x=_int(request.data.get("x"), 20 + (n % 6) * 110), y=_int(request.data.get("y"), 20 + (n // 6) * 80),
            w=96 if shape == "rect" else 72, h=64 if shape == "rect" else 72)
        return Response(DiningTableSerializer(t).data, status=201)


class TableDetailView(BusinessMixin, APIView):
    read_perm = ("tables_edit",)

    def get_obj(self, request, pk):
        return DiningTable.objects.filter(id=pk, business=self.biz(request)).first()

    def patch(self, request, pk):
        t = self.get_obj(request, pk)
        if not t:
            return _err("not_found", 404)
        d = request.data
        if "name" in d:
            name = str(d["name"]).strip()[:40]
            if not name:
                return _err("invalid_input")
            if t.business.tables.filter(name__iexact=name).exclude(id=t.id).exists():
                return _err("duplicate_name", 409)
            t.name = name
        if "section" in d:
            t.section = (str(d["section"]).strip() or "Dining room")[:40]
        if "seats" in d:
            t.seats = min(50, max(1, _int(d["seats"], t.seats)))
        if "shape" in d and d["shape"] in ("rect", "round"):
            t.shape = d["shape"]
        t.save()
        return Response(DiningTableSerializer(t).data)

    def delete(self, request, pk):
        t = self.get_obj(request, pk)
        if not t:
            return Response({"ok": True})
        if t.orders.filter(status="open").exists():
            return _err("table_occupied", 409, table=t.name)
        t.delete()
        return Response({"ok": True})


class TableLayoutView(BusinessMixin, APIView):
    """Saves the dragged positions of many tables in one request."""
    read_perm = ("tables_edit",)

    @transaction.atomic
    def put(self, request):
        b = self.biz(request)
        rows = request.data.get("tables")
        if not b or not isinstance(rows, list):
            return _err("invalid_input")
        mine = {str(t.id): t for t in b.tables.all()}
        for r in rows:
            t = mine.get(str(r.get("id")))
            x, y = _int(r.get("x")), _int(r.get("y"))
            if t and x is not None and y is not None:
                t.x, t.y = max(0, min(3000, x)), max(0, min(3000, y))
                t.save(update_fields=["x", "y"])
        return Response({"ok": True})


# ------------------------------------------------------------------ reports
def _paid(b, start, end):
    return b.orders.filter(status="paid", created_at__gte=start, created_at__lte=end)


def _cogs(b, start, end):
    return (OrderItem.objects.filter(order__business=b, order__status="paid",
            order__created_at__gte=start, order__created_at__lte=end)
            .aggregate(t=Sum(F("quantity") * F("unit_cost_cents")))["t"] or 0)


def _pct(cur, prev):
    if not prev:
        return None
    return round((cur - prev) * 100 / prev, 1)


def build_report(b, start, end, days):
    paid = _paid(b, start, end)
    rows = list(paid.values("created_at", "total_cents", "tax_cents", "discount_cents", "order_type", "created_by__full_name"))
    revenue = sum(r["total_cents"] for r in rows)
    tax = sum(r["tax_cents"] for r in rows)
    discounts = sum(r["discount_cents"] for r in rows)
    count = len(rows)
    cogs = _cogs(b, start, end)
    net_sales = revenue - tax

    by_day, by_hour, by_type, by_cashier = {}, [0] * 24, {}, {}
    hour_orders = [0] * 24
    for r in rows:
        lt = timezone.localtime(r["created_at"])
        d = by_day.setdefault(lt.date(), [0, 0]); d[0] += r["total_cents"]; d[1] += 1
        by_hour[lt.hour] += r["total_cents"]; hour_orders[lt.hour] += 1
        t = by_type.setdefault(r["order_type"], [0, 0]); t[0] += r["total_cents"]; t[1] += 1
        c = by_cashier.setdefault(r["created_by__full_name"] or "Unknown", [0, 0]); c[0] += r["total_cents"]; c[1] += 1
    first = timezone.localtime(start).date()
    n_days = min(366, (timezone.localtime(end).date() - first).days + 1)
    series = [{"date": (first + timedelta(days=i)).isoformat(),
               "revenue_cents": by_day.get(first + timedelta(days=i), [0, 0])[0],
               "orders": by_day.get(first + timedelta(days=i), [0, 0])[1]} for i in range(n_days)]

    items = OrderItem.objects.filter(order__in=paid)
    top = (items.values("product_id", "product__name")
           .annotate(qty=Sum("quantity"), revenue=Sum(F("quantity") * F("unit_price_cents")),
                     profit=Sum(F("quantity") * (F("unit_price_cents") - F("unit_cost_cents"))))
           .order_by("-qty")[:8])
    cats = (items.values("product__category__name")
            .annotate(qty=Sum("quantity"), revenue=Sum(F("quantity") * F("unit_price_cents"))).order_by("-revenue")[:8])
    custs = (paid.filter(customer__isnull=False).values("customer_id", "customer__name")
             .annotate(spent=Sum("total_cents"), n=Count("id")).order_by("-spent")[:5])
    methods = (Payment.objects.filter(order__in=paid).values("method")
               .annotate(total=Sum("amount_cents"), n=Count("id")))

    span = end - start
    pstart, pend = start - span - timedelta(seconds=1), start - timedelta(seconds=1)
    prev = _paid(b, pstart, pend).aggregate(t=Sum("total_cents"), n=Count("id"))
    prev_rev, prev_n = prev["t"] or 0, prev["n"] or 0
    return {
        "days": days, "revenue_cents": revenue, "orders": count,
        "average_order_cents": revenue // count if count else 0,
        "tax_cents": tax, "discount_cents": discounts, "net_sales_cents": net_sales,
        "cogs_cents": cogs, "gross_profit_cents": net_sales - cogs,
        "margin_pct": round((net_sales - cogs) * 100 / net_sales, 1) if net_sales else 0,
        "prev_revenue_cents": prev_rev, "prev_orders": prev_n,
        "revenue_change_pct": _pct(revenue, prev_rev), "orders_change_pct": _pct(count, prev_n),
        "series": series,
        "by_hour": [{"hour": h, "revenue_cents": by_hour[h], "orders": hour_orders[h]} for h in range(24)],
        "by_type": [{"order_type": k, "revenue_cents": v[0], "orders": v[1]} for k, v in by_type.items()],
        "by_method": [{"method": m["method"], "total_cents": m["total"], "count": m["n"]} for m in methods],
        "by_cashier": sorted([{"name": k, "revenue_cents": v[0], "orders": v[1]} for k, v in by_cashier.items()],
                             key=lambda x: -x["revenue_cents"]),
        "top_products": [{"product_id": str(t["product_id"]), "name": t["product__name"], "quantity": t["qty"],
                          "revenue_cents": t["revenue"], "profit_cents": t["profit"]} for t in top],
        "by_category": [{"name": c["product__category__name"] or "Uncategorised", "quantity": c["qty"],
                         "revenue_cents": c["revenue"]} for c in cats],
        "top_customers": [{"customer_id": str(c["customer_id"]), "name": c["customer__name"],
                           "spent_cents": c["spent"], "orders": c["n"]} for c in custs],
    }


def sales_report(b, days):
    return build_report(b, local_midnight(days - 1), timezone.now(), days)


class AnalyticsView(BusinessMixin, APIView):
    read_perm = ("analytics",)

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        start, end, days = report_range(request, 7)
        return Response(build_report(b, start, end, days))


class FinanceView(BusinessMixin, APIView):
    read_perm = ("finance",)

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        start, end, days = report_range(request, 30)
        today = local_midnight(0)
        total = lambda qs: qs.aggregate(t=Sum("total_cents"))["t"] or 0
        paid_all = b.orders.filter(status="paid")

        rep = build_report(b, start, end, days)
        exp_qs = b.expenses.filter(spent_on__gte=timezone.localtime(start).date(), spent_on__lte=timezone.localtime(end).date())
        expenses_total = exp_qs.aggregate(t=Sum("amount_cents"))["t"] or 0
        exp_by_cat = [{"category": e["category"], "total_cents": e["t"]}
                      for e in exp_qs.values("category").annotate(t=Sum("amount_cents")).order_by("-t")]
        exp_by_day = {e["spent_on"]: e["t"] for e in exp_qs.values("spent_on").annotate(t=Sum("amount_cents"))}

        # per-day profit table (net sales - cost - expenses)
        cogs_day = {}
        for it in (OrderItem.objects.filter(order__business=b, order__status="paid",
                   order__created_at__gte=start, order__created_at__lte=end)
                   .values("order__created_at", "quantity", "unit_cost_cents")):
            k = timezone.localtime(it["order__created_at"]).date()
            cogs_day[k] = cogs_day.get(k, 0) + it["quantity"] * it["unit_cost_cents"]
        tax_day = {}
        for o in _paid(b, start, end).values("created_at", "tax_cents"):
            k = timezone.localtime(o["created_at"]).date()
            tax_day[k] = tax_day.get(k, 0) + o["tax_cents"]
        daily = []
        for s in rep["series"]:
            d = datetime.strptime(s["date"], "%Y-%m-%d").date()
            net = s["revenue_cents"] - tax_day.get(d, 0)
            ex = exp_by_day.get(d, 0)
            daily.append({"date": s["date"], "orders": s["orders"], "revenue_cents": s["revenue_cents"],
                          "cogs_cents": cogs_day.get(d, 0), "expenses_cents": ex,
                          "profit_cents": net - cogs_day.get(d, 0) - ex})

        cash = next((m["total_cents"] for m in rep["by_method"] if m["method"] == "cash"), 0)
        recent = Payment.objects.filter(order__business=b).select_related("order").order_by("-created_at")[:20]
        return Response({
            "days": days,
            "today_cents": total(paid_all.filter(created_at__gte=today)),
            "week_cents": total(paid_all.filter(created_at__gte=local_midnight(6))),
            "month_cents": total(paid_all.filter(created_at__gte=local_midnight(29))),
            "all_time_cents": total(paid_all),
            "refunded_cents": total(b.orders.filter(status="refunded", created_at__gte=start, created_at__lte=end)),
            "revenue_cents": rep["revenue_cents"], "tax_cents": rep["tax_cents"],
            "discount_cents": rep["discount_cents"], "net_sales_cents": rep["net_sales_cents"],
            "cogs_cents": rep["cogs_cents"], "gross_profit_cents": rep["gross_profit_cents"],
            "expenses_cents": expenses_total, "net_profit_cents": rep["gross_profit_cents"] - expenses_total,
            "cash_expected_cents": cash,
            "open_orders_cents": total(b.orders.filter(status="open")),
            "by_method": rep["by_method"], "expenses_by_category": exp_by_cat, "daily": daily,
            "recent_payments": [{"id": str(p.id), "order_number": p.order.order_number, "method": p.method,
                                 "amount_cents": p.amount_cents, "order_status": p.order.status,
                                 "created_at": p.created_at} for p in recent],
        })


class ExpenseListCreateView(BusinessMixin, APIView):
    read_perm = ("finance",)

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        start, end, _ = report_range(request, 30)
        qs = b.expenses.filter(spent_on__gte=timezone.localtime(start).date(),
                               spent_on__lte=timezone.localtime(end).date()).order_by("-spent_on", "-created_at")
        return Response(ExpenseSerializer(qs, many=True).data)

    def post(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        d = request.data
        title = str(d.get("title", "")).strip()[:120]
        amount = _int(d.get("amount_cents"))
        day = parse_day(d.get("spent_on")) if d.get("spent_on") else timezone.localtime()
        if not title or amount is None or amount <= 0 or not day:
            return _err("invalid_input")
        e = Expense.objects.create(business=b, title=title, amount_cents=amount,
                                   category=(str(d.get("category", "")).strip() or "General")[:60],
                                   spent_on=day.date(), note=str(d.get("note", "")).strip()[:300],
                                   created_by=request.user)
        return Response(ExpenseSerializer(e).data, status=201)


class ExpenseDetailView(BusinessMixin, APIView):
    read_perm = ("finance",)

    def delete(self, request, pk):
        Expense.objects.filter(id=pk, business=self.biz(request)).delete()
        return Response({"ok": True})


# ------------------------------------------------------------ business profile
class BusinessProfileView(BusinessMixin, APIView):
    read_perm = ("settings",)

    def patch(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        if "name" in request.data:
            name = str(request.data["name"]).strip()
            if not name:
                return _err("invalid_input")
            b.name = name[:150]
        for f, n in (("phone", 30), ("email", 254), ("address", 500)):
            if f in request.data:
                setattr(b, f, str(request.data[f] or "").strip()[:n] or None)
        if "currency" in request.data:
            b.currency = str(request.data["currency"]).upper()[:3]
        if "tax_rate_bps" in request.data:
            bps = _int(request.data["tax_rate_bps"])
            if bps is None or not 0 <= bps <= 10000:
                return _err("invalid_input")
            b.tax_rate_bps = bps
        if "receipt_footer" in request.data:
            b.receipt_footer = str(request.data["receipt_footer"] or "").strip()[:200]
        b.save()
        return Response(BusinessSerializer(b).data)


# ---------------------------------------------------------------- stock
class StockAdjustView(BusinessMixin, APIView):
    read_perm = ("inventory",)

    @transaction.atomic
    def post(self, request, pk):
        b = self.biz(request)
        p = Product.objects.select_for_update().filter(id=pk, business=b).first() if b else None
        if not p:
            return _err("not_found", 404)
        delta = _int(request.data.get("change"))
        if not delta:
            return _err("invalid_input")
        if not p.track_stock:
            return _err("stock_not_tracked")
        if p.stock_quantity + delta < 0:
            return _err("insufficient_stock", 409, available=p.stock_quantity)
        reason = str(request.data.get("reason", "")).strip()[:60] or "manual adjustment"
        change_stock(b, p, delta, reason, request.user)
        return Response(ProductSerializer(p).data)


class StockMovementListView(BusinessMixin, APIView):
    read_perm = ("inventory",)

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        qs = b.stock_movements.select_related("product", "user").order_by("-created_at")[:200]
        return Response(StockMovementSerializer(qs, many=True).data)


# ---------------------------------------------------------------- staff
def _clean_perms(raw, role):
    if not isinstance(raw, list) or not raw:
        return list(ROLE_DEFAULTS[role])
    return [p for p in STAFF_PERMS if p in raw]


def _send_staff_code(user):
    try:
        create_otp(user, "verify")
        return True
    except Exception:
        return False


class StaffListCreateView(BusinessMixin, APIView):
    read_perm = ("users",)

    def get(self, request):
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        return Response({"staff": StaffSerializer(b.staff.select_related("user").order_by("created_at"), many=True).data,
                         "all_permissions": STAFF_PERMS, "role_defaults": ROLE_DEFAULTS})

    @transaction.atomic
    def post(self, request):
        """Step 'create': saves the account (unverified) and emails the staff member a code."""
        b = self.biz(request)
        if not b:
            return _err("no_business", 404)
        d = request.data
        email = str(d.get("email", "")).strip().lower()
        name = str(d.get("full_name", "")).strip()[:150]
        password = str(d.get("password", ""))
        role = d.get("role", CASHIER)
        if not email or "@" not in email or not name or len(password) < 8 or role not in (MANAGER, CASHIER):
            return _err("invalid_input")
        if User.objects.filter(email__iexact=email).exists():
            return _err("email_taken", 409)
        user = User.objects.create_user(email=email, password=password, full_name=name,
                                        phone=str(d.get("phone", "")).strip()[:30], approval_status="approved")
        m = StaffMember.objects.create(user=user, business=b, role=role, permissions=_clean_perms(d.get("permissions"), role))
        sent = _send_staff_code(user)
        data = StaffSerializer(m).data
        data["code_sent"] = sent
        return Response(data, status=201)


class StaffDetailView(BusinessMixin, APIView):
    read_perm = ("users",)

    def get_obj(self, request, pk):
        return StaffMember.objects.select_related("user").filter(user_id=pk, business=self.biz(request)).first()

    @transaction.atomic
    def patch(self, request, pk):
        m = self.get_obj(request, pk)
        if not m:
            return _err("not_found", 404)
        d = request.data
        if "role" in d:
            if d["role"] not in (MANAGER, CASHIER):
                return _err("invalid_input")
            m.role = d["role"]
        if "permissions" in d:
            m.permissions = _clean_perms(d["permissions"], m.role) if d["permissions"] else []
        m.save()
        if "is_active" in d:
            m.user.is_active = bool(d["is_active"])
            m.user.save(update_fields=["is_active"])
            if not m.user.is_active:
                revoke_all_sessions(m.user)
        if d.get("password"):
            pw = str(d["password"])
            if len(pw) < 8:
                return _err("invalid_input")
            m.user.set_password(pw)
            m.user.save()
            revoke_all_sessions(m.user)
        return Response(StaffSerializer(m).data)

    @transaction.atomic
    def delete(self, request, pk):
        m = self.get_obj(request, pk)
        if m:
            m.user.delete()   # cascades the membership and sessions
        return Response({"ok": True})


class StaffVerifyView(BusinessMixin, APIView):
    """Step 'verify': the code that was emailed to the staff member's address."""
    read_perm = ("users",)

    def post(self, request, pk):
        m = StaffMember.objects.select_related("user").filter(user_id=pk, business=self.biz(request)).first()
        if not m:
            return _err("not_found", 404)
        u = m.user
        if u.email_verified:
            return Response(StaffSerializer(m).data)
        if request.data.get("resend"):
            last = EmailOTP.objects.filter(user=u, purpose="verify").order_by("-created_at").first()
            if last and timezone.now() - last.created_at < timedelta(minutes=1):
                return _err("cooldown", 429)
            return Response({"code_sent": _send_staff_code(u)})
        row = EmailOTP.objects.filter(user=u, purpose="verify", consumed_at__isnull=True,
                                      expires_at__gt=timezone.now()).order_by("-created_at").first()
        if not row:
            return _err("invalid_or_expired")
        if not verify_otp(row, str(request.data.get("otp", "")).strip()):
            return _err("too_many_attempts" if row.attempts >= OTP_MAX_ATTEMPTS else "invalid_or_expired")
        row.consumed_at = timezone.now()
        row.save(update_fields=["consumed_at"])
        u.email_verified = True
        u.save(update_fields=["email_verified", "updated_at"])
        return Response(StaffSerializer(m).data)

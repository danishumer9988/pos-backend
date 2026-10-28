from datetime import timedelta
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone
from django.contrib.auth.hashers import make_password, check_password
from django.db.models import Sum, Count, F
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from .models import *
from .serializers import *
from .authentication import create_session, revoke_session, revoke_all_sessions
from .permissions import IsVerifiedUser, HasPerm
from .tenant import business_of, role_of, perms_of, access_state
from .notify import notify_admin_new_signup
from rest_framework.throttling import ScopedRateThrottle
from .utils import create_otp, verify_otp, OTP_MAX_ATTEMPTS

def public_user(user):
    return UserSerializer(user).data

def business_for(user):
    b = business_of(user)
    if not b:
        return None
    data = dict(BusinessSerializer(b).data)
    data["role"] = role_of(user)
    data["permissions"] = perms_of(user)
    return data


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

class SignupView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"
    def post(self, request):
        email = str(request.data.get("email","")).strip().lower()
        full_name = str(request.data.get("full_name","")).strip()
        password = str(request.data.get("password",""))
        if not email or not full_name or len(password) < 8:
            return Response({"error":"invalid_input"}, status=400)
        if User.objects.filter(email__iexact=email).exists():
            return Response({"error":"email_taken"}, status=409)
        if password != str(request.data.get("confirm_password", password)):
            return Response({"error":"password_mismatch"}, status=400)
        user = User.objects.create_user(email=email, password=password, full_name=full_name)
        try:
            create_otp(user, "verify")
        except Exception:
            user.delete()
            return Response({"error":"email_delivery_failed"}, status=502)
        return Response({"ok":True,"email":email,"requires_verification":True}, status=201)

class ResendOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = str(request.data.get("email","")).strip().lower()
        user = User.objects.filter(email__iexact=email).first()
        if not user or user.email_verified:
            return Response({"ok":True})
        recent = EmailOTP.objects.filter(user=user, purpose="verify",
                    created_at__gt=timezone.now()-timedelta(hours=1)).count()
        if recent >= 5:
            return Response({"error":"rate_limited"}, status=429)
        last = EmailOTP.objects.filter(user=user,purpose="verify").order_by("-created_at").first()
        if last and timezone.now()-last.created_at < timedelta(minutes=1):
            return Response({"error":"cooldown"}, status=429)
        try:
            create_otp(user,"verify")
        except Exception:
            return Response({"error":"email_delivery_failed"}, status=502)
        return Response({"ok":True})

class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"
    def post(self, request):
        email = str(request.data.get("email","")).strip().lower()
        otp = str(request.data.get("otp","")).strip()
        user = User.objects.filter(email__iexact=email).first()
        if not user or user.email_verified:
            return Response({"error":"invalid_or_expired"}, status=400)
        row = EmailOTP.objects.filter(user=user,purpose="verify",
            consumed_at__isnull=True,expires_at__gt=timezone.now()).order_by("-created_at").first()
        if not row:
            return Response({"error":"invalid_or_expired"}, status=400)
        ok = verify_otp(row,otp)
        if not ok:
            if row.attempts >= OTP_MAX_ATTEMPTS:
                return Response({"error":"too_many_attempts"},status=400)
            return Response({"error":"invalid_or_expired"},status=400)
        row.consumed_at=timezone.now(); row.save(update_fields=["consumed_at"])
        user.email_verified=True; user.save(update_fields=["email_verified","updated_at"])
        state=access_state(user)
        if state!="ok":
            if state=="pending":notify_admin_new_signup(user)
            return Response({"status":f"{'pending_approval' if state=='pending' else state}","user":public_user(user)})
        return Response({"token":create_session(user,request),"user":public_user(user),"business":business_for(user)})

class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"
    def post(self, request):
        email = str(request.data.get("email","")).strip().lower()
        password = str(request.data.get("password",""))
        user = User.objects.filter(email__iexact=email).first()
        if not user or not user.is_active or not user.check_password(password):
            return Response({"error":"invalid_credentials"},status=401)
        if not user.email_verified:
            return Response({"error":"email_unverified"},status=403)
        state=access_state(user)
        if state!="ok":
            return Response({"error":{"pending":"pending_approval","rejected":"rejected","expired":"access_expired"}[state]},status=403)
        return Response({"token":create_session(user,request),"user":public_user(user),"business":business_for(user)})

class LogoutView(APIView):
    permission_classes=[IsAuthenticated]
    def post(self,request):
        if request.auth:
            request.auth.revoked_at=timezone.now()
            request.auth.save(update_fields=["revoked_at"])
        return Response({"ok":True})

class MeView(APIView):
    permission_classes=[IsAuthenticated]
    def get(self,request):
        return Response({"user":public_user(request.user),"business":business_for(request.user)})

class ForgotPasswordView(APIView):
    permission_classes=[AllowAny]
    def post(self,request):
        email=str(request.data.get("email","")).strip().lower()
        user=User.objects.filter(email__iexact=email).first()
        if not user:
            return Response({"ok":True})
        recent=EmailOTP.objects.filter(user=user,purpose="reset",
            created_at__gt=timezone.now()-timedelta(hours=1)).count()
        if recent>=3: return Response({"error":"rate_limited"},status=429)
        last=EmailOTP.objects.filter(user=user,purpose="reset").order_by("-created_at").first()
        if last and timezone.now()-last.created_at<timedelta(minutes=1):
            return Response({"error":"cooldown"},status=429)
        try: create_otp(user,"reset")
        except Exception: return Response({"error":"email_delivery_failed"},status=502)
        return Response({"ok":True})

class ResetPasswordView(APIView):
    permission_classes=[AllowAny]
    def post(self,request):
        email=str(request.data.get("email","")).strip().lower()
        otp=str(request.data.get("otp","")).strip()
        new_password=str(request.data.get("new_password",""))
        if len(new_password)<8: return Response({"error":"invalid_input"},status=400)
        user=User.objects.filter(email__iexact=email).first()
        if not user: return Response({"error":"invalid_or_expired"},status=400)
        row=EmailOTP.objects.filter(user=user,purpose="reset",
            consumed_at__isnull=True,expires_at__gt=timezone.now()).order_by("-created_at").first()
        if not row: return Response({"error":"invalid_or_expired"},status=400)
        if not verify_otp(row,otp):
            return Response({"error":"too_many_attempts" if row.attempts>=OTP_MAX_ATTEMPTS else "invalid_or_expired"},status=400)
        user.set_password(new_password); user.email_verified=True; user.save()
        row.consumed_at=timezone.now(); row.save(update_fields=["consumed_at"])
        revoke_all_sessions(user)
        return Response({"ok":True})

class BusinessCategoriesView(APIView):
    permission_classes=[AllowAny]
    def get(self,request):
        return Response({
            "super_categories":SuperCategorySerializer(SuperCategory.objects.filter(is_active=True).order_by("label"),many=True).data,
            "business_types":BusinessTypeSerializer(BusinessType.objects.filter(is_active=True).order_by("label"),many=True).data,
        })

class OnboardingView(APIView):
    permission_classes=[IsVerifiedUser]
    def post(self,request):
        if business_of(request.user):
            return Response({"error":"business_exists"},status=409)
        sc=SuperCategory.objects.filter(id=request.data.get("super_category_id"),is_active=True).first()
        bt=BusinessType.objects.filter(id=request.data.get("business_type_id"),
            super_category=sc,is_active=True).first() if sc else None
        if not sc: return Response({"error":"invalid_super_category"},status=400)
        if not bt: return Response({"error":"invalid_business_type"},status=400)
        name=str(request.data.get("name","")).strip()
        if not name: return Response({"error":"invalid_input"},status=400)
        b=Business.objects.create(
            owner=request.user,super_category=sc,business_type=bt,name=name,
            country=str(request.data.get("country","PK")).upper()[:2],
            currency=str(request.data.get("currency","PKR")).upper()[:3],
            phone=request.data.get("phone") or None,email=request.data.get("email") or None,
            address=request.data.get("address") or None,onboarding_completed_at=timezone.now()
        )
        return Response(BusinessSerializer(b).data,status=201)

class CategoryListCreateView(APIView):
    permission_classes=[HasPerm]
    read_perm = ("checkout", "inventory")
    write_perm = ("inventory",)
    def get_business(self,request):
        return business_of(request.user)
    def get(self,request):
        b=self.get_business(request)
        if not b:return Response({"error":"no_business"},status=404)
        return Response(ProductCategorySerializer(b.categories.order_by("name"),many=True).data)
    def post(self,request):
        b=self.get_business(request)
        if not b:return Response({"error":"no_business"},status=404)
        name=str(request.data.get("name","")).strip()
        if not 1<=len(name)<=50:return Response({"error":"invalid_input"},status=400)
        if ProductCategory.objects.filter(business=b,name__iexact=name).exists():
            return Response({"error":"duplicate_name"},status=409)
        c=ProductCategory.objects.create(business=b,name=name)
        return Response(ProductCategorySerializer(c).data,status=201)

class CategoryDetailView(APIView):
    permission_classes=[HasPerm]
    read_perm = ("checkout", "inventory")
    write_perm = ("inventory",)
    def get_obj(self,request,pk):
        return ProductCategory.objects.filter(id=pk,business=business_of(request.user)).first()
    def patch(self,request,pk):
        c=self.get_obj(request,pk)
        if not c:return Response({"error":"not_found"},status=404)
        name=str(request.data.get("name","")).strip()
        if not 1<=len(name)<=50:return Response({"error":"invalid_input"},status=400)
        if ProductCategory.objects.filter(business=c.business,name__iexact=name).exclude(id=c.id).exists():
            return Response({"error":"duplicate_name"},status=409)
        c.name=name;c.save(update_fields=["name"])
        return Response(ProductCategorySerializer(c).data)
    def delete(self,request,pk):
        c=self.get_obj(request,pk)
        if c:c.delete()
        return Response({"ok":True})

class ProductListCreateView(APIView):
    permission_classes=[HasPerm]
    read_perm = ("checkout", "inventory")
    write_perm = ("inventory",)
    def get_business(self,request): return business_of(request.user)
    def get(self,request):
        b=self.get_business(request)
        if not b:return Response({"error":"no_business"},status=404)
        qs=b.products.select_related("category").order_by("-created_at")
        return Response(ProductSerializer(qs,many=True).data)
    def post(self,request):
        b=self.get_business(request)
        if not b:return Response({"error":"no_business"},status=404)
        name=str(request.data.get("name","")).strip()
        try: price=int(request.data.get("price_cents"))
        except (TypeError,ValueError): price=-1
        if not name or len(name)>100 or price<0:return Response({"error":"invalid_input"},status=400)
        cat=None
        if request.data.get("category_id"):
            cat=ProductCategory.objects.filter(id=request.data["category_id"],business=b).first()
            if not cat:return Response({"error":"invalid_category"},status=400)
        track=bool(request.data.get("track_stock",False))
        qty=to_int(request.data.get("stock_quantity",0),0) if track else 0
        thr=to_int(request.data.get("low_stock_threshold",5),5)
        if qty<0 or thr<0:return Response({"error":"invalid_input"},status=400)
        cost=to_int(request.data.get("cost_cents",0),0)
        if cost<0:return Response({"error":"invalid_input"},status=400)
        p=Product.objects.create(business=b,name=name,price_cents=price,category=cat,
            cost_cents=cost,barcode=str(request.data.get("barcode","")).strip()[:60],
            unit=(str(request.data.get("unit","pcs")).strip() or "pcs")[:20],
            description=str(request.data.get("description","")).strip()[:300],
            sku=str(request.data.get("sku","")).strip()[:40],track_stock=track,
            stock_quantity=qty,low_stock_threshold=thr,
            is_active=bool(request.data.get("is_active",True)))
        if track and qty:
            StockMovement.objects.create(business=b,product=p,change=qty,balance_after=qty,
                reason="initial stock",user=request.user)
        return Response(ProductSerializer(p).data,status=201)

class ProductDetailView(APIView):
    permission_classes=[HasPerm]
    read_perm = ("checkout", "inventory")
    write_perm = ("inventory",)
    def get_obj(self,request,pk):
        return Product.objects.select_related("category").filter(id=pk,business=business_of(request.user)).first()
    def patch(self,request,pk):
        p=self.get_obj(request,pk)
        if not p:return Response({"error":"not_found"},status=404)
        if "name" in request.data:
            name=str(request.data["name"]).strip()
            if not name:return Response({"error":"invalid_input"},status=400)
            p.name=name
        if "price_cents" in request.data:
            try: p.price_cents=int(request.data["price_cents"])
            except (TypeError,ValueError): return Response({"error":"invalid_input"},status=400)
            if p.price_cents<0: return Response({"error":"invalid_input"},status=400)
        if "is_active" in request.data:p.is_active=bool(request.data["is_active"])
        if "sku" in request.data:p.sku=str(request.data["sku"]).strip()[:40]
        if "barcode" in request.data:p.barcode=str(request.data["barcode"]).strip()[:60]
        if "unit" in request.data:p.unit=(str(request.data["unit"]).strip() or "pcs")[:20]
        if "description" in request.data:p.description=str(request.data["description"]).strip()[:300]
        if "cost_cents" in request.data:
            c=to_int(request.data["cost_cents"])
            if c is None or c<0:return Response({"error":"invalid_input"},status=400)
            p.cost_cents=c
        if "track_stock" in request.data:p.track_stock=bool(request.data["track_stock"])
        if "low_stock_threshold" in request.data:
            thr=to_int(request.data["low_stock_threshold"])
            if thr is None or thr<0:return Response({"error":"invalid_input"},status=400)
            p.low_stock_threshold=thr
        if "category_id" in request.data:
            cid=request.data["category_id"]
            p.category=ProductCategory.objects.filter(id=cid,business=p.business).first() if cid else None
        p.save()
        return Response(ProductSerializer(p).data)
    def delete(self,request,pk):
        p=self.get_obj(request,pk)
        if p:
            try: p.delete()
            except ProtectedError:
                return Response({"error":"protected"},status=409)
        return Response({"ok":True})

class DashboardView(APIView):
    permission_classes=[HasPerm]
    read_perm = ("dashboard",)
    def get(self,request):
        from .views_sales import sales_report
        from .serializers import OrderSerializer
        b=business_of(request.user)
        if not b:return Response({"error":"no_business"},status=404)
        paid=b.orders.filter(status="paid")
        revenue=paid.aggregate(total=Sum("total_cents"))["total"] or 0
        report=sales_report(b,7)
        recent=b.orders.select_related("customer").prefetch_related("items__product","payments").order_by("-created_at")[:6]
        return Response({
            "business":BusinessSerializer(b).data,
            "total_revenue_cents":revenue,
            "total_orders":paid.count(),
            "pending_orders":b.orders.filter(status="open").count(),
            "available_products":b.products.filter(is_active=True).count(),
            "low_stock_count":b.products.filter(is_active=True,track_stock=True,
                stock_quantity__lte=F("low_stock_threshold")).count(),
            "week":report,
            "recent_orders":OrderSerializer(recent,many=True).data,
        })


class DemoRequestView(APIView):
    """Landing-page form. Public, so it is rate-limited."""
    permission_classes=[AllowAny]
    authentication_classes=[]
    throttle_classes=[ScopedRateThrottle]
    throttle_scope="demo"
    def post(self,request):
        name=str(request.data.get("name","")).strip()[:120]
        phone=str(request.data.get("phone","")).strip()[:40]
        if not name or not phone:return Response({"error":"invalid_input"},status=400)
        DemoRequest.objects.create(name=name,phone=phone,
            business_type=str(request.data.get("business_type","")).strip()[:120])
        return Response({"ok":True},status=201)

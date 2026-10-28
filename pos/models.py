import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

class UserManager(BaseUserManager):
    def create_user(self, email, password=None, full_name="", **extra):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email).lower(),
                          full_name=full_name, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, full_name="Admin", **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("approval_status", "approved")
        extra.setdefault("email_verified", True)
        extra.setdefault("email_verified", True)
        return self.create_user(email, password, full_name, **extra)

class User(AbstractBaseUser, PermissionsMixin):
    APPROVAL_CHOICES = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150)
    email_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    phone = models.CharField(max_length=30, blank=True, default="")
    approval_status = models.CharField(max_length=10, choices=APPROVAL_CHOICES, default="pending")
    access_days = models.PositiveIntegerField(default=10, help_text="Days of access to grant when approving")
    access_expires_at = models.DateTimeField(blank=True, null=True, help_text="Empty = no expiry")
    approved_at = models.DateTimeField(blank=True, null=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]
    objects = UserManager()

    def __str__(self):
        return self.email

class EmailOTP(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="otps")
    otp_hash = models.CharField(max_length=255)
    purpose = models.CharField(max_length=20)
    expires_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class SessionToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    token_hash = models.CharField(max_length=255, unique=True)
    user_agent = models.TextField(blank=True, null=True)
    ip = models.GenericIPAddressField(blank=True, null=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class SuperCategory(models.Model):
    id = models.CharField(max_length=80, primary_key=True)
    label = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

class BusinessType(models.Model):
    id = models.CharField(max_length=80, primary_key=True)
    super_category = models.ForeignKey(SuperCategory, on_delete=models.PROTECT, related_name="business_types")
    label = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

class Business(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.OneToOneField(User, on_delete=models.CASCADE, related_name="business")
    super_category = models.ForeignKey(SuperCategory, on_delete=models.PROTECT)
    business_type = models.ForeignKey(BusinessType, on_delete=models.PROTECT)
    name = models.CharField(max_length=150)
    country = models.CharField(max_length=2, default="PK")
    currency = models.CharField(max_length=3, default="PKR")
    phone = models.CharField(max_length=30, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    address = models.CharField(max_length=500, blank=True, null=True)
    tax_rate_bps = models.PositiveIntegerField(default=0)  # basis points: 1700 = 17%
    receipt_footer = models.CharField(max_length=200, blank=True, default="Thank you for your purchase!")
    onboarding_completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class ProductCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="uniq_category_name_per_business"),
        ]

class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="products")
    category = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="products")
    name = models.CharField(max_length=100)
    price_cents = models.PositiveIntegerField()
    sku = models.CharField(max_length=40, blank=True, default="")
    barcode = models.CharField(max_length=60, blank=True, default="")
    unit = models.CharField(max_length=20, default="pcs")
    description = models.CharField(max_length=300, blank=True, default="")
    cost_cents = models.PositiveIntegerField(default=0)
    track_stock = models.BooleanField(default=False)
    stock_quantity = models.IntegerField(default=0)
    low_stock_threshold = models.PositiveIntegerField(default=5)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_low_stock(self):
        return self.track_stock and self.stock_quantity <= self.low_stock_threshold


class StaffMember(models.Model):
    ROLE_CHOICES = [("manager", "Manager"), ("cashier", "Cashier")]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="membership")
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="staff")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="cashier")
    permissions = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class DemoRequest(models.Model):
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=40)
    business_type = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class Customer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="customers")
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    address = models.CharField(max_length=300, blank=True, default="")
    notes = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class DiningTable(models.Model):
    SHAPES = [("rect", "Rectangle"), ("round", "Round")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="tables")
    name = models.CharField(max_length=40)
    section = models.CharField(max_length=40, default="Dining room")
    seats = models.PositiveIntegerField(default=4)
    shape = models.CharField(max_length=10, choices=SHAPES, default="rect")
    x = models.IntegerField(default=0)   # position on the floor canvas (px)
    y = models.IntegerField(default=0)
    w = models.PositiveIntegerField(default=96)
    h = models.PositiveIntegerField(default=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "name"], name="uniq_table_name_per_business")]


class Order(models.Model):
    STATUS_CHOICES = [("open","Open"),("paid","Paid"),("cancelled","Cancelled"),("refunded","Refunded")]
    TYPE_CHOICES = [("dine_in","Dine-in"),("takeaway","Takeaway"),("delivery","Delivery")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="orders")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    order_number = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    order_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default="takeaway")
    table = models.ForeignKey(DiningTable, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    table_name = models.CharField(max_length=40, blank=True, default="")  # snapshot, survives table deletion
    customer_phone = models.CharField(max_length=30, blank=True, default="")
    delivery_address = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders_created")
    subtotal_cents = models.PositiveIntegerField(default=0)
    discount_cents = models.PositiveIntegerField(default=0)
    tax_cents = models.PositiveIntegerField(default=0)
    total_cents = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "order_number"], name="uniq_order_number_per_business"),
        ]

class OrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    unit_price_cents = models.PositiveIntegerField()
    unit_cost_cents = models.PositiveIntegerField(default=0)  # snapshot, so later cost edits don't rewrite past profit
    notes = models.TextField(blank=True)

class Payment(models.Model):
    METHOD_CHOICES = [("cash","Cash"),("card","Card"),("other","Other")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    amount_cents = models.PositiveIntegerField()
    tendered_cents = models.PositiveIntegerField(default=0)   # cash handed over
    created_at = models.DateTimeField(auto_now_add=True)


class StockMovement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="stock_movements")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="movements")
    change = models.IntegerField()
    balance_after = models.IntegerField()
    reason = models.CharField(max_length=60)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Expense(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="expenses")
    title = models.CharField(max_length=120)
    category = models.CharField(max_length=60, default="General")
    amount_cents = models.PositiveIntegerField()
    spent_on = models.DateField()
    note = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

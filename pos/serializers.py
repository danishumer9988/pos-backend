from rest_framework import serializers
from .models import User, Business, SuperCategory, BusinessType, ProductCategory, Product, Customer, Order, OrderItem, Payment, StockMovement, StaffMember, DiningTable, Expense

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id","email","full_name","email_verified"]

class BusinessSerializer(serializers.ModelSerializer):
    super_category_label = serializers.CharField(source="super_category.label", read_only=True)
    business_type_label = serializers.CharField(source="business_type.label", read_only=True)
    class Meta:
        model = Business
        fields = [
            "id","name","super_category","business_type","country","currency",
            "phone","email","address","onboarding_completed_at","created_at",
            "super_category_label","business_type_label","tax_rate_bps","receipt_footer"
        ]

class SuperCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SuperCategory
        fields = ["id","label"]

class BusinessTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessType
        fields = ["id","label","super_category"]

class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ["id","name","created_at"]

class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default=None, allow_null=True)
    category_id = serializers.UUIDField(read_only=True, allow_null=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    class Meta:
        model = Product
        fields = ["id","name","price_cents","is_active","created_at","category","category_name","category_id",
                  "sku","barcode","unit","description","cost_cents","track_stock","stock_quantity",
                  "low_stock_threshold","is_low_stock"]


class CustomerSerializer(serializers.ModelSerializer):
    order_count = serializers.IntegerField(read_only=True, default=0)
    total_spent = serializers.IntegerField(read_only=True, default=0, allow_null=True)
    last_order_at = serializers.DateTimeField(read_only=True, default=None, allow_null=True)

    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "email", "address", "notes", "created_at",
                  "order_count", "total_spent", "last_order_at"]


class OrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "product", "product_name", "quantity", "unit_price_cents"]


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ["id", "method", "amount_cents", "tendered_cents", "created_at"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None, allow_null=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default=None, allow_null=True)
    payment_method = serializers.SerializerMethodField()
    tendered_cents = serializers.SerializerMethodField()
    change_cents = serializers.SerializerMethodField()

    def _pay(self, o):
        pays = list(o.payments.all())
        return pays[0] if pays else None

    def get_payment_method(self, o):
        p = self._pay(o)
        return p.method if p else None

    def get_tendered_cents(self, o):
        p = self._pay(o)
        return p.tendered_cents if p and p.method == "cash" else 0

    def get_change_cents(self, o):
        p = self._pay(o)
        return max(0, p.tendered_cents - p.amount_cents) if p and p.method == "cash" else 0

    class Meta:
        model = Order
        fields = ["id", "order_number", "status", "order_type", "table", "table_name",
                  "customer_phone", "delivery_address", "subtotal_cents", "discount_cents", "tax_cents",
                  "total_cents", "created_at", "customer", "customer_name", "created_by_name",
                  "payment_method", "tendered_cents", "change_cents", "items", "payments"]


class StockMovementSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    user_name = serializers.CharField(source="user.full_name", read_only=True, default=None, allow_null=True)

    class Meta:
        model = StockMovement
        fields = ["id", "product", "product_name", "change", "balance_after", "reason", "user_name", "created_at"]


class StaffSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="user.id", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    email_verified = serializers.BooleanField(source="user.email_verified", read_only=True)

    class Meta:
        model = StaffMember
        fields = ["id", "full_name", "email", "phone", "role", "permissions", "is_active",
                  "email_verified", "created_at"]


class DiningTableSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiningTable
        fields = ["id", "name", "section", "seats", "shape", "x", "y", "w", "h"]


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = ["id", "title", "category", "amount_cents", "spent_on", "note", "created_at"]

from django.db import migrations


def approve_existing(apps, schema_editor):
    """Accounts that existed before the approval feature keep working (no expiry)."""
    User = apps.get_model("pos", "User")
    User.objects.filter(approval_status="pending").update(approval_status="approved")


class Migration(migrations.Migration):
    dependencies = [("pos", "0004_features_v2")]
    operations = [migrations.RunPython(approve_existing, migrations.RunPython.noop)]

"""Every organization needs an admin. Existing superuser members become admins; an organization
with no superuser member gets its earliest member promoted."""
from django.db import migrations


def assign_admins(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    Membership = apps.get_model("core", "Membership")
    Membership.objects.filter(user__is_superuser=True).update(role="admin")
    for org in Organization.objects.all():
        members = Membership.objects.filter(organization=org)
        if members.exists() and not members.filter(role="admin").exists():
            first = members.order_by("id").first()
            first.role = "admin"
            first.save(update_fields=["role"])


class Migration(migrations.Migration):
    dependencies = [("core", "0002_enterprise_controls")]
    operations = [migrations.RunPython(assign_admins, migrations.RunPython.noop)]

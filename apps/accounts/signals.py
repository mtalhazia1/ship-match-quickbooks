from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.core.utils import audit

from .models import Profile
from .services import lockout


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.get_or_create(user=instance)


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def clear_lockout_on_new_password(sender, instance, **kwargs):
    """A new password (reset link, change, admin) lifts a sign-in lockout: the locked-out message tells people to
    reset their password, and the old guesses say nothing about the new one."""
    if instance.pk and getattr(instance, "_password", None) is not None:
        lockout.reset(instance.get_username(), None)


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    audit(None, "auth.login", user, actor=user)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        audit(None, "auth.logout", user, actor=user)

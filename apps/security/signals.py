"""Record account security changes as `AccountSecurityEvent` rows.

pre_save compares the incoming User / UserProfile with the stored row and
notes which watched fields changed; post_save writes one event per change.
New accounts are not recorded (nothing changed). Saves that only touch other
fields (e.g. `last_login`, which updates on every login) skip the lookup.
"""

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.accounts.models import UserProfile

from .models import AccountSecurityEvent

K = AccountSecurityEvent.Kind

USER_FIELDS = {"password": K.PASSWORD_CHANGED, "email": K.EMAIL_CHANGED}
PROFILE_FIELDS = {
    "phone_number": K.PHONE_CHANGED,
    "billing_address": K.BILLING_ADDRESS_CHANGED,
    "delivery_address": K.DELIVERY_ADDRESS_CHANGED,
}


def _normalise(field, value):
    value = value or ""
    if field == "password":
        return value  # hash: any difference is a change
    if field == "email":
        return value.strip().lower()
    return value.strip()


def _changed_kinds(sender, instance, update_fields, watched: dict) -> list:
    if instance.pk is None:
        return []
    fields = set(watched)
    if update_fields is not None:
        fields &= set(update_fields)
        if not fields:
            return []
    stored = sender.objects.filter(pk=instance.pk).values(*fields).first()
    if stored is None:
        return []
    return [
        watched[f] for f in sorted(fields)
        if _normalise(f, stored[f]) != _normalise(f, getattr(instance, f))
    ]


@receiver(pre_save, sender=get_user_model(), dispatch_uid="security_user_pre_save")
def _user_pre_save(sender, instance, update_fields=None, **kwargs):
    instance._security_changes = _changed_kinds(sender, instance, update_fields, USER_FIELDS)


@receiver(pre_save, sender=UserProfile, dispatch_uid="security_profile_pre_save")
def _profile_pre_save(sender, instance, update_fields=None, **kwargs):
    instance._security_changes = _changed_kinds(sender, instance, update_fields, PROFILE_FIELDS)


def _record(user_id, instance):
    kinds = getattr(instance, "_security_changes", None) or []
    instance._security_changes = []
    AccountSecurityEvent.objects.bulk_create([AccountSecurityEvent(user_id=user_id, kind=k) for k in kinds])


@receiver(post_save, sender=get_user_model(), dispatch_uid="security_user_post_save")
def _user_post_save(sender, instance, created=False, **kwargs):
    if not created:
        _record(instance.pk, instance)


@receiver(post_save, sender=UserProfile, dispatch_uid="security_profile_post_save")
def _profile_post_save(sender, instance, created=False, **kwargs):
    if not created:
        _record(instance.user_id, instance)

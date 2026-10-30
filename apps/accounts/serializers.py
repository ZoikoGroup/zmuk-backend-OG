import re

from rest_framework import serializers
from django.contrib.auth.models import User
from django.db import transaction
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.contrib.auth import authenticate


# ---------------- REGISTER ----------------

class RegisterSerializer(serializers.Serializer):
    """Plain Serializer (not ModelSerializer) so DRF does NOT auto-inject a
    UniqueValidator on `username` from the model's unique=True constraint.
    That auto-validator fires before validate() and blocks re-registration of
    inactive (unverified) users with a generic Django error message.
    We handle all uniqueness checks ourselves in validate()."""

    email    = serializers.EmailField()
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)
    first_name = serializers.CharField(required=False, allow_blank=True, default="")
    last_name  = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        attrs["email"]    = attrs["email"].strip().lower()
        attrs["username"] = attrs["username"].strip().lower()

        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})

        # Hard block: ACTIVE user already registered with this email or username.
        if User.objects.filter(email__iexact=attrs["email"], is_active=True).exists():
            raise serializers.ValidationError({"email": "This email is already registered. Please log in or reset your password."})

        if User.objects.filter(username__iexact=attrs["username"], is_active=True).exists():
            raise serializers.ValidationError({"username": "This username is already taken."})

        # Soft path: INACTIVE (unverified) user — update their record and
        # re-send the verification email instead of blocking them.
        inactive = User.objects.filter(email__iexact=attrs["email"], is_active=False).first()
        if inactive:
            try:
                validate_password(attrs["password"], user=inactive)
            except ValidationError as e:
                raise serializers.ValidationError({"password": list(e.messages)})
            inactive.set_password(attrs["password"])
            inactive.first_name = attrs.get("first_name", "")
            inactive.last_name  = attrs.get("last_name", "")
            inactive.save()
            attrs["_existing_inactive_user"] = inactive

        return attrs

    def create(self, validated_data):
        # If an inactive user exists, return them so the view re-sends
        # their verification email with a fresh token.
        existing = validated_data.pop("_existing_inactive_user", None)
        if existing:
            return existing

        validated_data.pop("password2", None)
        is_active = validated_data.pop("is_active", False)

        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
        )
        user.is_active = is_active
        user.save()
        return user


# ---------------- LOGIN ----------------

class LoginSerializer(serializers.Serializer):
    email    = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        email = data["email"].strip().lower()

        try:
            user_obj = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            raise serializers.ValidationError("Invalid email or password.")

        if not user_obj.check_password(data["password"]):
            raise serializers.ValidationError("Invalid email or password.")

        if not user_obj.is_active:
            raise serializers.ValidationError(
                "Please verify your email before logging in. "
                "Check your inbox or register again to receive a new link."
            )

        return user_obj


# ---------------- FORGOT PASSWORD ----------------

class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


# ---------------- RESET PASSWORD ----------------

class ResetPasswordSerializer(serializers.Serializer):
    password  = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs


# ---------------- UPDATE PROFILE ----------------

UK_MOBILE_RE = re.compile(r"^07\d{9}$")
PREFERRED_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ .'\-]+[^\W\d_]+)*\.?$")


def normalise_uk_mobile(value):
    """Return a UK mobile as '07123 456789', or None if it isn't one.
    Accepts 07..., +447..., 00447... and 447..., with spaces/dashes/brackets."""
    digits = re.sub(r"[\s\-().]", "", value)
    if digits.startswith("+44"):
        digits = "0" + digits[3:]
    elif digits.startswith("0044"):
        digits = "0" + digits[4:]
    elif digits.startswith("44") and len(digits) == 12:
        digits = "0" + digits[2:]
    if not UK_MOBILE_RE.match(digits):
        return None
    return f"{digits[:5]} {digits[5:]}"


class ProfilePreferencesSerializer(serializers.Serializer):
    """Whitelisted keys for UserProfile.preferences. Unknown keys are rejected
    so arbitrary data can't be stored in the JSON column."""

    CONTACT_METHODS = ["email", "sms", "phone", "post"]

    contact_method = serializers.ChoiceField(choices=CONTACT_METHODS, required=False)
    accessibility  = serializers.CharField(max_length=100, required=False, allow_blank=True, trim_whitespace=True)
    support_needs  = serializers.CharField(max_length=500, required=False, allow_blank=True, trim_whitespace=True)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object.")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {key: "Unknown preference." for key in sorted(unknown)}
            )
        # Frontend labels are capitalised ("Email", "SMS").
        if isinstance(data.get("contact_method"), str):
            data = {**data, "contact_method": data["contact_method"].strip().lower()}
        return super().to_internal_value(data)

    def to_representation(self, instance):
        # Always return every key so the frontend never has to guess.
        stored = instance or {}
        return {name: stored.get(name, "") for name in self.fields}


class UpdateUserSerializer(serializers.ModelSerializer):
    bq_enrollment_id = serializers.CharField(
        source="profile.bq_enrollment_id",
        required=False,
        allow_blank=True,
    )
    phone_number = serializers.CharField(
        source="profile.phone_number",
        required=False,
        allow_blank=True,
        max_length=30,
    )
    preferred_name = serializers.CharField(
        source="profile.preferred_name",
        required=False,
        allow_blank=True,
        max_length=50,
    )
    billing_address = serializers.CharField(
        source="profile.billing_address",
        required=False,
        allow_blank=True,
        max_length=300,
    )
    # Blank means "same as billing".
    delivery_address = serializers.CharField(
        source="profile.delivery_address",
        required=False,
        allow_blank=True,
        max_length=300,
    )
    # Partial: only the keys sent are changed, the rest are kept.
    preferences = ProfilePreferencesSerializer(
        source="profile.preferences",
        required=False,
        partial=True,
    )

    class Meta:
        model  = User
        fields = [
            "username", "email", "first_name", "last_name", "bq_enrollment_id",
            "preferred_name", "phone_number", "billing_address", "delivery_address",
            "preferences",
        ]

    def validate_email(self, value):
        user = self.context["request"].user
        if User.objects.exclude(pk=user.pk).filter(email=value).exists():
            raise serializers.ValidationError("Email already in use.")
        return value

    def validate_phone_number(self, value):
        if not value:
            return ""
        formatted = normalise_uk_mobile(value)
        if formatted is None:
            raise serializers.ValidationError("Enter a valid UK mobile number.")
        return formatted

    def validate_preferred_name(self, value):
        value = " ".join(value.split())
        if value and not PREFERRED_NAME_RE.match(value):
            raise serializers.ValidationError(
                "Use letters only (spaces, hyphens and apostrophes are allowed)."
            )
        return value

    def _validate_address(self, value):
        value = value.strip()
        if value and len(value) < 5:
            raise serializers.ValidationError("Address must be at least 5 characters.")
        return value

    def validate_billing_address(self, value):
        return self._validate_address(value)

    def validate_delivery_address(self, value):
        return self._validate_address(value)

    @transaction.atomic
    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        instance = super().update(instance, validated_data)

        if profile_data:
            profile = instance.profile
            if "preferences" in profile_data:
                profile_data["preferences"] = {
                    **(profile.preferences or {}),
                    **profile_data["preferences"],
                }
            for field, value in profile_data.items():
                setattr(profile, field, value)
            profile.save(update_fields=list(profile_data))
        return instance


# ---------------- CHANGE PASSWORD ----------------

class ChangePasswordSerializer(serializers.Serializer):
    password         = serializers.CharField(write_only=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs

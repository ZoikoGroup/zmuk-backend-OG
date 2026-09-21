from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
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

class UpdateUserSerializer(serializers.ModelSerializer):
    bq_enrollment_id = serializers.CharField(
        source="profile.bq_enrollment_id",
        required=False,
        allow_blank=True,
    )

    class Meta:
        model  = User
        fields = ["username", "email", "first_name", "last_name", "bq_enrollment_id"]

    def validate_email(self, value):
        user = self.context["request"].user
        if User.objects.exclude(pk=user.pk).filter(email=value).exists():
            raise serializers.ValidationError("Email already in use.")
        return value

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        vc_id = profile_data.get("bq_enrollment_id")
        instance = super().update(instance, validated_data)
        if vc_id is not None:
            instance.profile.bq_enrollment_id = vc_id
            instance.profile.save()
        return instance


# ---------------- CHANGE PASSWORD ----------------

class ChangePasswordSerializer(serializers.Serializer):
    password         = serializers.CharField(write_only=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs

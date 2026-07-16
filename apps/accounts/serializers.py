from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate


# ---------------- REGISTER ----------------
class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ("email", "username", "password", "password2", "first_name", "last_name")

    def validate(self, attrs):
        # Normalize case so "Login" always matches what was stored at registration.
        # (User.objects.create_user() lowercases the email *domain* on its own, so
        # without this, the stored email can silently end up different-case from
        # what the user typed, and login by exact match then fails.)
        attrs["email"] = attrs["email"].strip().lower()
        attrs["username"] = attrs["username"].strip().lower()

        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password": "Password fields didn't match."})
        if User.objects.filter(email__iexact=attrs["email"]).exists():
            raise serializers.ValidationError({"email": "Email already registered"})
        if User.objects.filter(username__iexact=attrs["username"]).exists():
            raise serializers.ValidationError({"username": "Username already taken"})
        return attrs

    def create(self, validated_data):
        # `is_active` is injected by the view via serializer.save(is_active=False).
        # Pull it out (and password2) so it is applied correctly instead of dropped.
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
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        email = data["email"].strip().lower()

        try:
            user_obj = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            raise serializers.ValidationError("Invalid credentials")

        # Check the password directly instead of going through authenticate().
        # authenticate() silently returns None for inactive users too, which
        # made "wrong password" and "not verified yet" look identical to the
        # user. Checking them separately gives an accurate error message.
        if not user_obj.check_password(data["password"]):
            raise serializers.ValidationError("Invalid credentials")

        if not user_obj.is_active:
            raise serializers.ValidationError(
                "Please verify your email before logging in."
            )

        return user_obj


# ---------------- FORGOT PASSWORD ----------------
class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


# ---------------- RESET PASSWORD ----------------
class ResetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs


# ---------------- UPDATE PROFILE ----------------
class UpdateUserSerializer(serializers.ModelSerializer):
    bq_enrollment_id = serializers.CharField(
        source="profile.bq_enrollment_id",  # points to profile model
        required=False,
        allow_blank=True,
    )

    class Meta:
        model = User
        fields = ["username", "email", "first_name", "last_name", "bq_enrollment_id"]

    def validate_email(self, value):
        user = self.context["request"].user
        if User.objects.exclude(pk=user.pk).filter(email=value).exists():
            raise serializers.ValidationError("Email already in use")
        return value

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        vc_id = profile_data.get("bq_enrollment_id")

        # Update User fields
        instance = super().update(instance, validated_data)

        # Update VC ID if provided
        if vc_id is not None:
            instance.profile.bq_enrollment_id = vc_id
            instance.profile.save()

        return instance


class ChangePasswordSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs
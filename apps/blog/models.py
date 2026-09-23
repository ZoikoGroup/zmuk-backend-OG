from django.db import models
from django.utils.text import slugify, Truncator
from django.utils.html import strip_tags
from django_ckeditor_5.fields import CKEditor5Field
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()


class BlogPost(models.Model):
    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('published', 'Published'),
    )

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    content = CKEditor5Field()

    # SEO fields
    seo_title = models.CharField(max_length=200, blank=True, null=True)
    seo_description = models.TextField(blank=True, null=True)
    seo_keywords = models.CharField(max_length=300, blank=True, null=True)

    featured_image = models.ImageField(upload_to='blog_images/', null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Set the first time a subscriber notification goes out for this post.
    # Without this, every admin save of a published post would re-email
    # everyone. Null means "not yet notified".
    notification_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="When the new-post email was sent to subscribers.",
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'], name='blog_status_created_idx'),
        ]

    def __str__(self):
        return self.title

    # ── Slug ────────────────────────────────────────────────────────────────
    # The original version did `self.slug = slugify(self.title)` with no
    # uniqueness check. Two posts with the same title raised IntegrityError
    # and crashed the admin with a 500. This appends -2, -3, ... instead.

    def _unique_slug(self):
        base = slugify(self.title)[:200] or 'post'
        slug = base
        n = 2
        while BlogPost.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{n}"
            n += 1
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug()
        super().save(*args, **kwargs)

    # ── Derived fields ──────────────────────────────────────────────────────
    # Properties, not database columns, so adding these needs NO migration.

    @property
    def excerpt(self):
        """Short plain-text summary for listing cards.

        Uses seo_description when the author wrote one, otherwise strips the
        HTML out of the CKEditor content and truncates it.
        """
        if self.seo_description:
            return self.seo_description.strip()
        text = strip_tags(self.content or '').replace('&nbsp;', ' ')
        return Truncator(' '.join(text.split())).chars(160)

    @property
    def display_title(self):
        """SEO title when set, plain title otherwise."""
        return self.seo_title or self.title

    def get_absolute_url(self):
        return reverse('blog:blog_detail', kwargs={'slug': self.slug})
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Article(models.Model):
    """
    A news / press article shown on the 'Zoiko's Latest Tech Tea' page.

    Mapped from the live article structure:
      - dateline:  LOCATION - DATE — DATELINE_SOURCE   (e.g. "LONDON - Nov. 17, 2025 — PRLog")
      - lead paragraphs (`intro`)
      - headed sections with bullet lists  -> ArticleSection
      - leadership quotes with attribution -> ArticleQuote
      - About block (`about`)
      - Media Contact block (contact_*)
      - Source Information block (source_*)
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    # ── Card / header ─────────────────────────────────────────────────────
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True)
    category = models.CharField(max_length=50, default="NEWS")  # badge on the card
    excerpt = models.TextField(blank=True)                      # list-card preview text
    featured_image = models.ImageField(upload_to="news/", blank=True, null=True)

    # ── Dateline:  "LONDON - Nov. 17, 2025 — PRLog" ───────────────────────
    location = models.CharField(max_length=120, blank=True)         # "LONDON"
    dateline_source = models.CharField(max_length=120, blank=True)  # "PRLog"

    # ── Body ──────────────────────────────────────────────────────────────
    intro = models.TextField(blank=True)   # the lead paragraphs before the first heading
    about = models.TextField(blank=True)   # "About Zoiko Mobile UK"

    # ── Media Contact block ───────────────────────────────────────────────
    contact_company = models.CharField(max_length=255, blank=True)
    contact_website = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)

    # ── Source Information block ──────────────────────────────────────────
    source_name = models.CharField(max_length=255, blank=True)     # "Zoiko Mobile UK"
    source_city = models.CharField(max_length=120, blank=True)     # "London City"
    source_industry = models.CharField(max_length=120, blank=True) # "Telecom"
    source_tag = models.CharField(max_length=255, blank=True)      # "Full eSIM Access"

    # ── Publishing ────────────────────────────────────────────────────────
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]
        verbose_name = "Article"
        verbose_name_plural = "Articles"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)[:280]
        if self.status == self.Status.PUBLISHED and self.published_at is None:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class ArticleSection(models.Model):
    """
    A headed block within an article. Used for the bulleted sections, e.g.
      "A Faster, Modernised and Mobile-First Customer Journey"
      "Serving a Broad and Diverse UK Community"
      "Looking Ahead"

    `body` is optional intro text for the section; `bullets` is the list of points.
    """

    article = models.ForeignKey(
        Article, related_name="sections", on_delete=models.CASCADE
    )
    heading = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    bullets = models.JSONField(default=list, blank=True)  # list[str]
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.article.title} — {self.heading}"


class ArticleQuote(models.Model):
    """A leadership statement: the quote plus who said it."""

    article = models.ForeignKey(
        Article, related_name="quotes", on_delete=models.CASCADE
    )
    quote = models.TextField()
    author = models.CharField(max_length=120)       # "Marcel Jones"
    role = models.CharField(max_length=160, blank=True)  # "Director of Consumer Relations"
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.author} — {self.article.title}"

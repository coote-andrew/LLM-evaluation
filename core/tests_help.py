"""Tests for Markdown-backed help guides."""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.services.guides import get_guide, guides_by_group, load_guides

User = get_user_model()


class GuideLoaderTests(TestCase):
    def test_bundled_guides_load(self):
        articles = load_guides()
        self.assertGreaterEqual(len(articles), 3)
        slugs = {a.slug for a in articles}
        self.assertIn("getting-started", slugs)
        self.assertIn("build-labelled-spreadsheet", slugs)
        self.assertIn("choosing-evaluation-type", slugs)

    def test_readme_is_ignored(self):
        articles = load_guides()
        self.assertNotIn("README", {a.slug for a in articles})
        self.assertNotIn("readme", {a.slug.lower() for a in articles})

    def test_guides_grouped_for_toc(self):
        groups = guides_by_group()
        self.assertTrue(groups)
        group_names = [name for name, _ in groups]
        self.assertIn("Start here", group_names)
        # Start here should appear before Evaluation types when both present
        if "Evaluation types" in group_names:
            self.assertLess(
                group_names.index("Start here"),
                group_names.index("Evaluation types"),
            )

    def test_get_guide_by_slug(self):
        article = get_guide("getting-started")
        self.assertIsNotNone(article)
        self.assertEqual(article.title, "Getting started")
        self.assertIn("<", article.body_html)

    def test_custom_guides_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha.md").write_text(
                "---\ntitle: Alpha\ngroup: Custom\norder: 1\n---\n\nHello **world**.\n",
                encoding="utf-8",
            )
            (root / "_draft.md").write_text(
                "---\ntitle: Draft\n---\n\nHidden\n",
                encoding="utf-8",
            )
            articles = load_guides(root=root)
            self.assertEqual(len(articles), 1)
            self.assertEqual(articles[0].slug, "alpha")
            self.assertIn("<strong>world</strong>", articles[0].body_html)


class HelpViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="helper", password="pass")

    def test_help_requires_login(self):
        response = self.client.get(reverse("core:help"))
        self.assertEqual(response.status_code, 302)

    def test_help_index_redirects_to_first_guide(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:help"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/help/", response["Location"])

    def test_help_article_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("core:help_article", kwargs={"slug": "build-labelled-spreadsheet"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Build a labelled spreadsheet")
        self.assertContains(response, "Getting started")  # TOC sibling
        self.assertContains(response, "input_note_text")

    def test_unknown_guide_404(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("core:help_article", kwargs={"slug": "does-not-exist"})
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(HELP_GUIDES_DIR=None)
    def test_nav_includes_help_link(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertContains(response, reverse("core:help"))

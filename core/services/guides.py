"""Load help guides from Markdown files under ``core/guides/``.

Each ``*.md`` file may start with YAML front matter between ``---`` lines
(title, summary, group, order, optional slug).

The guide list and TOC are built automatically by scanning this directory.
Add a new ``.md`` file to publish a new article — no code changes required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from django.conf import settings
from markdown_it import MarkdownIt

GUIDES_DIR = Path(__file__).resolve().parent.parent / "guides"

# Display order for known groups; unknown groups sort after these, alphabetically.
GROUP_ORDER = (
    "Start here",
    "Workflows",
    "Evaluation types",
    "Ops",
)

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)

_md = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")


@dataclass(frozen=True)
class GuideArticle:
    """One published help article."""

    slug: str
    title: str
    summary: str
    group: str
    order: int
    body_html: str
    path: Path

    @property
    def group_order(self) -> int:
        try:
            return GROUP_ORDER.index(self.group)
        except ValueError:
            return len(GROUP_ORDER) + 1


def guides_root() -> Path:
    """Return the guides directory (overridable in tests via settings)."""
    override = getattr(settings, "HELP_GUIDES_DIR", None)
    if override:
        return Path(override)
    return GUIDES_DIR


def _parse_front_matter(raw: str) -> tuple[dict, str]:
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        return {}, raw
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, match.group(2)


def _load_article(path: Path) -> GuideArticle | None:
    if path.suffix.lower() != ".md":
        return None
    if path.name.startswith("_") or path.name.lower() == "readme.md":
        return None
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(raw)
    slug = str(meta.get("slug") or path.stem)
    title = str(meta.get("title") or slug.replace("-", " ").title())
    summary = str(meta.get("summary") or "")
    group = str(meta.get("group") or "Guides")
    try:
        order = int(meta.get("order", 100))
    except (TypeError, ValueError):
        order = 100
    body_html = _md.render(body)
    return GuideArticle(
        slug=slug,
        title=title,
        summary=summary,
        group=group,
        order=order,
        body_html=body_html,
        path=path,
    )


def load_guides(*, root: Path | None = None) -> list[GuideArticle]:
    """Load and sort all guide articles from disk."""
    base = root or guides_root()
    if not base.is_dir():
        return []
    articles: list[GuideArticle] = []
    for path in sorted(base.glob("*.md")):
        article = _load_article(path)
        if article is not None:
            articles.append(article)
    articles.sort(key=lambda a: (a.group_order, a.group.lower(), a.order, a.title.lower()))
    return articles


def get_guide(slug: str, *, root: Path | None = None) -> GuideArticle | None:
    for article in load_guides(root=root):
        if article.slug == slug:
            return article
    return None


def guides_by_group(*, root: Path | None = None) -> list[tuple[str, list[GuideArticle]]]:
    """Return ``[(group_name, [articles…]), …]`` for TOC rendering."""
    grouped: dict[str, list[GuideArticle]] = {}
    order: list[str] = []
    for article in load_guides(root=root):
        if article.group not in grouped:
            grouped[article.group] = []
            order.append(article.group)
        grouped[article.group].append(article)
    return [(name, grouped[name]) for name in order]
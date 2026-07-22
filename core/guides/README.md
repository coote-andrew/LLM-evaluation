# Help guides

Add a Markdown file in this folder to publish a new help article.

The Help page TOC is built automatically from these files — no Python or
template edits required.

## Front matter

```yaml
---
title: My guide title
summary: One-line description shown in the TOC
group: Start here
order: 30
slug: optional-url-slug
---
```

- **title** — page heading (required for a clear TOC)
- **summary** — short blurb (optional)
- **group** — TOC section name. Known groups sort as:
  `Start here`, `Workflows`, `Evaluation types`, `Ops`, then others A–Z
- **order** — sort within the group (lower first; default 100)
- **slug** — URL path segment (defaults to the filename without `.md`)

Files starting with `_` are ignored (useful for drafts or includes).

## Body

Write normal Markdown after the front matter. Tables and basic HTML are supported.

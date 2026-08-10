#!/usr/bin/env python3
"""
Import msitarzewski/agency-agents markdown files into StaffDeck Open Gallery.

Each .md file (with YAML frontmatter containing `name`) becomes a GeneralSkill
record registered to the Open Gallery, available for binding to digital
employees via the "从开放广场导入" UI.

Usage:
    cd /root/data-platform/staffdeck/backend
    .venv/bin/python scripts/import_agency_agents_to_gallery.py [--force] [--cleanup-stale] [--dry-run]

Source repo: https://github.com/msitarzewski/agency-agents (MIT)
Local clone: ../vendor/agency-agents/
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# Ensure backend/ is on sys.path so `app.*` imports resolve when run as a script.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import inspect  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.agents.branching import (  # noqa: E402
    ensure_open_gallery_binding,
    open_gallery_metadata,
)
from app.db.database import engine  # noqa: E402
from app.db.models import AgentResourceBinding, GeneralSkill, utc_now  # noqa: E402


# === Constants ============================================================

TENANT_ID = "tenant_demo"
ADMIN_USER_ID = "admin"
ADMIN_USERNAME = "admin"
ADMIN_DISPLAY_NAME = "Administrator"
SEED_SOURCE = "agency_agents_import"
SOURCE_REPO_URL = "https://github.com/msitarzewski/agency-agents"
REPO_LOCAL_PATH = BACKEND_DIR.parent / "vendor" / "agency-agents"

# Directories in the upstream repo that are NOT agent personality definitions.
EXCLUDE_DIRS = {
    "integrations",  # tool integration docs (Claude Code / Cursor / etc.)
    "examples",      # workflow examples, not agent definitions
    "scripts",       # install scripts
    ".github",       # CI config
    ".git",          # git metadata
}

# Files that should never be treated as agents even if they sit in group dirs.
EXCLUDE_FILENAMES = {"README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CONTRIBUTING_zh-CN.md"}


# === Frontmatter parsing ==================================================

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(content: str) -> dict[str, str] | None:
    """Parse a leading YAML frontmatter block (minimal, no PyYAML dependency).

    Returns a flat dict of top-level keys, or None if no frontmatter present.
    Only supports `key: value` lines; ignores lists/nested objects (sufficient
    for agency-agents frontmatter which only uses scalar fields).
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None
    frontmatter_text = match.group(1)
    result: dict[str, str] = {}
    for line in frontmatter_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result if result else None


def strip_frontmatter(content: str) -> str:
    """Return content without the leading frontmatter block (preserve body)."""
    match = _FRONTMATTER_RE.match(content)
    if match:
        return content[match.end():]
    return content


# === Discovery ============================================================

def discover_agents(repo_root: Path) -> list[dict[str, str]]:
    """Walk group directories and return a list of agent file descriptors.

    Each descriptor has: group, filename, path (absolute), slug_stem (filename
    without .md extension, kebab-case already).
    """
    agents: list[dict[str, str]] = []
    for group_dir in sorted(repo_root.iterdir()):
        if not group_dir.is_dir():
            continue
        if group_dir.name in EXCLUDE_DIRS:
            continue
        for md_file in sorted(group_dir.glob("*.md")):
            if md_file.name in EXCLUDE_FILENAMES:
                continue
            agents.append({
                "group": group_dir.name,
                "filename": md_file.name,
                "stem": md_file.stem,
                "path": str(md_file.resolve()),
            })
    return agents


# === Metadata helpers =====================================================

def _open_gallery_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build metadata marking a resource as Open Gallery + our seed source."""
    metadata = open_gallery_metadata(extra)
    metadata.update({
        "seed_source": SEED_SOURCE,
        "managed_by_seed": True,
        "owner_user_id": ADMIN_USER_ID,
        "owner_username": ADMIN_USERNAME,
        "owner_display_name": ADMIN_DISPLAY_NAME,
        "created_by_user_id": ADMIN_USER_ID,
        "created_by_username": ADMIN_USERNAME,
        "created_by": ADMIN_USERNAME,
        "created_by_display_name": ADMIN_DISPLAY_NAME,
        "creator_name": ADMIN_USERNAME,
        "published_to_gallery": True,
        "gallery_published_by": ADMIN_USERNAME,
    })
    return metadata


# === Import logic =========================================================

def _build_slug(group: str, stem: str) -> str:
    """Build a unique, namespaced slug: agency_<group>_<stem>."""
    safe_group = re.sub(r"[^a-z0-9-]", "-", group.lower()).strip("-")
    safe_stem = re.sub(r"[^a-z0-9-]", "-", stem.lower()).strip("-")
    return f"agency_{safe_group}_{safe_stem}"


def _extract_description(frontmatter: dict[str, str] | None, body: str) -> str:
    if frontmatter and frontmatter.get("description"):
        return frontmatter["description"][:500]
    # Fallback: first non-empty, non-heading line of body
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith(">"):
            return stripped[:500]
    return ""


def import_single_agent(
    session: Session,
    descriptor: dict[str, str],
    content: str,
    dry_run: bool = False,
) -> str:
    """Insert or update a GeneralSkill for one agent file. Returns action taken."""
    frontmatter = parse_frontmatter(content)
    if frontmatter is None or "name" not in frontmatter:
        return "skipped_no_frontmatter"

    body = strip_frontmatter(content)
    slug = _build_slug(descriptor["group"], descriptor["stem"])
    name = frontmatter.get("name") or descriptor["stem"]
    description = _extract_description(frontmatter, body)
    emoji = frontmatter.get("emoji", "")
    color = frontmatter.get("color", "")
    vibe = frontmatter.get("vibe", "")

    extra_metadata = {
        "source_repo": SOURCE_REPO_URL,
        "source_path": f"{descriptor['group']}/{descriptor['filename']}",
        "source_group": descriptor["group"],
        "source_filename": descriptor["filename"],
        "source_emoji": emoji,
        "source_color": color,
        "source_vibe": vibe,
    }
    metadata = _open_gallery_metadata(extra_metadata)

    # Find existing by (tenant_id, slug) — the unique constraint
    existing = session.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == TENANT_ID,
            GeneralSkill.slug == slug,
        )
    ).first()

    payload = {
        "tenant_id": TENANT_ID,
        "slug": slug,
        "name": name,
        "description": description,
        "homepage": None,
        "skill_markdown": content,  # store full file (incl. frontmatter) for traceability
        "skill_files_json": [],
        "metadata_json": metadata,
        "status": "published",
        "permissions_json": {},
        "runtime_config_json": {},
    }

    if existing is None:
        if dry_run:
            return "would_create"
        row = GeneralSkill(**payload)
        session.add(row)
        session.flush()
        ensure_open_gallery_binding(
            session,
            TENANT_ID,
            "general_skill",
            row.id,
            status="active",
            metadata_json=metadata,
        )
        return "created"

    # Update path — only overwrite if it's our seed (avoid clobbering user-created skills)
    is_ours = (existing.metadata_json or {}).get("seed_source") == SEED_SOURCE
    if not is_ours:
        return "skipped_foreign_owner"

    if dry_run:
        return "would_update"

    for key, value in payload.items():
        setattr(existing, key, value)
    existing.updated_at = utc_now()
    session.add(existing)
    session.flush()
    ensure_open_gallery_binding(
        session,
        TENANT_ID,
        "general_skill",
        existing.id,
        status="active",
        metadata_json=metadata,
    )
    return "updated"


def cleanup_stale(session: Session, valid_slugs: set[str], dry_run: bool = False) -> list[str]:
    """Delete GeneralSkill rows we previously imported but no longer exist in repo."""
    rows = session.exec(
        select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT_ID)
    ).all()
    removed: list[str] = []
    for row in rows:
        metadata = row.metadata_json or {}
        if metadata.get("seed_source") != SEED_SOURCE:
            continue
        if row.slug in valid_slugs:
            continue
        if dry_run:
            removed.append(f"{row.slug} (would delete)")
            continue
        # Remove open_gallery binding first
        bindings = session.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == TENANT_ID,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == row.id,
            )
        ).all()
        for binding in bindings:
            session.delete(binding)
        session.delete(row)
        removed.append(row.slug)
    return removed


# === Main =================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="Overwrite existing records (default behavior; kept for clarity)")
    parser.add_argument("--cleanup-stale", action="store_true", help="Delete previously-imported skills no longer present in the repo")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing to DB")
    parser.add_argument("--repo-path", default=str(REPO_LOCAL_PATH), help=f"Local path to agency-agents repo (default: {REPO_LOCAL_PATH})")
    args = parser.parse_args()

    repo_root = Path(args.repo_path).resolve()
    if not repo_root.is_dir():
        print(f"ERROR: repo path does not exist: {repo_root}", file=sys.stderr)
        print("Run: cd /root/data-platform/staffdeck/vendor && git clone https://github.com/msitarzewski/agency-agents.git", file=sys.stderr)
        return 2

    print(f"Repo: {repo_root}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'WRITE'}")
    print()

    agents = discover_agents(repo_root)
    print(f"Discovered {len(agents)} candidate .md files across {len({a['group'] for a in agents})} groups")

    # Group breakdown
    by_group: dict[str, int] = {}
    for a in agents:
        by_group[a["group"]] = by_group.get(a["group"], 0) + 1
    for group in sorted(by_group):
        print(f"  {group:<25} {by_group[group]}")
    print()

    actions: dict[str, int] = {}
    valid_slugs: set[str] = set()
    skipped_no_frontmatter: list[str] = []

    with Session(engine) as session:
        for descriptor in agents:
            try:
                content = Path(descriptor["path"]).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"  ERROR reading {descriptor['path']}: {exc}", file=sys.stderr)
                actions["read_error"] = actions.get("read_error", 0) + 1
                continue

            valid_slugs.add(_build_slug(descriptor["group"], descriptor["stem"]))
            action = import_single_agent(session, descriptor, content, dry_run=args.dry_run)
            actions[action] = actions.get(action, 0) + 1
            if action == "skipped_no_frontmatter":
                skipped_no_frontmatter.append(f"{descriptor['group']}/{descriptor['filename']}")

        if args.cleanup_stale:
            print()
            print("Cleaning up stale records (previously imported but no longer in repo)...")
            removed = cleanup_stale(session, valid_slugs, dry_run=args.dry_run)
            print(f"  Removed {len(removed)} stale records")
            actions["stale_removed"] = len(removed)

        if not args.dry_run:
            session.commit()

    print()
    print("=== Summary ===")
    for action in sorted(actions):
        print(f"  {action:<30} {actions[action]}")

    if skipped_no_frontmatter:
        print()
        print(f"Skipped (no frontmatter) {len(skipped_no_frontmatter)} files:")
        for path in skipped_no_frontmatter[:10]:
            print(f"  - {path}")
        if len(skipped_no_frontmatter) > 10:
            print(f"  ... and {len(skipped_no_frontmatter) - 10} more")

    # Final DB count for sanity
    if not args.dry_run:
        with Session(engine) as session:
            total = session.exec(
                select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT_ID)
            ).all()
            ours = [r for r in total if (r.metadata_json or {}).get("seed_source") == SEED_SOURCE]
            print()
            print(f"DB state: {len(ours)} agency-agents skills in Open Gallery ({len(total)} total general_skills)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations
from typing import List, Dict, Tuple


def get_library_data() -> List[Dict]:
    from app.models import Story, db
    db.session.expire_all()
    stories = Story.query.order_by(Story.created_at.desc()).all()
    return [story.to_library_dict() for story in stories]


def get_all_category_names() -> List[str]:
    from app.models import Category, Story, db
    rows = (
        db.session.query(Category.name)
        .join(Story, Story.category_id == Category.id)
        .distinct()
        .order_by(Category.name)
        .all()
    )
    return [r[0] for r in rows]


def get_queue_count() -> int:
    """Number of stories currently on the reading queue."""
    from app.models import Story
    return Story.query.filter(Story.in_queue == True).count()  # noqa: E712


def get_stories_page(
    page: int = 1,
    per_page: int = 40,
    search: str = '',
    category='all',
    sort_by: str = 'date',
    sort_order: str = 'desc',
    queue_only: bool = False,
    min_community_score: float = 0.0,
    min_pages: int = 0,
    max_pages: int = 0,
    source: str = 'all',
    user_rating: str = '',
    max_community_score: float = 0.0,
    read_status: str = '',
) -> Tuple[List[Dict], int]:
    """Return (page_stories, total_count). Uses DB-level ops when search is empty.

    category: one name, 'all', 'uncategorized', or a list of these (any of them matches).
    user_rating: '1'–'5' (at least), 'below-2'–'below-5' (rated, fewer stars), 'unrated' or ''.
    read_status: 'read', 'unread' (not finished), 'reading' (started, not finished) or ''.
    """
    from app.models import Story, Category, ReadingProgress, db
    from sqlalchemy import asc, desc, or_, select

    # author/category sort require a join that conflicts with the eager-loaded joins;
    # fall back to Python sort for those two cases only.
    PYTHON_SORT_FIELDS = {'author', 'category'}
    use_python_sort = sort_by in PYTHON_SORT_FIELDS

    categories = [c for c in ([category] if isinstance(category, str) else list(category or []))
                  if c and c != 'all']

    def _apply_filters(query):
        if categories:
            names = [c for c in categories if c != 'uncategorized']
            conditions = []
            if 'uncategorized' in categories:
                conditions.append(Story.category_id.is_(None))
            if names:
                conditions.append(Story.category_id.in_(select(Category.id).where(Category.name.in_(names))))
            query = query.filter(or_(*conditions))
        if queue_only:
            query = query.filter(Story.in_queue == True)  # noqa: E712
        if min_community_score > 0:
            query = query.filter(Story.literotica_score >= min_community_score)
        if max_community_score > 0:
            query = query.filter(Story.literotica_score < max_community_score)
        if min_pages > 0:
            query = query.filter(Story.literotica_page_count >= min_pages)
        if max_pages > 0:
            query = query.filter(Story.literotica_page_count <= max_pages)
        if source and source != 'all':
            query = query.filter(Story.source_type == source)
        if user_rating == 'unrated':
            query = query.filter(Story.rating.is_(None))
        elif user_rating in ('1', '2', '3', '4', '5'):
            query = query.filter(Story.rating >= int(user_rating))
        elif user_rating.startswith('below-') and user_rating[6:] in ('2', '3', '4', '5'):
            query = query.filter(Story.rating.isnot(None), Story.rating < int(user_rating[6:]))
        finished = select(ReadingProgress.story_id).where(ReadingProgress.is_completed == True)  # noqa: E712
        if read_status == 'read':
            query = query.filter(Story.id.in_(finished))
        elif read_status == 'unread':
            query = query.filter(Story.id.notin_(finished))
        elif read_status == 'reading':
            started = select(ReadingProgress.story_id).where(
                ReadingProgress.is_completed != True, ReadingProgress.percentage > 0)  # noqa: E712
            query = query.filter(Story.id.in_(started))
        return query

    if search or use_python_sort:
        query = _apply_filters(Story.query)
        all_stories = [s.to_library_dict() for s in query.all()]

        if search:
            from app.services import search_index

            search_lower = search.lower()
            by_id = {s['id']: s for s in all_stories}

            fts_hits = search_index.search(search)
            fts_snippets = {h['story_id']: h['snippet'] for h in fts_hits}

            scored = []
            metadata_hit_ids = set()
            for story in all_stories:
                score = 0
                if search_lower in story.get('title', '').lower():
                    score += 100
                if search_lower in story.get('author', '').lower():
                    score += 50
                if search_lower in (story.get('category') or '').lower():
                    score += 25
                if any(search_lower in t.lower() for t in story.get('tags', [])):
                    score += 10
                if score > 0:
                    if story['id'] in fts_snippets:
                        story['content_snippet'] = fts_snippets[story['id']]
                    scored.append((score, story))
                    metadata_hit_ids.add(story['id'])
            scored.sort(key=lambda x: x[0], reverse=True)
            ordered = [s for _, s in scored]

            # Body-only matches (respecting the DB-level filters already applied above)
            for hit in fts_hits:
                story = by_id.get(hit['story_id'])
                if story is not None and hit['story_id'] not in metadata_hit_ids:
                    story['content_snippet'] = hit['snippet']
                    ordered.append(story)

            all_stories = ordered
        else:
            def _sort_key(story: dict) -> tuple:
                if sort_by == 'author':
                    return ((story.get('author') or '').lower(),)
                return ((story.get('category') or '').lower(),)
            all_stories.sort(key=_sort_key, reverse=(sort_order == 'desc'))

        total = len(all_stories)
        start = (page - 1) * per_page
        return all_stories[start:start + per_page], total

    # Full DB-level filtering + sorting + pagination
    query = _apply_filters(Story.query)

    col_map = {
        'date': Story.created_at,
        'name': Story.title,
        'length': Story.word_count,
        'rating': Story.rating,
        'last_opened': Story.last_opened_at,
        'community_score': Story.literotica_score,
        'pages': Story.literotica_page_count,
        'queued': Story.queued_at,
    }
    order_col = col_map.get(sort_by, Story.created_at)
    if sort_order == 'desc':
        query = query.order_by(desc(order_col).nullslast())
    else:
        query = query.order_by(asc(order_col).nullsfirst())

    total = query.count()
    page_stories = query.offset((page - 1) * per_page).limit(per_page).all()
    return [s.to_library_dict() for s in page_stories], total

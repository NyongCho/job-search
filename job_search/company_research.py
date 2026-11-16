"""Read-only, dated company research; explicit aliases avoid fuzzy identity matches."""

import json
import math
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit


def _name(value):
    return ' '.join(value.split()).casefold()


def load_research(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported company research schema')
    checked_on = data['checked_on']
    if date.fromisoformat(checked_on).isoformat() != checked_on:
        raise ValueError('Invalid research date')
    index, keys = {}, set()
    for record in data['companies']:
        key, status = record['key'], record['status']
        if key in keys or status not in {'reviewed', 'no_reviews', 'unmatched'}:
            raise ValueError('Duplicate company or invalid research status')
        keys.add(key)
        for field in ('name', 'pros', 'cons', 'caveat'):
            if not isinstance(record[field], str):
                raise ValueError('Invalid company research text')
        count = record['review_count']
        scores = [record[field] for field in ('rating', 'benefits_rating', 'work_life_rating')]
        if status == 'reviewed':
            if type(count) is not int or count < 1 or any(
                type(score) not in (int, float) or not math.isfinite(score) or not 1 <= score <= 5
                for score in scores
            ):
                raise ValueError('Reviewed companies require a count and valid ratings')
        elif any(score is not None for score in scores) or (
            status == 'no_reviews' and (type(count) is not int or count != 0)
        ) or (status == 'unmatched' and count is not None):
            raise ValueError('Missing research must not have a numeric rating')
        url = record['jobplanet_url']
        if status == 'unmatched':
            if url is not None:
                raise ValueError('Unmatched companies must not link to a different company')
        else:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.netloc != 'www.jobplanet.co.kr'
                    or not re.fullmatch(r'/companies/\d+/reviews/[^/]+', parsed.path)
                    or parsed.query or parsed.fragment or any(c.isspace() for c in url)):
                raise ValueError('Expected a direct HTTPS Jobplanet review URL')
        if record['latest_review_month'] is not None:
            date.fromisoformat(record['latest_review_month'] + '-01')
        aliases = record['aliases']
        if not isinstance(aliases, list) or not aliases:
            raise ValueError('Explicit company aliases are required')
        entry = {**record, 'checked_on': checked_on}
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError('Invalid company alias')
            normalized = _name(alias)
            if normalized in index and index[normalized]['key'] != key:
                raise ValueError('Ambiguous company alias')
            index[normalized] = entry
    return index


def lookup_research(index: dict, company: str):
    return index.get(_name(company))

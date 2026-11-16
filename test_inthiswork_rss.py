"""인디스워크 RSS 최신 공고 10건 수신 테스트: python3 test_inthiswork_rss.py"""

import json
import unittest
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.request import urlopen
from xml.etree import ElementTree


FEED_URL = "https://inthiswork.com/feed/"


class InthisworkRssTest(unittest.TestCase):
    def test_public_feed_contains_ten_unique_job_postings(self):
        with urlopen(FEED_URL, timeout=20) as response:
            self.assertEqual(response.status, 200)
            final_url = response.url
            root = ElementTree.fromstring(response.read())

        self.assertEqual(root.tag, "rss")
        channel = root.find("channel")
        self.assertIsNotNone(channel, "RSS channel이 없습니다.")
        items = channel.findall("item")
        self.assertTrue(items, "현재 RSS에 공고가 없습니다.")

        pages_fetched = 1
        # shortcut: 수집 중 새 글이 올라오면 중복 검사에서 실패할 수 있다. 지속 수집 시 재시도를 추가한다.
        while len(items) < 10:
            page_url = f"{FEED_URL}?paged={pages_fetched + 1}"
            with urlopen(page_url, timeout=20) as response:
                self.assertEqual(response.status, 200)
                page_root = ElementTree.fromstring(response.read())
            self.assertEqual(page_root.tag, "rss")
            page_items = page_root.findall("./channel/item")
            self.assertTrue(page_items, f"{page_url}에 공고가 없습니다.")
            items.extend(page_items)
            pages_fetched += 1

        postings = []
        for item in items[:10]:
            posting = {
                field: (item.findtext(field) or "").strip()
                for field in ("title", "link", "pubDate")
            }
            with self.subTest(title=posting["title"]):
                for field, value in posting.items():
                    self.assertTrue(value, f"{field} 값이 없습니다.")
                link = urlsplit(posting["link"])
                self.assertIn(link.scheme, ("http", "https"))
                self.assertEqual(link.hostname, "inthiswork.com")
                published_at = parsedate_to_datetime(posting["pubDate"])
                self.assertIsNotNone(published_at.tzinfo)
            postings.append(posting)

        self.assertEqual(len(postings), 10, "최신 공고 10건을 수집해야 합니다.")
        self.assertEqual(len({post["link"] for post in postings}), 10,
                         "수집한 공고에 중복 링크가 있습니다.")

        print(json.dumps({
            "feed_url": final_url,
            "feed_title": channel.findtext("title"),
            "last_build_date": channel.findtext("lastBuildDate"),
            "pages_fetched": pages_fetched,
            "count": len(postings),
            "postings": postings,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""공고 한 건의 JD 수집 가능성 검증: python3 prototype_job_evidence.py 419129

검증용 코드. 저장된 본문은 적합성 판단의 자료이며 자동 추천 결과가 아니다.
"""

import argparse
import html
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen


class Document(HTMLParser):
    BLOCKS = {"p", "li", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "tr"}
    HIDDEN = {"script", "style", "noscript", "template"}

    def __init__(self, source, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.fragments = []
        self.links = []
        self.images = []
        self.hidden = 0
        self.anchor = None
        self.feed(source)
        self.close()
        self.lines = [" ".join(line.split()) for line in "".join(self.fragments).splitlines() if line.strip()]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.HIDDEN:
            self.hidden += 1
        if self.hidden:
            return
        if tag in self.BLOCKS:
            self.fragments.append("\n")
        if tag == "a" and attrs.get("href"):
            self.anchor = {"url": urljoin(self.base_url, attrs["href"]), "parts": []}
        if tag == "img" and attrs.get("src"):
            self.images.append({"url": urljoin(self.base_url, attrs["src"]), "alt": attrs.get("alt", "")})

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "a" and self.anchor:
            if urlsplit(self.anchor["url"]).scheme in {"http", "https"}:
                self.links.append({"url": self.anchor["url"], "text": " ".join("".join(self.anchor["parts"]).split())})
            self.anchor = None
        if tag in self.BLOCKS:
            self.fragments.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.fragments.append(data)
            if self.anchor is not None:
                self.anchor["parts"].append(data)


def fetch(url):
    with urlopen(url, timeout=30) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8"), response.url


def record(source_id, url, document, retrieval_url=None):
    if not document.lines:
        raise ValueError(f"본문을 읽지 못했습니다: {url}")
    return {
        "id": source_id, "url": url, "retrieval_url": retrieval_url or url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "lines": [{"id": f"{source_id}:L{number}", "text": text}
                  for number, text in enumerate(document.lines, 1)],
        "links": document.links, "images": document.images,
    }


def application_link(document, label):
    return next((link["url"] for link in document.links
                 if re.sub(r"\s+", "", link["text"]) == label), None)


def self_check():
    document = Document('<h2>조건</h2><p>CS <strong>경험</strong> &amp; 역량</p>'
                        '<script>숨긴 내용</script><a href="/apply"><span>지원</span>하기</a>',
                        "https://example.com/job")
    assert document.lines == ["조건", "CS 경험 & 역량", "지원하기"]
    assert application_link(document, "지원하기") == "https://example.com/apply"
    assert not any("숨긴" in line for line in document.lines)


def main():
    self_check()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("post_id", type=int)
    args = parser.parse_args()
    api_url = f"https://inthiswork.com/wp-json/wp/v2/posts/{args.post_id}?_fields=id,date,modified,link,title,content,categories"
    body, _ = fetch(api_url)
    post = json.loads(body)
    if post["id"] != args.post_id:
        raise ValueError("요청한 공고와 응답 ID가 다릅니다.")
    document = Document(post["content"]["rendered"], post["link"])
    sources = [record("inthiswork", post["link"], document, api_url)]
    fetch_errors = []
    # shortcut: 이 샘플의 공개 HTML과 지원 버튼만 처리한다. 다른 ATS/이미지형 공고는 별도 검증이 필요하다.
    for source_id, label in (("employer", "지원하러가기"), ("application", "지원하기")):
        next_url = application_link(document, label)
        if not next_url or next_url in {source["url"] for source in sources}:
            break
        try:
            body, final_url = fetch(next_url)
        except (HTTPError, URLError, TimeoutError) as error:
            fetch_errors.append({"source": source_id, "url": next_url,
                                 "error": str(error), "http_status": getattr(error, "code", None)})
            break
        document = Document(body, final_url)
        sources.append(record(source_id, final_url, document))

    result = {
        "prototype": True, "post_id": post["id"],
        "title": html.unescape(post["title"]["rendered"]),
        "published_local": post["date"], "modified_local": post["modified"],
        "sources": sources,
        "fetch_errors": fetch_errors,
        "scope": "JD와 출처 수집 가능성 검증. 개인 적합도 판단은 수행하지 않음.",
    }
    output = Path(__file__).resolve().parent / "evidence" / str(args.post_id)
    output.mkdir(parents=True, exist_ok=True)
    (output / "sources.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    for source in sources:
        lines = [f"Source: {source['url']}", f"Fetched: {source['fetched_at']}", ""]
        lines.extend(f"[{line['id']}] {line['text']}" for line in source["lines"])
        (output / f"{source['id']}.txt").write_text("\n".join(lines) + "\n")
    print(json.dumps({"title": result["title"], "output": str(output),
                      "fetch_errors": fetch_errors,
                      "sources": [{"id": source["id"], "url": source["url"], "lines": len(source["lines"])}
                                  for source in sources]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

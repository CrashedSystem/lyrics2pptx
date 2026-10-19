"""Open-web lyric collector: search → fetch → extract → cross-validate (L2).

ADR-018. Coverage of the key-free public lyric APIs is poor for Korean worship
music (lrclib found 4 of 10 test songs and none of the hardest ones), but the
same songs *are* transcribed on ordinary blogs. This adapter therefore:

1. asks a :mod:`lyrics2pptx.crawl.websearch` backend for candidate pages,
2. fetches each candidate through :mod:`lyrics2pptx.crawl.httputil` (robots +
   user agent + per-host delay stay non-bypassable),
3. extracts the densest block of lyric-like lines without any site-specific
   selector, and
4. votes the resulting transcriptions with :mod:`lyrics2pptx.crawl.crosscheck`.

The first returned candidate is the *consensus* (majority spelling); the
remaining candidates are the individual transcriptions, so the user can compare
and pick another one when the majority is wrong.

Multiple pages that carry byte-identical text count as **one** vote, not many:
mirrors are common and would otherwise outvote independent transcriptions.

ADR-020 adds the rule that a *page* must look like a transcription before its
lines may vote at all. A query that is also a Bible verse ("정금같이 나오리라",
욥 23:10) otherwise collects scripture pages — bible.com's verse page and a blog
posting Job 23:1-9 — and those pages carried the vote because the real
transcription was a 353-line catalogue page that the similarity filter dropped.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass

from .. import config
from ..errors import ConfigError
from . import httputil, progress
from .base import RawSong, SourceError, SourceNotFoundError
from .crosscheck import Consensus, Transcript, cross_check, line_key
from .websearch import get_backend

__all__ = [
    "BlogLyricsSource",
    "TranscriptPage",
    "collect_and_vote",
    "extract_lyrics",
]

log = logging.getLogger(__name__)

# Script/style bodies and tags that carry no visible text.
_SCRIPT_RE = re.compile(
    r"<\s*(script|style|noscript|svg|iframe|template)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Block-level boundaries become line breaks; everything else becomes a space.
_BLOCK_RE = re.compile(
    r"<\s*/?\s*(?:br|p|div|li|ul|ol|h[1-6]|tr|td|th|blockquote|section|article"
    r"|header|footer|figure|figcaption|pre)\b[^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v\u00a0\u200b\ufeff]+")
# Anchor text is marked before tags are stripped: related-post lists and menus
# are link text, while real lyric lines are plain text. Without this marker a
# run of syllables runs straight from the last lyric into the related songs.
_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_LINK_MARK = "\x01"

# Page chrome that never belongs to a lyric line. Kept deliberately narrow: a
# false positive here only shortens a candidate run, it cannot invent lyrics.
_NOISE_RE = re.compile(
    r"(?:https?://|www\.|@[\w.]+|\.(?:com|net|org|co\.kr|kr)\b|^by\s"
    r"|공감|댓글|스크랩|신고|이웃추가|구독|공유하기|게시글\s*관리|URL\s*복사|다운로드|카테고리"
    r"|로그인|회원가입|티스토리|네이버\s*블로그|글쓴이|작성자|조회수|추천"
    r"|저작권|Copyright|ⓒ|©|Powered|RSS|이전글|다음글|맨\s*위로|반응형"
    r"|더\s*보기|블로그\s*내\s*검색|악보|영상\s*보기|내려받기"
    # Title lines of other songs ("… (Feat. 홍길동)") and scripture-page chrome
    # ("모든 번역본 비교 : 욥기 23:10"). Both were on real pages that reached the
    # vote (ADR-020).
    r"|\b(?:feat|ft)\.|모든\s*번역본|묵상\s*계획|무료\s*묵상|가사\s*더보기|노래\s*듣기)",
    re.IGNORECASE,
)
# Korean prose ends with these; lyrics almost never do.
_PROSE_END_RE = re.compile(
    r"(?:[.!?…]|습니다|입니다|합니다|됩니다|십니다|십시오|세요|니까|는데요)\s*$"
)
_HANGUL_RE = re.compile(r"[가-힣]")
# Verse numbers and section labels ("후렴", "[Verse 2]") sit between verses and
# must not break a run. ``text.normalize`` owns the canonical N6 pattern, but
# crawl must not import text (rule P4), so the short bracket-ish shape is
# restated here; the keyword check is unnecessary because a real lyric line
# never reaches this test (``_is_lyric_line`` claims it first).
_SEPARATOR_SHAPE_RE = re.compile(
    r"^\s*[\[\(（]?[^\]\)）]{0,12}[\]\)）]?\s*$",
    re.IGNORECASE,
)

# Furniture a lyric line never *equals*. Keys are ``line_key``-normalised
# (space-free, alphanumeric) and matched exactly, so a lyric that merely contains
# "검색" survives. These lines are dropped **without** ending the run: a login
# form or a share button between two verses must not split the song. Measured on
# gasazip.com, where eleven such lines sat directly above the lyrics and were
# being transcribed as lyrics (ADR-020).
_CHROME_LINES = frozenset(
    {
        "한국어", "공유", "보관", "검색", "가사검색", "가사등록", "싱크가사등록",
        "ai해석", "가사카드", "동영상새로고침", "회원아이디필수", "비밀번호필수",
        "url복사", "더보기", "맨위로", "자동로그인",
    }
)

# Headings that announce the lyric block is *over* and a catalogue of other songs
# begins. Unlike chrome these end the run: everything after them is a list of
# other titles, and letting it join the run is how an allklyrics.com candidate
# grew to 353 lines of other songs (ADR-020).
_TAIL_HEADINGS = frozenset(
    {
        "관련가사", "관련곡", "다른가사", "인기가사", "최신가사", "추천가사",
        "이가수듣기", "가사더보기", "이곡과비슷한", "같은가수의다른곡",
    }
)

#: A transcription, not a lyric *site*. A longer densest run is a catalogue page.
_MAX_LYRIC_LINES = 80

#: A Bible chapter renders as "1 욥이 대답하여 이르되" line after line; lyrics are
#: not numbered. Verse numbers are also kept *inside* the line (unlike the bare
#: "1." markers between stanzas), so counting them identifies a passage page.
_VERSE_LINE_RE = re.compile(r"^\d{1,3}[.)]?\s+\S")
_MIN_VERSE_LINES = 3

#: Sync-format sites print a line twice, so the slide would read
#: "내가 정금같이 나오리라 내가 정금같이 나오리라". Only a *phrase* (four or more
#: tokens) is collapsed, which leaves a short repeat like "사랑해 사랑해" alone.
_DOUBLED_MIN_TOKENS = 4


def _collapse_duplicate_phrase(line: str) -> str:
    """Collapse ``A A`` (sync-format duplication) into ``A``; else return ``line``."""
    tokens = line.split()
    if len(tokens) < _DOUBLED_MIN_TOKENS or len(tokens) % 2:
        return line
    half = len(tokens) // 2
    if tokens[:half] == tokens[half:]:
        return " ".join(tokens[:half])
    return line


def _collapse_duplicated_block(lines: list[str]) -> list[str]:
    """Drop a block the page prints twice in a row (layout duplication, not song).

    gasazip.com renders the same six lines above and below a "영상 보기" button, so
    every slide appeared twice in the deck. Only a *whole-run* duplication is
    removed, which leaves a real two-verse song alone (its halves differ) and
    only loses a repeat whose words are still present (ADR-020).
    """
    while len(lines) >= 2 and len(lines) % 2 == 0:
        half = len(lines) // 2
        if lines[:half] != lines[half:]:
            break
        lines = lines[:half]
    return lines


def _to_lines(page: str) -> list[tuple[str, bool]]:
    """Flatten HTML into ``(line, is_link)`` pairs of trimmed text lines."""
    text = _SCRIPT_RE.sub(" ", page)
    text = _ANCHOR_RE.sub(lambda match: _LINK_MARK + match.group(1) + _LINK_MARK, text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    # Some CMSs store the post body HTML-escaped, so ``&amp;nbsp;`` survives the
    # first pass as the literal text ``&nbsp;``. A second unescape turns it into
    # a non-breaking space, which ``_SPACE_RE`` then collapses. Without this,
    # the literal token is not lyric-like and silently truncates the run.
    text = html_module.unescape(html_module.unescape(text))
    lines: list[tuple[str, bool]] = []
    for raw in text.split("\n"):
        line = _SPACE_RE.sub(" ", raw).strip()
        if not line:
            continue
        is_link = _LINK_MARK in line
        line = line.replace(_LINK_MARK, "").strip()
        if line:
            lines.append((line, is_link))
    return lines


def _is_lyric_line(line: str, *, is_link: bool = False) -> bool:
    """Heuristic: short, mostly-Korean, not chrome, not a link, not prose."""
    if is_link:
        return False
    if not 2 <= len(line) <= 55:
        return False
    if _NOISE_RE.search(line):
        return False
    if _PROSE_END_RE.search(line):
        return False
    hangul = len(_HANGUL_RE.findall(line))
    visible = len(line.replace(" ", ""))
    if hangul < 2 or hangul * 2 < visible:
        return False
    # Long prose lines that happen not to end in punctuation.
    if line.count(" ") > 14:
        return False
    return True


def _is_separator(line: str) -> bool:
    """True for verse numbers / section markers / decoration between verses.

    They must not break a lyric run: a blog that writes "1." and "2." between
    verses would otherwise split one song into several short runs, and the
    longest run (often just the first verse) would win.
    """
    stripped = line.strip()
    if _SEPARATOR_SHAPE_RE.match(stripped):
        return True
    return len(stripped) <= 4 and not _HANGUL_RE.search(stripped)


def extract_lyrics(page: str, *, min_lines: int = 6) -> tuple[str, ...]:
    """Return the longest run of consecutive lyric-like lines in ``page``.

    No site-specific selector is used, so the same code works for tistory,
    Naver blogs and ordinary HTML pages. Returns ``()`` when no run of at least
    ``min_lines`` lines exists (e.g. the lyrics are published as an image) or
    when the page is not a transcription at all: a Bible passage (numbered
    verse lines), a catalogue of other songs (an implausibly long run), or page
    furniture that happens to be short Korean text (ADR-020).
    """
    best: list[str] = []
    current: list[str] = []
    for line, is_link in _to_lines(page):
        key = line_key(line)
        if key in _CHROME_LINES:
            # Furniture: not a lyric, but not a break either — a menu between
            # two verses must not cut the song in half.
            continue
        if key in _TAIL_HEADINGS:
            if len(current) > len(best):
                best = current
            current = []
            continue
        if _is_lyric_line(line, is_link=is_link):
            current.append(_collapse_duplicate_phrase(line))
            continue
        if _is_separator(line):
            continue
        if len(current) > len(best):
            best = current
        current = []
    if len(current) > len(best):
        best = current
    best = _collapse_duplicated_block(best)
    if len(best) < max(1, min_lines):
        return ()
    # A page that lists hundreds of other songs has no single transcription; the
    # longest run is the catalogue itself (allklyrics.com, 353 lines).
    if len(best) > _MAX_LYRIC_LINES:
        return ()
    # "1 욥이 대답하여 이르되 / 2 오늘도 내게 반항하는 마음과 …" is a passage, not a
    # song. Requiring half the run as well keeps a blog that quotes three verses
    # *around* its lyrics from being rejected.
    verse_lines = sum(1 for line in best if _VERSE_LINE_RE.match(line))
    if verse_lines >= _MIN_VERSE_LINES and verse_lines * 2 >= len(best):
        return ()
    # A page that publishes the lyrics as an image still has menus, "악보",
    # "영상보기" and related-post lists. Those lines are short, while real lyric
    # lines are substantial, so require at least half of the run (and no fewer
    # than four lines) to be reasonably long. This keeps a menu block from being
    # mistaken for lyrics when no better page is available.
    substantial = sum(1 for line in best if len(line) >= 6)
    if substantial < max(4, (len(best) + 1) // 2):
        return ()
    return tuple(best)


_HEADING_KEYS = frozenset({"가사", "찬양가사", "ccm찬양가사", "가사모음"})


def _strip_headings(lines: tuple[str, ...], *, title_key: str) -> tuple[str, ...]:
    """Drop leading/trailing title and heading lines so they do not shift votes.

    Blogs commonly start the lyric block with the post title (which is the
    query) and a generic "가사" heading, and some repeat the title at the end.
    Those lines are page furniture, not lyrics. Only the edges are touched: a
    title phrase that occurs *inside* the song is kept.
    """
    interesting = _HEADING_KEYS | {title_key}
    start = 0
    limit = min(len(lines), 4)
    while start < limit and line_key(lines[start]) in interesting:
        start += 1

    end = len(lines)
    while end > start + 1 and line_key(lines[end - 1]) in interesting:
        end -= 1

    return lines[start:end]


def _is_http_url(url: str) -> bool:
    return httputil.is_http_url(url)


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    """One fetched page that yielded lyrics."""

    source: str
    url: str
    lines: tuple[str, ...]


def _search_backend():
    return get_backend()


def collect_and_vote(
    query: str,
    *,
    max_pages: int | None = None,
) -> tuple[Consensus, tuple[TranscriptPage, ...]]:
    """Search, fetch, extract and vote; return the consensus and its evidence.

    Raises:
        ConfigError: the query is empty.
        SourceNotFoundError: no candidate page yielded lyrics.
        SourceError: the search backend itself failed.
    """
    text = (query or "").strip()
    if not text:
        raise ConfigError("검색어가 비어 있습니다")
    pages_wanted = config.BLOG_MAX_PAGES if max_pages is None else max(1, max_pages)

    backend = _search_backend()
    progress.report("블로그 검색 중…")
    # Ask for extra hits: some pages publish the lyrics as an image or are
    # duplicates, so the first few results are not all usable.
    results = backend.search(f"{text} 가사", limit=max(pages_wanted * 2, pages_wanted))
    progress.report(f"후보 {len(results)}건에서 가사를 확인합니다", done=0, total=pages_wanted)

    transcripts: list[TranscriptPage] = []
    fingerprints: set[str] = set()
    checked = 0
    for result in results:
        if len(transcripts) >= pages_wanted:
            break
        if not _is_http_url(result.url):
            continue
        checked += 1
        # ``checked`` (attempts) rather than ``len(transcripts)`` (successes):
        # the bar must move forward even when a page turns out to be unusable.
        # The ``k/N`` prefix keeps two pages on the same host distinguishable,
        # which also stops the UI from collapsing them into one line.
        progress.report(
            f"{min(checked, pages_wanted)}/{pages_wanted} {httputil.host_of(result.url)} 페이지 확인 중…",
            done=min(checked, pages_wanted),
            total=pages_wanted,
        )
        try:
            page = httputil.get_text(result.url)
        except (SourceError, ValueError) as exc:
            # One unusable page must not abort the whole vote. ``ValueError``
            # covers a malformed URL that urllib refuses to encode.
            log.info("blog skip url=%s reason=%s", result.url, type(exc).__name__)
            continue
        lines = extract_lyrics(page)
        if not lines:
            log.info("blog no lyrics url=%s", result.url)
            continue
        lines = _strip_headings(lines, title_key=line_key(text))
        if len(lines) < 6:
            log.info("blog too short url=%s", result.url)
            continue
        # One vote per distinct transcription: mirrors must not outvote
        # independent pages (the lrclib/lyrics.ovh lesson, ADR-017).
        fingerprint = "".join("".join(lines).split())
        if fingerprint in fingerprints:
            log.info("blog duplicate url=%s", result.url)
            continue
        fingerprints.add(fingerprint)
        transcripts.append(
            TranscriptPage(source=httputil.host_of(result.url), url=result.url, lines=lines)
        )

    if not transcripts:
        raise SourceNotFoundError(
            f"'{text}' 가사를 찾지 못했습니다. 직접 입력을 사용하세요."
        )

    # Deterministic order: the vote's medoid, the candidate list and therefore
    # ``--index`` must not change with the order pages happened to arrive in
    # (ADR-020).
    transcripts.sort(key=lambda item: (item.source, item.url))

    consensus = cross_check(
        [Transcript(item.source, item.lines, item.url) for item in transcripts]
    )
    progress.report(_consensus_progress(consensus))
    log.info(
        "blog voted pages=%d of=%d lines=%d disputed=%d",
        consensus.source_count,
        consensus.pages_seen,
        len(consensus.lines),
        len(consensus.disagreements),
    )
    return consensus, tuple(transcripts)


def _consensus_progress(consensus: Consensus) -> str:
    """One honest line about the vote; a single page is never a cross-check.

    The user sees this line while the collect runs and it becomes the last step
    of the progress card, so it must not claim agreement the vote did not have
    (ADR-019/ADR-020).
    """
    if consensus.cross_checked:
        return f"{consensus.source_count}개 전사본 교차검증 완료 → {len(consensus.lines)}줄"
    if consensus.pages_seen > 1:
        return (
            f"교차검증 안 됨 — 후보 {consensus.pages_seen}개가 서로 다른 곡입니다"
            f" ({len(consensus.lines)}줄)"
        )
    return f"교차검증 안 됨 — 가사를 준 페이지가 1장뿐입니다 ({len(consensus.lines)}줄)"


def _consensus_artist(consensus: Consensus) -> str:
    """Short label for the voted candidate, shown next to the title in the UI."""
    if consensus.cross_checked:
        return f"교차검증 {consensus.source_count}곳"
    if consensus.pages_seen > 1:
        return f"교차검증 안 됨 — 후보 {consensus.pages_seen}개가 서로 다름"
    return "교차검증 안 됨 — 페이지 1장"


class BlogLyricsSource:
    """Collector that finds lyrics on public pages found by a search backend."""

    name = "blog"

    def search(self, query: str, *, limit: int = 5) -> list[RawSong]:
        consensus, transcripts = collect_and_vote(query)
        if not consensus.lines:
            raise SourceNotFoundError(f"'{query}' 가사를 찾지 못했습니다.")

        songs = [
            RawSong(
                title=(query or "").strip(),
                artist=_consensus_artist(consensus),
                lyrics="\n".join(consensus.lines),
                source_name=self.name,
                source_url=transcripts[0].url,
            )
        ]
        for item in transcripts:
            songs.append(
                RawSong(
                    title=(query or "").strip(),
                    # Host *and* size: five page candidates used to be shown with
                    # the same title and host-only artist, which told the user
                    # nothing about which one to pick (ADR-020).
                    artist=f"{item.source} · {len(item.lines)}줄",
                    lyrics="\n".join(item.lines),
                    source_name=self.name,
                    source_url=item.url,
                )
            )
        return songs[: max(1, limit)]

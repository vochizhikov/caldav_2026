import re
from html import unescape
from urllib.parse import urlsplit

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def trim_link(value: str) -> str:
    url = value.rstrip(".,;!?")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while url.endswith(closing) and url.count(closing) > url.count(opening):
            url = url[:-1]
    return url


def http_links(value: str):
    """Read HTTP(S) URLs in their textual order, including HTML href attributes."""
    for match in URL_PATTERN.finditer(unescape(value)):
        url = trim_link(match.group())
        try:
            parsed = urlsplit(url)
            if parsed.hostname and not parsed.username and len(url) <= 2048:
                yield url
        except ValueError:
            continue


def first_link(value: str) -> str:
    return next(http_links(value), "")


def is_meeting_link(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host in {"telemost.360.yandex.ru", "telemost.yandex.ru"}:
        return path.startswith("/j/") and bool(path[3:])
    if host == "meet.google.com":
        return bool(re.fullmatch(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}/?", path)) or path.startswith(
            "/lookup/"
        )
    if any(host == domain or host.endswith(f".{domain}") for domain in ("zoom.us", "zoom.com")):
        return path.startswith(("/j/", "/my/", "/s/", "/w/"))
    if host in {"teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"}:
        return path.startswith(("/l/meetup-join/", "/meet/"))
    if host == "webex.com" or host.endswith(".webex.com"):
        return path.startswith(("/meet/", "/join/")) or "/j.php" in path
    return host == "meet.jit.si" and bool(path.strip("/"))


def meeting_link(component) -> str:
    # Ignore links to documents: prefer a meeting in DESCRIPTION, then other metadata.
    for name in ("DESCRIPTION", "URL", "LOCATION", "CONFERENCE", "X-ALT-DESC"):
        values = component.get(name, [])
        if not isinstance(values, list):
            values = [values]
        for value in values:
            for link in http_links(str(value)):
                if name == "CONFERENCE" or is_meeting_link(link):
                    return link
    return ""


def meeting_location(component) -> str:
    # A conference URL in LOCATION is a link, not a meeting room.
    value = unescape(str(component.get("LOCATION", "")))
    value = URL_PATTERN.sub(lambda match: match.group()[len(trim_link(match.group())) :], value)
    value = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", value)
    return " ".join(value.split()).strip(" ,;|.")

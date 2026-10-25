"""Render note records as Markdown without reading files or making requests."""
from urllib.parse import quote, urlsplit
from pathlib import PurePosixPath


def _title(note):
    title = note.get("title", "").strip()
    return f"# {title}" if title else ""


def _text(note):
    return note.get("text", "").strip()


def _images(note):
    images = []
    local_files = {asset["position"]: asset["path"] for asset in note.get("image_files", [])}
    for position, url in enumerate(note.get("images") or [], start=1):
        local = PurePosixPath(local_files.get(position, ""))
        if (len(local.parts) == 3 and local.parts[:2] == ("clean", "images")
                and local.parts[2] not in {".", ".."} and "\\" not in local.parts[2]):
            target = quote(PurePosixPath(*local.parts[1:]).as_posix(), safe="/-._~")
            images.append(f"![图片{position}](<{target}>)")
            continue
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        # Preserve URL parameters while escaping Markdown delimiters and whitespace.
        target = quote(url, safe=":/?#[]@!$&'()*+,;=%")
        images.append(f"![图片{position}](<{target}>)")
    return "\n\n".join(images)


def _source(note):
    url = note.get("source_url", "").strip()
    return f"来源：{url}" if url else ""


_SECTION_RENDERERS = {
    "title": _title,
    "text": _text,
    "images": _images,
    "source": _source,
}


def render_note_markdown(note, *, sections=("title", "text", "images", "source")):
    """Select and order sections; add new sections in _SECTION_RENDERERS."""
    blocks = []
    for name in sections:
        if name not in _SECTION_RENDERERS:
            raise ValueError(f"Unknown Markdown section: {name}")
        block = _SECTION_RENDERERS[name](note)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks) + "\n" if blocks else ""

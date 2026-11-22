"""The one media-type vocabulary: every media type (MIME type) the code names, in lower case (RFC 6838: names are
case-insensitive, lower case is canonical).

The IANA-registered name is canonical wherever one exists (IANA Media Types registry, checked 2026-10-28); where IANA
has none, the de facto name is (audio/wav, video/webm, audio/webm). Other spellings stay here as aliases a client or a
detector may send: an upload is accepted by its format's content type and aliases, and stored under the format's
canonical name (services/files/formats.py). Every member that is not an IANA registration says why it is used.

Values are written only here (tests/test_media_type_literals.py): code imports the member; migrations keep frozen
copies of their own (migrations/support/media_types_v1.py). Parsing and formatting Content-Type values is
utils/content_type.py.
"""
from enum import Enum


class ContentType(str, Enum):
    """A media type by its name; str(member) and f-strings give the value"""

    # ---- what the API reads and writes itself ------------------------------------------------------------------
    JSON = "application/json"
    PROBLEM_JSON = "application/problem+json"                 # RFC 9457
    MULTIPART_FORM_DATA = "multipart/form-data"               # RFC 7578
    FORM_URLENCODED = "application/x-www-form-urlencoded"     # registered under its x- name (WHATWG URL)
    OCTET_STREAM = "application/octet-stream"                 # RFC 2046: opaque bytes, also the sniffer's "unknown"
    # Not registered (no "binary" top-level type): some HTTP clients and S3 tools send it for opaque bytes
    BINARY_OCTET_STREAM = "binary/octet-stream"
    # Not registered: the tus resumable upload protocol's own name for a chunk body (UPLOADS.md)
    OFFSET_OCTET_STREAM = "application/offset+octet-stream"
    WEBVTT = "text/vtt"                                       # W3C WebVTT: always UTF-8, no charset parameter
    EVENT_STREAM = "text/event-stream"                        # WHATWG HTML server-sent events: always UTF-8
    PKCS12 = "application/pkcs12"                             # RFC 7292 (.p12); application/x-pkcs12 is not registered
    HTML = "text/html"
    # A media range (RFC 9110 section 12.5.1), not a media type: "any type", for OpenAPI descriptions of originals
    ANY = "*/*"

    # ---- images ------------------------------------------------------------------------------------------------
    IMAGE_JPEG = "image/jpeg"
    IMAGE_PJPEG = "image/pjpeg"                    # not registered: old Internet Explorer name for progressive JPEG
    IMAGE_PNG = "image/png"
    IMAGE_WEBP = "image/webp"                      # RFC 9649
    IMAGE_GIF = "image/gif"
    IMAGE_SVG = "image/svg+xml"

    # ---- video -------------------------------------------------------------------------------------------------
    VIDEO_MP4 = "video/mp4"
    VIDEO_X_M4V = "video/x-m4v"                    # not registered: Apple's name for .m4v, what libmagic reports
    VIDEO_WEBM = "video/webm"                      # not registered: the WebM project's name, de facto canonical
    VIDEO_QUICKTIME = "video/quicktime"
    VIDEO_MATROSKA = "video/matroska"              # RFC 9559
    VIDEO_X_MATROSKA = "video/x-matroska"          # not registered: the name before RFC 9559 (libmagic, older tools)

    # ---- audio -------------------------------------------------------------------------------------------------
    AUDIO_MPEG = "audio/mpeg"                      # RFC 3003 (MP3)
    AUDIO_MP3 = "audio/mp3"                        # not registered: what Chromium and some uploaders send for MP3
    AUDIO_WAV = "audio/wav"                        # not registered (IANA has no WAVE type): de facto canonical
    AUDIO_X_WAV = "audio/x-wav"                    # not registered: what libmagic reports for WAVE
    AUDIO_WAVE = "audio/wave"                      # not registered: older browsers' name for WAVE
    AUDIO_VND_WAVE = "audio/vnd.wave"              # not registered (RFC 2361 named it, never registered)
    AUDIO_FLAC = "audio/flac"                      # RFC 9639
    AUDIO_X_FLAC = "audio/x-flac"                  # not registered: the name before RFC 9639 (libmagic)
    AUDIO_OGG = "audio/ogg"                        # RFC 5334
    AUDIO_OPUS = "audio/opus"                      # RFC 7587
    APPLICATION_OGG = "application/ogg"            # RFC 5334: an Ogg file of unknown content (libmagic)
    AUDIO_MP4 = "audio/mp4"                        # RFC 4337
    AUDIO_X_M4A = "audio/x-m4a"                    # not registered: Apple's name for .m4a, what libmagic reports
    AUDIO_AAC = "audio/aac"

    # ---- documents ---------------------------------------------------------------------------------------------
    PDF = "application/pdf"                                                         # RFC 8118
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ODT = "application/vnd.oasis.opendocument.text"
    MSWORD = "application/msword"
    TEXT_MARKDOWN = "text/markdown"                # RFC 7763: charset is a required parameter when served
    TEXT_PLAIN = "text/plain"
    TEXT_CSV = "text/csv"                          # RFC 4180
    APPLICATION_RTF = "application/rtf"
    TEXT_RTF = "text/rtf"                          # registered too: what libmagic reports for RTF

    # ---- never a format's type (they run code when opened) -----------------------------------------------------
    XHTML = "application/xhtml+xml"
    JAVASCRIPT = "text/javascript"                 # RFC 9239
    APPLICATION_JAVASCRIPT = "application/javascript"   # obsoleted by RFC 9239, still sent
    X_MSDOWNLOAD = "application/x-msdownload"      # not registered: Windows executables
    X_SH = "application/x-sh"                      # not registered: shell scripts
    JAVA_ARCHIVE = "application/java-archive"

    def __str__(self) -> str:
        return self.value


class TopLevelType(str, Enum):
    """A top-level type used alone (RFC 6838 section 4.2): the family of a media type ("image" of image/png), as
    utils/content_type.py parses it; the family rule of services/files/formats.py and the text rule of
    services/files/sniff.py compare with these"""

    APPLICATION = "application"
    AUDIO = "audio"
    IMAGE = "image"
    TEXT = "text"
    VIDEO = "video"

    def __str__(self) -> str:
        return self.value


class StreamType(str, Enum):
    """The type of one stream inside a container, as ffprobe reports it (codec_type); services/media/probe.py reads
    the first stream of each"""

    VIDEO = "video"
    AUDIO = "audio"

    def __str__(self) -> str:
        return self.value


class Disposition(str, Enum):
    """How a served file is presented (Content-Disposition type, RFC 6266 section 4.2); services/files/serve.py
    builds the header"""

    ATTACHMENT = "attachment"   # saved as a download
    INLINE = "inline"           # shown or played in the page

    def __str__(self) -> str:
        return self.value

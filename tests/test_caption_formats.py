"""Caption formats beyond SRT and WebVTT: TTML, SBV and styled ASS (the burned-in looks)."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
from kauli import caption_formats as cf  # noqa: E402


def _job():
    segs = [SimpleNamespace(final_text="Hello there, my friend.", source_final_text="Habari rafiki yangu.", start_ms=1000, end_ms=3500, speaker_id=None),
            SimpleNamespace(final_text="Tom & Jerry <3 {fun}", source_final_text="Tom na Jerry", start_ms=4000, end_ms=6000, speaker_id=None)]
    return SimpleNamespace(segments=segs)


def test_ttml_is_valid_xml_with_escaped_text_and_the_right_times():
    xml = cf.to_ttml(_job(), lang="en")
    root = ET.fromstring(xml)                                                                  # well-formed
    ps = root.findall(".//{http://www.w3.org/ns/ttml}p")
    assert len(ps) == 2 and ps[0].get("begin") == "00:00:01.000" and ps[0].get("end").startswith("00:00:0")
    assert "Tom &amp; Jerry &lt;3" in xml and root.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"
    assert "Habari rafiki" in cf.to_ttml(_job(), lang="sw", source=True)


def test_sbv_uses_youtubes_timestamp_form():
    sbv = cf.to_sbv(_job())
    first = sbv.split("\n")[0]
    assert first.startswith("0:00:01.000,0:00:0") and "Hello there, my friend." in sbv and sbv.count(",0:00:") >= 2


def test_every_look_is_a_complete_ass_file_sized_to_the_video():
    for style in cf.STYLES:
        ass = cf.to_ass(_job(), style, width=1920, height=1080)
        assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass and ass.count("Dialogue:") == 2 and "[V4+ Styles]" in ass
        assert "{fun}" not in ass and "(fun)" in ass                                           # braces from the text cannot become effects
    assert cf.to_ass(_job(), "nonsense").count("Style: Default") == 1                          # an unknown look falls back to classic


def test_the_social_look_lights_words_up_one_by_one_across_the_caption():
    ass = cf.to_ass(_job(), "social", width=1080, height=1920)
    line = [ln for ln in ass.split("\n") if ln.startswith("Dialogue:")][0]
    assert line.count(r"{\k") == 4 and r"{\fad(80,60)}" in line                                # four words, four highlights
    cs = [int(x) for x in __import__("re").findall(r"\\k(\d+)", line)]
    assert all(c >= 1 for c in cs) and 0.7 * 250 <= sum(cs) <= 250                             # spread over most of the caption, not past it
    assert "&H0000E5FF" in ass and ",-1," in ass                                               # highlight colour, bold
    assert r"{\k" not in cf.to_ass(_job(), "classic") and r"{\k" not in cf.to_ass(_job(), "box")


def test_a_vertical_video_gets_readable_text():
    land = [ln for ln in cf.to_ass(_job(), "social", width=1920, height=1080).split("\n") if ln.startswith("Style:")][0]
    port = [ln for ln in cf.to_ass(_job(), "social", width=1080, height=1920).split("\n") if ln.startswith("Style:")][0]
    size = lambda s: int(s.split(",")[2])        # noqa: E731
    assert size(land) == round(1080 * 0.075) and size(port) == round(1080 * 0.075)             # sized by the short side

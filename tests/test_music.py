"""Phase 6: the music library, the importer for your Downloads folder, the track picker, Claude's mood request and the mix."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from test_formats import LAYERS, WORDS, FakeClaude, FakeJev, good_raw, needs_ffmpeg, probe

from villomillo import config, formats
from villomillo.db import Database
from villomillo.formats import blueprints, designs, music, render, text

LICENCE = "YouTube Audio Library License: no attribution is required."


def tone(path, seconds=40, freq=220):
    codec = "libmp3lame" if path.suffix == ".mp3" else "pcm_s16le"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-c:a", codec, str(path)], check=True, timeout=120)
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "FOLDER", tmp_path / "library")
    return Database(tmp_path / "m.db")


def add(db, tmp_path, name="a", mood="calm", categories=("Podcasts",), seconds=40, freq=220):
    return music.add_track(db, tone(tmp_path / f"{name}.mp3", seconds, freq), title=name, artist="Someone", mood=mood,
                           categories=list(categories), source="YouTube Audio Library", licence=LICENCE)


# --- the shortlist and matching downloaded files to it ---------------------------------------------------------------

def test_the_shortlist_names_every_track_with_a_real_mood_and_category():
    entries = music.shortlist()["tracks"]
    assert len(entries) == 42
    assert len({(e["title"], e["artist"]) for e in entries}) == 42
    assert all(e["mood"] in music.MOODS and set(e["categories"]) <= set(music.CATEGORIES) for e in entries)


@pytest.mark.parametrize(
    ("stem", "title"),
    [("Pulsar - The Grey Room _ Density & Time", "Pulsar"), ("Pulsar - The Grey Room _ Density & Time (1)", "Pulsar"),
     ("Ten - Anno Domini Beats", "Ten"), ("Point - Counterpoint - John Patitucci", "Point - Counterpoint"),
     ("Groove - Dyalla", "Groove"), ("Some other song - Someone", None), ("Ten - Someone Else", None)],
)
def test_a_downloaded_file_is_matched_by_its_title_and_artist(stem, title):
    found = music.match_shortlist(stem, music.shortlist()["tracks"])
    assert (found["title"] if found else None) == title


# --- filing tracks ------------------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_a_track_is_filed_with_its_licence_record_and_stored_once(lib, tmp_path):
    track = add(lib, tmp_path)
    assert track["ext"] == "mp3" and 39 < track["duration_s"] < 41 and track["attribution"] == "none"
    assert track["source"] == "YouTube Audio Library" and track["licence"] == LICENCE and track["added_at"]
    assert music.file_path(track).exists()
    with pytest.raises(music.MusicError, match="already in the library"):
        music.add_track(lib, tmp_path / "a.mp3", title="again", artist="Someone", mood="calm", categories=["Podcasts"],
                        source="x", licence="y")


@needs_ffmpeg
@pytest.mark.parametrize(
    ("change", "message"),
    [({"mood": "grumpy"}, "Pick one of the moods"), ({"categories": []}, "at least one category"),
     ({"categories": ["Weddings"]}, "at least one category"), ({"licence": " "}, "licence record"),
     ({"source": ""}, "licence record"), ({"title": ""}, "title and an artist")],
)
def test_a_track_without_its_details_is_refused(lib, tmp_path, change, message):
    kwargs = {"title": "t", "artist": "a", "mood": "calm", "categories": ["Podcasts"], "source": "s", "licence": "l", **change}
    with pytest.raises(music.MusicError, match=message):
        music.add_track(lib, tone(tmp_path / "x.mp3"), **kwargs)
    assert music.tracks(lib) == []


@needs_ffmpeg
def test_a_file_that_is_not_music_is_refused_in_plain_words(lib, tmp_path):
    details = {"title": "t", "artist": "a", "mood": "calm", "categories": ["Podcasts"], "source": "s", "licence": "l"}
    (tmp_path / "notes.mp3").write_text("not audio")
    with pytest.raises(music.MusicError, match="not audio the app can read"):
        music.add_track(lib, tmp_path / "notes.mp3", **details)
    shutil.copy(tone(tmp_path / "ok.mp3"), tmp_path / "song.flac")
    with pytest.raises(music.MusicError, match="MP3 or WAV"):
        music.add_track(lib, tmp_path / "song.flac", **details)
    with pytest.raises(music.MusicError, match="between 20 seconds and 15 minutes"):
        music.add_track(lib, tone(tmp_path / "short.mp3", seconds=5), **details)


@needs_ffmpeg
def test_importing_from_downloads_files_only_the_shortlisted_tracks(lib, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    tone(downloads / "Pulsar - The Grey Room _ Density & Time.mp3")
    tone(downloads / "My holiday song.mp3", freq=330)
    (downloads / "readme.txt").write_text("x")
    first = music.import_downloads(lib, downloads)
    assert first["imported"] == ["Pulsar"] and first["problems"] == []
    track = music.tracks(lib)[0]
    assert track["mood"] == "calm" and set(track["categories"]) == {"Podcasts", "Campaigns"} and "no attribution" in track["licence"].lower()
    second = music.import_downloads(lib, downloads)
    assert second["imported"] == [] and len(second["already"]) == 1
    have = {c["title"]: c["have"] for c in music.checklist(lib)}
    assert have["Pulsar"] is True and have["Ten"] is False


# --- choosing a track ---------------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_only_tracks_for_the_clips_category_are_offered_and_a_campaign_adds_its_own(lib, tmp_path):
    add(lib, tmp_path, "pod", "calm", ["Podcasts"], freq=220)
    add(lib, tmp_path, "game", "angry", ["Stream clips"], freq=330)
    add(lib, tmp_path, "camp", "dramatic", ["Campaigns"], freq=440)
    assert music.available_moods(lib, "Podcasts", False) == ["calm"]
    assert music.available_moods(lib, "podcasts", True) == ["calm", "dramatic"]
    assert music.available_moods(lib, "Stream clips", False) == ["angry"]
    assert music.available_moods(lib, "Talks", False) == []   # nothing filed for it: the AI is never asked about music


@needs_ffmpeg
def test_the_track_used_least_recently_is_picked_so_clips_do_not_all_carry_one_song(lib, tmp_path):
    one, two = add(lib, tmp_path, "one", freq=220), add(lib, tmp_path, "two", freq=330)
    first = music.pick(lib, "Podcasts", False, "calm")
    music.mark_used(lib, first["id"])
    second = music.pick(lib, "Podcasts", False, "calm")
    assert {first["id"], second["id"]} == {one["id"], two["id"]}
    assert music.pick(lib, "Podcasts", False, "sad") is None


@needs_ffmpeg
def test_a_track_whose_file_is_gone_is_not_offered(lib, tmp_path):
    track = add(lib, tmp_path)
    music.file_path(track).unlink()
    assert music.available_moods(lib, "Podcasts", False) == []


# --- the settings and Claude's request -----------------------------------------------------------------------------------

def test_the_music_setting_defaults_to_only_when_it_helps_and_is_checked():
    assert designs.normalize({})["music"] == {"mode": "when_helps", "volume": -24}
    with pytest.raises(designs.DesignError, match="off, only when it helps, or always"):
        designs.normalize({"music": {"mode": "loud"}})
    with pytest.raises(designs.DesignError, match="music volume must be a whole number from -30 to -16"):
        designs.normalize({"music": {"volume": -5}})


def test_claudes_music_mood_is_kept_only_from_the_moods_the_library_offered():
    raw = good_raw(music={"mood": "Calm", "reason": "a quiet explanation"})
    assert text.check(raw, WORDS, 0.0, ("calm", "sad")).music == ("calm", "a quiet explanation")
    assert text.check(raw, WORDS, 0.0, ("sad",)).music is None              # that mood is not in the library
    assert text.check(raw, WORDS, 0.0, ()).music is None                    # no tracks: nothing is decided about music
    assert text.check(good_raw(music={"mood": "calm", "reason": "x" * 91}), WORDS, 0.0, ("calm",)).music is None
    assert text.check(good_raw(music=None), WORDS, 0.0, ("calm",)).music is None


# --- the whole path through a clip ---------------------------------------------------------------------------------------

@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=500:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True, timeout=120)
    return path


def run_clip(lib, tmp_path, source, raw, template_music=None, category="Podcasts"):
    profile = config.Profile(id="tt", display_name="T", niche={"definition": "x"}, category=category)
    settings = designs.normalize({"music": template_music} if template_music else {})
    template = {"id": "talk", "name": "Talk", "layers": designs.layers_from(settings), "settings": settings}
    return formats.format_clip(lib, profile, template, claude_client=FakeClaude(raw), jev=FakeJev(), video_id="v", start_s=0.2, end_s=7.0,
                               out_path=tmp_path / "out.mp4", episode="E", source=source, words=WORDS, category=category)


@needs_ffmpeg
def test_a_clip_gets_the_track_for_the_mood_claude_asked_for_and_says_why(lib, tmp_path, source):
    track = add(lib, tmp_path)
    made = run_clip(lib, tmp_path, source, good_raw(music={"mood": "calm", "reason": "a quiet explanation"}))
    assert made["music"] == {"mood": "calm", "reason": "a quiet explanation", "track_id": track["id"], "title": "a", "artist": "Someone"}
    assert {s["codec_type"] for s in probe(tmp_path / "out.mp4")} >= {"video", "audio"}
    assert music.get(lib, track["id"])["last_used_at"]


@needs_ffmpeg
def test_an_empty_library_means_no_music_is_decided_and_nothing_fails(lib, tmp_path, source):
    made = run_clip(lib, tmp_path, source, good_raw(music={"mood": "calm", "reason": "a quiet explanation"}))
    assert made["music"] is None and (tmp_path / "out.mp4").exists()


@needs_ffmpeg
def test_a_serious_subject_gets_no_music_even_when_the_template_says_always(lib, tmp_path, source):
    add(lib, tmp_path)
    made = run_clip(lib, tmp_path, source, good_raw(serious=True, music={"mood": "calm", "reason": "a quiet explanation"}),
                    template_music={"mode": "always", "volume": -24})
    assert made["music"] is None


@needs_ffmpeg
def test_off_means_no_music_and_always_means_a_track_when_the_ai_asked_for_none(lib, tmp_path, source):
    add(lib, tmp_path)
    assert run_clip(lib, tmp_path, source, good_raw(), template_music={"mode": "off", "volume": -24})["music"] is None
    assert run_clip(lib, tmp_path, source, good_raw(), template_music={"mode": "when_helps", "volume": -24})["music"] is None
    always = run_clip(lib, tmp_path, source, good_raw(), template_music={"mode": "always", "volume": -24})
    assert always["music"]["mood"] == "calm" and always["music"]["reason"] == "this template always has music"


@needs_ffmpeg
def test_the_mix_keeps_the_clip_the_same_length_and_a_missing_music_file_fails_the_render(source, tmp_path):
    d = designs.normalize({})
    plan = blueprints.resolve("Talk", designs.layers_from(d), {"words", "title", "points", "takeaway"})
    clip_text = render.ClipText(["A TITLE", "SECOND LINE"], [("A POINT", 1.0)], "A TAKEAWAY")
    song = tone(tmp_path / "song.mp3", seconds=30, freq=330)
    job = render.Job(source, 0.2, 7.0, WORDS, clip_text, plan, "@x", tmp_path / "mixed.mp4", design=d, music={"path": song, "volume": -24})
    out = render.render(job, preview=True)
    audio = next(s for s in probe(out) if s["codec_type"] == "audio")
    assert 6.4 < float(audio["duration"]) < 7.2
    job.music = {"path": tmp_path / "gone.mp3", "volume": -24}
    with pytest.raises(render.RenderError, match="music file is missing"):
        render.render(job, preview=True)

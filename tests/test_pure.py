"""Unit tests for logic that needs no media files or binaries."""

import struct

import numpy as np

from videocook import bluray, encoder, intake, naming, sampling
from videocook.scan import ScanResult


def _mpls(items: list[tuple[str, int, int]], marks: list[tuple[int, int]]) -> bytes:
    """Minimal MPLS: header, playlist with play items, entry marks."""
    pl = b""
    for clip, a, b in items:
        body = clip.encode() + b"M2TS" + struct.pack(">H", 1) + b"\x00" + struct.pack(">II", a, b)
        body += b"\x00" * 12
        pl += struct.pack(">H", len(body)) + body
    playlist = struct.pack(">IHHH", 0, 0, len(items), 0) + pl
    mk = b"".join(struct.pack(">BBHIHI", 0, 1, i, t, 0, 0) for i, t in marks)
    marks_blob = struct.pack(">IH", 0, len(marks)) + mk
    pl_start = 20
    mark_start = pl_start + len(playlist)
    header = b"MPLS0200" + struct.pack(">III", pl_start, mark_start, 0)
    return header + playlist + marks_blob


def test_parse_mpls(tmp_path):
    t = bluray.TICKS
    data = _mpls([("00001", 10 * t, 70 * t), ("00002", 0, 30 * t)], [(0, 10 * t), (1, 15 * t)])
    f = tmp_path / "00800.mpls"
    f.write_bytes(data)
    pl = bluray.parse_mpls(f)
    assert pl.clips == ("00001", "00002")
    assert pl.duration == 90
    assert pl.chapters == [0.0, 75.0]


def test_group_and_classify():
    mk = lambda name, clips, dur: bluray.Playlist(  # noqa: E731
        name, [bluray.PlayItem(c, 0, int(dur / len(clips) * bluray.TICKS)) for c in clips], [])
    pls = [mk("1", ["a", "b"], 7000), mk("2", ["a", "b"], 7000), mk("3", ["a", "c"], 6500),
           mk("4", ["x"] * 50, 7300), mk("5", ["z"], 90)]
    groups = bluray.group_playlists(pls)
    bluray.classify_groups(groups)
    roles = {g["playlist"]: g["role"] for g in groups}
    assert roles["4"] == "suspicious-loop"
    assert roles["1"] == roles["3"] == "feature-candidate"
    assert roles["5"] == "extra"
    assert next(g for g in groups if g["playlist"] == "1")["duplicates"] == ["2"]


def test_encoder_merge_overrides_and_negations():
    args = encoder.merge(["--crf", "18", "--sao", "--deblock", "-1:-1"], ["--no-sao", "--deblock", "-2:-2"])
    assert args == ["--crf", "18", "--no-sao", "--deblock", "-2:-2"]


def test_encoder_build_hdr_and_tier():
    hdr = {"hdr10": True, "static": {"master_display": "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)", "max_cll": "1000,400"}}
    colour = {"primaries": "bt2020", "transfer": "smpte2084", "matrix": "bt2020nc"}
    args = encoder.build({"content": "anime", "tier": "archival", "x265_extra": "--psy-rd 2.5"}, colour, hdr, 2160)
    assert args[args.index("--crf") + 1] == "15"
    assert args[args.index("--psy-rd") + 1] == "2.5"
    assert args.count("--psy-rd") == 1
    assert "--hdr10" in args and args[args.index("--transfer") + 1] == "smpte2084"


def test_allocate_budget():
    comp = {b: {"shots": 0, "frames": 0, "share": 0.0} for b in sampling.BUCKETS}
    comp["dark_flat"] = {"shots": 20, "frames": 300, "share": 0.3}
    comp["normal"] = {"shots": 50, "frames": 600, "share": 0.6}
    comp["static"] = {"shots": 2, "frames": 100, "share": 0.1}
    comp["blank"] = {"shots": 3, "frames": 10, "share": 0.01}
    alloc = sampling.allocate(comp, 24)
    assert "blank" not in alloc
    assert alloc["static"] >= 1
    assert alloc["dark_flat"] > alloc["static"]
    assert sum(alloc.values()) <= 24


def test_assess_synthetic_scan():
    n = 2400
    rng = np.random.default_rng(0)
    luma = np.where(np.arange(n) < 1200, 0.12, 0.5).astype(np.float32)
    data = {
        "luma": luma, "luma_min": luma - 0.05, "luma_max": luma + 0.3,
        "flat": np.full(n, 0.5, np.float32),
        "dark_flat": np.where(np.arange(n) < 1200, 0.6, 0.0).astype(np.float32),
        "edge": rng.random(n).astype(np.float32) * 0.1,
        "noise": rng.random(n).astype(np.float32),
        "oor": np.zeros(n, np.float32),
        "motion": rng.random(n).astype(np.float32) * 0.05,
        "cambi": np.where(np.arange(n) < 1200, 3.0, 0.5).astype(np.float32),
        "scene": (np.arange(n) % 120 == 0).astype(np.float32),
    }
    res = sampling.assess(ScanResult(24.0, n, 1920, 1080, data), budget=8)
    assert res["composition"]["dark_flat"]["share"] > 0.4
    assert 1 <= len(res["picks"]) <= 8 + 2  # up to two CAMBI hotspots on top of the budget
    assert any(p["role"] == "cambi-hotspot" for p in res["picks"])
    assert any(p["bucket"] == "dark_flat" for p in res["picks"])
    shots = {tuple(p["shot"]) for p in res["picks"]}
    assert len(shots) == len(res["picks"])  # at most one frame per shot


def test_episode_ids_and_languages():
    assert intake.episode_id("Show S02E05 1080p.mkv", 1) == "S02E05"
    assert intake.episode_id("[VCB-Studio] Show [07][Ma10p_1080p].mkv", 1) == "S01E07"
    assert intake.episode_id("random.mkv", 3) == "S01E03"
    assert intake.guess_language(["sc_jp"]) == ("chi", "简体中文 & 日本語")
    assert intake.guess_language(["tc"]) == ("chi", "繁體中文")
    assert intake.guess_language([]) == ("und", "")


def test_naming_helpers():
    assert naming.resolution_label(2160, 3840) == "2160p"
    assert naming.resolution_label(800, 1920) == "1080p"  # cropped scope 1080p
    assert naming.resolution_label(720, 1280) == "720p"
    assert naming.safe('House M.D.: "Pilot"') == "House M.D.   Pilot"


def test_detect_scenes_spikes():
    from videocook.scan import detect_scenes

    motion = np.full(100, 0.01, np.float32)
    motion[[30, 70]] = 0.3
    assert list(np.nonzero(detect_scenes(motion))[0]) == [30, 70]

"""vspipe → x265 encode wrapper: failure detection, retries and failure records."""

import json

import pytest

from videocook import pipe
from videocook.toolchain import ToolMissing, tool


def test_encoded_frames():
    out = "x265 [info]: frame I: 1\nencoded 24 frames in 0.51s (47.06 fps), 12.3 kb/s, Avg QP:30.1"
    assert pipe.encoded_frames(out) == 24
    assert pipe.encoded_frames("x265 [error]: unable to open input file") is None


def test_error_line_prefers_errors_over_summary():
    vspipe = ("Plugin x.dll uses API 3, which is no longer supported.\n"
              "Error: fwrite() call failed when writing video plane 0, errno: 22\n"
              "Output 6 frames in 2.70 seconds (2.22 fps)\n")
    assert pipe.error_line(vspipe).startswith("Error: fwrite()")
    assert pipe.error_line("a\nlast\n") == "last"
    assert pipe.error_line("") == ""


def test_classify():
    assert pipe.classify(0, 0, 24, 24) is None
    assert pipe.classify(0, 0, 10, 24) == "short_encode"   # the errno 22 case when vspipe still exits 0
    assert pipe.classify(0, 0, None, 24) == "short_encode"
    assert pipe.classify(1, 0, 6, 24) == "vspipe_error"
    assert pipe.classify(0, 1, None, 24) == "x265_error"


SCRIPT = """
import vapoursynth as vs
core = vs.core
clip = core.std.BlankClip(width=64, height=64, format=vs.YUV420P10, length=24, fpsnum=24, fpsden=1)
{body}
clip.set_output()
"""

FAST = ["--preset", "ultrafast", "--output-depth", "10"]


@pytest.fixture
def tools_present():
    try:
        tool("vspipe"), tool("x265")
    except ToolMissing:
        pytest.skip("vspipe/x265 not installed (run bootstrap)")


def test_encode_ok_with_inclusive_range(tmp_path, tools_present):
    vpy = tmp_path / "ok.vpy"
    vpy.write_text(SCRIPT.format(body=""), encoding="utf-8")
    res = pipe.encode(vpy, FAST, tmp_path / "ok.hevc", tmp_path / "ok.log", 6, frame_range=(10, 15))
    assert res["attempts"] == 1 and res["encoded"] == 6
    assert not (tmp_path / pipe.FAILURE_LOG).exists()


def test_short_encode_is_retried_and_recorded(tmp_path, tools_present):
    vpy = tmp_path / "short.vpy"
    vpy.write_text(SCRIPT.format(body=""), encoding="utf-8")
    retries = []
    with pytest.raises(pipe.EncodeError, match="short_encode"):
        pipe.encode(vpy, FAST, tmp_path / "s.hevc", tmp_path / "s.log", 30, attempts=2, on_retry=retries.append)
    assert len(retries) == 1
    assert (tmp_path / "s_try2.log").exists() and not (tmp_path / "s.hevc").exists()
    recs = [json.loads(line) for line in (tmp_path / pipe.FAILURE_LOG).read_text(encoding="utf-8").splitlines()]
    assert [r["attempt"] for r in recs] == [1, 2]
    assert recs[0]["encoded"] == 24 and recs[0]["vspipe_rc"] == 0 and recs[0]["x265_rc"] == 0
    assert recs[0]["memory"]["phys_total_gib"] > 0


def test_vspipe_error_mid_stream(tmp_path, tools_present):
    body = ("def boom(n, f):\n    if n == 5:\n        raise RuntimeError('boom at 5')\n    return f\n"
            "clip = core.std.ModifyFrame(clip, clip, boom)")
    vpy = tmp_path / "boom.vpy"
    vpy.write_text(SCRIPT.format(body=body), encoding="utf-8")
    (tmp_path / "logs").mkdir()
    with pytest.raises(pipe.EncodeError, match="vspipe_error"):
        pipe.encode(vpy, FAST, tmp_path / "b.hevc", tmp_path / "b.log", 24, attempts=1,
                    failure_log=tmp_path / "logs" / "f.jsonl")
    rec = json.loads((tmp_path / "logs" / "f.jsonl").read_text(encoding="utf-8"))
    assert rec["vspipe_rc"] != 0 and "boom at 5" in rec["vspipe_stderr"]
    assert rec["vspipe_exit_s"] is not None and rec["x265_exit_s"] is not None

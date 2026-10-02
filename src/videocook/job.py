"""Job directory and job.toml — the single source of truth for an encode (design §17).

Layout of <workspace>/jobs/<name>/:
    job.toml          decisions (written by the skill / user, executed by vcook)
    probe.json/.md    source facts
    assess/           scan, report, sampled images (per episode under assess/<ep>/)
    cache/            lsmas indexes
    scripts/          generated .vpy
    trial/            filter triptychs, test clips
    work/             intermediates (removed after delivery)
    out/              deliverables before copy to the NAS
    logs/, state.json run state
"""

from __future__ import annotations

import copy
import datetime as dt
import re
import tomllib
from pathlib import Path

import tomli_w

from videocook.config import load_settings

DEFAULTS: dict = {
    "job": {"name": "", "title": "", "year": 0, "type": "film", "created": "",
            "derived_from": "", "status": "new"},
    "source": {},
    "episodes": [],
    "video": {
        "mode": "encode",            # encode | passthrough
        "content": "live_action",    # anime | live_action | webrip
        "tier": "standard",          # archival | standard | compact
        "crf": 0.0,                  # 0 = use the tier
        "x265_extra": "",
        "filters": {},               # see filters.standard_chain
    },
    "audio": {"tracks": []},         # [{index, action, language, title, default, bitrate}]
    "subtitles": {"tracks": [], "font_dirs": []},
    "chapters": {"mode": "source", "rename": True, "language": "eng", "keyframes": True},
    "output": {"nas_dir": "", "resolution_label": "", "audio_label": ""},
    "run": {"parallel": 0, "trim": []},
    "trial": {"clips": 4, "clip_seconds": 10},
}


def slugify(text: str) -> str:
    text = re.sub(r"[\[\(].*?[\]\)]", " ", text)
    text = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-")
    return re.sub(r"-{2,}", "-", text)[:60] or "job"


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _clean(obj):
    """tomli-w cannot write None; drop None values recursively."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_clean(v) for v in obj if v is not None]
    return obj


class Job:
    def __init__(self, path: Path):
        self.dir = Path(path)
        self.toml = self.dir / "job.toml"
        self.data = deep_merge(DEFAULTS, tomllib.loads(self.toml.read_text(encoding="utf-8")))

    # -- persistence --------------------------------------------------------------
    def save(self) -> None:
        self.toml.write_text(tomli_w.dumps(_clean(self.data)), encoding="utf-8")

    @classmethod
    def resolve(cls, ref: str | Path) -> "Job":
        p = Path(ref)
        if not (p / "job.toml").exists():
            p = load_settings().jobs_dir / str(ref)
        if not (p / "job.toml").exists():
            raise FileNotFoundError(f"no job at {ref}")
        return cls(p)

    @classmethod
    def create(cls, name: str, initial: dict) -> "Job":
        d = load_settings().jobs_dir / name
        if (d / "job.toml").exists():
            raise FileExistsError(f"job {name} already exists at {d}")
        d.mkdir(parents=True, exist_ok=True)
        data = deep_merge(DEFAULTS, initial)
        data["job"]["name"] = name
        data["job"]["created"] = dt.datetime.now().isoformat(timespec="seconds")
        (d / "job.toml").write_text(tomli_w.dumps(_clean(data)), encoding="utf-8")
        return cls(d)

    # -- convenience ----------------------------------------------------------------
    def sub(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cache(self) -> Path:
        return self.sub("cache")

    @property
    def is_series(self) -> bool:
        return self.data["job"]["type"] == "series"

    def units(self) -> list[dict]:
        """Encode units: one per episode for a series, otherwise the single film.

        Each unit: {"id", "source" (vsource spec), "kind" ("main"|"ncop"|"nced"|...)}.
        """
        if self.is_series:
            return [dict(e) for e in self.data["episodes"] if not e.get("skip")]
        return [{"id": "film", "source": self.data["source"], "kind": "main"}]

    def unit(self, unit_id: str) -> dict:
        for u in self.units():
            if u["id"] == unit_id:
                return u
        raise KeyError(f"unknown unit {unit_id!r}")

    def probe(self) -> dict:
        import json

        return json.loads((self.dir / "probe.json").read_text(encoding="utf-8"))

    def stream_facts(self) -> dict:
        """Colour/HDR/size facts of the main stream from probe.json."""
        ms = self.probe()["main_stream"]
        return {"colour": {**ms["hdr"], "chroma_location": ms["video"].get("chroma_location")},
                "hdr": ms["hdr"], "width": ms["video"]["width"], "height": ms["video"]["height"],
                "fps": ms["video"]["fps"]}

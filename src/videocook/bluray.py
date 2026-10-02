"""Blu-ray structure: MPLS playlist parsing and main-feature candidates.

Only the fields needed for choosing playlists are parsed: play items (clips,
in/out times, angles) and entry marks (chapters). Stream details are read
later with mkvmerge/ffprobe on the chosen playlist.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

TICKS = 45000  # MPLS timestamps are in 45 kHz ticks


@dataclass
class PlayItem:
    clip: str
    in_time: int
    out_time: int
    angles: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return (self.out_time - self.in_time) / TICKS


@dataclass
class Playlist:
    name: str
    items: list[PlayItem]
    chapters: list[float]  # seconds from playlist start

    @property
    def duration(self) -> float:
        return sum(i.duration for i in self.items)

    @property
    def clips(self) -> tuple[str, ...]:
        return tuple(i.clip for i in self.items)

    @property
    def signature(self) -> tuple:
        return tuple((i.clip, i.in_time, i.out_time) for i in self.items)


def parse_mpls(path: Path) -> Playlist:
    data = path.read_bytes()
    if data[:4] != b"MPLS":
        raise ValueError(f"{path} is not an MPLS file")
    pl_start, mark_start = struct.unpack_from(">II", data, 8)

    n_items = struct.unpack_from(">H", data, pl_start + 6)[0]
    pos = pl_start + 10
    items: list[PlayItem] = []
    for _ in range(n_items):
        length = struct.unpack_from(">H", data, pos)[0]
        body = pos + 2
        clip = data[body : body + 5].decode("ascii")
        flags = struct.unpack_from(">H", data, body + 9)[0]
        multi_angle = bool(flags & 0x10)
        in_time, out_time = struct.unpack_from(">II", data, body + 12)
        item = PlayItem(clip, in_time, out_time)
        if multi_angle:
            # after UO mask(8), flags(1), still mode(1), still time(2)
            a = body + 20 + 8 + 4
            n_angles = data[a]
            off = a + 2
            for _ in range(n_angles - 1):
                item.angles.append(data[off : off + 5].decode("ascii"))
                off += 10
        items.append(item)
        pos = body + length

    # Entry marks -> chapter times relative to the playlist start.
    n_marks = struct.unpack_from(">H", data, mark_start + 4)[0]
    starts: list[float] = []
    acc = 0.0
    for it in items:
        starts.append(acc)
        acc += it.duration
    chapters: list[float] = []
    for k in range(n_marks):
        m = mark_start + 6 + 14 * k
        mark_type = data[m + 1]
        item_id, ts = struct.unpack_from(">HI", data, m + 2)
        if mark_type == 1 and item_id < len(items):
            chapters.append(round(starts[item_id] + (ts - items[item_id].in_time) / TICKS, 3))
    return Playlist(path.stem, items, sorted(set(chapters)))


def find_bdmv_root(path: Path) -> Path | None:
    """Return the directory that contains BDMV/, or None."""
    for cand in (path, *path.parents):
        if (cand / "BDMV" / "index.bdmv").exists():
            return cand
    if path.is_dir():
        hits = list(path.glob("*/BDMV/index.bdmv"))
        if len(hits) == 1:
            return hits[0].parent.parent
    return None


def scan_playlists(root: Path, min_seconds: float = 30) -> list[Playlist]:
    out: list[Playlist] = []
    for f in sorted((root / "BDMV" / "PLAYLIST").glob("*.mpls")):
        try:
            pl = parse_mpls(f)
        except (ValueError, struct.error):
            continue
        if pl.duration >= min_seconds:
            out.append(pl)
    return out


def group_playlists(playlists: list[Playlist]) -> list[dict]:
    """Collapse identical playlists (obfuscated discs carry hundreds of copies).

    Returns groups sorted by duration (longest first), each with the
    representative playlist, its duplicates and how many distinct clips it uses.
    """
    groups: dict[tuple, list[Playlist]] = {}
    for pl in playlists:
        groups.setdefault(pl.signature, []).append(pl)
    result = []
    for members in groups.values():
        rep = members[0]
        result.append(
            {
                "playlist": rep.name,
                "duplicates": [m.name for m in members[1:]],
                "duration": round(rep.duration, 3),
                "items": len(rep.items),
                "distinct_clips": len(set(rep.clips)),
                "clips": list(rep.clips),
                "chapters": len(rep.chapters),
                "angles": any(i.angles for i in rep.items),
            }
        )
    result.sort(key=lambda g: g["duration"], reverse=True)
    return result


def classify_groups(groups: list[dict]) -> None:
    """Annotate groups with a coarse role: feature / episode / extra / short."""
    if not groups:
        return
    real = [g for g in groups if not (g["items"] >= 10 and g["distinct_clips"] <= 2)]
    longest = (real or groups)[0]["duration"]
    for g in groups:
        d = g["duration"]
        if g["items"] >= 10 and g["distinct_clips"] <= 2:
            g["role"] = "suspicious-loop"
        elif d >= 0.75 * longest and d >= 40 * 60:
            g["role"] = "feature-candidate"
        elif 15 * 60 <= d < 40 * 60:
            g["role"] = "episode-candidate"
        elif 60 <= d < 15 * 60:
            g["role"] = "extra"
        else:
            g["role"] = "short"
    # Feature candidates that share most clips are alternate cuts (seamless branching).
    feats = [g for g in groups if g["role"] == "feature-candidate"]
    for g in feats:
        others = [o for o in feats if o is not g]
        overlap = max(
            (len(set(g["clips"]) & set(o["clips"])) / max(1, len(set(g["clips"]))) for o in others),
            default=0,
        )
        g["shares_clips_with_other_feature"] = round(overlap, 2)

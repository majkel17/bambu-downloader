"""Model files are named <Design_title>__<Profile_name>.<ext> so they sort
and read sensibly in file managers and Printventory."""

from __future__ import annotations

import pytest

# Classes and helpers via the module at call time — see test_skip_failed.py.
import app.downloader as dl


@pytest.mark.parametrize(
    ("title", "profile", "expected"),
    [
        (
            "Mini Ghost Tea Light Lantern",
            "Ghost + Stand (No AMS)",
            "Mini_Ghost_Tea_Light_Lantern__Ghost_Stand_No_AMS",
        ),
        ("Grumpy Candle", "0.2mm layer, 6 walls", "Grumpy_Candle__0.2mm_layer_6_walls"),
        # Old files: the current stem (Bambu's sanitized profile name).
        (
            "Grumpy Candle",
            "0.2mm_layer__6_walls__",
            "Grumpy_Candle__0.2mm_layer_6_walls",
        ),
        ("Grumpy Candle", "plate.3mf", "Grumpy_Candle__plate"),  # extension dropped
        ("Lantern", "多色一盘打印", "Lantern"),  # nothing ASCII left: title only
        ("Lantern", None, "Lantern"),
        ("Lantern", "Lantern", "Lantern"),  # no Lantern__Lantern
        ("???", "x", "model__x"),
        ("../../etc/passwd", "a/b", "etc_passwd__a_b"),  # no path tricks
    ],
)
def test_model_filename(title, profile, expected):
    assert dl._model_filename(title, profile) == expected


def test_model_filename_is_bounded():
    name = dl._model_filename("t" * 300, "p" * 300)
    assert name == "t" * 80 + "__" + "p" * 60


def _row(db, root, design_id, title, filename, profile_title=None, pid=None):
    path = root / f"{design_id}-x" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    db.insert_model(
        design_id=design_id,
        profile_id=pid,
        title=title,
        slug="s",
        url="u",
        filename=filename,
        file_path=str(path),
        file_size=1,
        profile_title=profile_title,
    )
    return path


def test_rename_migration(db, tmp_path):
    old1 = _row(db, tmp_path, 1, "Grumpy Candle", "0.2mm_layer__6_walls__.3mf")
    old2 = _row(
        db,
        tmp_path,
        2,
        "Lantern",
        "Ghost___Stand__No_AMS_.3mf",
        "Ghost + Stand (No AMS)",
        5,
    )
    done = _row(db, tmp_path, 3, "Cube", "Cube__Default.3mf", "Default", 6)
    m = dl.DownloadManager(db)
    assert m.rename_model_files() == 2
    by_id = {r["design_id"]: r for r in db.list_models()}
    assert by_id[1]["filename"] == "Grumpy_Candle__0.2mm_layer_6_walls.3mf"
    assert by_id[2]["filename"] == "Lantern__Ghost_Stand_No_AMS.3mf"
    assert by_id[3]["filename"] == "Cube__Default.3mf"
    for r in by_id.values():
        assert r["file_path"].endswith(r["filename"])
        assert (tmp_path / r["file_path"]).is_file()
    assert not old1.exists() and not old2.exists() and done.exists()
    assert db.get_meta("file_names_v3") == "1"
    assert m.rename_model_files() == 0  # one-time


def test_rename_never_clobbers(db, tmp_path):
    """Two profiles whose names collapse to the same ASCII name."""
    a = _row(db, tmp_path, 1, "Lantern", "a.3mf", "多色", 5)
    _row(db, tmp_path, 1, "Lantern", "b.3mf", "彩色", 6)
    m = dl.DownloadManager(db)
    assert m.rename_model_files() == 2
    names = {r["filename"] for r in db.list_models()}
    assert "Lantern.3mf" in names
    assert len(names) == 2  # the other one deduped as Lantern-<row id>
    assert not a.exists()


def test_failed_rename_is_retried_next_boot(db, tmp_path, monkeypatch):
    _row(db, tmp_path, 1, "Grumpy Candle", "old.3mf")
    m = dl.DownloadManager(db)

    def boom(self, target):
        raise OSError("read-only file system")

    monkeypatch.setattr(dl.Path, "rename", boom)
    assert m.rename_model_files() == 0
    assert db.get_meta("file_names_v3") is None


# ------------------------------------------------------- transliteration
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("16cm großer Pokal", "16cm_grosser_Pokal"),
        ("Łódź Żółć", "Lodz_Zolc"),
        ("Crème brûlée", "Creme_brulee"),
        ("Smørrebrød Æble Œuvre", "Smorrebrod_AEble_OEuvre"),
        ("Ñandú Çağrı", "Nandu_Cagri"),  # noqa: RUF001 — Turkish dotless i
        ("Ёлка 多色", "model"),  # other scripts are dropped, as before
    ],
)
def test_latin_letters_are_folded(title, expected):
    assert dl._model_filename(title, None) == expected


def test_v2_names_get_folded_once(db, tmp_path):
    """Files named by the first scheme (accents dropped, no stored profile
    name) keep their profile part — no Title__Title__profile."""
    _row(db, tmp_path, 1, "16cm großer Pokal", "16cm_gro_er_Pokal__Kelch_0.2mm.3mf")
    _row(db, tmp_path, 2, "Grumpy Candle", "Grumpy_Candle__0.2mm_layer.3mf")
    _row(db, tmp_path, 3, "Żaba", "aba-7.3mf")
    db.set_meta("file_names_v2", "1")
    assert dl.DownloadManager(db).rename_model_files() == 2
    names = {r["design_id"]: r["filename"] for r in db.list_models()}
    assert names == {
        1: "16cm_grosser_Pokal__Kelch_0.2mm.3mf",
        2: "Grumpy_Candle__0.2mm_layer.3mf",  # untouched
        3: "Zaba.3mf",
    }

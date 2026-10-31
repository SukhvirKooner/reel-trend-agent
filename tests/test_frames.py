from app import frames as fr
from tests.conftest import noise, solid, write_frames


def test_frame_time_and_order(isolated):
    paths = write_frames(isolated / "f", [solid("red")] * 3, [12.0, 0.0, 2.5])
    listed = fr.list_frames(isolated / "f")
    assert [fr.frame_time(p) for p in listed] == [0.0, 2.5, 12.0]
    assert len(paths) == 3


def test_static_video_has_no_cuts(isolated):
    paths = write_frames(isolated / "s", [solid((120, 60, 30))] * 10, range(10))
    assert fr.cuts_per_10s(paths) == 0


def test_alternating_scenes_count_as_cuts(isolated):
    imgs = [solid("black") if i % 2 == 0 else solid("white") for i in range(11)]
    paths = write_frames(isolated / "a", imgs, range(11))
    assert fr.cuts_per_10s(paths) == 10.0            # a cut every second over 10 s


def test_one_cut(isolated):
    imgs = [solid("black")] * 5 + [solid("white")] * 6
    paths = write_frames(isolated / "o", imgs, range(11))
    assert fr.cuts_per_10s(paths) == 1.0


def test_too_few_frames(isolated):
    assert fr.cuts_per_10s(write_frames(isolated / "x", [solid("red")], [0])) is None


def test_phash_similarity(isolated):
    a = write_frames(isolated / "p1", [noise(1)], [0])[0]
    b = write_frames(isolated / "p2", [noise(1)], [0])[0]
    c = write_frames(isolated / "p3", [noise(99)], [0])[0]
    assert fr.phash_distance(fr.phash(a), fr.phash(b)) <= 6
    assert fr.phash_distance(fr.phash(a), fr.phash(c)) > 6

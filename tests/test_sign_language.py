"""Fingerspelling: features, recognition by comparison with taught signs, typing."""

import math
import random

from handgen import LM, make_hand
from sign_language import SignBook, SignTyper, sign_features


def jitter(hand, rng, amount=0.004):
    return [LM(p.x + rng.gauss(0, amount), p.y + rng.gauss(0, amount), p.z + rng.gauss(0, amount / 2)) for p in hand]


# A handful of clearly different handshapes, standing in for letters.
POSES = {
    "B": dict(extended=("index", "middle", "ring", "pinky")),
    "D": dict(extended=("index",), pinch=0.9),
    "U": dict(extended=("index", "middle")),
    "W": dict(extended=("index", "middle", "ring")),
    "I": dict(extended=("pinky",)),
    "A": dict(extended=()),
}


def examples(sign, rng, n=30, **kw):
    pose = dict(POSES[sign])
    pose.update(kw)
    return [sign_features(jitter(make_hand(**pose), rng), 16 / 9) for _ in range(n)]


def taught_book(rng):
    book = SignBook()
    for sign in POSES:
        book.teach(sign, examples(sign, rng))
    return book


def test_features_ignore_where_the_hand_is_and_how_big_it_looks():
    a = sign_features(make_hand(("index",), center=(0.3, 0.4), size=0.10), 16 / 9)
    b = sign_features(make_hand(("index",), center=(0.7, 0.6), size=0.20), 16 / 9)
    assert max(abs(x - y) for x, y in zip(a, b)) < 1e-6


def test_features_keep_which_way_the_hand_points():
    up = sign_features(make_hand(("index", "middle")), 16 / 9)
    down = sign_features(make_hand(("index", "middle"), rotate=180), 16 / 9)    # like K vs P
    assert math.dist(up, down) > 2.0


def test_recognises_each_taught_sign_from_fresh_examples():
    rng = random.Random(3)
    book = taught_book(rng)
    for sign in POSES:
        for f in examples(sign, rng, n=10):
            got, conf = book.recognise(f)
            assert got == sign, (sign, got)
            assert conf > 0.2


def test_tilting_the_hand_a_little_still_recognises_it():
    rng = random.Random(4)
    book = taught_book(rng)
    for angle in (-15, 15):
        got, _ = book.recognise(sign_features(make_hand(("index", "middle", "ring"), rotate=angle), 16 / 9))
        assert got == "W"


def test_refuses_to_guess_at_a_hand_it_was_never_shown():
    rng = random.Random(5)
    book = taught_book(rng)
    upside_down_pinky = sign_features(make_hand(("pinky",), rotate=170), 16 / 9)
    assert book.recognise(upside_down_pinky) == (None, 0.0)
    assert SignBook().recognise(upside_down_pinky) == (None, 0.0)


def test_round_trips_through_json_and_rejects_garbage():
    rng = random.Random(6)
    book = taught_book(rng)
    again = SignBook.from_json(book.to_json())
    assert again.taught == book.taught
    f = examples("U", rng, n=1)[0]
    assert again.recognise(f)[0] == "U"
    assert SignBook.from_json("nonsense").taught == []
    assert SignBook.from_json('{"signs": {"NOT_A_SIGN": [[1, 2]], "A": "x"}}').taught == []


def test_typer_types_a_held_sign_once_and_again_only_after_letting_go():
    t = SignTyper(hold_seconds=0.5, release_seconds=0.2)
    out = [t.update("L", 0.8, i / 30) for i in range(40)]           # held 1.3 s
    assert out.count("L") == 1
    out = [t.update(None, 0.0, 1.4 + i / 30) for i in range(10)]    # relax 0.33 s
    out += [t.update("L", 0.8, 1.8 + i / 30) for i in range(20)]
    assert out.count("L") == 1                                       # "ll"


def test_typer_ignores_flickers_and_low_confidence():
    t = SignTyper(hold_seconds=0.5)
    flicker = ["A", "S", "A", "S"] * 10
    assert all(t.update(s, 0.9, i / 30) is None for i, s in enumerate(flicker))
    t.reset()
    assert all(t.update("A", 0.05, i / 30) is None for i in range(40))

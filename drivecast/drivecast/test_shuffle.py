"""Pins the seeded-shuffle algorithm to shared cross-language test vectors.

These vectors MUST match SeededShuffleTest.kt on the Fire TV app side — both
sides implement the identical SplitMix64-driven Fisher-Yates, so computing the
vectors once here and reusing the same constants in Kotlin guarantees parity.
"""
from drivecast.shuffle import seeded_shuffle


def test_shuffle_vector_ints():
    result = seeded_shuffle(list(range(10)), 123456789)
    print("vector(range(10), 123456789) ->", result)
    assert result == [8, 3, 9, 2, 4, 6, 1, 5, 0, 7]


def test_shuffle_vector_letters():
    result = seeded_shuffle(list("abcdef"), 42)
    print("vector(abcdef, 42) ->", result)
    assert result == ["e", "d", "a", "c", "f", "b"]


def test_shuffle_vector_small():
    result = seeded_shuffle(list(range(5)), 0)
    print("vector(range(5), 0) ->", result)
    assert result == [2, 3, 1, 4, 0]


def test_shuffle_deterministic_same_seed():
    items = list(range(20))
    assert seeded_shuffle(items, 999) == seeded_shuffle(items, 999)


def test_shuffle_does_not_mutate_input():
    items = list(range(10))
    original = list(items)
    seeded_shuffle(items, 55)
    assert items == original

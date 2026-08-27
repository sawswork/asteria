from engine.rng import Rng


def test_same_seed_same_sequence():
    a = Rng(seed=42)
    b = Rng(seed=42)
    assert [a.uniform(0, 1) for _ in range(20)] == [b.uniform(0, 1) for _ in range(20)]


def test_counter_resume_reproduces_tail():
    a = Rng(seed=7)
    head = [a.uniform(0, 1) for _ in range(5)]
    resumed = Rng(seed=7, counter=a.counter)
    tail_a = [a.uniform(0, 1) for _ in range(5)]
    tail_b = [resumed.uniform(0, 1) for _ in range(5)]
    assert tail_a == tail_b
    assert head != tail_a


def test_randint_bounds():
    r = Rng(seed=1)
    values = [r.randint(1, 3) for _ in range(200)]
    assert set(values) == {1, 2, 3}


def test_choice_deterministic():
    assert Rng(seed=5).choice(["a", "b", "c"]) == Rng(seed=5).choice(["a", "b", "c"])

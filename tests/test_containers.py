from truckbot.containers import (ResultsStore, group_by_tower,
                                 load_containers)


def make_list(tmp_path, text):
    p = tmp_path / "containers.csv"
    p.write_text(text)
    return p


def test_resume_skips_terminal_rows(tmp_path):
    res = ResultsStore(tmp_path / "results.csv")
    res.record("AAAA1111111", "BOOKED", "tower 109")
    res.record("BBBB2222222", "RETRY", "no openings")   # not terminal
    # reload from disk like a restart
    res2 = ResultsStore(tmp_path / "results.csv")
    assert res2.done_set() == {"AAAA1111111"}
    lst = make_list(tmp_path, "container,tower\n"
                    "AAAA1111111,109\nBBBB2222222,202\n")
    pending = load_containers(lst, res2.done_set())
    assert [p["container"] for p in pending] == ["BBBB2222222"]


def test_single_column_format_uses_only_tower(tmp_path):
    lst = make_list(tmp_path, "container\nAAAA1111111\nbbbb2222222\n")
    pending = load_containers(lst, set(), only_tower="205")
    assert pending == [{"container": "AAAA1111111", "tower": "205"},
                       {"container": "BBBB2222222", "tower": "205"}]


def test_only_tower_filters_other_towers(tmp_path):
    lst = make_list(tmp_path, "container,tower\nAAAA1111111,109\n"
                    "BBBB2222222,202\n")
    pending = load_containers(lst, set(), only_tower="202")
    assert [p["container"] for p in pending] == ["BBBB2222222"]


def test_duplicates_dropped_preserving_order(tmp_path):
    lst = make_list(tmp_path, "container,tower\nAAAA1111111,109\n"
                    "AAAA1111111,202\nBBBB2222222,202\n")
    pending = load_containers(lst, set())
    assert [p["container"] for p in pending] == \
        ["AAAA1111111", "BBBB2222222"]


def test_group_by_tower_follows_rotation_order():
    pending = [{"container": "C1", "tower": "205"},
               {"container": "C2", "tower": "109"},
               {"container": "C3", "tower": "205"}]
    buckets = group_by_tower(pending, ["109", "202", "203", "205"])
    assert list(buckets) == ["109", "205"]
    assert buckets["205"] == ["C1", "C3"]


def test_summary_counts(tmp_path):
    res = ResultsStore(tmp_path / "results.csv")
    res.record("A", "BOOKED")
    res.record("B", "SKIPPED")
    res.record("C", "BOOKED")
    assert res.summary() == {"BOOKED": 2, "SKIPPED": 1}


def test_remove_container_rewrites_list(tmp_path):
    from truckbot.containers import remove_container
    lst = make_list(tmp_path, "container,tower\nAAAA1111111,109\n"
                    "BBBB2222222,203\n")
    assert remove_container(lst, "aaaa1111111") is True
    assert lst.read_text() == "container,tower\nBBBB2222222,203\n"
    assert remove_container(lst, "AAAA1111111") is False   # already gone

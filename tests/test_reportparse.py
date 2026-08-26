from truckbot.reportparse import append_to_list, parse_report

REPORT = """
Units assigned to trucking company AVEMEL LOG
PCIU9529335  KOTA LIMA   12/08/26 09:15   Yard    null
MSDU4523340  MSC LUISA   Inbound
TRHU8231290  KOTA LIMA   10/08/26 11:00  14/08/26 06:00  Departed  null
FCIU5869173  MSC ZIVANA  11/08/26 15:20   EC/Out  null
CAAU8403819  KOTA LIMA   12/08/26 10:05   Yard    CUSTOMS
PCIU9529335  KOTA LIMA   12/08/26 09:15   Yard    null
"""


def test_filtering_rules():
    bookable, excluded = parse_report(REPORT)
    assert bookable == ["PCIU9529335"]          # deduped too
    why = dict(excluded)
    assert "inbound" in why["MSDU4523340"]
    assert "departed" in why["TRHU8231290"]
    assert "EC/Out" in why["FCIU5869173"]
    assert why["CAAU8403819"] == "hold=customs"


def test_append_skips_existing(tmp_path):
    out = tmp_path / "containers_all.csv"
    out.write_text("container,tower\nPCIU9529335,109\n")
    new = append_to_list(["PCIU9529335", "AAAA1234567"], "202", out)
    assert new == ["AAAA1234567"]
    text = out.read_text()
    assert "PCIU9529335,109" in text and "AAAA1234567,202" in text


def test_fifo_oldest_in_date_first():
    report = """
NEWU1111111  VESSEL A  20/08/26 10:00   Yard  null
OLDU2222222  VESSEL B  05/08/26 08:30   Yard  null
MIDU3333333  VESSEL C  12/08/26 14:00   Yard  null
"""
    bookable, _ = parse_report(report)
    assert bookable == ["OLDU2222222", "MIDU3333333", "NEWU1111111"]

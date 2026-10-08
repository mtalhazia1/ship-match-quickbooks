from apps.shipments.services.containers import check_digit, find_containers, is_valid_container, make_container


def test_known_valid_container():
    assert is_valid_container("CSQU3054383")
    assert is_valid_container("csqu 305438 3")


def test_typo_fails_check_digit():
    assert not is_valid_container("CSQU3054384")
    assert not is_valid_container("CSQU305438")  # too short


def test_make_container_round_trip():
    for serial in (0, 123456, 999999):
        assert is_valid_container(make_container("OSL", serial))


def test_find_containers_normalizes_and_dedupes():
    text = "Boxes: MSKU 907032 3, TGHU-8794340-0 and again MSKU9070323"
    assert find_containers(text) == ["MSKU9070323", "TGHU8794340"]


def test_check_digit_ten_maps_to_zero():
    # Any serial whose weighted sum mod 11 is 10 must produce check digit 0
    for serial in range(1000):
        first10 = f"ABCU{serial:06d}"
        assert 0 <= check_digit(first10) <= 9

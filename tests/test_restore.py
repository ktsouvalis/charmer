from charmer.phases.restore_phase import PANGOLIN_DEFAULT_GERBIL_SUBNET_GROUP, widen_utility_subnet

ORG = ["100.90.128.0/24"]
GERBIL = [PANGOLIN_DEFAULT_GERBIL_SUBNET_GROUP, "100.89.137.1/24"]


def test_widens_to_aligned_supernet_containing_current():
    new, _ = widen_utility_subnet("100.96.128.0/24", 22, ORG, GERBIL)
    assert new == "100.96.128.0/22"
    new, _ = widen_utility_subnet("100.96.128.0/22", 21, ORG, GERBIL)
    assert new == "100.96.128.0/21"


def test_already_wide_enough_is_left_alone():
    assert widen_utility_subnet("100.96.128.0/22", 22, ORG, GERBIL)[0] is None
    assert widen_utility_subnet("100.96.128.0/20", 22, ORG, GERBIL)[0] is None


def test_refuses_overlap_with_org_subnet():
    new, reason = widen_utility_subnet("100.96.128.0/24", 10, ORG, GERBIL)
    assert new is None and "org subnet" in reason
    new, reason = widen_utility_subnet("100.90.132.0/24", 20, ["100.90.128.0/24"], [])
    assert new is None and "org subnet" in reason


def test_refuses_overlap_with_gerbil_network():
    new, reason = widen_utility_subnet("100.89.160.0/24", 18, [], GERBIL)
    assert new is None and "Gerbil" in reason


def test_unparseable_current_is_left_alone():
    new, reason = widen_utility_subnet("garbage", 20, ORG, GERBIL)
    assert new is None and "unparseable" in reason

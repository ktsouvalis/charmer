from charmer.phases.pangolin_phase import restart_pangolin_first, rollout_actions

SERVICES = ("postgres", "pangolin", "gerbil", "traefik", "maintenance")
BEFORE = {s: (f"{s}-1", "running", "healthy" if s in ("postgres", "pangolin") else "") for s in SERVICES}


def after(**changes):
    out = dict(BEFORE)
    for svc, v in changes.items():
        out[svc] = (f"{svc}-2", "running", BEFORE[svc][2]) if v == "new" else (BEFORE[svc][0], v, "")
    return out


def test_first_run_needs_nothing():
    assert not restart_pangolin_first({}, True)
    assert rollout_actions({}, after(), True) == ([], False)


def test_nothing_changed_needs_nothing():
    assert not restart_pangolin_first(BEFORE, False)
    assert rollout_actions(BEFORE, after(), False) == ([], False)


def test_changed_config_yml_restarts_pangolin_before_up():
    assert restart_pangolin_first(BEFORE, True)


def test_unhealthy_or_crash_looping_pangolin_restarts_before_up():
    unhealthy = dict(BEFORE, pangolin=("pangolin-1", "running", "unhealthy"))
    looping = dict(BEFORE, pangolin=("pangolin-1", "restarting", ""))
    assert restart_pangolin_first(unhealthy, False)
    assert restart_pangolin_first(looping, False)


def test_postgres_recreated_restarts_only_pangolin():
    assert rollout_actions(BEFORE, after(postgres="new"), False) == (["pangolin"], False)


def test_changed_traefik_files_restart_traefik():
    assert rollout_actions(BEFORE, after(), True) == (["traefik"], False)


def test_recreated_service_is_not_also_restarted():
    assert rollout_actions(BEFORE, after(postgres="new", pangolin="new"), False) == ([], False)


def test_gerbil_recreated_forces_traefik_recreate_instead_of_restart():
    assert rollout_actions(BEFORE, after(gerbil="new"), True) == ([], True)


def test_crash_looping_service_after_up_is_restarted():
    assert rollout_actions(BEFORE, after(maintenance="restarting"), False) == (["maintenance"], False)


def test_restart_order_is_pangolin_before_traefik():
    assert rollout_actions(BEFORE, after(postgres="new"), True) == (["pangolin", "traefik"], False)


def test_gerbil_restart_also_forces_traefik_recreate():
    assert rollout_actions(BEFORE, after(gerbil="restarting"), True) == (["gerbil"], True)

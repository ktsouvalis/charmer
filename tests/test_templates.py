import yaml

from charmer.remote import render


def test_compose_matches_official_bridge_networking():
    rendered = render("pangolin-compose.yml.j2", pangolin_image_tag="postgresql-1.22.0",
                      gerbil_tag="1.5.0", traefik_tag="v3.7.12", database="postgres",
                      postgres_tag="17", postgres_user="pangolin", postgres_password="secret",
                      maintenance_tag="1.27-alpine", maintenance_port=8091,
                      tls_enabled=True, enable_integration_api=False)
    doc = yaml.safe_load(rendered)
    for svc in ("pangolin", "gerbil", "postgres"):
        assert "network_mode" not in doc["services"][svc]
    assert doc["services"]["traefik"]["network_mode"] == "service:gerbil"
    assert doc["services"]["pangolin"]["image"] == "docker.io/fosrl/pangolin:postgresql-1.22.0"
    assert "command" not in doc["services"]["postgres"]
    assert doc["networks"]["default"]["name"] == "pangolin"


def test_compose_gerbil_publishes_wireguard_and_web_ports_wildcard():
    rendered = render("pangolin-compose.yml.j2", pangolin_image_tag="1.22.0", gerbil_tag="1.5.0",
                      traefik_tag="v3.7.12", database="sqlite", postgres_tag="17",
                      postgres_user="pangolin", postgres_password="",
                      maintenance_tag="1.27-alpine", maintenance_port=8091,
                      tls_enabled=True, enable_integration_api=False)
    doc = yaml.safe_load(rendered)
    gerbil = doc["services"]["gerbil"]
    assert "51820:51820/udp" in gerbil["ports"]
    assert "21820:21820/udp" in gerbil["ports"]
    assert "80:80" in gerbil["ports"]
    assert "443:443" in gerbil["ports"]
    assert "--reachableAt=http://gerbil:3004" in gerbil["command"]
    assert "--remoteConfig=http://pangolin:3001/api/v1/" in gerbil["command"]


def test_compose_gerbil_omits_443_publish_when_tls_disabled():
    rendered = render("pangolin-compose.yml.j2", pangolin_image_tag="1.22.0", gerbil_tag="1.5.0",
                      traefik_tag="v3.7.12", database="sqlite", postgres_tag="17",
                      postgres_user="pangolin", postgres_password="",
                      maintenance_tag="1.27-alpine", maintenance_port=8091,
                      tls_enabled=False, enable_integration_api=False)
    doc = yaml.safe_load(rendered)
    assert "443:443" not in doc["services"]["gerbil"]["ports"]


def test_compose_pangolin_publishes_integration_api_only_when_enabled():
    rendered = render("pangolin-compose.yml.j2", pangolin_image_tag="1.22.0", gerbil_tag="1.5.0",
                      traefik_tag="v3.7.12", database="sqlite", postgres_tag="17",
                      postgres_user="pangolin", postgres_password="",
                      maintenance_tag="1.27-alpine", maintenance_port=8091,
                      tls_enabled=True, enable_integration_api=True)
    doc = yaml.safe_load(rendered)
    assert "127.0.0.1:3003:3003" in doc["services"]["pangolin"]["ports"]

    rendered_off = render("pangolin-compose.yml.j2", pangolin_image_tag="1.22.0", gerbil_tag="1.5.0",
                          traefik_tag="v3.7.12", database="sqlite", postgres_tag="17",
                          postgres_user="pangolin", postgres_password="",
                          maintenance_tag="1.27-alpine", maintenance_port=8091,
                          tls_enabled=True, enable_integration_api=False)
    doc_off = yaml.safe_load(rendered_off)
    assert "127.0.0.1:3003:3003" not in doc_off["services"]["pangolin"]["ports"]
    assert "127.0.0.1:3001:3001" in doc_off["services"]["pangolin"]["ports"]


def test_compose_omits_postgres_for_sqlite():
    rendered = render("pangolin-compose.yml.j2", pangolin_image_tag="1.22.0", gerbil_tag="1.5.0",
                      traefik_tag="v3.7.12", database="sqlite", postgres_tag="17",
                      postgres_user="pangolin", postgres_password="",
                      maintenance_tag="1.27-alpine", maintenance_port=8091,
                      tls_enabled=True, enable_integration_api=False)
    doc = yaml.safe_load(rendered)
    assert "postgres" not in doc["services"]
    assert "depends_on" not in doc["services"]["pangolin"]


def test_compose_maintenance_service_is_bridge_networked_on_loopback():
    rendered = render("pangolin-compose.yml.j2", pangolin_image_tag="1.22.0", gerbil_tag="1.5.0",
                      traefik_tag="v3.7.12", database="sqlite", postgres_tag="17",
                      postgres_user="pangolin", postgres_password="",
                      maintenance_tag="1.27-alpine", maintenance_port=8091,
                      tls_enabled=True, enable_integration_api=False)
    doc = yaml.safe_load(rendered)
    maint = doc["services"]["maintenance"]
    assert maint["image"] == "docker.io/library/nginx:1.27-alpine"
    assert "network_mode" not in maint
    assert maint["ports"] == ["127.0.0.1:8091:80"]


def test_pangolin_config_gerbil_port_is_always_51820():
    rendered = render("pangolin-config.yml.j2", base_url="https://p.example", dashboard_host="p.example",
                      base_domain="p.example", server_secret="s", enable_integration_api=False,
                      database="sqlite", postgres_connection_string="", smtp_enabled=False)
    doc = yaml.safe_load(rendered)
    assert doc["gerbil"]["start_port"] == 51820


def test_pangolin_config_integration_api_flag():
    rendered = render("pangolin-config.yml.j2", base_url="https://p.example", dashboard_host="p.example",
                      base_domain="p.example", server_secret="s", enable_integration_api=True,
                      database="sqlite", postgres_connection_string="", smtp_enabled=False)
    doc = yaml.safe_load(rendered)
    assert doc["flags"]["enable_integration_api"] is True
    assert doc["server"]["integration_port"] == 3003


def test_pangolin_config_smtp_email_section():
    rendered = render("pangolin-config.yml.j2", base_url="https://p.example", dashboard_host="p.example",
                      base_domain="p.example", server_secret="s", enable_integration_api=False,
                      database="sqlite", postgres_connection_string="", smtp_enabled=True,
                      smtp_host="smtp.example.org", smtp_port=587, smtp_user="no-reply@example.org",
                      smtp_pass="hunter2", smtp_no_reply="no-reply@example.org", smtp_secure=False,
                      smtp_tls_reject_unauthorized=True)
    doc = yaml.safe_load(rendered)
    assert doc["email"] == {
        "smtp_host": "smtp.example.org", "smtp_port": 587, "smtp_user": "no-reply@example.org",
        "smtp_pass": "hunter2", "smtp_secure": False, "no_reply": "no-reply@example.org",
        "smtp_tls_reject_unauthorized": True,
    }


def test_pangolin_config_omits_email_section_when_smtp_disabled():
    rendered = render("pangolin-config.yml.j2", base_url="https://p.example", dashboard_host="p.example",
                      base_domain="p.example", server_secret="s", enable_integration_api=False,
                      database="sqlite", postgres_connection_string="", smtp_enabled=False)
    doc = yaml.safe_load(rendered)
    assert "email" not in doc


def test_traefik_entrypoints_are_wildcard():
    rendered = render("traefik-config.yml.j2", tls_enabled=True, cert_resolver="letsencrypt",
                      acme_email="a@example.org", acme_directory_url="https://acme.example/directory")
    doc = yaml.safe_load(rendered)
    assert doc["entryPoints"]["web"]["address"] == ":80"
    assert doc["entryPoints"]["websecure"]["address"] == ":443"
    assert "proxyProtocol" not in doc["entryPoints"]["web"]
    assert doc["certificatesResolvers"]["letsencrypt"]["acme"]["email"] == "a@example.org"
    assert doc["providers"]["http"]["endpoint"] == "http://pangolin:3001/api/v1/traefik-config"


def test_traefik_tls_none_has_no_websecure_or_resolver():
    rendered = render("traefik-config.yml.j2", tls_enabled=False, cert_resolver=None,
                      acme_email="", acme_directory_url="")
    doc = yaml.safe_load(rendered)
    assert "websecure" not in doc["entryPoints"]
    assert "certificatesResolvers" not in doc


def test_dynamic_config_self_signed_uses_default_cert_no_resolver():
    rendered = render("traefik-dynamic-config.yml.j2", dashboard_host="p.example", tls_enabled=True,
                      cert_resolver=None, default_cert_file="/etc/traefik/certs/fullchain.pem",
                      default_key_file="/etc/traefik/certs/privkey.pem", maintenance_port=8091)
    doc = yaml.safe_load(rendered)
    assert "certResolver" not in doc["http"]["routers"]["next-router"]["tls"]
    assert doc["tls"]["certificates"][0]["certFile"] == "/etc/traefik/certs/fullchain.pem"


def test_dynamic_config_acme_sets_cert_resolver():
    rendered = render("traefik-dynamic-config.yml.j2", dashboard_host="p.example", tls_enabled=True,
                      cert_resolver="letsencrypt", default_cert_file="", default_key_file="",
                      maintenance_port=8091)
    doc = yaml.safe_load(rendered)
    assert doc["http"]["routers"]["next-router"]["tls"]["certResolver"] == "letsencrypt"
    assert "tls" not in doc or "certificates" not in doc.get("tls", {})


def test_dynamic_config_wires_maintenance_errors_on_dashboard_routers():
    rendered = render("traefik-dynamic-config.yml.j2", dashboard_host="p.example", tls_enabled=True,
                      cert_resolver="letsencrypt", default_cert_file="", default_key_file="",
                      maintenance_port=8091)
    doc = yaml.safe_load(rendered)
    mw = doc["http"]["middlewares"]["maintenance-errors"]["errors"]
    assert mw["service"] == "maintenance-service"
    assert "502-504" in mw["status"]
    for router in ("next-router", "api-router", "ws-router"):
        assert "maintenance-errors" in doc["http"]["routers"][router]["middlewares"]
    assert doc["http"]["services"]["maintenance-service"]["loadBalancer"]["servers"][0]["url"] \
        == "http://maintenance:80"
    assert doc["http"]["services"]["next-service"]["loadBalancer"]["servers"][0]["url"] \
        == "http://pangolin:3002"
    assert doc["http"]["services"]["api-service"]["loadBalancer"]["servers"][0]["url"] \
        == "http://pangolin:3000"


def test_newt_compose_is_host_type_agnostic():
    rendered = render("newt-compose.yml.j2", image_tag="1.5.0", pangolin_endpoint="https://p.example",
                      newt_id="id123", newt_secret="secret123", tun_device="/dev/net/tun",
                      docker_socket=False, skip_tls_verify=False)
    doc = yaml.safe_load(rendered)
    svc = doc["services"]["newt"]
    assert svc["image"] == "docker.io/fosrl/newt:1.5.0"
    assert svc["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert "NET_ADMIN" in svc["cap_add"]
    assert "volumes" not in svc
    assert "SKIP_TLS_VERIFY" not in svc["environment"]


def test_maintenance_page_renders_message_without_logo():
    rendered = render("maintenance.html.j2", message="We'll be back shortly.", logo_data_uri=None)
    assert "We'll be back shortly." in rendered
    assert "<img" not in rendered
    assert 'name="charmer-maintenance"' in rendered


def test_maintenance_page_embeds_logo_data_uri():
    rendered = render("maintenance.html.j2", message="Down for maintenance",
                      logo_data_uri="data:image/png;base64,AAAA")
    assert 'src="data:image/png;base64,AAAA"' in rendered
    assert "Down for maintenance" in rendered


def test_newt_compose_mounts_docker_socket_when_requested():
    rendered = render("newt-compose.yml.j2", image_tag="1.5.0", pangolin_endpoint="https://p.example",
                      newt_id="id", newt_secret="secret", tun_device="/dev/net/tun", docker_socket=True,
                      skip_tls_verify=False)
    doc = yaml.safe_load(rendered)
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in doc["services"]["newt"]["volumes"]


def test_newt_compose_skips_tls_verify_for_self_signed():
    rendered = render("newt-compose.yml.j2", image_tag="1.5.0", pangolin_endpoint="https://p.example",
                      newt_id="id", newt_secret="secret", tun_device="/dev/net/tun", docker_socket=False,
                      skip_tls_verify=True)
    doc = yaml.safe_load(rendered)
    assert doc["services"]["newt"]["environment"]["SKIP_TLS_VERIFY"] == "true"

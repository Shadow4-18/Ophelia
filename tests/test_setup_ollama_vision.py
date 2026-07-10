"""Setup helpers for remote Ollama vision."""

from ophelia.setup.interactive import normalize_ollama_openai_base


def test_normalize_ollama_bare_ip():
    assert normalize_ollama_openai_base("192.168.1.50") == "http://192.168.1.50:11434/v1"


def test_normalize_ollama_host_port():
    assert (
        normalize_ollama_openai_base("192.168.1.50:11434")
        == "http://192.168.1.50:11434/v1"
    )


def test_normalize_ollama_full_url():
    assert (
        normalize_ollama_openai_base("http://100.64.1.2:11434/v1")
        == "http://100.64.1.2:11434/v1"
    )


def test_normalize_ollama_strips_api_suffix():
    assert (
        normalize_ollama_openai_base("http://192.168.1.50:11434/api")
        == "http://192.168.1.50:11434/v1"
    )


def test_normalize_ollama_empty_defaults_localhost():
    assert normalize_ollama_openai_base("") == "http://127.0.0.1:11434/v1"


def test_vision_ollama_endpoint_helper_exists():
    from ophelia.setup import interactive as setup

    assert callable(setup._configure_ollama_vision_endpoint)
    src = setup.__file__
    body = open(src, encoding="utf-8").read()
    assert "Remote PC on LAN / Tailscale" in body
    assert "OPHELIA_OLLAMA_AUTOSTART" in body

import iki_spec


def test_version_matches_distribution_metadata() -> None:
    assert iki_spec.__version__ == "0.1.0.dev0"
    assert iki_spec.API_VERSION == "iki/v1"

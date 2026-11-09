import iki_server


def test_version_matches_distribution_metadata() -> None:
    assert iki_server.__version__ == "0.1.0.dev0"

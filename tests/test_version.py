from importlib.metadata import version

import llm_wiki


def test_public_version_matches_package_metadata():
    assert llm_wiki.__version__ == version("llm-wiki")

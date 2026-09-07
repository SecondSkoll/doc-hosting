"""Sphinx configuration for the doc-hosting documentation."""

import datetime
import os
import textwrap

project = "doc-hosting"
author = "doc-hosting maintainers"
copyright = f"{datetime.date.today().year}, {author}"  # noqa: A001
html_title = "doc-hosting documentation"

ogp_site_url = os.environ.get("READTHEDOCS_CANONICAL_URL", "/")
ogp_site_name = project
ogp_image = "https://assets.ubuntu.com/v1/cc828679-docs_illustration.svg"
html_baseurl = os.environ.get("READTHEDOCS_CANONICAL_URL", "/")
sitemap_url_scheme = "{link}"
sitemap_show_lastmod = True
sitemap_excludes = ["404/", "genindex/", "search/"]

templates_path = ["_templates"]
html_context = {
    "product_page": "",
    "discourse": "",
    "mattermost": "",
    "matrix": "",
    "github_url": "",
    "repo_default_branch": "main",
    "repo_folder": "/docs/",
    "display_contributors": False,
    "github_issues": "disabled",
    "author": author,
    "license": {"name": "", "url": ""},
}
disable_feedback_button = True
rediraffe_redirects = "redirects.txt"
rediraffe_dir_only = True

llms_txt_description = textwrap.dedent(
    """\
    Documentation for doc-hosting, a proof of concept that builds, stores,
    registers, and serves static documentation in a Juju deployment.
    """
)
if os.environ.get("READTHEDOCS"):
    markdown_http_base = html_baseurl

linkcheck_ignore = [
    r"http://localhost(:[0-9]+)?/.*",
    r"http://[^/]+\.svc\.cluster\.local(:[0-9]+)?/.*",
    r"http://<[^>]+>.*",
]
linkcheck_retries = 3

extensions = [
    "canonical_sphinx",
    "notfound.extension",
    "sphinx_design",
    "sphinx_rerediraffe",
    "sphinx_reredirects",
    "sphinx_tabs.tabs",
    "sphinxcontrib.jquery",
    "sphinxext.opengraph",
    "sphinx_llm.txt",
    "sphinxcontrib.cairosvgconverter",
    "sphinx_last_updated_by_git",
    "sphinx.ext.intersphinx",
    "sphinx_sitemap",
]
exclude_patterns = ["_build", "_dev", ".venv*"]
rst_prolog = """
.. role:: center
   :class: align-center
.. role:: h2
   :class: hclass2
.. role:: woke-ignore
   :class: woke-ignore
.. role:: vale-ignore
   :class: vale-ignore
"""

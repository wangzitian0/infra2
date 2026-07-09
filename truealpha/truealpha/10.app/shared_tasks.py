from invoke import task

from libs.common import check_service


@task
def status(c):
    """Check TrueAlpha application health status (web + llm)."""
    # 127.0.0.1, not localhost: see compose.yaml's healthcheck comments.
    llm_ok = check_service(
        c,
        "truealpha-llm",
        "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)\"",
    )
    web_ok = check_service(c, "truealpha-web", "wget -q -T 3 -O /dev/null http://127.0.0.1:3000")
    return llm_ok and web_ok

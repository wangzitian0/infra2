from invoke import task

from libs.common import check_service


@task
def status(c):
    """Check TrueAlpha application health status (web + llm)."""
    llm_ok = check_service(
        c,
        "truealpha-llm",
        "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)\"",
    )
    web_ok = check_service(c, "truealpha-web", "wget -q -T 3 -O /dev/null http://localhost:3000")
    return llm_ok and web_ok

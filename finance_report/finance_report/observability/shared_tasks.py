"""finance_report SigNoz observability tasks (#373); bodies: common/observability/apply_tasks.py.

uv run python -m invoke fr-observability.shared.print-alerts      # offline payloads
uv run python -m invoke fr-observability.shared.apply-alerts      # production apply
uv run python -m invoke fr-observability.shared.verify-alert-routing
"""

from invoke import task

from common import observability_dashboards
from common.observability.apply_tasks import make_observability_tasks

_bodies = make_observability_tasks(
    catalog=observability_dashboards,
    managed_alert_source="infra2/finance_report-alerts",
    task_namespace="fr-observability.shared",
)
print_alerts = task(_bodies["print_alerts"])
print_dashboard = task(_bodies["print_dashboard"])
print_openpanel_analytics = task(_bodies["print_openpanel_analytics"])
apply_alerts = task(_bodies["apply_alerts"])
apply_dashboard = task(_bodies["apply_dashboard"])
verify_dashboard = task(_bodies["verify_dashboard"])
verify_alert_routing = task(_bodies["verify_alert_routing"])
test_alert_channel = task(_bodies["test_alert_channel"])

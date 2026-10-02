import pytest
from unittest.mock import patch
from workers.tasks import run_reconciliation_audit

@pytest.mark.django_db
@patch('workers.tasks.run_global_reconciliation')
def test_reconciliation_task_dispatch(mock_run_reconciliation):
    # Execute the celery task synchronously for testing
    run_reconciliation_audit.apply()
    
    # Assert the internal engine was actually called
    mock_run_reconciliation.assert_called_once()

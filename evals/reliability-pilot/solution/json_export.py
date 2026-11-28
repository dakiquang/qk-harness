"""JSON export for record lists."""

import json


def render(records):
    """Serialize records without modifying them."""
    return json.dumps(records)

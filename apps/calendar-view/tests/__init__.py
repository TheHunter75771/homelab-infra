import logging

# Expected warnings (failed fetches in the stale tests) stay out of the test output; assertLogs still sees them.
logging.getLogger("calendar_view").addHandler(logging.NullHandler())

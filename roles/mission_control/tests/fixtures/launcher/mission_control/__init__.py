"""Stand-in package for the launcher test only (tests/test_mission_control.yml): the real MC1 CLI imports aiohttp,
which the offline test has no venv for. It proves the launcher's mechanics: the -IB shebang, sys.path, runpy."""

"""Prints what the launcher passed through: argv and the isolation flags."""
import sys

print("stand-in %s isolated=%d dont_write_bytecode=%s" % (" ".join(sys.argv[1:]), sys.flags.isolated, sys.dont_write_bytecode))

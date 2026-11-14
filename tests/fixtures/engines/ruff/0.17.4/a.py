import os
import sys


def used():
    unused_local = 1
    return sys.argv


def never_called():
    return used()

# Package marker: lets test modules import shared helpers via
# `from tests._support import ...` on every platform (pytest rootdir-based
# sys.path injection alone is not package-consistent with xdist workers).

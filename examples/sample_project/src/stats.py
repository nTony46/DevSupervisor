"""Small statistics helpers."""


def total(values):
    return sum(values)


def average(values):
    # Known defect: an empty sequence raises ZeroDivisionError instead of
    # returning a defined result. Left in place as a fixture for the bug workflow.
    return total(values) / len(values)

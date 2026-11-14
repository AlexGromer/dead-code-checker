from pkg.shapes import Alpha, helper


def use(a: Alpha):
    return a.ping() + helper()


def main():
    return use(Alpha())

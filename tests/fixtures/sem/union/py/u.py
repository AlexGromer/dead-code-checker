from typing import Union


class A:
    def m(self):
        return 1


class B:
    def m(self):
        return 2


def use(x: Union[A, B]):
    return x.m()


def use2(x: A | B):
    return x.m()


def twice(x: A, y):
    return x.m() + y.m()
